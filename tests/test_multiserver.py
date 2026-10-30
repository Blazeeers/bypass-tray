"""Схема ``xray_routes`` v2: правило «домен → свой сервер».

Старый файл (строка ``"direct"|"proxy"``) обязан читаться как раньше, а новое
правило с сервером — получать собственный outbound Xray. Тесты чистые: сети
нет, файлы конфига подменяются временным каталогом.

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

from bypass_tray import config, networks, routes, xray  # noqa: E402

PORT = 10899

#: Текущий сервер: именно он попадает в конфиг как маршрут по умолчанию.
CURRENT = {
    "name": "current",
    "host": "cur.example",
    "port": 443,
    "link": "vless://11111111-1111-1111-1111-111111111111@cur.example:443#current",
}

#: Другой сервер из списка — то, куда должно вести правило с ``server``.
OTHER = {
    "name": "other",
    "host": "other.example",
    "port": 443,
    "latency": 12,
    "link": "vless://22222222-2222-2222-2222-222222222222@other.example:443#other",
}


def _full(row: dict) -> dict:
    """Сервер в том виде, в каком его ждёт ``build_config``: с outbound."""
    server = dict(row)
    server["outbound"] = xray.parse_link(row["link"])["outbound"]
    return server


CURRENT_FULL = _full(CURRENT)


def _tags(config_dict: dict) -> list[str]:
    return [str(outbound.get("tag")) for outbound in config_dict["outbounds"]]


def _rules_for(config_dict: dict, domain: str) -> list[str]:
    """Теги правил, в которых встречается домен."""
    needle = f"domain:{domain}"
    return [str(rule.get("outboundTag")) for rule in config_dict["routing"]["rules"]
            if needle in (rule.get("domain") or [])]


class NormaliseRoutesTests(unittest.TestCase):
    """Чтение обеих схем: v1, v2 и их смесь."""

    def test_v1_string_reads_as_object(self) -> None:
        rules = routes.normalise_routes({"x.com": "direct", "y.com": "proxy"})
        self.assertEqual(rules, {
            "x.com": {"route": "direct", "server": None},
            "y.com": {"route": "proxy", "server": None},
        })

    def test_v2_object_reads(self) -> None:
        rules = routes.normalise_routes(
            {"a.com": {"route": "proxy", "server": "other"}})
        self.assertEqual(rules["a.com"], {"route": "proxy", "server": "other"})

    def test_mixed_file(self) -> None:
        rules = routes.normalise_routes({
            "old.com": "direct",
            "new.com": {"route": "proxy", "server": "other"},
            # Напрямую сервер не нужен: поле отбрасывается.
            "direct-new.com": {"route": "direct", "server": "other"},
            # Пустая строка — это «текущий сервер».
            "blank.com": {"route": "proxy", "server": ""},
            "www.dup.com": "proxy",
            "dup.com": {"route": "direct"},
        })
        self.assertEqual(rules["old.com"], {"route": "direct", "server": None})
        self.assertEqual(rules["new.com"], {"route": "proxy", "server": "other"})
        self.assertEqual(rules["direct-new.com"],
                         {"route": "direct", "server": None})
        self.assertEqual(rules["blank.com"], {"route": "proxy", "server": None})
        # Тот же домен после нормализации: direct сильнее proxy.
        self.assertEqual(rules["dup.com"]["route"], "direct")

    def test_user_routes_keeps_old_contract(self) -> None:
        direct, proxy = routes.user_routes({"xray_routes": {
            "a.com": "direct",
            "b.com": {"route": "proxy", "server": "other"},
            # Конфликт: direct у того же домена после нормализации.
            "www.c.com": {"route": "proxy", "server": "other"},
            "c.com": "direct",
        }})
        self.assertEqual(direct, ["a.com", "c.com"])
        self.assertEqual(proxy, ["b.com"])


class BuildConfigServerTests(unittest.TestCase):
    """Outbounds и правила при правиле «домен → свой сервер»."""

    def build(self, **kwargs) -> dict:
        params = {"extra_domains": None, "stream_via_proxy": False,
                  "blocked_rule": None, "routing_mode": "blocked"}
        params.update(kwargs)
        return xray.build_config(CURRENT_FULL, PORT, **params)

    def test_foreign_server_gets_own_outbound(self) -> None:
        built = self.build(
            user_routes={"a.com": {"route": "proxy", "server": "other"}},
            servers=[CURRENT, OTHER])
        tags = _tags(built)
        extra = [tag for tag in tags if tag not in ("direct", "proxy", "block")]
        self.assertEqual(len(extra), 1, tags)
        # Правило ведёт домен именно на этот сервер, а не на текущий.
        self.assertEqual(_rules_for(built, "a.com"), [extra[0]])
        self.assertEqual(tags[-1], "block")
        self.assertEqual(built.warnings, [])

    def test_no_foreign_servers_matches_old_config(self) -> None:
        base = {"direct_domains": ["direct.example"],
                "user_routes": {"direct.example": "direct", "x.com": "proxy",
                                "y.com": "proxy"}}
        plain = json.dumps(self.build(**base), ensure_ascii=False)
        with_servers = json.dumps(self.build(
            servers=[CURRENT, OTHER], current_server=CURRENT_FULL,
            **base), ensure_ascii=False)
        self.assertEqual(plain, with_servers)
        # И правило proxy осталось одно — то же, что было до схемы v2.
        config_dict = json.loads(plain)
        self.assertEqual(_rules_for(config_dict, "x.com"), ["proxy"])
        self.assertEqual(_tags(config_dict), ["direct", "proxy", "block"])

    def test_rule_on_current_server_keeps_plain_proxy(self) -> None:
        built = self.build(
            user_routes={"a.com": {"route": "proxy", "server": "current"}},
            servers=[CURRENT, OTHER], current_server=CURRENT_FULL)
        self.assertEqual(_tags(built), ["direct", "proxy", "block"])
        self.assertEqual(_rules_for(built, "a.com"), ["proxy"])
        self.assertEqual(built.warnings, [])

    def test_unknown_server_falls_back_with_warning(self) -> None:
        built = self.build(
            user_routes={"a.com": {"route": "proxy", "server": "нет такого"}},
            servers=[CURRENT, OTHER])
        self.assertEqual(_rules_for(built, "a.com"), ["proxy"])
        self.assertEqual(len(built.warnings), 1, built.warnings)
        self.assertIn("нет такого", built.warnings[0])
        # Тегов-сирот нет: каждое правило ссылается на существующий outbound.
        tags = set(_tags(built))
        for rule in built["routing"]["rules"]:
            self.assertIn(str(rule.get("outboundTag")), tags, rule)

    def test_two_rules_one_server_single_outbound(self) -> None:
        built = self.build(
            user_routes={"a.com": {"route": "proxy", "server": "other"},
                         "b.com": {"route": "proxy", "server": "other"}},
            servers=[CURRENT, OTHER])
        tags = _tags(built)
        extra = [tag for tag in tags if tag not in ("direct", "proxy", "block")]
        self.assertEqual(len(extra), 1, tags)
        # Оба домена в одном правиле этого сервера.
        self.assertEqual(_rules_for(built, "a.com"), [extra[0]])
        self.assertEqual(_rules_for(built, "b.com"), [extra[0]])


class BypassListTests(unittest.TestCase):
    """Исключения системного прокси на схеме v2."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(routes, "config_dir",
                                    return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _bypass(self, cfg: dict) -> list[str]:
        with mock.patch.object(config, "load_config", return_value=cfg):
            return xray._proxy_bypass_list(False).split(";")

    def test_v2_direct_in_bypass_proxy_not(self) -> None:
        entries = self._bypass({
            "routing_mode": "blocked",
            "xray_routes": {
                "direct.example": "direct",
                "blocked.example": {"route": "direct", "server": "other"},
                "x.com": {"route": "proxy", "server": "other"},
                "y.com": "proxy",
            },
        })
        self.assertIn("direct.example", entries)
        self.assertIn("blocked.example", entries)
        # Домен, ведущий в VPN чужим сервером, в обход прокси не попадает.
        self.assertNotIn("x.com", entries)
        self.assertNotIn("y.com", entries)
        self.assertIn("localhost", entries)


