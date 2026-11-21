"""Маршрут Telegram: режим в конфиге, статус, сторожа и переключение.

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import (  # noqa: E402
    actions, app, config, guard, health, status, telegram, xray,
)


class ModeTests(unittest.TestCase):
    def test_default_is_xray(self) -> None:
        self.assertEqual(config.normalise_telegram_mode(None), "xray")
        self.assertEqual(config.normalise_telegram_mode(""), "xray")
        self.assertEqual(config.normalise_telegram_mode("garbage"), "xray")
        self.assertEqual(config.DEFAULTS["telegram_mode"], "xray")

    def test_known_modes(self) -> None:
        self.assertEqual(config.normalise_telegram_mode("xray"), "xray")
        self.assertEqual(config.normalise_telegram_mode(" TGWS "), "tgws")

    def test_endpoint_follows_mode(self) -> None:
        cfg = {"xray_port": 10818, "tgws_port": 1443}
        self.assertEqual(telegram.endpoint(cfg, "xray"), "127.0.0.1:10818")
        self.assertEqual(telegram.endpoint(cfg, "tgws"), "127.0.0.1:1443")


class StatusTests(unittest.TestCase):
    def _collect(self, cfg: dict) -> dict:
        with mock.patch.object(status, "_processes", return_value=[]), \
                mock.patch.object(status, "_service_exists", return_value=False), \
                mock.patch.object(status, "port_open", return_value=False), \
                mock.patch.object(xray, "coverage", return_value={}):
            return status.collect(cfg)

    def test_telegram_key_xray(self) -> None:
        result = self._collect({"telegram_mode": "xray", "xray_port": 10818})
        self.assertEqual(result["telegram"],
                         {"mode": "xray", "endpoint": "127.0.0.1:10818"})

    def test_telegram_key_tgws(self) -> None:
        result = self._collect({"telegram_mode": "tgws", "tgws_port": 1443})
        self.assertEqual(result["telegram"],
                         {"mode": "tgws", "endpoint": "127.0.0.1:1443"})


class RuleTests(unittest.TestCase):
    def _rule(self, payload: dict) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "xray-config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(xray, "config_path", return_value=path):
                return xray.has_rule()

    def test_rule_present(self) -> None:
        self.assertTrue(self._rule({"routing": {"rules": [
            {"type": "field", "outboundTag": "proxy", "ip": ["geoip:telegram"]},
        ]}}))

    def test_rule_absent_or_other_outbound(self) -> None:
        self.assertFalse(self._rule({"routing": {"rules": [
            {"type": "field", "outboundTag": "direct", "ip": ["geoip:telegram"]},
            {"type": "field", "outboundTag": "proxy", "domain": ["x.test"]},
        ]}}))
        self.assertFalse(self._rule({}))


class ApplyTests(unittest.TestCase):
    def test_xray_already_running_keeps_xray_and_stops_tgws(self) -> None:
        cfg = {"telegram_mode": "xray"}
        with mock.patch.object(xray, "is_running", return_value=True), \
                mock.patch.object(xray, "has_rule", return_value=True), \
                mock.patch.object(actions, "tgws_running", return_value=True), \
                mock.patch.object(actions, "stop_tgws",
                                  return_value={"killed": 1}) as stop:
            result = telegram.apply(cfg, "xray")
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "already")
        self.assertTrue(result["stopped_tgws"])
        stop.assert_called_once_with()

    def test_xray_old_config_is_restarted(self) -> None:
        cfg = {"telegram_mode": "xray"}
        with mock.patch.object(xray, "is_running", return_value=True), \
                mock.patch.object(xray, "has_rule",
                                  side_effect=[False, True]), \
                mock.patch.object(xray, "restart",
                                  return_value={"ok": True}) as restart, \
                mock.patch.object(actions, "tgws_running", return_value=False):
            result = telegram.apply(cfg, "xray")
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "restarted")
        restart.assert_called_once_with(cfg)

    def test_xray_off_is_not_started_on_tray_start(self) -> None:
        cfg = {"telegram_mode": "xray"}
        with mock.patch.object(xray, "is_running", return_value=False), \
                mock.patch.object(xray, "has_rule", return_value=True), \
                mock.patch.object(xray, "start") as start, \
                mock.patch.object(actions, "tgws_running", return_value=False):
            result = telegram.apply(cfg, "xray", start_xray=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "off")
        start.assert_not_called()

    def test_xray_off_with_stale_config_is_not_success(self) -> None:
        cfg = {"telegram_mode": "xray"}
        with mock.patch.object(xray, "is_running", return_value=False), \
                mock.patch.object(xray, "has_rule", return_value=False), \
                mock.patch.object(xray, "start") as start, \
                mock.patch.object(actions, "tgws_running", return_value=False):
            result = telegram.apply(cfg, "xray", start_xray=False)
        self.assertFalse(result["ok"])
        self.assertIn("устарел", result["error"])
        start.assert_not_called()

    def test_tgws_starts_and_waits_15s(self) -> None:
        cfg = {"telegram_mode": "tgws", "tgws_exe": "C:/TgWsProxy.exe"}
        with mock.patch.object(actions, "tgws_running", return_value=False), \
                mock.patch.object(actions, "start_tgws",
                                  return_value=True) as start:
            result = telegram.apply(cfg, "tgws")
        self.assertTrue(result["ok"])
        start.assert_called_once_with(cfg, timeout=telegram.TGWS_START_TIMEOUT)
        self.assertEqual(telegram.TGWS_START_TIMEOUT, 15.0)

    def test_tgws_running_but_closed_restarts(self) -> None:
        cfg = {"telegram_mode": "tgws", "tgws_exe": "C:/TgWsProxy.exe"}
        with mock.patch.object(actions, "tgws_running",
                               side_effect=[True, False, False]), \
                mock.patch.object(telegram, "port_open", return_value=False), \
                mock.patch.object(actions, "stop_tgws",
                                  return_value={"killed": 1}) as stop, \
                mock.patch.object(actions, "start_tgws",
                                  return_value=True):
            result = telegram.apply(cfg, "tgws")
        self.assertTrue(result["ok"])
        stop.assert_called_once_with()

    def test_tgws_stop_timeout_is_error(self) -> None:
        cfg = {"telegram_mode": "tgws", "tgws_exe": "C:/TgWsProxy.exe"}
        with mock.patch.object(actions, "tgws_running", return_value=True), \
                mock.patch.object(telegram, "port_open", return_value=False), \
                mock.patch.object(actions, "stop_tgws",
                                  return_value={"killed": 1}), \
                mock.patch.object(telegram, "TGWS_STOP_TIMEOUT", 0.0), \
                mock.patch.object(actions, "start_tgws") as start:
            result = telegram.apply(cfg, "tgws")
        self.assertFalse(result["ok"])
        self.assertIn("не завершился", result["error"])
        start.assert_not_called()

    def test_hints_never_contain_secret(self) -> None:
        cfg = {"xray_port": 10818, "tgws_port": 1443}
        xray_hint = telegram.hint(cfg, "xray")
        tgws_hint = telegram.hint(cfg, "tgws")
        self.assertIn("SOCKS5 127.0.0.1:10818", xray_hint)
        self.assertIn("MTProto-прокси 127.0.0.1:1443", tgws_hint)
        self.assertIn("секрет", tgws_hint)

    def test_hints_use_configured_ports(self) -> None:
        cfg = {"xray_port": 11080, "tgws_port": 2443}
        self.assertIn("127.0.0.1:11080", telegram.hint(cfg, "xray"))
        self.assertIn("2443", telegram.hint(cfg, "xray"))
        self.assertIn("127.0.0.1:2443", telegram.hint(cfg, "tgws"))


class _Process:
    def __init__(self, pid: int = 111, alive: bool = True) -> None:
        self.pid = pid
        self.alive = alive

    def poll(self):
        return None if self.alive else 1


class ActionsTests(unittest.TestCase):
    def test_start_tgws_accepts_own_pid(self) -> None:
        cfg = {"tgws_exe": __file__}
        with mock.patch.object(actions, "tgws_running", return_value=False), \
                mock.patch.object(actions, "_popen",
                                  return_value=_Process(111)), \
                mock.patch.object(actions, "port_open", return_value=True), \
                mock.patch.object(actions, "port_pid", return_value=111):
            self.assertTrue(actions.start_tgws(cfg))

    def test_start_tgws_accepts_child_pid(self) -> None:
        cfg = {"tgws_exe": __file__}
        with mock.patch.object(actions, "tgws_running", return_value=False), \
                mock.patch.object(actions, "_popen",
                                  return_value=_Process(111)), \
                mock.patch.object(actions, "port_open", return_value=True), \
                mock.patch.object(actions, "port_pid", return_value=222), \
                mock.patch.object(actions, "_descends_from",
                                  return_value=True):
            self.assertTrue(actions.start_tgws(cfg))

    def test_start_tgws_reports_busy_port(self) -> None:
        cfg = {"tgws_exe": __file__}
        with mock.patch.object(actions, "tgws_running", return_value=False), \
                mock.patch.object(actions, "_popen",
                                  return_value=_Process(111)), \
                mock.patch.object(actions, "port_open", return_value=True), \
                mock.patch.object(actions, "port_pid", return_value=222), \
                mock.patch.object(actions, "_descends_from",
                                  return_value=False), \
                mock.patch.object(actions, "_pid_name",
                                  return_value="Other.exe"):
            with self.assertRaisesRegex(RuntimeError, "занят другим процессом"):
                actions.start_tgws(cfg)

    def test_start_tgws_dead_process_is_false(self) -> None:
        cfg = {"tgws_exe": __file__}
        with mock.patch.object(actions, "tgws_running", return_value=False), \
                mock.patch.object(actions, "_popen",
                                  return_value=_Process(111, alive=False)), \
                mock.patch.object(actions, "port_open", return_value=False), \
                mock.patch.object(actions, "TGWS_PORT_WAIT", 0.0):
            self.assertFalse(actions.start_tgws(cfg))

    def test_stop_tgws_kills_both_names(self) -> None:
        calls: list[str] = []

        def fake_kill(name: str) -> dict:
            calls.append(name)
            return {"killed": 1}

        with mock.patch.object(actions, "kill_process",
                               side_effect=fake_kill):
            result = actions.stop_tgws()
        self.assertEqual(calls, ["TgWsProxy", "tg-ws-proxy"])
        self.assertEqual(result["killed"], 2)


class MigrationTests(unittest.TestCase):
    def test_process_running_means_tgws(self) -> None:
        with mock.patch.object(actions, "tgws_running", return_value=True):
            self.assertTrue(config.tgws_already_used({}))

    def test_configured_exe_and_port_mean_tgws(self) -> None:
        with mock.patch.object(actions, "tgws_running", return_value=False), \
                mock.patch.object(actions, "port_open", return_value=True):
            self.assertTrue(config.tgws_already_used(
                {"tgws_exe": "C:/TgWsProxy.exe"}))
            self.assertFalse(config.tgws_already_used({}))

    def _old_config(self, tmp: str) -> Path:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({
            "zapret_dir": "C:/Zapret",
            "tgws_exe": "C:/TgWsProxy.exe",
            "xray_routes": {},
        }), encoding="utf-8")
        return path

    def test_load_config_migrates_when_tgws_was_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._old_config(tmp)
            with mock.patch.object(config, "_OVERRIDE_DIR", None), \
                    mock.patch.object(config, "CONFIG_FILE", path), \
                    mock.patch.object(config, "tgws_already_used",
                                      return_value=True):
                cfg = config.load_config()
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["telegram_mode"], "tgws")
        self.assertEqual(stored["telegram_mode"], "tgws")

    def test_load_config_keeps_xray_when_tgws_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._old_config(tmp)
            with mock.patch.object(config, "_OVERRIDE_DIR", None), \
                    mock.patch.object(config, "CONFIG_FILE", path), \
                    mock.patch.object(config, "tgws_already_used",
                                      return_value=False):
                cfg = config.load_config()
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["telegram_mode"], "xray")
        self.assertNotIn("telegram_mode", stored)


def _tray(mode: str) -> app.TrayApp:
    tray = app.TrayApp.__new__(app.TrayApp)
    tray.cfg = {"telegram_mode": mode}
    tray.busy = {}
    tray.health_report = {}
    tray.health_running = False
    tray.guard_running = False
    tray.updates_running = False
    tray._tgws_user_off = False
    tray._shutting_down = False
    tray._tgws_down = False
    tray._last_tgws_restart = 0.0
    return tray


class TrayTests(unittest.TestCase):
    def test_watchdog_silent_in_xray_mode(self) -> None:
        tray = _tray("xray")
        with mock.patch.object(actions, "start_tgws") as start:
            tray._watch_tgws()
        start.assert_not_called()

    def test_watchdog_works_in_tgws_mode(self) -> None:
        tray = _tray("tgws")
        tray.cfg["tgws_exe"] = __file__      # файл существует
        tray.refresh_activity = lambda: None
        tray._mark_service = lambda *a, **k: None
        with mock.patch.object(actions, "tgws_running", return_value=False), \
                mock.patch.object(actions, "start_tgws",
                                  return_value=True) as start, \
                mock.patch.object(app.activity, "log"):
            tray._watch_tgws()
        start.assert_called_once()

    def test_guard_does_not_heal_in_xray_mode(self) -> None:
        state = {"tgwsproxy": {"running": False, "listening": False}}
        with mock.patch.object(actions, "start_tgws") as start:
            self.assertEqual(guard._heal_tgws({"telegram_mode": "xray"}, state), [])
        start.assert_not_called()

    def test_health_off_in_xray_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "TgWsProxy_windows.exe"
            exe.write_bytes(b"MZ")
            item = health._check_tgws(
                {"tgwsproxy": {"listening": False}},
                {"tgws_exe": str(exe), "telegram_mode": "xray"})
        self.assertEqual(item["state"], "off")
        self.assertIn("Xray", item["detail"])

    def test_health_bad_in_tgws_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "TgWsProxy_windows.exe"
            exe.write_bytes(b"MZ")
            item = health._check_tgws(
                {"tgwsproxy": {"listening": False}},
                {"tgws_exe": str(exe), "telegram_mode": "tgws"})
        self.assertEqual(item["state"], "bad")


class SetModeTests(unittest.TestCase):
    def _mode_tray(self, mode: str = "xray") -> app.TrayApp:
        tray = _tray(mode)
        tray.proxy = {"busy": False}
        tray.refresh_activity = lambda: None
        tray.notify = lambda *a, **k: None
        tray._signature = None
        return tray

    def test_mode_saved_only_after_success(self) -> None:
        tray = self._mode_tray()
        saved: list[dict] = []

        def run(key, text, fn, *args, **kwargs):
            fn()
            return True

        tray._run_action_busy = run
        with mock.patch.object(app.telegram, "apply",
                               return_value={"ok": True, "changed": False,
                                             "mode": "tgws",
                                             "endpoint": "127.0.0.1:1443",
                                             "hint": "hint"}), \
                mock.patch.object(app.config, "save_config",
                                  side_effect=lambda cfg: (
                                      saved.append(dict(cfg)) or True)):
            self.assertTrue(tray.set_telegram_mode("tgws"))
        self.assertEqual(tray.cfg["telegram_mode"], "tgws")
        self.assertEqual(saved[0]["telegram_mode"], "tgws")

    def test_failed_apply_keeps_mode(self) -> None:
        tray = self._mode_tray()
        errors: list[str] = []

        def run(key, text, fn, *args, **kwargs):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
            return True

        tray._run_action_busy = run
        with mock.patch.object(app.telegram, "apply",
                               return_value={"ok": False,
                                             "error": "Xray не запустился"}), \
                mock.patch.object(app.config, "save_config") as save:
            tray.set_telegram_mode("tgws")
        self.assertEqual(tray.cfg["telegram_mode"], "xray")
        save.assert_not_called()
        self.assertIn("Xray не запустился", errors)

    def test_busy_vpn_blocks_switch(self) -> None:
        tray = self._mode_tray()
        tray.proxy = {"busy": True}
        tray._run_action_busy = lambda *a, **k: self.fail("не должно запускаться")
        self.assertFalse(tray.set_telegram_mode("tgws"))


if __name__ == "__main__":
    unittest.main()
