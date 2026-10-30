"""Проверки сервисов в интерфейсе: форма, порядок, сохранение и CLI ``--check``.

Сеть не используется: HTTP и сокеты подменяются, конфиг пишется во временный
каталог. Тесты панели и страницы настроек — с настоящим Tk и пропускаются,
если Tk не поднимается.

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from bypass_tray import config, health, theme, ui  # noqa: E402
from bypass_tray.settings_ui import (  # noqa: E402
    SettingsPage, validate_health_item,
)

BUILTIN_ORDER = ["zapret", "youtube", "discord", "deepseek",
                 "telegram", "tgwsproxy", "vpn"]


def _tk_available() -> bool:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


class _TempData(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        config.set_config_dir(Path(self._tmp.name))
        self.addCleanup(config.set_config_dir, None)


class FormValidationTests(_TempData):
    """Валидация формы — чистая функция: мусор не становится записью."""

    def setUp(self) -> None:
        super().setUp()
        self.existing = config._default_health_items()

    def check(self, **values) -> tuple[dict | None, str]:
        base = {"title": "Свой сайт", "kind": "https",
                "target": "https://example.test/api", "via": "auto",
                "level": "minor", "timeout": "5", "status": "",
                "contains": ""}
        base.update(values)
        return validate_health_item(base, self.existing)

    def test_empty_title_is_refused(self) -> None:
        record, error = self.check(title="   ")
        self.assertIsNone(record)
        self.assertIn("название", error.lower())

    def test_broken_https_target_is_refused(self) -> None:
        for target in ("example.test/api", "ftp://example.test", "https://"):
            with self.subTest(target=target):
                record, error = self.check(target=target)
                self.assertIsNone(record)
                self.assertIn("http", error)

    def test_tcp_target_needs_port(self) -> None:
        for target in ("example.test", "example.test:", "example.test:0",
                       "example.test:70000", "example.test:abc"):
            with self.subTest(target=target):
                record, error = self.check(kind="tcp", target=target)
                self.assertIsNone(record)
                self.assertIn("TCP-адрес", error)

    def test_timeout_and_status_are_validated(self) -> None:
        self.assertIsNone(self.check(timeout="0")[0])
        self.assertIsNone(self.check(timeout="99")[0])
        self.assertIsNone(self.check(timeout="скоро")[0])
        self.assertIsNone(self.check(status="много")[0])
        self.assertIsNone(self.check(status="700")[0])

    def test_valid_https_becomes_record(self) -> None:
        record, error = self.check(status="2xx", contains="готово", timeout="7")
        self.assertEqual(error, "")
        self.assertIsNotNone(record)
        self.assertEqual(record["kind"], "https")
        self.assertEqual(record["expect"], {"status": "2xx",
                                            "contains": "готово"})
        self.assertEqual(record["timeout"], 7)
        self.assertTrue(record["id"].startswith("svc_"))

    def test_valid_tcp_ignores_expectation(self) -> None:
        record, error = self.check(kind="tcp", target="example.test:443",
                                   status="200")
        self.assertEqual(error, "")
        self.assertEqual(record["expect"], {})

    def test_custom_limit_is_enforced(self) -> None:
        existing = list(self.existing) + [
            {"id": f"svc_{index}", "title": f"c{index}", "kind": "tcp",
             "target": f"h.test:{1000 + index}"}
            for index in range(config.HEALTH_MAX_CUSTOM)
        ]
        base = {"title": "лишняя", "kind": "tcp", "target": "h.test:9",
                "via": "auto", "level": "minor", "timeout": "5",
                "status": "", "contains": ""}
        record, error = validate_health_item(base, existing)
        self.assertIsNone(record)
        self.assertIn("не больше", error)


class SaveOrderTests(_TempData):
    """Порядок и тумблеры из формы попадают в config.json."""

    def setUp(self) -> None:
        if not _tk_available():
            self.skipTest("Tk недоступен в этой среде")
        super().setUp()
        import tkinter as tk

        from tests.smoke_test import FakeController

        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        self.fake = FakeController()
        self.fake.cfg["health_items"] = config._default_health_items()
        self.page = SettingsPage(self.root, theme.Theme(False), self.fake)

    def saved_ids(self) -> list[str]:
        data = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
        return [record["id"] for record in data["health_items"]]

    def test_move_up_changes_order_and_is_saved(self) -> None:
        self.page._move_health(1, -1)  # youtube выше zapret
        self.assertEqual([r["id"] for r in self.page.health_items][:2],
                         ["youtube", "zapret"])
        self.assertTrue(self.page._save())
        self.assertEqual(self.saved_ids()[:2], ["youtube", "zapret"])

    def test_toggles_are_saved(self) -> None:
        index = BUILTIN_ORDER.index("telegram")
        self.page._set_health_flag(index, "show", False)
        self.page._set_health_flag(index, "check", False)
        self.assertTrue(self.page._save())
        data = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
        saved = {record["id"]: record for record in data["health_items"]}
        self.assertFalse(saved["telegram"]["show"])
        self.assertFalse(saved["telegram"]["check"])

    def test_builtin_cannot_be_deleted(self) -> None:
        before = [record["id"] for record in self.page.health_items]
        self.page._delete_health(0)
        self.assertEqual([record["id"] for record in self.page.health_items],
                         before)

    def test_custom_can_be_deleted(self) -> None:
        self.page.health_items.append({
            "id": "svc_mine", "title": "Своя", "kind": "tcp",
            "probe": "", "target": "example.test:443", "expect": {},
            "via": "auto", "level": "minor", "check": True, "show": True,
            "timeout": 5,
        })
        self.page._render_health_items()
        index = [record["id"] for record in self.page.health_items].index(
            "svc_mine")
        self.page._delete_health(index)
        self.assertNotIn("svc_mine",
                         [record["id"] for record in self.page.health_items])

    def test_snapshot_keeps_unsaved_flags(self) -> None:
        self.page._set_health_flag(0, "show", False)
        state = self.page.snapshot()
        self.assertFalse(state["health"][0]["show"])


@unittest.skipUnless(_tk_available(), "Tk недоступен в этой среде")
class PanelVisibilityTests(_TempData):
    """Панель: скрытая строка отсутствует и не считается в плитке."""

    def setUp(self) -> None:
        super().setUp()
        import tkinter as tk

        from tests.smoke_test import FakeController

        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        self.fake = FakeController()
        self.fake.cfg["health_items"] = config._default_health_items()
        self.panel = ui.Panel(self.fake)
        self.fake.panel = self.panel
        self.panel._root = self.root
        self.panel._build(self.root, theme.Theme(False))
        self.pump()

    def pump(self) -> None:
        for _ in range(6):
            self.root.update()
            time.sleep(0.01)

    def records(self, item_id: str, **flags) -> None:
        records = config._default_health_items()
        for record in records:
            if record["id"] == item_id:
                record.update(flags)
        self.fake.cfg["health_items"] = records
        self.panel._render()
        self.pump()

    def panel_order(self) -> list[str]:
        return [str(record.get("id")) for record in self.panel._services_order]

    def test_hidden_row_is_absent(self) -> None:
        self.records("telegram", show=False)
        self.assertNotIn("telegram", self.panel._services)
        self.assertIn("zapret", self.panel._services)
        self.assertEqual(len(self.panel._services), 6)

    def test_hidden_row_is_not_counted_in_tile(self) -> None:
        # Скрытая строка не проверяется: плитка считает ровно видимые.
        self.records("telegram", show=False, check=True)
        tile = self.panel._tiles["services"]["value"].cget("text")
        self.assertEqual(tile, "6 из 6")

    def test_visible_row_with_check_false_is_shown_and_counted(self) -> None:
        # show — старший флаг: нормализация подтягивает check за ним.
        self.records("telegram", show=True, check=False)
        self.assertIn("telegram", self.panel._services)
        tile = self.panel._tiles["services"]["value"].cget("text")
        self.assertEqual(tile, "7 из 7")

    def test_panel_rebuilds_in_config_order(self) -> None:
        records = config._default_health_items()
        records.insert(0, records.pop(1))  # youtube — первой
        self.fake.cfg["health_items"] = records
        self.panel._render()
        self.pump()
        self.assertEqual(self.panel_order(),
                         [record["id"] for record in records])
        # Порядок виджетов в сетке идёт за списком: первая ячейка — youtube.
        first = self.panel._services_order[0]["id"]
        self.assertEqual(self.panel._services[first]["cell"].grid_info()["row"], 0)
        self.assertEqual(self.panel._services[records[1]["id"]]["cell"]
                         .grid_info()["row"], 1)

    def test_tick_with_settings_open_keeps_services_before_journal(self) -> None:
        # Пересборка при скрытой главной не должна уводить карточку в конец.
        records = config._default_health_items()
        records.insert(0, records.pop(2))
        self.panel._open_settings()
        self.pump()
        self.fake.cfg["health_items"] = records
        self.panel._render()  # тик, пока открыты настройки
        self.pump()
        self.assertEqual(self.panel_order(),
                         [record["id"] for record in records])
        slaves = list(self.panel._body.inner.pack_slaves())
        self.assertIn(self.panel._services_card, slaves)
        self.assertIn(self.panel._activity_card, slaves)
        self.assertLess(slaves.index(self.panel._services_card),
                        slaves.index(self.panel._activity_card))
        self.panel._close_settings()

    def test_all_hidden_removes_card(self) -> None:
        records = config._default_health_items()
        for record in records:
            record["show"] = False
        self.fake.cfg["health_items"] = records
        self.panel._render()
        self.pump()
        self.assertIsNone(self.panel._services_card)
        self.assertIsNone(self.panel._services_head)
        self.assertEqual(self.panel._services, {})


class CheckTargetTests(_TempData):
    """``--check``: тот же движок, код 0/1, отчёт не пишется на диск."""

    def _response(self, status: int = 200, body: str = "") -> dict:
        return {"status": status, "body": body, "ms": 4, "error": ""}

    def test_https_ok(self) -> None:
        with mock.patch.object(health, "_proxy_status",
                               return_value={"running": False}), \
                mock.patch.object(health, "_fetch_direct",
                                  return_value=self._response(200)):
            result = health.check_target("https://example.test/api",
                                         {"xray_port": 10818})
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "ok")
        self.assertIn("example.test/api", result["detail"])

    def test_https_failure(self) -> None:
        with mock.patch.object(health, "_proxy_status",
                               return_value={"running": False}), \
                mock.patch.object(health, "_fetch_direct",
                                  return_value=self._response(503)):
            result = health.check_target("https://example.test/")
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "warn")

    def test_tcp_ok_and_closed(self) -> None:
        with mock.patch.object(health, "_proxy_status",
                               return_value={"running": False}), \
                mock.patch("socket.create_connection",
                           return_value=mock.MagicMock()):
            opened = health.check_target("example.test:443")
        with mock.patch.object(health, "_proxy_status",
                               return_value={"running": False}), \
                mock.patch("socket.create_connection",
                           side_effect=ConnectionRefusedError("closed")):
            closed = health.check_target("example.test:443")
        self.assertTrue(opened["ok"])
        self.assertFalse(closed["ok"])

    def test_garbage_is_refused(self) -> None:
        result = health.check_target("просто текст")
        self.assertFalse(result["ok"])
        self.assertIn("не распознан", result["detail"])

    def test_no_files_are_written(self) -> None:
        with mock.patch.object(health, "_proxy_status",
                               return_value={"running": False}), \
                mock.patch.object(health, "_fetch_direct",
                                  return_value=self._response(200)):
            health.check_target("https://example.test/")
        self.assertFalse((config.config_dir() / "health.json").exists())

    def _run_cli(self, argv: list[str], ok: bool) -> int:
        from bypass_tray import __main__ as cli

        with mock.patch.object(sys, "argv", ["bypass-tray", *argv]), \
                mock.patch.object(health, "check_target",
                                  return_value={"ok": ok, "state": "ok"
                                                if ok else "bad"}), \
                redirect_stdout(io.StringIO()):
            return cli.main()

    def test_cli_exit_codes(self) -> None:
        self.assertEqual(self._run_cli(["--check", "https://a.test"], True), 0)
        self.assertEqual(self._run_cli(["--check", "https://a.test"], False), 1)


if __name__ == "__main__":
    unittest.main()
