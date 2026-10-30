"""Тесты переноса настроек: экспорт, импорт, отказы. Без сети и без Tk.

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import (  # noqa: E402
    config, history, networks, routes, status, transfer, xray,
)

#: UUID внутри ссылок — по нему проверяется, что в safe-файл ничего не утекло.
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

LINKS = [
    "vless://11111111-2222-3333-4444-555555555555@example.com:443#мой",
    "trojan://secretpass@example.org:443#второй",
]


def sample_cfg(**overrides) -> dict:
    """Конфиг с секретами и правилами: от него экспортируем."""
    cfg = dict(config.DEFAULTS)
    cfg.update({
        "xray_subscription": "https://sub.example/personal-token",
        "xray_links": list(LINKS),
        "xray_routes": {"x.com": "proxy", "steam.com": "direct"},
        "routing_mode": "all",
        "zapret_dir": "C:/Zapret",
    })
    cfg.update(overrides)
    return cfg


class TransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        config.set_config_dir(self.dir)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(config.set_config_dir, None)

    # ---- экспорт ---------------------------------------------------------
    def test_export_safe_has_no_secrets(self) -> None:
        path = self.dir / "safe.json"
        result = transfer.export_settings(sample_cfg(), path)

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["secrets_included"])
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertNotIn("xray_links", data["settings"])
        self.assertNotIn("xray_subscription", data["settings"])
        self.assertFalse(data["secrets_included"])
        self.assertNotIn("vless://", text)
        self.assertNotIn("trojan://", text)
        self.assertIsNone(UUID_RE.search(text), "в safe-файле остался UUID")

    def test_export_full_contains_links_and_flag(self) -> None:
        path = self.dir / "full.json"
        result = transfer.export_settings(
            sample_cfg(), path, include_secrets=True)

        self.assertTrue(result["ok"], result)
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertTrue(data["secrets_included"])
        self.assertTrue(result["secrets_included"])
        self.assertEqual(data["settings"]["xray_links"], LINKS)
        self.assertIn("vless://", text)
        self.assertIsNotNone(UUID_RE.search(text))

    # ---- импорт ----------------------------------------------------------
    def test_round_trip_keeps_keys_routes_and_networks(self) -> None:
        config.save_config(sample_cfg())
        networks.remember("gw-1", "general (ALT).bat",
                          {"ssid": "Дом", "gateway": "192.168.1.1"},
                          server="node-a")
        export_path = self.dir / "full.json"
        self.assertTrue(transfer.export_settings(
            sample_cfg(), export_path, include_secrets=True)["ok"])

        fresh = self.dir / "fresh"
        fresh.mkdir()
        config.set_config_dir(fresh)
        result = transfer.import_settings(export_path)

        self.assertTrue(result["ok"], result)
        self.assertIn("xray_links", result["applied"])
        loaded = config.load_config()
        self.assertEqual(loaded["xray_links"], LINKS)
        self.assertEqual(loaded["xray_subscription"],
                         "https://sub.example/personal-token")
        self.assertEqual(loaded["routing_mode"], "all")
        self.assertEqual(
            routes.normalise_routes(loaded),
            {"x.com": {"route": "proxy", "server": None},
             "steam.com": {"route": "direct", "server": None}})
        saved = networks.all()
        self.assertEqual(saved["gw-1"]["bat"], "general (ALT).bat")
        self.assertEqual(saved["gw-1"]["server"], "node-a")
        self.assertEqual(saved["gw-1"]["gateway"], "192.168.1.1")

    def test_import_ignores_unknown_keys(self) -> None:
        payload = {
            "version": transfer.FORMAT_VERSION, "at": 1,
            "app": config.APP_NAME, "secrets_included": False,
            "settings": {"routing_mode": "all", "чужой_ключ": 42},
            "xray_routes": {}, "networks": {},
        }
        path = self.dir / "mixed.json"
        path.write_text(json.dumps(payload, ensure_ascii=False),
                        encoding="utf-8")
        result = transfer.import_settings(path)

        self.assertTrue(result["ok"], result)
        self.assertIn("чужой_ключ", result["skipped"])
        self.assertIn("routing_mode", result["applied"])
        self.assertNotIn("чужой_ключ", config.load_config())

    def test_import_makes_backup_of_current_config(self) -> None:
        config.save_config(sample_cfg(routing_mode="blocked"))
        path = self.dir / "up.json"
        self.assertTrue(transfer.export_settings(
            sample_cfg(routing_mode="all"), path)["ok"])

        result = transfer.import_settings(path)

        self.assertTrue(result["ok"], result)
        backup = Path(result["backup"])
        self.assertTrue(backup.is_file(), result["backup"])
        self.assertIn(".bak-", backup.name)
        self.assertEqual(backup.parent, config.CONFIG_FILE.parent)
        self.assertIn('"routing_mode": "blocked"',
                      backup.read_text(encoding="utf-8"))

    def test_broken_json_and_foreign_file_change_nothing(self) -> None:
        config.save_config(sample_cfg())
        before = config.CONFIG_FILE.read_bytes()

        broken = self.dir / "broken.json"
        broken.write_text("{не json", encoding="utf-8")
        result = transfer.import_settings(broken)
        self.assertFalse(result["ok"])
        self.assertIn("JSON", result["error"])
        self.assertEqual(config.CONFIG_FILE.read_bytes(), before)

        foreign = self.dir / "foreign.json"
        foreign.write_text(json.dumps({"settings": {"xray_port": 9}}),
                           encoding="utf-8")
        result = transfer.import_settings(foreign)
        self.assertFalse(result["ok"])
        self.assertIn("чужой", result["error"])
        self.assertEqual(config.CONFIG_FILE.read_bytes(), before)

        future = self.dir / "future.json"
        future.write_text(json.dumps({
            "app": config.APP_NAME, "version": 99, "settings": {},
        }), encoding="utf-8")
        result = transfer.import_settings(future)
        self.assertFalse(result["ok"])
        self.assertEqual(config.CONFIG_FILE.read_bytes(), before)

    def test_safe_file_into_clean_profile_is_valid(self) -> None:
        export_path = self.dir / "safe.json"
        self.assertTrue(transfer.export_settings(
            sample_cfg(), export_path)["ok"])

        fresh = self.dir / "clean"
        fresh.mkdir()
        config.set_config_dir(fresh)
        result = transfer.import_settings(export_path)

        self.assertTrue(result["ok"], result)
        loaded = config.load_config()
        # Без секретов конфиг валиден: все ключи умолчаний на месте.
        self.assertEqual(set(config.DEFAULTS) - set(loaded), set())
        self.assertEqual(loaded["xray_links"], [])
        self.assertEqual(loaded["routing_mode"], "all")

    # ---- серверы в правилах и сетях -------------------------------------
    def _rule_and_network(self) -> dict:
        """Конфиг с правилом на своём сервере и сетью, помнящей сервер."""
        networks.remember("gw-1", "general (ALT).bat",
                          {"ssid": "Дом", "gateway": "192.168.1.1"},
                          server="node-a")
        return sample_cfg(xray_routes={
            "x.com": {"route": "proxy", "server": "node-a"},
            "steam.com": "direct",
        })

    def test_safe_export_drops_server_from_rule_and_network(self) -> None:
        path = self.dir / "safe.json"
        self.assertTrue(transfer.export_settings(
            self._rule_and_network(), path)["ok"])

        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        # Правило осталось, имя сервера — нет.
        self.assertEqual(data["xray_routes"],
                         {"x.com": "proxy", "steam.com": "direct"})
        self.assertNotIn("node-a", text)
        # Сеть: стратегия и ключ, без сервера, SSID и шлюза.
        entry = data["networks"]["gw-1"]
        self.assertEqual(set(entry), {"bat", "at"})
        self.assertEqual(entry["bat"], "general (ALT).bat")
        self.assertNotIn("Дом", text)
        self.assertNotIn("192.168.1.1", text)
        self.assertEqual(data["networks"]["gw-1"]["bat"],
                         networks.all()["gw-1"]["bat"])

    def test_full_export_keeps_server_in_rule_and_network(self) -> None:
        path = self.dir / "full.json"
        self.assertTrue(transfer.export_settings(
            self._rule_and_network(), path, include_secrets=True)["ok"])

        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["xray_routes"]["x.com"],
                         {"route": "proxy", "server": "node-a"})
        self.assertEqual(data["networks"]["gw-1"]["server"], "node-a")
        self.assertEqual(data["networks"]["gw-1"]["ssid"], "Дом")
        self.assertEqual(data["networks"]["gw-1"]["gateway"], "192.168.1.1")

    def test_round_trip_full_restores_server_choices(self) -> None:
        cfg = self._rule_and_network()
        config.save_config(cfg)
        export_path = self.dir / "full.json"
        self.assertTrue(transfer.export_settings(
            cfg, export_path, include_secrets=True)["ok"])

        fresh = self.dir / "fresh"
        fresh.mkdir()
        config.set_config_dir(fresh)
        result = transfer.import_settings(export_path)

        self.assertTrue(result["ok"], result)
        loaded = config.load_config()
        self.assertEqual(routes.normalise_routes(loaded)["x.com"],
                         {"route": "proxy", "server": "node-a"})
        self.assertEqual(routes.user_routes(loaded),
                         (["steam.com"], ["x.com"]))
        saved = networks.all()
        self.assertEqual(saved["gw-1"]["server"], "node-a")
        self.assertEqual(saved["gw-1"]["gateway"], "192.168.1.1")

    def test_round_trip_safe_keeps_routes_and_strategy(self) -> None:
        cfg = self._rule_and_network()
        config.save_config(cfg)
        export_path = self.dir / "safe.json"
        self.assertTrue(transfer.export_settings(cfg, export_path)["ok"])

        fresh = self.dir / "fresh-safe"
        fresh.mkdir()
        config.set_config_dir(fresh)
        result = transfer.import_settings(export_path)

        self.assertTrue(result["ok"], result)
        loaded = config.load_config()
        self.assertEqual(routes.normalise_routes(loaded)["x.com"],
                         {"route": "proxy", "server": None})
        self.assertEqual(networks.strategy_for("gw-1"),
                         "general (ALT).bat")
        self.assertEqual(networks.all()["gw-1"]["server"], "")

    def test_safe_import_does_not_clear_saved_server(self) -> None:
        # В профиле сервер уже выбран, а safe-файл его не содержит: импорт
        # обязан оставить выбор, а не стереть его пустой строкой.
        config.save_config(sample_cfg())
        self._rule_and_network()
        export_path = self.dir / "safe.json"
        self.assertTrue(transfer.export_settings(
            sample_cfg(), export_path)["ok"])

        result = transfer.import_settings(export_path)

        self.assertTrue(result["ok"], result)
        self.assertEqual(networks.all()["gw-1"]["server"], "node-a")


#: Сервер для ``xray.build_config``: нужен только ``outbound``.
SERVER = {
    "name": "test", "host": "example.com", "port": 443,
    "outbound": {"tag": "proxy", "protocol": "vless",
                 "settings": {"vnext": []}},
}


class LegacyFilesTests(unittest.TestCase):
    """Старые файлы профиля читаются и не роняют соседние модули."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        config.set_config_dir(self.dir)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(config.set_config_dir, None)
        now = time.time()
        # config.json прошлой версии: без routing_mode, xray_routes и xray_links.
        (self.dir / "config.json").write_text(json.dumps({
            "zapret_dir": "F:/Zapret",
            "zapret_bat": "general (ALT).bat",
            "tgws_exe": "F:/TgWsProxy.exe",
            "xray_port": 10818,
            "xray_direct_domains": ["*.steampowered.com"],
            "xray_extra_domains": ["example.org"],
        }, ensure_ascii=False), encoding="utf-8")
        # networks.json без поля server.
        (self.dir / "networks.json").write_text(json.dumps({
            "networks": {"192.168.1.1": {"bat": "general.bat", "at": now}},
        }, ensure_ascii=False), encoding="utf-8")
        # route_overrides.json плоского формата.
        (self.dir / "route_overrides.json").write_text(json.dumps({
            "x.com": {"route": "direct", "checked": now},
        }, ensure_ascii=False), encoding="utf-8")
        # activity.json и history без полей новых версий.
        (self.dir / "activity.json").write_text(json.dumps([
            {"at": now, "text": "старая запись", "kind": "auto"},
        ], ensure_ascii=False), encoding="utf-8")
        (self.dir / "health_history.json").write_text(json.dumps([
            {"at": now, "level": "ok", "verdict": "ok", "items": {"vpn": "ok"}},
        ], ensure_ascii=False), encoding="utf-8")

    def test_load_config_migrates_and_keeps_working(self) -> None:
        cfg = config.load_config()
        self.assertEqual(cfg["routing_mode"], "blocked")
        self.assertEqual(cfg["xray_links"], [])
        # Старые списки доменов стали правилами.
        direct, proxy = routes.user_routes(cfg)
        self.assertIn("steampowered.com", direct)
        self.assertIn("example.org", proxy)

    def test_legacy_network_entry_reads_without_server(self) -> None:
        self.assertEqual(networks.strategy_for("192.168.1.1"), "general.bat")
        self.assertEqual(networks.server_for("192.168.1.1"), "")

    def test_legacy_overrides_flat_format(self) -> None:
        saved = routes.load_overrides()
        self.assertEqual(saved["x.com"]["route"], "direct")

    def test_status_collect_and_build_config_survive(self) -> None:
        cfg = config.load_config()
        with mock.patch.object(status, "_processes", lambda: []):
            collected = status.collect(cfg)
        self.assertIn("zapret", collected)
        self.assertEqual(collected["routing_mode"], "blocked")
        built = xray.build_config(
            SERVER, int(cfg["xray_port"]),
            user_routes=routes.normalise_routes(cfg),
            routing_mode=cfg["routing_mode"])
        self.assertTrue(built["routing"]["rules"])

    def test_history_summary_reads_old_rows(self) -> None:
        now = time.time()
        data = history.summary(now=now)
        self.assertEqual(data["reports"], 1)
        self.assertEqual(data["uptime_pct"], 100)


if __name__ == "__main__":
    unittest.main()
