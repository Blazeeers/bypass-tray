"""Перестройка панели при смене темы: страница настроек и ввод.

Тест с настоящим Tk: проверяется живой сценарий «открыты настройки →
введено правило → тема сменилась дважды подряд». Пропускается, если Tk
не поднимается (среда без рабочего стола).

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from bypass_tray import config, theme, ui  # noqa: E402
from bypass_tray.settings_ui import SettingsPage  # noqa: E402


def _tk_available() -> bool:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(_tk_available(), "Tk недоступен в этой среде")
class ThemeRebuildTests(unittest.TestCase):
    """После перестройки панели живёт только новая страница настроек."""

    def setUp(self) -> None:
        import tkinter as tk

        from tests.smoke_test import FakeController

        self.tmp = tempfile.TemporaryDirectory()
        config.set_config_dir(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(config.set_config_dir, None)
        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        self.fake = FakeController()
        self.panel = ui.Panel(self.fake)
        self.fake.panel = self.panel
        self.panel._root = self.root
        self.panel._build(self.root, theme.Theme(False))
        self.pump()
        self.panel._open_settings()
        self.pump()

    def pump(self, times: int = 8) -> None:
        for _ in range(times):
            self.root.update()
            time.sleep(0.01)

    def alive(self, widget) -> bool:
        try:
            return bool(widget.winfo_exists())
        except Exception:  # noqa: BLE001
            return False

    def fill_form(self, page: SettingsPage) -> None:
        page.route_var.set("example.com")
        page.route_segment.set("proxy")
        page._add_rule()
        page.rule_servers["example.com"] = "Сервер А"
        page.vars["xray_subscription"].set("https://sub.example/personal")
        page.vars["xray_port"].set("10999")
        page.routing_segment.set("all")
        page.toggles["youtube"].set(True)
        page.blocked_toggle.set(False)

    def assert_form(self, page: SettingsPage, mode: str) -> None:
        self.assertEqual(page.rules.get("example.com"), "proxy")
        self.assertEqual(page.rule_servers.get("example.com"), "Сервер А")
        self.assertEqual(page.vars["xray_subscription"].get(),
                         "https://sub.example/personal")
        self.assertEqual(page.vars["xray_port"].get(), "10999")
        self.assertEqual(page.routing_segment.get(), "all")
        self.assertTrue(page.toggles["youtube"].get())
        self.assertFalse(page.blocked_toggle.get())
        self.assertEqual(page.theme_segment.get(), mode)
        self.assertTrue(page._routes_dirty())

    def test_two_switches_keep_settings_and_links(self) -> None:
        page = self.panel._settings_page
        self.fill_form(page)
        self.pump()

        for mode in ("dark", "light"):
            old = self.panel._settings_page
            old.theme_segment.set(mode, notify=True)
            self.pump()
            new = self.panel._settings_page
            with self.subTest(mode=mode):
                self.assertIsNot(old, new, "страница не пересоздана")
                self.assertFalse(self.alive(old), "старая страница жива")
                self.assertTrue(self.alive(new), "новая страница мертва")
                self.assertIs(new, self.panel._settings_page)
                self.assertTrue(new.theme_segment.winfo_exists())
                self.assertEqual(self.panel._theme.dark, mode == "dark")
                self.assertTrue(self.panel._settings_visible)
                self.assert_form(new, mode)
                # Мёртвая страница не бросает TclError ни на снимок, ни на
                # восстановление: панель уже не держит на неё ссылок.
                self.assertEqual(old.snapshot(), {})
                old.restore({"vars": {}, "rules": {}})

            # Панель не держит ссылок на старую страницу.
            pages = [value for value in vars(self.panel).values()
                     if isinstance(value, SettingsPage)]
            self.assertEqual(pages, [new], "есть ссылки на другие страницы")


if __name__ == "__main__":
    unittest.main()

