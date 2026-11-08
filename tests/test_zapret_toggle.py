"""Тумблер zapret: выбор пути start/stop и флаг ручного выключения.

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

from bypass_tray import actions, app, guard, health  # noqa: E402


def _tray(running: bool) -> app.TrayApp:
    """Экземпляр без ``__init__``: конфиг, окно и трей здесь не нужны."""
    tray = app.TrayApp.__new__(app.TrayApp)
    tray.status = {"zapret": {"running": running, "as_service": False}}
    tray.cfg = {"zapret_dir": r"C:\Zapret"}
    tray.busy = {}
    tray._signature = None
    tray._zapret_user_off = False
    tray.calls = []
    tray.start_zapret = lambda: tray.calls.append("start") or True
    tray.stop_zapret = lambda: tray.calls.append("stop") or True
    return tray


def _bare() -> app.TrayApp:
    """Экземпляр с настоящими методами, но без конфига/окна/иконки."""
    tray = app.TrayApp.__new__(app.TrayApp)
    tray.cfg = {}
    tray.status = {"zapret": {"running": False, "as_service": False}}
    tray.busy = {}
    tray._signature = None
    tray._zapret_user_off = False
    tray.refresh = lambda: None
    tray.refresh_activity = lambda: None
    tray.notify = lambda message, title="bypass-tray": None
    return tray


def _wait(predicate, timeout: float = 1.0) -> None:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.01)


class SetZapretTests(unittest.TestCase):
    """Тумблер идёт существующим путём, а не заводит второй."""

    def test_turn_on_starts(self) -> None:
        tray = _tray(False)
        self.assertTrue(tray.set_zapret(True))
        self.assertEqual(tray.calls, ["start"])

    def test_turn_off_stops(self) -> None:
        tray = _tray(True)
        self.assertTrue(tray.set_zapret(False))
        self.assertEqual(tray.calls, ["stop"])

    def test_same_state_is_noop(self) -> None:
        tray = _tray(False)
        self.assertTrue(tray.set_zapret(False))
        self.assertEqual(tray.calls, [])

    def test_stop_sets_manual_flag_after_confirmation(self) -> None:
        tray = _bare()
        tray.status = {"zapret": {"running": False}}
        with mock.patch.object(actions, "stop_zapret", return_value={}):
            self.assertTrue(tray.stop_zapret())
            _wait(lambda: tray._zapret_user_off)
        self.assertTrue(tray._zapret_user_off)

    def test_start_clears_manual_flag_after_confirmation(self) -> None:
        tray = _bare()
        tray.status = {"zapret": {"running": True, "as_service": False}}
        tray._zapret_user_off = True
        with mock.patch.object(actions, "start_zapret", return_value={}):
            self.assertTrue(tray.start_zapret())
            _wait(lambda: not tray._zapret_user_off)
        self.assertFalse(tray._zapret_user_off)

    def test_denied_stop_does_not_set_flag(self) -> None:
        tray = _bare()
        tray.status = {"zapret": {"running": True}}
        with mock.patch.object(actions, "stop_zapret",
                               side_effect=PermissionError("UAC отклонён")), \
                mock.patch.object(app.activity, "log"):
            self.assertTrue(tray.stop_zapret())
            _wait(lambda: not tray.busy)
        self.assertFalse(tray._zapret_user_off)

    def test_unconfirmed_stop_does_not_set_flag(self) -> None:
        # actions.stop_zapret при отказе не бросает: winws всё ещё работает.
        tray = _bare()
        tray.status = {"zapret": {"running": True}}
        with mock.patch.object(actions, "stop_zapret",
                               return_value={"killed": 0, "denied": 1}):
            self.assertTrue(tray.stop_zapret())
            _wait(lambda: not tray.busy)
        self.assertFalse(tray._zapret_user_off)

    def test_service_turn_on_uses_control_service(self) -> None:
        tray = _bare()
        tray.status = {"zapret": {"running": False, "as_service": True}}
        called = []
        tray._run_action_busy = (
            lambda key, text, fn, *a, **k: called.append((fn, a)) or True)
        self.assertTrue(tray.set_zapret(True))
        self.assertEqual(called, [(actions.control_service, ("start",))])


class GuardManualOffTests(unittest.TestCase):
    """Ручное выключение — намерение, а не сбой."""

    def test_manual_off_is_silent(self) -> None:
        with mock.patch.object(guard, "_log_throttled") as log:
            self.assertEqual(
                guard._heal_zapret({"zapret": {"running": False}}, user_off=True), [])
        log.assert_not_called()

    def test_unexpected_stop_warns(self) -> None:
        with mock.patch.object(guard, "_log_throttled") as log:
            result = guard._heal_zapret({"zapret": {"running": False}})
        self.assertEqual(result[0]["kind"], "warn")
        log.assert_called_once_with(guard.ZAPRET_WARN)

    def test_health_softens_manual_off(self) -> None:
        item = health._check_zapret(
            {"zapret": {"running": False, "dir": "C:/Zapret"}}, user_off=True)
        self.assertEqual(item["state"], "off")
        self.assertEqual(health.level([item]), "ok")


if __name__ == "__main__":
    unittest.main()
