"""Тесты текстов панели: склонения и сводка «Стабильность за сутки».

Формы слов проверяются на чистой функции ``_plural`` и её обёртках, тексты
карточки и подписи — на методе ``Panel._stability_text``/``_stability_brief``:
там нужны только числа, окно не создаётся.

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import ui  # noqa: E402


def _stats(reports: int, uptime: int | None, incidents: int = 0,
           switches: int = 0, heals: int = 0) -> dict:
    return {"reports": reports, "uptime_pct": uptime, "incidents": incidents,
            "server_switches": switches, "heals": heals}


class PluralTests(unittest.TestCase):
    """1/2/5 и 11/21 — ловушка русских числительных."""

    def test_incident_word(self):
        cases = {0: "сбоев", 1: "сбой", 2: "сбоя", 5: "сбоев",
                 11: "сбоев", 12: "сбоев", 14: "сбоев",
                 21: "сбой", 22: "сбоя", 25: "сбоев",
                 101: "сбой", 111: "сбоев"}
        for count, expected in cases.items():
            with self.subTest(count=count):
                self.assertEqual(ui._incident_word(count), expected)

    def test_times_word(self):
        cases = {0: "раз", 1: "раз", 2: "раза", 5: "раз",
                 11: "раз", 12: "раз", 14: "раз",
                 21: "раз", 22: "раза", 25: "раз",
                 101: "раз", 111: "раз"}
        for count, expected in cases.items():
            with self.subTest(count=count):
                self.assertEqual(ui._times_word(count), expected)

    def test_domain_word_still_works(self):
        self.assertEqual(ui._domain_word(1), "домен")
        self.assertEqual(ui._domain_word(3), "домена")
        self.assertEqual(ui._domain_word(11), "доменов")


class StabilityCardTests(unittest.TestCase):
    """Карточка: нулевые части не показываются, формы слов правильные."""

    def text(self, **kwargs) -> str:
        return ui.Panel._stability_text(None, _stats(**kwargs))

    def test_no_reports(self):
        self.assertEqual(self.text(reports=0, uptime=None),
                         "пока нет данных за сутки")

    def test_low_reports_keeps_prefix(self):
        self.assertEqual(self.text(reports=2, uptime=83, incidents=0),
                         "мало данных · в сети 83%")

    def test_only_uptime_without_zero_tails(self):
        self.assertEqual(self.text(reports=12, uptime=96), "в сети 96%")

    def test_full_line(self):
        self.assertEqual(
            self.text(reports=12, uptime=83, incidents=2, switches=1, heals=2),
            "в сети 83% · 2 сбоя · сервер менялся 1 раз · автолечение 2")

    def test_five_incidents_and_five_switches(self):
        self.assertEqual(
            self.text(reports=12, uptime=70, incidents=5, switches=5),
            "в сети 70% · 5 сбоев · сервер менялся 5 раз")

    def test_unknown_uptime(self):
        self.assertEqual(self.text(reports=12, uptime=None), "в сети —")


class StabilityBriefTests(unittest.TestCase):
    """Подпись в шапке журнала — те же правила, без префикса «мало данных»."""

    def brief(self, **kwargs) -> str:
        return ui.Panel._stability_brief(None, _stats(**kwargs))

    def test_no_reports(self):
        self.assertEqual(self.brief(reports=0, uptime=None),
                         "за сутки: нет данных")

    def test_low_reports(self):
        self.assertEqual(self.brief(reports=2, uptime=83, incidents=2),
                         "за сутки: мало данных")

    def test_with_incidents(self):
        self.assertEqual(self.brief(reports=12, uptime=83, incidents=2),
                         "за сутки: в сети 83% · 2 сбоя")

    def test_without_incidents(self):
        self.assertEqual(self.brief(reports=12, uptime=96, incidents=0),
                         "за сутки: в сети 96%")

    def test_single_incident(self):
        self.assertEqual(self.brief(reports=12, uptime=96, incidents=1),
                         "за сутки: в сети 96% · 1 сбой")


if __name__ == "__main__":
    unittest.main()
