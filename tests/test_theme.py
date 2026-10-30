"""Тесты настройки темы: нормализация, выбор режима, конфиг и форма.

Окно не создаётся: проверяются чистые функции и чтение/запись config.json.

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

from bypass_tray import config, settings_ui, theme  # noqa: E402


class ThemeModeNormaliseTests(unittest.TestCase):
    """Мусор в поле ``theme_mode`` не должен ломать запуск."""

    def test_default_is_auto(self) -> None:
        self.assertEqual(config.DEFAULTS["theme_mode"], "auto")

    def test_unknown_values_fall_back_to_auto(self) -> None:
        for value in (None, "", "weird", "auto2", 42, [], {}):
            with self.subTest(value=value):
                self.assertEqual(config.normalise_theme_mode(value), "auto")

    def test_known_values_are_lowercased_and_stripped(self) -> None:
        cases = {"LIGHT": "light", " dark ": "dark", "Auto": "auto",
                 "  auto  ": "auto", "Light": "light", "DARK": "dark"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(config.normalise_theme_mode(value), expected)

    def test_cfg_reader_understands_legacy_key(self) -> None:
        self.assertEqual(config.theme_mode_of({"theme_mode": "dark"}), "dark")
        self.assertEqual(config.theme_mode_of({"theme": "light"}), "light")
        self.assertEqual(config.theme_mode_of({"theme": "neon"}), "auto")
        self.assertEqual(config.theme_mode_of({}), "auto")
        self.assertEqual(config.theme_mode_of(None), "auto")


class ResolveDarkTests(unittest.TestCase):
    """``auto`` спрашивает систему, ``light``/``dark`` — принудительно."""

    def test_forced_modes_ignore_system(self) -> None:
        with mock.patch.object(theme, "system_is_dark", return_value=True):
            self.assertFalse(theme.resolve_dark("light"))
        with mock.patch.object(theme, "system_is_dark", return_value=False):
            self.assertTrue(theme.resolve_dark("dark"))

    def test_auto_uses_system_setting(self) -> None:
        with mock.patch.object(theme, "system_is_dark", return_value=True):
            self.assertTrue(theme.resolve_dark("auto"))
        with mock.patch.object(theme, "system_is_dark", return_value=False):
            self.assertFalse(theme.resolve_dark("auto"))

    def test_auto_is_default_and_garbage(self) -> None:
        with mock.patch.object(theme, "system_is_dark", return_value=True):
            self.assertTrue(theme.resolve_dark())
            self.assertTrue(theme.resolve_dark(None))
            self.assertTrue(theme.resolve_dark(42))


class ThemeConfigTests(unittest.TestCase):
    """Чтение и запись ``theme_mode`` через настоящий config.json."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        config.set_config_dir(self.dir)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(config.set_config_dir, None)

    def write_raw(self, data: dict) -> None:
        """Пишет config.json как есть — мимо save_config и нормализации."""
        (self.dir / "config.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def base(self, **extra) -> dict:
        data = {"zapret_dir": "C:/Zapret", "tgws_exe": "C:/tgws.exe"}
        data.update(extra)
        return data

    def test_round_trip_dark(self) -> None:
        self.write_raw(self.base(theme_mode="dark"))
        loaded = config.load_config()
        self.assertEqual(loaded["theme_mode"], "dark")
        self.assertEqual(json.loads((self.dir / "config.json").read_text(
            encoding="utf-8"))["theme_mode"], "dark")

    def test_uppercase_is_normalised_and_saved(self) -> None:
        self.write_raw(self.base(theme_mode="LIGHT"))
        loaded = config.load_config()
        self.assertEqual(loaded["theme_mode"], "light")
        on_disk = json.loads((self.dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["theme_mode"], "light")

    def test_garbage_becomes_auto(self) -> None:
        self.write_raw(self.base(theme_mode=123))
        self.assertEqual(config.load_config()["theme_mode"], "auto")

    def test_missing_key_becomes_auto(self) -> None:
        self.write_raw(self.base())
        self.assertEqual(config.load_config()["theme_mode"], "auto")

    def test_legacy_theme_key_is_migrated(self) -> None:
        self.write_raw(self.base(theme="dark"))
        loaded = config.load_config()
        self.assertEqual(loaded["theme_mode"], "dark")
        on_disk = json.loads((self.dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["theme_mode"], "dark")
        self.assertNotIn("theme", on_disk)

    def test_legacy_garbage_theme_becomes_auto(self) -> None:
        self.write_raw(self.base(theme="neon"))
        self.assertEqual(config.load_config()["theme_mode"], "auto")


class SettingsControlTests(unittest.TestCase):
    """Значение сегмента «Тема» → ключ конфига (путь ``_save``)."""

    def test_known_values(self) -> None:
        for value in ("auto", "light", "dark"):
            with self.subTest(value=value):
                self.assertEqual(settings_ui._theme_mode_from_control(value), value)

    def test_garbage_becomes_auto(self) -> None:
        for value in (None, "", "weird", 42, [], {}):
            with self.subTest(value=value):
                self.assertEqual(
                    settings_ui._theme_mode_from_control(value), "auto")

    def test_case_and_spaces(self) -> None:
        self.assertEqual(settings_ui._theme_mode_from_control(" Dark "), "dark")


if __name__ == "__main__":
    unittest.main()
