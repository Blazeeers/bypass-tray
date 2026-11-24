"""Пропущенный перебор: нет файла результата и наблюдатель не завершается.

Чужой перебор (живой маркер другого процесса) — это не результат: процесс
не пишет ``sweep-result.json``, а трей продолжает ждать настоящий отчёт.
Сеть, winws и UAC не трогаются: всё подменяется заглушками.

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

from bypass_tray import app, config, strategy  # noqa: E402
from bypass_tray import __main__ as entry  # noqa: E402


def skipped_report() -> dict:
    return {
        "started": 1.0, "finished": 2.0, "results": [], "baseline": None,
        "best": "", "installed": False, "restored": False,
        "error": "перебор уже идёт", "skipped": True,
    }


def good_report() -> dict:
    return {
        "started": 1.0, "finished": 2.0,
        "results": [{"name": "general.bat", "working": True, "score": 5}],
        "best": "general.bat", "installed": True, "restored": True,
    }


class TempData(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        self.zapret = self.data / "zapret"
        self.zapret.mkdir()
        self.out = self.data / "sweep-result.json"
        self.report_path = self.data / "sweep-progress.json"


class MainSweepSkippedTests(TempData):
    def run_main(self, report: dict) -> int:
        argv = ["bypass-tray", "--sweep-strategies",
                "--out", str(self.out), "--report", str(self.report_path)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(config, "load_config",
                                  return_value={"zapret_dir": str(self.zapret)}), \
                mock.patch.object(strategy.actions, "is_admin",
                                  return_value=True), \
                mock.patch.object(strategy, "sweep", return_value=report):
            return entry.main()

    def test_skipped_report_is_not_written(self) -> None:
        self.assertEqual(self.run_main(skipped_report()), 0)
        self.assertFalse(self.out.exists())
        # Прогресс принадлежит живому перебору — пропуск его не трогает.
        self.assertFalse(self.report_path.exists())

    def test_success_still_writes_report(self) -> None:
        self.assertEqual(self.run_main(good_report()), 0)
        written = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(written.get("best"), "general.bat")
        progress = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertTrue(progress.get("done"))


class RunSweepSkippedTests(TempData):
    def test_run_sweep_skips_write(self) -> None:
        with mock.patch.object(strategy.actions, "is_admin", return_value=True), \
                mock.patch.object(strategy, "sweep",
                                  return_value=skipped_report()):
            self.assertEqual(strategy.run_sweep(self.out, self.zapret, []), 0)
        self.assertFalse(self.out.exists())

    def test_run_sweep_writes_success(self) -> None:
        with mock.patch.object(strategy.actions, "is_admin", return_value=True), \
                mock.patch.object(strategy, "sweep", return_value=good_report()):
            self.assertEqual(strategy.run_sweep(self.out, self.zapret, []), 0)
        self.assertTrue(self.out.exists())


class WatchSweepSkippedTests(TempData):
    def make_tray(self) -> app.TrayApp:
        tray = app.TrayApp.__new__(app.TrayApp)
        tray.cfg = {}
        tray.state = {}
        tray.sweep = {"running": True, "message": ""}
        tray.busy = {"sweep": "подбираю стратегию…"}
        tray._signature = None
        tray.update_icon = lambda: None
        tray.refresh = lambda: None
        tray.refresh_activity = lambda: None
        tray.notify = mock.Mock()
        return tray

    def watch(self, tray, after_sleep=None) -> int:
        """Гоняет наблюдатель на поддельных часах: шаг сна — 2 с."""
        clock = {"now": 1000.0}
        calls = {"n": 0}

        def tick(seconds: float) -> None:
            clock["now"] += 2.0
            calls["n"] += 1
            if after_sleep is not None:
                after_sleep(calls["n"])

        with mock.patch.object(app.time, "time",
                               side_effect=lambda: clock["now"]), \
                mock.patch.object(app.time, "sleep", side_effect=tick), \
                mock.patch.object(app.activity, "log"), \
                mock.patch.object(app.config, "save_config"), \
                mock.patch.object(app.config, "save_state"), \
                mock.patch.object(app.networks, "current", return_value={}):
            tray._watch_sweep(self.out, self.report_path)
        return calls["n"]

    def test_skipped_report_does_not_end_watch(self) -> None:
        self.out.write_text(json.dumps(skipped_report(), ensure_ascii=False),
                            encoding="utf-8")
        tray = self.make_tray()

        def later(n: int) -> None:
            if n == 3:
                self.out.write_text(json.dumps(good_report(), ensure_ascii=False),
                                    encoding="utf-8")

        sleeps = self.watch(tray, later)
        self.assertEqual(sleeps, 3, "наблюдатель завершился по skipped-отчёту")
        self.assertFalse(tray.sweep["running"])
        self.assertEqual(tray.sweep["message"],
                         "Лучшая стратегия: general.bat (установлена)")
        self.assertEqual(tray.cfg.get("zapret_bat"), "general.bat")

    def test_skipped_report_ends_at_deadline(self) -> None:
        # Чужой перебор так и не завершился — наблюдатель обязан выйти сам.
        self.out.write_text(json.dumps(skipped_report(), ensure_ascii=False),
                            encoding="utf-8")
        tray = self.make_tray()
        sleeps = self.watch(tray)
        self.assertLessEqual(sleeps, 450)  # дедлайн 900 с при шаге 2 с
        self.assertFalse(tray.sweep["running"])
        self.assertEqual(tray.sweep["message"], "Перебор не завершён")
        self.assertEqual(tray.busy, {})


if __name__ == "__main__":
    unittest.main()
