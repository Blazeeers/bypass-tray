"""Эскалация надзирателя: 5 серверов, бэкофф и сброс по сети.

Сеть и Xray не трогаются: переключение сервера, маршруты и журнал — заглушки.
Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import guard, routes, xray  # noqa: E402


def _health(vpn: str = "bad", youtube: str = "bad") -> dict:
    return {"items": [{"id": "vpn", "state": vpn},
                      {"id": "youtube", "state": youtube}]}


class EscalationTests(unittest.TestCase):
    def setUp(self) -> None:
        guard.reset_escalation()
        guard._VPN_NETWORK = ""
        self.switches: list[set] = []
        self.overrides: list[list[str]] = []
        self.patches = [
            mock.patch.object(guard.networks, "current",
                              return_value={"key": "net-a"}),
            mock.patch.object(guard.activity, "log"),
            mock.patch.object(xray, "switch_server", side_effect=self._switch),
            mock.patch.object(routes, "enable_service_proxy",
                              side_effect=self._enable),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(guard.reset_escalation)

    def _switch(self, exclude=None, cfg=None):
        self.switches.append(set(exclude or ()))
        number = len(self.switches)
        return {"ok": True,
                "server": {"name": f"new-{number}", "host": f"newhost-{number}"}}

    def _enable(self, services, cfg=None):
        self.overrides.append(list(services))
        return {"ok": True, "changed": ["youtube.com"], "status": "changed"}

    def heal(self, host: str = "current-host", name: str = "current") -> list:
        return guard._heal_vpn(
            {}, {"running": True, "server": name, "host": host}, _health())

    def test_five_servers_then_service_override(self) -> None:
        for _ in range(guard.VPN_MAX_SERVERS):
            guard._VPN_SWITCH_AT = 0.0
            self.heal()
        self.assertEqual(len(self.switches), guard.VPN_MAX_SERVERS)
        self.assertEqual(guard.escalation_state()["attempts"],
                         guard.VPN_MAX_SERVERS)
        actions = self.heal()
        self.assertEqual(len(self.switches), guard.VPN_MAX_SERVERS)
        self.assertEqual(self.overrides, [["youtube"]])
        self.assertEqual(actions[0]["kind"], "ok")
        # Повторно тот же оверрайд не пишется и не логируется.
        self.assertEqual(self.heal(), [])

    def test_backoff_grows_and_blocks(self) -> None:
        guard._VPN_SWITCH_AT = 0.0
        self.heal()
        self.assertEqual(guard.escalation_state()["backoff"], 15 * 60)
        # В пределах бэкоффа — новое переключение не делаем.
        self.heal()
        self.assertEqual(len(self.switches), 1)

        guard._VPN_SWITCH_AT = time.time() - (15 * 60 + 1)
        self.heal()
        self.assertEqual(guard.escalation_state()["backoff"], 30 * 60)

        guard._VPN_SWITCH_AT = time.time() - (30 * 60 + 1)
        self.heal()
        self.assertEqual(guard.escalation_state()["backoff"], 60 * 60)

        guard._VPN_SWITCH_AT = time.time() - (60 * 60 + 1)
        self.heal()
        self.assertEqual(guard.escalation_state()["backoff"], 60 * 60)

    def test_exclude_uses_host_and_name(self) -> None:
        guard._VPN_SWITCH_AT = 0.0
        self.heal(host="host-1", name="name-1")
        self.assertIn("host-1", self.switches[0])
        self.assertIn("name-1", self.switches[0])
        # Новый сервер тоже запоминается по обоим полям.
        self.assertIn("newhost-1", guard._VPN_FAILED)
        self.assertIn("new-1", guard._VPN_FAILED)

    def test_network_change_resets_escalation(self) -> None:
        guard._VPN_ATTEMPTS = 3
        guard._VPN_FAILED = ["old"]
        guard._VPN_SWITCH_AT = time.time()
        guard._VPN_NETWORK = "net-a"
        with mock.patch.object(guard.networks, "current",
                               return_value={"key": "net-b"}):
            self.assertTrue(guard._sync_network())
        state = guard.escalation_state()
        self.assertEqual(state["attempts"], 0)
        self.assertEqual(state["failed"], [])
        self.assertEqual(state["backoff"], 15 * 60)
        self.assertEqual(state["next_at"], 15 * 60)
        # Та же сеть — счётчик не сбрасывается повторно.
        with mock.patch.object(guard.networks, "current",
                               return_value={"key": "net-b"}):
            self.assertFalse(guard._sync_network())


class ServiceOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(routes, "config_dir",
                                    return_value=Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        routes.save_overrides({})

    def test_manual_direct_domain_is_not_overridden(self) -> None:
        cfg = {"xray_routes": {"youtube.com": "direct"}}
        result = routes.enable_service_proxy(["youtube"], cfg)
        self.assertNotIn("youtube.com", result["changed"])
        overrides = routes.load_overrides()
        self.assertNotIn("youtube.com", overrides)
        self.assertEqual(overrides["googlevideo.com"]["route"], "proxy")
        again = routes.enable_service_proxy(["youtube"], cfg)
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(again["changed"], [])

    def test_manual_geosite_blocks_whole_service(self) -> None:
        cfg = {"xray_routes": {"geosite:youtube": "direct"}}
        result = routes.enable_service_proxy(["youtube"], cfg)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(routes.load_overrides(), {})

    def test_broken_file_is_not_overwritten(self) -> None:
        path = routes.overrides_path()
        path.write_text("{ это не json", encoding="utf-8")
        before = path.read_bytes()
        result = routes.enable_service_proxy(["youtube"], {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "broken")
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(list(Path(self._tmp.name).glob(
            "route_overrides.json.broken-*")))

    def test_existing_direct_override_is_kept(self) -> None:
        routes.save_overrides({"youtube.com": {
            "route": "direct", "since": 1.0, "checked": 1.0}})
        result = routes.enable_service_proxy(["youtube"], {})
        self.assertNotIn("youtube.com", result["changed"])
        self.assertEqual(routes.load_overrides()["youtube.com"]["route"],
                         "direct")

    def test_rollback_restores_previous_and_keeps_audit_at(self) -> None:
        routes.save_overrides({"old.example": {
            "route": "direct", "since": 1.0, "checked": 1.0}}, at=1000.0)
        result = routes.enable_service_proxy(["youtube"], {})
        self.assertEqual(result["status"], "changed")
        self.assertEqual(routes._read_state()[0]["at"], 1000.0)
        with mock.patch.object(xray, "is_running", return_value=False):
            rolled = routes.rollback_last({})
        self.assertTrue(rolled.get("ok"), rolled)
        overrides = routes.load_overrides()
        self.assertIn("old.example", overrides)
        self.assertNotIn("youtube.com", overrides)

    def test_escalation_survives_full_audit(self) -> None:
        self.assertEqual(
            routes.enable_service_proxy(["youtube"], {})["status"], "changed")
        result = {"full": True, "items": [
            {"domain": "other.example", "route": "proxy"}]}
        applied = routes.apply_audit(result, {})
        self.assertTrue(applied.get("saved"), applied)
        overrides = routes.load_overrides()
        self.assertEqual(overrides["youtube.com"]["route"], "proxy")
        self.assertEqual(overrides["youtube.com"]["source"], "escalation")


class OverrideRouteOrderTests(unittest.TestCase):
    """Итоговый конфиг: proxy-оверрайд сервиса должен быть выше direct."""

    SERVER = {
        "name": "test",
        "host": "example.com",
        "port": 443,
        "outbound": {"tag": "proxy", "protocol": "vless",
                     "settings": {"vnext": []}},
    }

    def test_override_rule_precedes_stream_direct(self) -> None:
        built = xray.build_config(
            self.SERVER, 10899,
            extra_domains=["youtube.com", "googlevideo.com"],
            stream_via_proxy=False, routing_mode="blocked",
            priority_proxy_domains=["youtube.com", "googlevideo.com"])
        rules = built["routing"]["rules"]

        def index_of(tag: str, needle: str) -> int:
            for index, rule in enumerate(rules):
                if rule.get("outboundTag") == tag and needle in (
                        rule.get("domain") or []):
                    return index
            raise AssertionError(f"нет правила {tag}: {needle} в {rules}")

        proxy = index_of("proxy", "domain:youtube.com")
        direct = index_of("direct", "geosite:youtube")
        self.assertLess(proxy, direct)
        self.assertLess(index_of("proxy", "domain:googlevideo.com"), direct)

    def test_priority_domain_not_duplicated_in_general_proxy(self) -> None:
        built = xray.build_config(
            self.SERVER, 10899,
            extra_domains=["youtube.com", "googlevideo.com"],
            priority_proxy_domains=["youtube.com", "googlevideo.com"])
        hits = [rule for rule in built["routing"]["rules"]
                if rule.get("outboundTag") == "proxy"
                and "domain:youtube.com" in (rule.get("domain") or [])]
        self.assertEqual(len(hits), 1, hits)

    def test_user_direct_beats_priority_proxy(self) -> None:
        built = xray.build_config(
            self.SERVER, 10899, extra_domains=["youtube.com"],
            user_routes={"youtube.com": "direct"},
            priority_proxy_domains=["youtube.com"])
        rules = built["routing"]["rules"]
        self.assertFalse(any(
            rule.get("outboundTag") == "proxy"
            and "domain:youtube.com" in (rule.get("domain") or [])
            for rule in rules))
        self.assertTrue(any(
            rule.get("outboundTag") == "direct"
            and "domain:youtube.com" in (rule.get("domain") or [])
            for rule in rules))


if __name__ == "__main__":
    unittest.main()
