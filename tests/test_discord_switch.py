"""Discord при тумблере zapret: перезапуск через прокси и обратно.

Сеть, процессы и Xray подменяются. Запуск:
``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import actions, app, config, routes  # noqa: E402


class _Proc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.terminated = False
        self.killed = False
        self.alive = True

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def kill(self) -> None:
        self.killed = True
        self.alive = False

    def is_running(self) -> bool:
        return self.alive

    def status(self) -> str:
        return "running" if self.alive else "stopped"


class DiscordRestartTests(unittest.TestCase):
    def base(self, tmp: str) -> Path:
        directory = Path(tmp) / "Discord"
        directory.mkdir()
        (directory / "Update.exe").write_bytes(b"MZ")
        return directory

    def test_via_proxy_adds_start_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = self.base(tmp)
            popen: list[list[str]] = []
            with mock.patch.object(actions, "discord_base",
                                   return_value=base), \
                    mock.patch.object(actions, "discord_pids",
                                      return_value=[1]), \
                    mock.patch.object(actions.psutil, "Process",
                                      side_effect=lambda pid: _Proc(pid)), \
                    mock.patch.object(actions.subprocess, "Popen",
                                      side_effect=lambda args, **kw:
                                      popen.append(list(args))):
                result = actions.restart_discord(True, 10818)
        self.assertTrue(result["ok"])
        command = popen[0]
        self.assertIn("--process-start-args", command)
        self.assertIn("--proxy-server=http://127.0.0.1:10818", command)

    def test_direct_has_no_proxy_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = self.base(tmp)
            popen: list[list[str]] = []
            with mock.patch.object(actions, "discord_base",
                                   return_value=base), \
                    mock.patch.object(actions, "discord_pids",
                                      return_value=[]), \
                    mock.patch.object(actions.subprocess, "Popen",
                                      side_effect=lambda args, **kw:
                                      popen.append(list(args))):
                actions.restart_discord(False, 10818)
        self.assertNotIn("--process-start-args", popen[0])
        self.assertFalse(any("proxy-server" in part for part in popen[0]))

    def test_absent_discord(self) -> None:
        with mock.patch.object(actions, "discord_base",
                               return_value=None), \
                mock.patch.object(actions.subprocess, "Popen") as popen:
            result = actions.restart_discord(True, 10818)
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "absent")
        popen.assert_not_called()


class DiscordProxyCheckTests(unittest.TestCase):
    def test_proxy_connection_detected(self) -> None:
        conn = SimpleNamespace(pid=1, status="ESTABLISHED",
                               raddr=("127.0.0.1", 10818),
                               laddr=("127.0.0.1", 50000))
        fake_psutil = SimpleNamespace(
            CONN_ESTABLISHED="ESTABLISHED",
            net_connections=lambda kind=None: [conn])
        with mock.patch.object(actions, "psutil", fake_psutil), \
                mock.patch.object(actions, "discord_pids", return_value=[1]):
            self.assertTrue(actions.discord_uses_proxy(10818))
            self.assertFalse(actions.discord_uses_proxy(9999))


class DiscordForcedProxyTests(unittest.TestCase):
    def check(self, processes: list) -> bool:
        fake = SimpleNamespace(
            process_iter=lambda attrs=None: iter(processes))
        with mock.patch.object(actions, "psutil", fake):
            return actions.discord_forced_proxy()

    @staticmethod
    def process(name: str, command: list[str]) -> SimpleNamespace:
        return SimpleNamespace(info={"name": name},
                               cmdline=lambda: list(command))

    def test_main_with_proxy_flag(self) -> None:
        self.assertTrue(self.check([
            self.process("Discord.exe", [
                "Discord.exe", "--proxy-server=http://127.0.0.1:10818"]),
        ]))

    def test_main_without_proxy_flag(self) -> None:
        self.assertFalse(self.check([
            self.process("Discord.exe", ["Discord.exe"]),
        ]))

    def test_helper_type_is_ignored(self) -> None:
        self.assertFalse(self.check([
            self.process("Discord.exe", ["Discord.exe"]),
            self.process("Discord.exe", [
                "Discord.exe", "--type=renderer",
                "--proxy-server=http://127.0.0.1:10818"]),
        ]))

    def test_no_discord(self) -> None:
        self.assertFalse(self.check([
            self.process("DiscordSystemHelper.exe", ["helper.exe"]),
        ]))


class DiscordZapretIntegrationTests(unittest.TestCase):
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
        restart_patcher = mock.patch.object(
            app.xray, "restart", return_value={"ok": True})
        restart_patcher.start()
        self.addCleanup(restart_patcher.stop)

    def controller(self) -> app.TrayApp:
        tray = app.TrayApp.__new__(app.TrayApp)
        tray.cfg = {"discord_switch_on_zapret": True}
        tray.status = {"zapret": {"running": False}}
        tray.proxy = {"running": True, "busy": False, "port": 10818}
        tray.busy = {}
        tray._zapret_user_off = True
        tray._zapret_off_pending = False
        tray._signature = None
        tray.refresh_activity = lambda: None
        tray.update_icon = lambda: None
        return tray

    def patches(self, *, pids, forced, restart):
        return [
            mock.patch.object(actions, "discord_pids", return_value=pids),
            mock.patch.object(actions, "discord_forced_proxy",
                              return_value=forced),
            mock.patch.object(actions, "restart_discord", side_effect=restart),
        ]

    def test_stop_restarts_discord_via_proxy(self) -> None:
        tray = self.controller()
        calls: list[tuple] = []
        logs: list[str] = []
        with mock.patch.object(app.health, "_check_youtube",
                               return_value={"state": "ok"}), \
                mock.patch.object(app.health, "_check_discord",
                                  return_value={"state": "ok"}), \
                mock.patch.object(app.xray, "status",
                                  return_value={"running": True}), \
                mock.patch.object(app.activity, "log",
                                  side_effect=lambda text, kind="info":
                                  logs.append(text)):
            for item in self.patches(
                    pids=[1], forced=False,
                    restart=lambda via, port: calls.append((via, port))
                    or {"ok": True, "action": "restarted"}):
                item.start()
                self.addCleanup(item.stop)
            tray._after_zapret_stopped()
        self.assertEqual(calls, [(True, 10818)])
        self.assertTrue(any("через VPN" in text for text in logs))

    def test_stop_no_discord_no_restart(self) -> None:
        tray = self.controller()
        calls: list[tuple] = []
        with mock.patch.object(app.health, "_check_youtube",
                               return_value={"state": "ok"}), \
                mock.patch.object(app.health, "_check_discord",
                                  return_value={"state": "ok"}), \
                mock.patch.object(app.xray, "status",
                                  return_value={"running": True}), \
                mock.patch.object(app.activity, "log"):
            for item in self.patches(
                    pids=[], forced=False,
                    restart=lambda via, port: calls.append((via, port))):
                item.start()
                self.addCleanup(item.stop)
            tray._after_zapret_stopped()
        self.assertEqual(calls, [])

    def test_stop_already_proxied_no_restart(self) -> None:
        tray = self.controller()
        calls: list[tuple] = []
        with mock.patch.object(app.health, "_check_youtube",
                               return_value={"state": "ok"}), \
                mock.patch.object(app.health, "_check_discord",
                                  return_value={"state": "ok"}), \
                mock.patch.object(app.xray, "status",
                                  return_value={"running": True}), \
                mock.patch.object(app.activity, "log"):
            for item in self.patches(
                    pids=[1], forced=True,
                    restart=lambda via, port: calls.append((via, port))):
                item.start()
                self.addCleanup(item.stop)
            tray._after_zapret_stopped()
        self.assertEqual(calls, [])

    def test_flag_disables_stop_switch(self) -> None:
        tray = self.controller()
        tray.cfg = {"discord_switch_on_zapret": False}
        calls: list[tuple] = []
        with mock.patch.object(app.health, "_check_youtube",
                               return_value={"state": "ok"}), \
                mock.patch.object(app.health, "_check_discord",
                                  return_value={"state": "ok"}), \
                mock.patch.object(app.xray, "status",
                                  return_value={"running": True}), \
                mock.patch.object(app.activity, "log"):
            for item in self.patches(
                    pids=[1], forced=False,
                    restart=lambda via, port: calls.append((via, port))):
                item.start()
                self.addCleanup(item.stop)
            tray._after_zapret_stopped()
        self.assertEqual(calls, [])

    def test_start_returns_discord_direct(self) -> None:
        tray = self.controller()
        calls: list[tuple] = []
        logs: list[str] = []
        with mock.patch.object(app.xray, "status",
                               return_value={"running": True}), \
                mock.patch.object(app.activity, "log",
                                  side_effect=lambda text, kind="info":
                                  logs.append(text)):
            for item in self.patches(
                    pids=[1], forced=True,
                    restart=lambda via, port: calls.append((via, port))
                    or {"ok": True, "action": "restarted"}):
                item.start()
                self.addCleanup(item.stop)
            tray._after_zapret_started()
        self.assertEqual(calls, [(False, 10818)])
        self.assertTrue(any("напрямую" in text for text in logs))

    def test_start_direct_discord_not_touched(self) -> None:
        tray = self.controller()
        calls: list[tuple] = []
        with mock.patch.object(app.xray, "status",
                               return_value={"running": True}), \
                mock.patch.object(app.activity, "log"):
            for item in self.patches(
                    pids=[1], forced=False,
                    restart=lambda via, port: calls.append((via, port))):
                item.start()
                self.addCleanup(item.stop)
            tray._after_zapret_started()
        self.assertEqual(calls, [])

    def test_config_default_disables_manual_discord_switch(self) -> None:
        self.assertFalse(config.DEFAULTS["discord_switch_on_zapret"])
        self.assertFalse(config.DEFAULTS["youtube_via_vpn"])


if __name__ == "__main__":
    unittest.main()
