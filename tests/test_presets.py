"""Тесты сценариев настроек: чистые функции, без Tk и без файлов.

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import config, presets  # noqa: E402

#: Что каждый сценарий обязан дать на выходе. Записано здесь руками, а не
#: взято из PRESETS: иначе тест подтверждал бы сам себя.
EXPECTED = {
    "zapret": {"routing_mode": "blocked", "youtube_via_vpn": False,
               "blocked_list_enabled": True},
    "vpn": {"routing_mode": "all", "youtube_via_vpn": True,
            "blocked_list_enabled": False},
    "telegram": {"routing_mode": "blocked", "youtube_via_vpn": False,
                 "blocked_list_enabled": False},
    "max": {"routing_mode": "all", "youtube_via_vpn": True,
            "blocked_list_enabled": True},
}

SCENARIO_KEYS = {"routing_mode", "youtube_via_vpn", "blocked_list_enabled"}

#: Конфиг с «чужими» ключами: сценарий не должен их терять или менять.
USER_CFG = {
    "routing_mode": "blocked",
    "youtube_via_vpn": False,
    "blocked_list_enabled": True,
    "xray_routes": {"x.com": "proxy", "steam.com": "direct"},
    "xray_links": ["vless://user@host:443#server"],
    "networks": {"8.8.8.8": "server-1"},
    "xray_port": 10818,
}


class PresetShapeTests(unittest.TestCase):
    """Список сценариев: id, ключи, значения."""

    def test_ids_are_unique_and_cover_expected(self) -> None:
        ids = [preset["id"] for preset in presets.PRESETS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), set(EXPECTED))

    def test_values_use_only_real_default_keys(self) -> None:
        known = set(config.DEFAULTS)
        for preset in presets.PRESETS:
            with self.subTest(preset=preset["id"]):
                self.assertEqual(set(preset["values"]) - known, set())
                self.assertTrue(preset["title"])
                self.assertTrue(preset["hint"])

    def test_every_preset_has_full_triple(self) -> None:
        # Полный набор — залог того, что active() не путает сценарии.
        for preset in presets.PRESETS:
            with self.subTest(preset=preset["id"]):
                self.assertEqual(set(preset["values"]), SCENARIO_KEYS)


class ApplyTests(unittest.TestCase):
    """``apply`` — чистая функция: новый словарь, чужие ключи как есть."""

    def test_each_preset_applies_expected_values(self) -> None:
        for preset_id, expected in EXPECTED.items():
            with self.subTest(preset=preset_id):
                cfg = presets.apply(dict(config.DEFAULTS), preset_id)
                for key, value in expected.items():
                    self.assertEqual(cfg[key], value)

    def test_apply_returns_new_dict_without_mutating_input(self) -> None:
        source = {key: value for key, value in USER_CFG.items()}
        result = presets.apply(source, "vpn")
        self.assertIsNot(result, source)
        self.assertEqual(source, USER_CFG)

    def test_apply_keeps_routes_links_and_networks(self) -> None:
        result = presets.apply(dict(USER_CFG), "telegram")
        self.assertEqual(result["xray_routes"], USER_CFG["xray_routes"])
        self.assertEqual(result["xray_links"], USER_CFG["xray_links"])
        self.assertEqual(result["networks"], USER_CFG["networks"])
        self.assertEqual(result["xray_port"], USER_CFG["xray_port"])

    def test_unknown_id_returns_same_object(self) -> None:
        source = dict(USER_CFG)
        for preset_id in ("", None, "no-such", "нет"):
            with self.subTest(preset_id=preset_id):
                self.assertIs(presets.apply(source, preset_id), source)
        self.assertEqual(source, USER_CFG)

    def test_id_is_stripped(self) -> None:
        result = presets.apply(dict(USER_CFG), "  vpn ")
        self.assertEqual(result["routing_mode"], "all")

    def test_apply_does_not_write_files(self) -> None:
        # Запись остаётся за кнопкой «Сохранить»: сценарий её не вызывает.
        with mock.patch.object(config, "save_config",
                               side_effect=AssertionError("нельзя писать")):
            presets.apply(dict(USER_CFG), "max")


class ActiveTests(unittest.TestCase):
    """``active``: id по значениям, ``None`` для промежуточного состояния."""

    def test_active_after_apply(self) -> None:
        for preset_id in EXPECTED:
            with self.subTest(preset=preset_id):
                cfg = presets.apply(dict(config.DEFAULTS), preset_id)
                self.assertEqual(presets.active(cfg), preset_id)

    def test_defaults_are_the_zapret_preset(self) -> None:
        self.assertEqual(presets.active(dict(config.DEFAULTS)), "zapret")
        self.assertEqual(presets.active({}), "zapret")

    def test_intermediate_state_has_no_preset(self) -> None:
        for cfg in (
            {"routing_mode": "blocked", "youtube_via_vpn": True,
             "blocked_list_enabled": True},
            {"routing_mode": "blocked", "youtube_via_vpn": True,
             "blocked_list_enabled": False},
        ):
            with self.subTest(cfg=cfg):
                self.assertIsNone(presets.active(cfg))

    def test_active_tolerates_bad_values(self) -> None:
        for cfg in (None, [], "blocked", 42):
            with self.subTest(cfg=cfg):
                self.assertIsNone(presets.active(cfg))
        # Строка в правленом вручную config.json не должна читаться как
        # «включено» только потому, что она непустая: "1" — включено.
        self.assertEqual(presets.active(
            {"routing_mode": "  ?", "youtube_via_vpn": "1",
             "blocked_list_enabled": False}), None)

    def test_active_normalises_mode(self) -> None:
        cfg = {"routing_mode": "  ALL ", "youtube_via_vpn": True,
               "blocked_list_enabled": False}
        self.assertEqual(presets.active(cfg), "vpn")

    def test_title_for_known_and_unknown(self) -> None:
        self.assertEqual(presets.title("vpn"), "Всё через VPN")
        self.assertEqual(presets.title("no-such"), "")


if __name__ == "__main__":
    unittest.main()
