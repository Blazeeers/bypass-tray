"""Небольшой набор виджетов для аккуратной панели.

Tkinter не умеет скруглённые углы и «плоские» кнопки, поэтому карточки,
индикаторы и кнопки рисуются на ``tk.Canvas`` вручную.
"""

from __future__ import annotations

import tkinter as tk

from .theme import Theme


def rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                 radius: float, **kwargs) -> int:
    """Скруглённый прямоугольник через сглаженный полигон."""
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=18, **kwargs)


class Card(tk.Canvas):
    """Карточка со скруглёнными углами; содержимое кладётся в ``self.inner``."""

    def __init__(self, master: tk.Misc, theme: Theme, radius: int = 14,
                 pad: int = 16, **kwargs):
        super().__init__(master, highlightthickness=0, bd=0, bg=theme.bg, height=20, **kwargs)
        self.theme = theme
        self.radius = radius
        self.pad = pad
        self.inner = tk.Frame(self, bg=theme.card)
        self._window = self.create_window(pad, pad, anchor="nw", window=self.inner)
        self.inner.bind("<Configure>", self._sync_height)
        self.bind("<Configure>", lambda _event: self._draw())

    def _sync_height(self, _event=None) -> None:
        wanted = self.inner.winfo_reqheight() + 2 * self.pad
        if abs(self.winfo_height() - wanted) > 1:
            self.configure(height=wanted)
        self._draw()

    def _draw(self) -> None:
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return
        self.delete("card")
        rounded_rect(
            self, 1, 1, width - 1, height - 1, self.radius,
            fill=self.theme.card, outline=self.theme.card_border, tags="card",
        )
        self.tag_lower("card")
        self.itemconfigure(self._window, width=max(1, width - 2 * self.pad))


def blend(foreground: str, background: str, alpha: float) -> str:
    """Смешивает цвета: Tk не понимает 8-значный hex с альфой."""
    def channels(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    fg = channels(foreground)
    bg = channels(background)
    mixed = tuple(round(fg[i] * alpha + bg[i] * (1 - alpha)) for i in range(3))
    return "#%02x%02x%02x" % mixed


class StatusDot(tk.Canvas):
    """Цветной индикатор состояния."""

    def __init__(self, master: tk.Misc, theme: Theme, size: int = 12, bg: str | None = None):
        self._bg = bg or theme.card
        super().__init__(master, width=size, height=size, highlightthickness=0, bd=0,
                         bg=self._bg)
        self._size = size
        self.set(theme.dim)

    def set(self, color: str) -> None:
        self.delete("all")
        size = self._size
        halo = blend(color, self._bg, 0.28)
        self.create_oval(0, 0, size - 1, size - 1, fill=halo, outline="")
        inset = size * 0.22
        self.create_oval(inset, inset, size - 1 - inset, size - 1 - inset,
                         fill=color, outline="")


class PillButton(tk.Canvas):
    """Плоская кнопка со скруглёнными углами и подсветкой при наведении."""

    def __init__(self, master: tk.Misc, theme: Theme, text: str, command,
                 kind: str = "secondary", pad_x: int = 14, height: int = 32,
                 bg: str | None = None, width: int | None = None):
        self.theme = theme
        self.kind = kind
        self.command = command
        self._text = text
        self._enabled = True
        self._hover = False
        self._bg = bg or theme.card
        self._font = theme.font(9, "bold" if kind == "primary" else "normal")
        self._height = height
        computed = width or (self._font.measure(text) + pad_x * 2)
        super().__init__(master, width=computed, height=height, highlightthickness=0,
                         bd=0, bg=self._bg, cursor="hand2")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._render()

    # -- состояние ---------------------------------------------------------
    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        self._font = self.theme.font(9, "bold" if self.kind == "primary" else "normal")
        self.configure(width=self._font.measure(text) + 28)
        self._render()

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._render()

    def set_kind(self, kind: str) -> None:
        if kind == self.kind:
            return
        self.kind = kind
        self._render()

    # -- события -----------------------------------------------------------
    def _on_enter(self, _event) -> None:
        if self._enabled:
            self._hover = True
            self._render()

    def _on_leave(self, _event) -> None:
        self._hover = False
        self._render()

    def _on_click(self, _event) -> None:
        if self._enabled and self.command is not None:
            self.command()

    # -- отрисовка ---------------------------------------------------------
    def _palette(self) -> tuple[str, str, str]:
        theme = self.theme
        if not self._enabled:
            return theme.btn, theme.btn_border, theme.text_faint
        if self.kind == "primary":
            fill = theme.accent_hover if self._hover else theme.accent
            return fill, fill, theme.accent_text
        if self.kind == "ghost":
            fill = theme.btn_hover if self._hover else theme.card
            border = theme.btn_border if self._hover else theme.card
            return fill, border, theme.text_dim
        fill = theme.btn_hover if self._hover else theme.btn
        return fill, theme.btn_border, theme.btn_text

    def _render(self) -> None:
        self.delete("all")
        fill, border, fg = self._palette()
        width = int(self.winfo_reqwidth())
        height = self._height
        self.configure(bg=self._bg)
        rounded_rect(self, 1, 1, width - 1, height - 1, min(9, height // 2 - 1),
                     fill=fill, outline=border)
        self.create_text(width / 2, height / 2, text=self._text, font=self._font, fill=fg)


class Divider(tk.Frame):
    """Тонкая разделительная линия."""

    def __init__(self, master: tk.Misc, theme: Theme, bg: str | None = None):
        super().__init__(master, height=1, bg=theme.card_border)
        self.configure(bg=theme.card_border)
        if bg:
            self.configure(bg=bg)
