"""Резервный маршрут по health: winws запущен, но сервис не работает.

Никаких перезапусков Xray: ``xray.restart`` и запись оверрайдов подменяются.
Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import app, routes, strategy, xray  # noqa: E402


def _controller(*, running: bool = True, broken: str | None = "bad") -> object:
    controller = app.TrayApp.__new__(app.TrayApp)
    controller.cfg = {"xray_stream_fallback": True, "health_minutes": 10}
    controller.proxy = {"running": running, "busy": False, "stream": False,
                        "server": "s1"}
    controller.status = {"zapret": {"running": True}}
    items = [{"id": "youtube", "state": broken or "ok"},
             {"id": "discord", "state": "ok"}]
    controller.health_report = {"items": items}
    controller.health_at = time.time()
    controller._stream_since = None
    controller._health_fallback_at = 0.0
    controller._signature = None
    controller.refresh_activity = lambda: None
    controller._notify_throttled = lambda *args, **kwargs: None
    return controller


class HealthFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logs: list[tuple[str, str]] = []
        self.restarts: list[dict] = []
        self.enabled: list[list[str]] = []
        self.rolled: list[dict] = []
        self.patches = [
            mock.patch.object(strategy, "trial_active", return_value=False),
            mock.patch.object(app.activity, "log",
                              side_effect=lambda text, kind="info": self.logs.append((text, kind))),
            mock.patch.object(routes, "enable_service_proxy",
                              side_effect=self._enable),
            mock.patch.object(routes, "rollback_last", side_effect=self._rollback),
            mock.patch.object(xray, "restart", side_effect=self._restart),
            mock.patch.object(xray, "status", return_value={
                "running": True, "stream": False, "server": "s1"}),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _enable(self, services, cfg=None):
        self.enabled.append(list(services))
        return {"ok": True, "changed": ["youtube.com"], "status": "changed"}

    def _restart(self, cfg=None, **kwargs):
        self.restarts.append(dict(cfg or {}))
        return {"ok": True}

    def _rollback(self, cfg=None):
        self.rolled.append(dict(cfg or {}))
        return {"ok": True, "changed": 1}

    def test_broken_youtube_with_running_winws(self) -> None:
        controller = _controller(broken="bad")
        controller.check_stream_fallback()
        self.assertEqual(self.enabled, [["youtube"]])
        self.assertEqual(len(self.restarts), 1)
        self.assertTrue(any(kind == "ok" and "через VPN" in text
                            for text, kind in self.logs))

    def test_cooldown_blocks_second_restart(self) -> None:
        controller = _controller(broken="bad")
        controller.check_stream_fallback()
        self.assertEqual(len(self.restarts), 1)
        controller.check_stream_fallback()
        self.assertEqual(len(self.restarts), 1)
        self.assertEqual(len(self.enabled), 1)

    def test_no_tunnel_no_fallback(self) -> None:
        controller = _controller(running=False)
        controller.check_stream_fallback()
        self.assertEqual(self.enabled, [])
        self.assertEqual(self.restarts, [])

    def test_warn_not_trigger(self) -> None:
        # warn — «видео не проверено»/неопределённость: не перезапускаем Xray.
        controller = _controller(broken="warn")
        controller.check_stream_fallback()
        self.assertEqual(self.enabled, [])
        self.assertEqual(self.restarts, [])
        self.assertEqual(controller._health_fallback_at, 0.0)

    def test_manual_override_is_respected(self) -> None:
        routes.enable_service_proxy.side_effect = lambda services, cfg=None: {
            "ok": True, "changed": [], "status": "unchanged",
            "manual": ["youtube.com"]}
        controller = _controller(broken="bad")
        controller.check_stream_fallback()
        self.assertEqual(self.restarts, [])
        # Кулдаун всё равно поставлен: не дёргаем запись каждый тик.
        self.assertGreater(controller._health_fallback_at, 0)

    def test_healthy_service_does_nothing(self) -> None:
        controller = _controller(broken=None)
        controller.check_stream_fallback()
        self.assertEqual(self.enabled, [])
        self.assertEqual(self.restarts, [])

    def test_trial_skips_fallback(self) -> None:
        with mock.patch.object(strategy, "trial_active", return_value=True):
            controller = _controller(broken="bad")
            controller.check_stream_fallback()
        self.assertEqual(self.enabled, [])
        self.assertEqual(self.restarts, [])

    def test_failed_restart_rolls_overrides_back(self) -> None:
        def fail(cfg=None):
            self.restarts.append(dict(cfg or {}))
            return {"ok": False, "error": "порт занят"}

        xray.restart.side_effect = fail
        controller = _controller(broken="bad")
        controller.check_stream_fallback()
        self.assertEqual(len(self.restarts), 1)
        self.assertEqual(len(self.rolled), 1)
        self.assertTrue(any("откатаны" in text for text, _kind in self.logs))

    def test_youtube_flag_keeps_discord_fallback(self) -> None:
        # YouTube уже в VPN по своему флагу, но при падении zapret Discord
        # тоже должен уйти в VPN: общий stream больше не блокирует решение.
        controller = _controller(broken=None)
        controller.cfg["youtube_via_vpn"] = True
        controller.proxy = {"running": True, "busy": False, "stream": True,
                            "youtube_proxy": True, "discord_proxy": False,
                            "server": "s1"}
        controller.status = {"zapret": {"running": False}}
        controller._stream_since = time.time() - 46
        with mock.patch.object(xray, "restart_with_stream",
                               return_value={"ok": True}) as restart:
            controller.check_stream_fallback()
        restart.assert_called_once_with(True, controller.cfg)

    def test_discord_broken_escalates_when_youtube_proxied(self) -> None:
        # Сломан Discord, YouTube уже в VPN: оверрайд нужен только Discord.
        controller = _controller(broken=None)
        controller.cfg["youtube_via_vpn"] = True
        controller.proxy = {"running": True, "busy": False, "stream": True,
                            "youtube_proxy": True, "discord_proxy": False,
                            "server": "s1"}
        controller.status = {"zapret": {"running": True}}
        controller.health_report = {"items": [
            {"id": "youtube", "state": "ok"},
            {"id": "discord", "state": "bad"}]}
        controller.check_stream_fallback()
        self.assertEqual(self.enabled, [["discord"]])

    def test_partial_fallback_is_not_unchanged(self) -> None:
        # YouTube уже в VPN, Discord ещё нет: это не «unchanged», нужен рестарт.
        saved = {"stream": True, "youtube_proxy": True, "discord_proxy": False,
                 "port": 10818, "link": "vless://x@y:443", "extra": []}
        with mock.patch.object(xray, "is_running", return_value=True), \
                mock.patch.object(xray, "_saved_state", return_value=saved), \
                mock.patch.object(xray, "parse_link",
                                  return_value={"host": "y", "port": 443,
                                                "outbound": {}}), \
                mock.patch.object(xray, "_kill_process"), \
                mock.patch.object(xray.time, "sleep"), \
                mock.patch.object(xray, "start",
                                  return_value={"ok": True}) as start:
            result = xray._restart_with_stream(
                True, {"youtube_via_vpn": True})
        self.assertNotIn("unchanged", result)
        self.assertTrue(result.get("ok"))
        start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