class PayloadTests(unittest.TestCase):
    """Сериализация формы: строка без сервера, объект с сервером."""

    def test_no_server_writes_plain_string(self) -> None:
        self.assertEqual(routes.routes_payload({"x.com": "proxy"}),
                         {"x.com": "proxy"})
        self.assertEqual(
            routes.routes_payload({"a.com": "direct"}, {"a.com": None}),
            {"a.com": "direct"})

    def test_selected_server_writes_object(self) -> None:
        payload = routes.routes_payload(
            {"x.com": "proxy", "a.com": "direct"},
            {"x.com": "other", "a.com": "other"})
        # У direct сервер не пишется: напрямую он не нужен.
        self.assertEqual(payload, {"x.com": {"route": "proxy", "server": "other"},
                                   "a.com": "direct"})

    def test_v2_survives_reload_and_save(self) -> None:
        """Цикл открыть настройки → закрыть: правило с сервером не теряется.

        Раньше `reload` фильтровал значения через `str(route) in
        ("direct", "proxy")`, и объект v2 отбрасывался.
        """
        source = {"x.com": {"route": "proxy", "server": "other"},
                  "old.com": "direct"}
        loaded = routes.normalise_routes(source)
        rules = {name: info["route"] for name, info in loaded.items()}
        servers = {name: info["server"] for name, info in loaded.items()}
        self.assertEqual(rules, {"x.com": "proxy", "old.com": "direct"})
        self.assertEqual(servers, {"x.com": "other", "old.com": None})
        self.assertEqual(routes.routes_payload(rules, servers), source)

    def test_direct_never_keeps_server(self) -> None:
        source = {"a.com": {"route": "direct", "server": "other"}}
        loaded = routes.normalise_routes(source)
        rules = {name: info["route"] for name, info in loaded.items()}
        servers = {name: info["server"] for name, info in loaded.items()}
        self.assertEqual(routes.routes_payload(rules, servers), {"a.com": "direct"})


