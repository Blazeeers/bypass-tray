"""Подсказки на кнопках: одновременно открыта только одна.

Регрессия: Segmented и IconButton вешали свой <Leave> после создания
подсказки и без add="+", из-за чего скрытие затиралось и каждое наведение
оставляло висеть новое окно (видно было столбик подсказок в настройках).
"""

from __future__ import annotations

import tkinter as tk
import unittest

from bypass_tray import theme
from bypass_tray.widgets import Segmented, Tooltip


def _tk_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


@unittest.skipUnless(_tk_available(), "нужен рабочий Tk")
class TooltipSingleWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.theme = theme.Theme(True)
        self.widgets: list[Segmented] = []
        for index in range(3):
            seg = Segmented(
                self.root, self.theme, [("direct", "Напрямую"), ("proxy", "Через VPN")],
                "direct", height=32, width=200,
                tooltips={"direct": "Вести напрямую", "proxy": "Вести через VPN"},
            )
            seg.pack()
            self.widgets.append(seg)
        self.root.update_idletasks()

    def tearDown(self) -> None:
        Tooltip._visible = None
        self.root.destroy()

    def _open(self) -> list[Segmented]:
        return [seg for seg in self.widgets if seg._tooltip and seg._tooltip._window is not None]

    def test_only_one_tooltip_at_a_time(self) -> None:
        for seg in self.widgets:
            seg._tooltip.show()
            self.root.update_idletasks()
            self.assertEqual(len(self._open()), 1, "должна быть открыта ровно одна подсказка")

    def test_leave_hides_tooltip(self) -> None:
        seg = self.widgets[1]
        seg._tooltip.show()
        self.root.update_idletasks()
        self.assertEqual(len(self._open()), 1)
        seg._leave()
        self.root.update_idletasks()
        self.assertEqual(self._open(), [], "после ухода курсора подсказок быть не должно")

    def test_leave_binding_keeps_both_handlers(self) -> None:
        # Скрытие подсказки должно остаться в обработчике <Leave>, а не быть
        # затёртым биндом самого виджета.
        script = str(self.root.tk.call("bind", str(self.widgets[0]), "<Leave>"))
        self.assertIn("hide", script)
        self.assertIn("_leave", script)


if __name__ == "__main__":
    unittest.main()
