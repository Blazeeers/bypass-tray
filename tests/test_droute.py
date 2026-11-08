"""Droute: отдельный SOCKS-inbound для Discord и переключение proxy/direct.

Сеть, Xray и health подменяются. Запуск:
``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import app, config, routes, xray  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
