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


if __name__ == "__main__":
    unittest.main()
