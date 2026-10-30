"""Тесты переноса настроек: экспорт, импорт, отказы. Без сети и без Tk.

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import config, networks, routes, transfer  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
