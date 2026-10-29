"""Тесты режима маршрутизации: конфиг приложения, конфиг Xray, статус.

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

from bypass_tray import config, status, xray  # noqa: E402

#: Минимальный сервер: ``build_config`` читает только ``outbound``.
SERVER = {
    "name": "test",
    "host": "example.com",
    "port": 443,
    "outbound": {"tag": "proxy", "protocol": "vless",
                 "settings": {"vnext": []}},
}


class RoutingModeConfigTests(unittest.TestCase):
    """Значение из конфига: по умолчанию и при мусоре — ``blocked``."""

    def test_default_is_blocked(self) -> None:
        self.assertEqual(config.DEFAULTS["routing_mode"], "blocked")

    def test_unknown_values_fall_back_to_blocked(self) -> None:
        for value in (None, "", "weird", "ALL?", 42, [], {}, "blocked2"):
            with self.subTest(value=value):
                self.assertEqual(config.normalise_routing_mode(value), "blocked")

    def test_all_is_kept(self) -> None:
        self.assertEqual(config.normalise_routing_mode("all"), "all")
        self.assertEqual(config.normalise_routing_mode("  ALL  "), "all")


class BuildConfigTests(unittest.TestCase):
    """Маршрут по умолчанию = первый outbound; правила не зависят от режима."""

    def build(self, mode: str | None = None, **kwargs) -> dict:
        if mode is None:
            return xray.build_config(SERVER, 10899, **kwargs)
        return xray.build_config(SERVER, 10899, routing_mode=mode, **kwargs)

    def test_default_mode_is_blocked(self) -> None:
        cfg = self.build()
        self.assertEqual(cfg["outbounds"][0]["tag"], "direct")

    def test_all_routes_default_traffic_to_proxy(self) -> None:
        cfg = self.build("all")
        # Проверка по сгенерированному JSON, а не по объекту в памяти.
        payload = json.loads(json.dumps(cfg, ensure_ascii=False))
        first = payload["outbounds"][0]
        self.assertEqual(first["tag"], "proxy")
        self.assertEqual(first["protocol"], "vless")

    def test_blocked_routes_default_traffic_direct(self) -> None:
        cfg = self.build("blocked")
        first = cfg["outbounds"][0]
        self.assertEqual(first["tag"], "direct")
        self.assertEqual(first["protocol"], "freedom")

    def test_russian_and_private_stay_direct_in_both_modes(self) -> None:
        for mode in ("blocked", "all"):
            with self.subTest(mode=mode):
                rules = self.build(mode)["routing"]["rules"]
                domains = [d for rule in rules
                           if rule.get("outboundTag") == "direct"
                           for d in (rule.get("domain") or [])]
                ips = [d for rule in rules
                       if rule.get("outboundTag") == "direct"
                       for d in (rule.get("ip") or [])]
                self.assertIn("geosite:category-ru", domains)
                self.assertIn("geosite:private", domains)
                self.assertIn("geoip:ru", ips)
                self.assertIn("geoip:private", ips)

    def test_user_direct_stays_direct_in_all_mode(self) -> None:
        cfg = self.build("all", user_routes={"example.org": "direct"})
        rule = next(rule for rule in cfg["routing"]["rules"]
                    if "domain:example.org" in (rule.get("domain") or []))
        self.assertEqual(rule["outboundTag"], "direct")

    def test_stream_direct_skipped_in_all_mode(self) -> None:
        # В режиме "all" YouTube/Discord не исключение: явного direct нет.
        rules = self.build("all")["routing"]["rules"]
        for rule in rules:
            if rule.get("outboundTag") != "direct":
                continue
            joined = " ".join(rule.get("domain") or [])
            self.assertNotIn("youtube", joined)


class StatusTests(unittest.TestCase):
    """``status.collect`` отдаёт режим и состояние системного прокси."""

    def test_collect_reports_routing_mode(self) -> None:
        with mock.patch.object(
                xray, "coverage",
                return_value={"routing_mode": "blocked", "system_proxy": True,
                              "bypass": ["localhost", "*.steampowered.com"]}):
            state = status.collect({"routing_mode": "all"})
        # Режим берётся из уже прочитанного конфига, а не переспрашивается.
        self.assertEqual(state["routing_mode"], "all")
        self.assertTrue(state["system_proxy"])
        self.assertEqual(state["bypass"],
                         ["localhost", "*.steampowered.com"])

    def test_collect_falls_back_to_config(self) -> None:
        with mock.patch.object(xray, "coverage",
                               side_effect=RuntimeError("boom")):
            state = status.collect({"routing_mode": "all"})
        self.assertEqual(state["routing_mode"], "all")
        self.assertFalse(state["system_proxy"])
        self.assertEqual(state["bypass"], [])

    def test_collect_does_not_load_config(self) -> None:
        # ``config.load_config`` на пустом ``zapret_dir`` обходит диск (~2 с),
        # а ``collect`` опрашивается каждые 5 секунд.
        calls: list[tuple] = []

        def boom(*args, **kwargs):
            calls.append(args)
            raise AssertionError("collect не должен читать конфиг")

        with mock.patch.object(config, "load_config", side_effect=boom):
            state = status.collect({"routing_mode": "all"})
        self.assertEqual(calls, [])
        self.assertEqual(state["routing_mode"], "all")


if __name__ == "__main__":
    unittest.main()