class NetworksTests(unittest.TestCase):
    """Сервер рядом со стратегией сети: старый файл читается, новый пишется."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(networks, "config_dir",
                                    return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, records: dict) -> None:
        networks.path().write_text(json.dumps({"networks": records}),
                                   encoding="utf-8")

    def test_old_format_reads_without_server(self) -> None:
        self._write({"10.0.0.1": {"bat": "general.bat", "at": 1,
                                   "ssid": "Home", "gateway": "10.0.0.1"}})
        self.assertEqual(networks.strategy_for("10.0.0.1"), "general.bat")
        self.assertEqual(networks.server_for("10.0.0.1"), "")

    def test_remember_saves_server(self) -> None:
        info = {"ssid": "Home", "gateway": "10.0.0.1"}
        networks.remember("10.0.0.1", "general.bat", info, server="Amsterdam")
        self.assertEqual(networks.server_for("10.0.0.1"), "Amsterdam")
        self.assertEqual(networks.strategy_for("10.0.0.1"), "general.bat")
        raw = json.loads(networks.path().read_text(encoding="utf-8"))
        self.assertEqual(raw["networks"]["10.0.0.1"]["server"], "Amsterdam")
        self.assertEqual(raw["networks"]["10.0.0.1"]["bat"], "general.bat")

    def test_remember_without_server_keeps_saved(self) -> None:
        info = {"ssid": "Home", "gateway": "10.0.0.1"}
        networks.remember("10.0.0.1", "a.bat", info, server="Amsterdam")
        # Прежний вызов (подбор стратегии) сервер не стирает.
        networks.remember("10.0.0.1", "b.bat", info)
        self.assertEqual(networks.strategy_for("10.0.0.1"), "b.bat")
        self.assertEqual(networks.server_for("10.0.0.1"), "Amsterdam")
        # Пустая строка — явная очистка.
        networks.remember("10.0.0.1", "b.bat", info, server="")
        self.assertEqual(networks.server_for("10.0.0.1"), "")

    def test_server_found_by_gateway_of_old_record(self) -> None:
        # Старый ключ — MAC, шлюз лежит внутри записи.
        self._write({"aa:bb:cc:dd:ee:ff": {"bat": "general.bat",
                                           "server": "Paris",
                                           "gateway": "10.0.0.1"}})
        self.assertEqual(networks.server_for("10.0.0.1"), "Paris")
        self.assertEqual(networks.strategy_for("10.0.0.1"), "general.bat")


if __name__ == "__main__":
    unittest.main()
