"""Droute: отдельный SOCKS-inbound для Discord и переключение proxy/direct.

Сеть, Xray и health подменяются. Запуск:
``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import app, config, droute, routes, xray  # noqa: E402

SERVER = {
    "name": "test",
    "host": "example.com",
    "port": 443,
    "outbound": {"tag": "proxy", "protocol": "vless",
                 "settings": {"vnext": []}},
}


class DrouteConfigTests(unittest.TestCase):
    def build(self, via_proxy: bool) -> dict:
        return xray.build_config(
            SERVER, 10818, routing_mode="blocked",
            droute_port=10819, discord_via_proxy=via_proxy)

    def test_socks_inbound(self) -> None:
        built = self.build(False)
        inbound = next(item for item in built["inbounds"]
                       if item.get("tag") == "droute")
        self.assertEqual(inbound["protocol"], "socks")
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["port"], 10819)
        self.assertTrue(inbound["settings"]["udp"])
        self.assertEqual(inbound["settings"]["auth"], "noauth")

    def test_droute_rule_is_first_and_switches(self) -> None:
        via = self.build(True)["routing"]["rules"]
        direct = self.build(False)["routing"]["rules"]
        self.assertEqual(via[0],
                         {"type": "field", "inboundTag": ["droute"],
                          "outboundTag": "proxy"})
        self.assertEqual(direct[0],
                         {"type": "field", "inboundTag": ["droute"],
                          "outboundTag": "direct"})

    def test_mixed_inbound_and_rf_rules_kept(self) -> None:
        built = self.build(False)
        mixed = next(item for item in built["inbounds"]
                     if item.get("tag") == "in")
        self.assertEqual(mixed["protocol"], "mixed")
        self.assertEqual(mixed["port"], 10818)
        domains = [d for rule in built["routing"]["rules"]
                   if rule.get("outboundTag") == "direct"
                   for d in (rule.get("domain") or [])]
        self.assertIn("geosite:category-ru", domains)

    def test_config_defaults(self) -> None:
        self.assertEqual(config.DEFAULTS["droute_socks_port"], 10819)
        self.assertFalse(config.DEFAULTS["discord_switch_on_zapret"])


class DrouteToggleTests(unittest.TestCase):
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

    def controller(self, *, vpn: bool = True) -> app.TrayApp:
        tray = app.TrayApp.__new__(app.TrayApp)
        tray.cfg = {}
        tray.status = {"zapret": {"running": False}}
        tray.proxy = {"running": vpn, "busy": False, "port": 10818}
        tray.busy = {}
        tray._zapret_user_off = True
        tray._zapret_off_pending = False
        tray._signature = None
        tray.refresh_activity = lambda: None
        tray.update_icon = lambda: None
        return tray

    def test_stop_rebuilds_with_proxy(self) -> None:
        tray = self.controller()
        calls: list[dict] = []
        with mock.patch.object(app.health, "_check_youtube",
                               return_value={"state": "ok"}), \
                mock.patch.object(app.health, "_check_discord",
                                  return_value={"state": "ok"}), \
                mock.patch.object(app.xray, "restart",
                                  side_effect=lambda cfg=None, **kw:
                                  calls.append(dict(kw)) or {"ok": True}), \
                mock.patch.object(app.xray, "status",
                                  return_value={"running": True}), \
                mock.patch.object(app.activity, "log"):
            tray._after_zapret_stopped()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get("discord_via_proxy"))

    def test_start_rebuilds_direct(self) -> None:
        tray = self.controller()
        calls: list[dict] = []
        with mock.patch.object(app.xray, "restart",
                               side_effect=lambda cfg=None, **kw:
                               calls.append(dict(kw)) or {"ok": True}), \
                mock.patch.object(app.xray, "status",
                                  return_value={"running": True}), \
                mock.patch.object(app.activity, "log"):
            tray._after_zapret_started()
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].get("discord_via_proxy"))

    def test_stop_without_vpn_no_rebuild(self) -> None:
        tray = self.controller(vpn=False)
        with mock.patch.object(app.xray, "restart") as restart, \
                mock.patch.object(app.activity, "log"):
            tray._after_zapret_stopped()
        restart.assert_not_called()


class DrouteManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(droute, "_is_windows", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_base(self, name: str = "Discord") -> Path:
        base = self.root / name
        base.mkdir(parents=True, exist_ok=True)
        (base / "Update.exe").write_bytes(b"MZ")
        return base

    def make_app(self, base: Path, number: int = 100,
                 with_dlls: bool = True) -> Path:
        app_dir = base / f"app-{number}"
        app_dir.mkdir()
        if with_dlls:
            (app_dir / "version.dll").write_bytes(b"v")
            (app_dir / "droute.dll").write_bytes(b"d")
        return app_dir

    def test_download_url_parsing(self) -> None:
        payload = json.dumps({
            "tag_name": "v1.2.3",
            "assets": [
                {"name": "other.zip", "browser_download_url": "https://x/o"},
                {"name": "droute.zip", "browser_download_url": "https://x/d"},
            ],
        }).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return payload

        with mock.patch.object(droute.urllib.request, "urlopen",
                               return_value=Response()):
            self.assertEqual(droute.download_url(), ("v1.2.3", "https://x/d"))

    def test_download_url_network_error(self) -> None:
        with mock.patch.object(droute.urllib.request, "urlopen",
                               side_effect=OSError("no net")):
            self.assertEqual(droute.download_url(), ("", ""))

    def test_discord_bases_and_branch(self) -> None:
        stable = self.make_base("Discord")
        canary = self.make_base("DiscordCanary")
        (self.root / "DiscordPTB").mkdir()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            bases = droute.discord_bases()
        self.assertIn(stable, bases)
        self.assertIn(canary, bases)
        self.assertEqual(droute.discord_branch(stable), "stable")
        self.assertEqual(droute.discord_branch(canary), "canary")

    def test_latest_and_installed(self) -> None:
        base = self.make_base()
        self.make_app(base, 99)
        newest = self.make_app(base, 100)
        self.assertEqual(droute.latest_app_dir(base), newest)
        self.assertTrue(droute.installed(base))
        with mock.patch.object(droute, "_file_version", return_value="1.2.3"):
            self.assertEqual(droute.installed_version(base), "1.2.3")
        (newest / "droute.dll").unlink()
        self.assertFalse(droute.installed(base))

    def test_install_command_and_callbacks(self) -> None:
        base = self.make_base()
        self.make_app(base, 1, with_dlls=False)
        calls: list[list[str]] = []
        closed: list[bool] = []
        opened: list[bool] = []

        def run(executable, args):
            calls.append(list(args))
            app_dir = droute.latest_app_dir(base)
            (app_dir / "version.dll").write_bytes(b"v")
            (app_dir / "droute.dll").write_bytes(b"d")
            return True, ""

        with mock.patch.object(droute, "_base_for_branch", return_value=base), \
                mock.patch.object(droute, "download_url",
                                  return_value=("v1", "https://x/d")), \
                mock.patch.object(droute, "_extract_installer",
                                  return_value=(Path("droute.exe"), None)), \
                mock.patch.object(droute, "_run_installer", side_effect=run), \
                mock.patch.object(droute, "_discord_running",
                                  return_value=True), \
                mock.patch.object(droute, "_close_discord",
                                  side_effect=lambda cb:
                                  closed.append(True) or True), \
                mock.patch.object(droute, "_file_version", return_value="1.2.3"):
            result = droute.install("stable", 10819,
                                    close_discord_cb=lambda: None,
                                    reopen_cb=lambda: opened.append(True))
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0], [
            "--install", "--branch", "stable", "--host", "127.0.0.1",
            "--port", "10819"])
        self.assertTrue(closed and opened)
        self.assertEqual(result["version"], "1.2.3")

    def test_uninstall_command(self) -> None:
        base = self.make_base()
        app_dir = self.make_app(base, 1)
        calls: list[list[str]] = []

        def run(executable, args):
            calls.append(list(args))
            (app_dir / "version.dll").unlink()
            (app_dir / "droute.dll").unlink()
            return True, ""

        with mock.patch.object(droute, "_base_for_branch", return_value=base), \
                mock.patch.object(droute, "download_url",
                                  return_value=("v1", "https://x/d")), \
                mock.patch.object(droute, "_extract_installer",
                                  return_value=(Path("droute.exe"), None)), \
                mock.patch.object(droute, "_run_installer", side_effect=run):
            result = droute.uninstall("stable")
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0], ["--uninstall", "--branch", "stable"])

    def test_ensure_paths(self) -> None:
        base = self.make_base()
        with mock.patch.object(droute, "_base_for_branch", return_value=base), \
                mock.patch.object(droute, "installed", return_value=False), \
                mock.patch.object(droute, "install",
                                  return_value={"ok": True, "action": "installed"}) as install:
            self.assertEqual(droute.ensure(10819)["action"], "installed")
        install.assert_called_once()

        with mock.patch.object(droute, "_base_for_branch", return_value=base), \
                mock.patch.object(droute, "installed", return_value=True), \
                mock.patch.object(droute, "configured_port",
                                  return_value=10819), \
                mock.patch.object(droute, "install") as install:
            self.assertEqual(droute.ensure(10819)["action"], "present")
        install.assert_not_called()

        with mock.patch.object(droute, "_base_for_branch", return_value=base), \
                mock.patch.object(droute, "installed", return_value=True), \
                mock.patch.object(droute, "configured_port",
                                  return_value=10820), \
                mock.patch.object(droute, "install",
                                  return_value={"ok": True, "action": "installed"}) as install:
            self.assertEqual(droute.ensure(10819)["action"], "reinstalled")
        install.assert_called_once()

        with mock.patch.object(droute, "_base_for_branch", return_value=None):
            self.assertEqual(droute.ensure(10819)["action"], "discord_absent")

    def test_configured_port_branch_isolation(self) -> None:
        with mock.patch.object(droute, "_branch_registry_values",
                               return_value=({"Port": 10819}, True)), \
                mock.patch.object(droute, "_root_registry_values",
                                  return_value={"Port": 9999}):
            self.assertEqual(droute.configured_port("stable"), 10819)
        with mock.patch.object(droute, "_branch_registry_values",
                               return_value=({}, False)), \
                mock.patch.object(droute, "_root_registry_values",
                                  return_value={"ProxyPort": "127.0.0.1:10818"}):
            self.assertEqual(droute.configured_port("stable"), 10818)

    def test_configured_port_rejects_dates_and_times(self) -> None:
        values = {"Port": "2025-11-08 12:34", "LastRun": "10:45",
                  "Updated": "installed 10:45"}
        with mock.patch.object(droute, "_branch_registry_values",
                               return_value=(values, True)):
            self.assertIsNone(droute.configured_port("stable"))
        with mock.patch.object(droute, "_branch_registry_values",
                               return_value=({"Port": "10819"}, True)):
            self.assertEqual(droute.configured_port("stable"), 10819)

    def test_ensure_unknown_port_is_present(self) -> None:
        base = self.make_base()
        with mock.patch.object(droute, "_base_for_branch", return_value=base), \
                mock.patch.object(droute, "installed", return_value=True), \
                mock.patch.object(droute, "configured_port",
                                  return_value=None), \
                mock.patch.object(droute, "install") as install:
            result = droute.ensure(10819)
        self.assertEqual(result["action"], "present")
        self.assertFalse(result["port_known"])
        install.assert_not_called()

    def test_reopen_after_failed_install(self) -> None:
        base = self.make_base()
        self.make_app(base, 1, with_dlls=False)
        reopened: list[bool] = []
        with mock.patch.object(droute, "_base_for_branch", return_value=base), \
                mock.patch.object(droute, "download_url",
                                  return_value=("v1", "https://x/d")), \
                mock.patch.object(droute, "_extract_installer",
                                  return_value=(Path("droute.exe"), None)), \
                mock.patch.object(droute, "_discord_running",
                                  return_value=True), \
                mock.patch.object(droute, "_close_discord",
                                  return_value=True), \
                mock.patch.object(droute, "_run_installer",
                                  return_value=(False, "fail")):
            result = droute.install("stable", 10819,
                                    reopen_cb=lambda: reopened.append(True))
        self.assertFalse(result["ok"])
        self.assertEqual(reopened, [True])

    def test_no_reopen_when_discord_was_not_running(self) -> None:
        base = self.make_base()
        self.make_app(base, 1, with_dlls=False)
        reopened: list[bool] = []
        with mock.patch.object(droute, "_base_for_branch", return_value=base), \
                mock.patch.object(droute, "download_url",
                                  return_value=("v1", "https://x/d")), \
                mock.patch.object(droute, "_extract_installer",
                                  return_value=(Path("droute.exe"), None)), \
                mock.patch.object(droute, "_discord_running",
                                  return_value=False), \
                mock.patch.object(droute, "_close_discord",
                                  return_value=False), \
                mock.patch.object(droute, "_run_installer",
                                  return_value=(False, "fail")):
            droute.install("stable", 10819,
                           reopen_cb=lambda: reopened.append(True))
        self.assertEqual(reopened, [])

    def test_config_defaults(self) -> None:
        self.assertFalse(config.DEFAULTS["droute_enabled"])
        self.assertEqual(config.DEFAULTS["droute_branch"], "stable")
        self.assertEqual(config.DEFAULTS["droute_socks_port"], 10819)


if __name__ == "__main__":
    unittest.main()
