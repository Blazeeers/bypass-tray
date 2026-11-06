"""DNS в конфиге Xray: DoH, российский резолвер и bootstrap подписки.

Сеть не используется: DoH-ответ и загрузка подписки подменяются.
Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import xray  # noqa: E402

SERVER = {
    "name": "test",
    "host": "example.com",
    "port": 443,
    "outbound": {"tag": "proxy", "protocol": "vless",
                 "settings": {"vnext": []}},
}

LINK = "vless://11111111-1111-1111-1111-111111111111@example.com:443#boot"


class DnsConfigTests(unittest.TestCase):
    def test_doh_and_ru_resolvers(self) -> None:
        built = xray.build_config(
            SERVER, 10899, dns_hosts={"example.com": ["203.0.113.9"]})
        dns = built["dns"]
        addresses = [item["address"] for item in dns["servers"]]
        self.assertIn("https://1.1.1.1/dns-query", addresses)
        self.assertIn("https://dns.google/dns-query", addresses)
        self.assertIn(xray.DNS_RU_SERVER, addresses)
        self.assertEqual(dns["hosts"]["example.com"], ["203.0.113.9"])
        # Домен сервера известен из bootstrap — в RU-резолвер не дублируется.
        self.assertNotIn("domain:example.com", dns["servers"][0]["domains"])
        json.dumps(built)

    def test_doh_ip_goes_through_proxy(self) -> None:
        built = xray.build_config(SERVER, 10899)
        rules = built["routing"]["rules"]
        self.assertTrue(any(
            rule.get("outboundTag") == "proxy"
            and "1.1.1.1" in (rule.get("ip") or [])
            for rule in rules))

    def test_without_bootstrap_server_uses_ru_resolver(self) -> None:
        # build_config не делает bootstrap сам: пустые dns_hosts — штатный
        # путь, домен сервера уходит к прямому резолверу.
        built = xray.build_config(SERVER, 10899)
        self.assertIn("domain:example.com",
                      built["dns"]["servers"][0]["domains"])
        self.assertNotIn("hosts", built["dns"])

    def test_direct_domains_resolve_via_ru_not_doh(self) -> None:
        built = xray.build_config(
            SERVER, 10899, direct_domains=["direct.example"])
        ru = built["dns"]["servers"][0]["domains"]
        self.assertIn("domain:direct.example", ru)
        self.assertIn("geosite:private", ru)


class DohBootstrapTests(unittest.TestCase):
    def test_resolve_parses_a_records(self) -> None:
        answer = {"Answer": [
            {"type": 1, "data": "203.0.113.5"},
            {"type": 1, "data": "203.0.113.5"},
            {"type": 28, "data": "2001:db8::1"},
        ]}
        with mock.patch.object(xray, "_doh_lookup", return_value=answer):
            self.assertEqual(xray.doh_resolve("sub.example"), ["203.0.113.5"])

    def test_resolve_failure_is_empty(self) -> None:
        with mock.patch.object(xray, "_doh_lookup",
                               side_effect=OSError("no route")):
            self.assertEqual(xray.doh_resolve("sub.example"), [])

    def test_bootstrap_not_used_for_ip_or_http(self) -> None:
        self.assertIsNone(xray._bootstrap_get("https://203.0.113.1/sub", 1))
        self.assertIsNone(xray._bootstrap_get("http://sub.example/sub", 1))

    def test_subscription_uses_bootstrap(self) -> None:
        with mock.patch.object(xray, "_bootstrap_get",
                               return_value=(200, LINK.encode())), \
                mock.patch.object(xray.urllib.request, "urlopen",
                                  side_effect=AssertionError("системный DNS")):
            links = xray.fetch_subscription("https://sub.example/list")
        self.assertEqual(links, [LINK])

    def test_subscription_falls_back_to_urlopen(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return LINK.encode()

        with mock.patch.object(xray, "_bootstrap_get", return_value=None), \
                mock.patch.object(xray.urllib.request, "urlopen",
                                  return_value=Response()):
            links = xray.fetch_subscription("https://sub.example/list")
        self.assertEqual(links, [LINK])

    def test_subscription_non_2xx_bootstrap_falls_back(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return LINK.encode()

        with mock.patch.object(xray, "_bootstrap_get",
                               return_value=(500, b"")), \
                mock.patch.object(xray.urllib.request, "urlopen",
                                  return_value=Response()):
            links = xray.fetch_subscription("https://sub.example/list")
        self.assertEqual(links, [LINK])


class SwitchServerFilterTests(unittest.TestCase):
    """Реальный фильтр ``_switch_server`` по host или name."""

    ROWS = [
        {"name": "A", "host": "host-a", "link": "link-a", "latency": 1},
        {"name": "B", "host": "host-b", "link": "link-b", "latency": 2},
    ]

    def choose(self, exclude: set[str]) -> str:
        chosen: list[dict] = []

        def switch(server, cfg=None):
            chosen.append(server)
            return {"ok": True}

        with mock.patch.object(xray, "cached_servers", return_value=self.ROWS), \
                mock.patch.object(xray, "parse_link",
                                  side_effect=lambda link: {"name": link}), \
                mock.patch.object(xray, "switch_to", side_effect=switch):
            result = xray._switch_server(exclude=exclude)
        self.assertTrue(result.get("ok"))
        return chosen[0]["name"]

    def test_exclude_by_host(self) -> None:
        self.assertEqual(self.choose({"host-a"}), "link-b")

    def test_exclude_by_name(self) -> None:
        self.assertEqual(self.choose({"A"}), "link-b")

    def test_exclude_both_fields_of_one_server(self) -> None:
        self.assertEqual(self.choose({"host-a", "A"}), "link-b")


if __name__ == "__main__":
    unittest.main()
