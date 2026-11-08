"""Повторное ревью тумблера: busy, сериализация, повтор, state, health.

Всё офлайн: сеть, Xray, службы и файлы состояния подменяются/в temp.
Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import actions, app, config, health, routes  # noqa: E402


def wait(predicate, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.01)
    return predicate()


class TempData(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        config.set_config_dir(self.data)
        self.addCleanup(config.set_config_dir, None)
        patcher = mock.patch.object(routes, "config_dir", return_value=self.data)
        patcher.start()
        self.addCleanup(patcher.stop)
        routes.save_overrides({})


class BusyAndStopTests(TempData):
    def bare(self) -> app.TrayApp:
        tray = app.TrayApp.__new__(app.TrayApp)
        tray.cfg = {"guard_enabled": True}
        tray.status = {"zapret": {"running": False}}
        tray.busy = {}
        tray._signature = None
        tray.health_running = False
        tray.guard_running = False
        tray._zapret_user_off = False
        tray._zapret_off_pending = False
        tray.refresh = lambda: None
        tray.refresh_activity = lambda: None
        tray.notify = lambda *args, **kwargs: None
        return tray

    def test_busy_held_through_after(self) -> None:
        tray = self.bare()
        seen: list[bool] = []
        self.assertTrue(tray._run_action_busy(
            "zapret", "останавливаю…", lambda: None,
            after=lambda: seen.append(tray.busy.get("zapret") == "останавливаю…")))
        self.assertTrue(wait(lambda: not tray.busy))
        self.assertEqual(seen, [True])

    def test_health_and_guard_skip_while_zapret_busy(self) -> None:
        tray = self.bare()
        tray.busy = {"zapret": "останавливаю…"}
        self.assertFalse(tray.run_health())
        self.assertFalse(tray.run_guard())
        self.assertEqual(routes.load_overrides(), {})

    def test_stop_warns_when_windivert_not_stopped(self) -> None:
        tray = self.bare()
        logs: list[tuple[str, str]] = []
        with mock.patch.object(actions, "stop_zapret",
                               return_value={"windivert": "failed",
                                             "killed": 1, "denied": 0}), \
                mock.patch.object(app.config, "load_state", return_value={}), \
                mock.patch.object(app.config, "save_state", return_value=True), \
                mock.patch.object(app.activity, "log",
                                  side_effect=lambda text, kind="info":
                                  logs.append((text, kind))):
            self.assertTrue(tray.stop_zapret())
            self.assertTrue(wait(lambda: not tray.busy))
        self.assertTrue(any(kind == "warn" and "WinDivert" in text
                            for text, kind in logs))


class PendingRetryTests(TempData):
    def controller(self) -> app.TrayApp:
        tray = app.TrayApp.__new__(app.TrayApp)
        tray.cfg = {}
        tray.status = {"zapret": {"running": False}}
        tray.proxy = {"running": True, "busy": False}
        tray.busy = {}
        tray._zapret_user_off = True
        tray._zapret_off_pending = True
        tray._signature = None
        tray.refresh_activity = lambda: None
        tray.update_icon = lambda: None
        tray._zapret_off_pending = True
        return tray

    def test_pending_applies_once_when_vpn_appears(self) -> None:
        tray = self.controller()
        restarts: list[dict] = []
        with mock.patch.object(app.health, "_check_youtube",
                               return_value={"state": "bad"}), \
                mock.patch.object(app.health, "_check_discord",
                                  return_value={"state": "ok"}), \
                mock.patch.object(app.xray, "restart",
                                  side_effect=lambda cfg=None:
                                  restarts.append(dict(cfg or {})) or {"ok": True}), \
                mock.patch.object(app.xray, "status",
                                  return_value={"running": True}), \
                mock.patch.object(app.activity, "log"):
            tray._maybe_zapret_off_routes()
            tray._maybe_zapret_off_routes()
        self.assertEqual(len(restarts), 1)
        self.assertFalse(tray._zapret_off_pending)
        self.assertEqual(routes.load_overrides()["youtube.com"]["source"],
                         "zapret_off")


class PersistFlagTests(TempData):
    def test_flag_saved_and_read_on_start(self) -> None:
        config.save_state({"zapret_user_off": True})
        with mock.patch.object(app.config, "load_config",
                               return_value={"zapret_dir": str(self.data)}), \
                mock.patch.object(app.health, "load_cache", return_value=None), \
                mock.patch.object(app, "Panel", lambda controller: object()):
            tray = app.TrayApp()
        self.assertTrue(tray._zapret_user_off)
        self.assertTrue(tray._zapret_off_pending)

    def test_persist_writes_state(self) -> None:
        tray = app.TrayApp.__new__(app.TrayApp)
        tray.state = {}
        tray._persist_zapret_off(True)
        self.assertTrue(config.load_state().get("zapret_user_off"))


class SerialisedWriteTests(unittest.TestCase):
    def test_decorator_serialises_calls(self) -> None:
        active = 0
        peak = 0
        guard = threading.Lock()

        @routes._serialised
        def critical() -> None:
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with guard:
                active -= 1

        threads = [threading.Thread(target=critical) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak, 1)


class StreamContextOverrideTests(TempData):
    def test_zapret_off_override_switches_probe_to_proxy(self) -> None:
        routes.save_overrides({
            "youtube.com": {"route": "proxy", "source": "zapret_off",
                            "since": 1.0, "checked": 1.0},
        })
        proxied, port = health._stream_context(
            {}, {"running": True, "port": 10818}, "youtube")
        self.assertTrue(proxied)
        self.assertEqual(port, 10818)
        # Без оверрайда поведение прежнее.
        self.assertEqual(health._stream_context(
            {}, {"running": True, "port": 10818}, "discord"), (False, None))


if __name__ == "__main__":
    unittest.main()
