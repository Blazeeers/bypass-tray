"""Небольшой набор виджетов для аккуратной панели.

Tkinter не умеет скруглённые углы и «плоские» кнопки, поэтому карточки,
индикаторы и кнопки рисуются на ``tk.Canvas`` вручную.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

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
                 bg: str | None = None, width: int | None = None,
                 min_width: int | None = None):
        self.theme = theme
        self.kind = kind
        self.command = command
        self._text = text
        self._enabled = True
        self._hover = False
        self._bg = bg or theme.card
        self._pad_x = pad_x
        self._min_width = min_width or 0
        self._font = theme.font(9, "bold" if kind == "primary" else "normal")
        self._height = height
        computed = width or (self._font.measure(text) + pad_x * 2)
        super().__init__(master, width=max(computed, self._min_width), height=height,
                         highlightthickness=0, bd=0, bg=self._bg, cursor="hand2")
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
        self.configure(width=max(self._font.measure(text) + self._pad_x * 2, self._min_width))
        self._render()

    def set_command(self, command) -> None:
        self.command = command

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


class Chip(tk.Canvas):
    """Маленькая скруглённая плашка состояния."""

    def __init__(self, master: tk.Misc, theme: Theme, text: str, color: str,
                 bg: str | None = None, height: int = 20):
        self.theme = theme
        self.color = color
        self._text = text
        self._bg = bg or theme.card
        self._height = height
        self._font = theme.font(8, "bold")
        super().__init__(master, width=self._measure(), height=height,
                         highlightthickness=0, bd=0, bg=self._bg)
        self._draw()

    def _measure(self) -> int:
        return self._font.measure(self._text) + 18

    def set(self, text: str, color: str) -> None:
        if text == self._text and color == self.color:
            return
        self._text = text
        self.color = color
        self.configure(width=self._measure())
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = int(self.winfo_reqwidth())
        height = self._height
        fill = blend(self.color, self.theme.card, 0.14)
        rounded_rect(self, 1, 1, width - 1, height - 1, height / 2 - 1,
                     fill=fill, outline="")
        self.create_text(width / 2, height / 2, text=self._text,
                         font=self._font, fill=self.color)


class Tooltip:
    """Подсказка над кнопкой; создаётся только по наведению."""

    def __init__(self, widget: tk.Misc, text: str, delay: int = 450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after: str | None = None
        self._window: tk.Toplevel | None = None
        if not text:
            return
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _event=None) -> None:
        self.hide()
        try:
            self._after = self.widget.after(self.delay, self.show)
        except tk.TclError:
            self._after = None

    def show(self) -> None:
        if self._window is not None or not self.text:
            return
        widget = self.widget
        try:
            window = tk.Toplevel(widget)
            window.wm_overrideredirect(True)
            window.attributes("-topmost", True)
            label = tk.Label(
                window, text=self.text, bg="#1B2735", fg="#FFFFFF",
                font=("Segoe UI", 8), padx=8, pady=4, bd=0,
            )
            label.pack()
            x = widget.winfo_rootx() + widget.winfo_width() // 2 - 40
            y = widget.winfo_rooty() + widget.winfo_height() + 6
            window.geometry(f"+{max(0, x)}+{max(0, y)}")
            self._window = window
        except tk.TclError:
            self._window = None

    def set(self, text: str) -> None:
        self.text = text
        if self._window is not None:
            self.hide()

    def hide(self, _event=None) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        if self._window is not None:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


class IconButton(tk.Canvas):
    """Кнопка-иконка с подсветкой при наведении и подсказкой."""

    def __init__(self, master: tk.Misc, theme: Theme, image, command,
                 tooltip: str = "", bg: str | None = None, size: int = 30):
        self.theme = theme
        self._image = image
        self.command = command
        self._bg = bg or theme.card
        self._size = size
        self._hover = False
        self._badge: str | None = None
        super().__init__(master, width=size, height=size, bg=self._bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._tooltip = Tooltip(self, tooltip)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._render()

    def _on_enter(self, _event) -> None:
        self._hover = True
        self._render()

    def _on_leave(self, _event) -> None:
        self._hover = False
        self._render()

    def _on_click(self, _event) -> None:
        if self.command is not None:
            self.command()

    def set_image(self, image) -> None:
        self._image = image
        self._render()

    def set_badge(self, color: str | None) -> None:
        """Маленькая точка в правом верхнем углу (янтарная — есть обновления)."""
        if color == self._badge:
            return
        self._badge = color
        self._render()

    def set_tooltip(self, text: str) -> None:
        self._tooltip.set(text)

    def _render(self) -> None:
        self.delete("all")
        size = self._size
        if self._hover:
            fill = blend(self.theme.text, self._bg, 0.10)
            rounded_rect(self, 1, 1, size - 1, size - 1, 8, fill=fill, outline="")
        self.create_image(size / 2, size / 2, image=self._image)
        if self._badge:
            r = 3
            self.create_oval(size - 2 * r - 2, 2, size - 2, 2 * r + 2,
                             fill=self._badge, outline=self._bg)


class Segmented(tk.Canvas):
    """Переключатель с общим треком: выбранная половина — accent.

    Трек один на всю ширину (``theme.btn`` с рамкой), выбранный сегмент
    рисуется поверх него акцентом. Невыбранный при наведении слегка светлеет.
    """

    WIDTH = 160

    def __init__(self, master: tk.Misc, theme: Theme, items: list[tuple[str, str]],
                 value: str | None = None, command=None, bg: str | None = None,
                 height: int = 30, width: int = WIDTH):
        self.theme = theme
        self._items = [(str(key), str(label)) for key, label in items]
        if not self._items:
            raise ValueError("Segmented: нужна хотя бы одна опция")
        keys = [key for key, _label in self._items]
        self._value = str(value) if value is not None and str(value) in keys else keys[0]
        self.command = command
        self._bg = bg or theme.card
        self._height = int(height)
        self._width = int(width)
        self._hover = -1
        self._font = theme.font(9, "bold")
        super().__init__(master, width=self._width, height=self._height,
                         bg=self._bg, highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)
        self._draw()

    def get(self) -> str:
        return self._value

    def set(self, value: str, notify: bool = False) -> None:
        value = str(value)
        if value == self._value:
            return
        if value not in [key for key, _label in self._items]:
            return
        self._value = value
        self._draw()
        if notify and self.command is not None:
            self.command(value)

    def _segment_box(self, index: int) -> tuple[float, float]:
        inner = self._width - 4
        step = inner / len(self._items)
        left = 2 + index * step
        return left, left + step

    def _index_at(self, x: int) -> int:
        if x < 1 or x > self._width - 1:
            return -1
        for index in range(len(self._items)):
            left, right = self._segment_box(index)
            if left <= x <= right:
                return index
        return -1

    def _click(self, event) -> None:
        index = self._index_at(int(event.x))
        if index < 0:
            return
        key = self._items[index][0]
        if key != self._value:
            self.set(key, notify=True)

    def _motion(self, event) -> None:
        index = self._index_at(int(event.x))
        if index != self._hover:
            self._hover = index
            self._draw()

    def _leave(self, _event=None) -> None:
        if self._hover != -1:
            self._hover = -1
            self._draw()

    def _draw(self) -> None:
        self.delete("all")
        theme = self.theme
        width, height = self._width, self._height
        rounded_rect(self, 1, 1, width - 1, height - 1, 9,
                     fill=theme.btn, outline=theme.btn_border)
        for index, (key, label) in enumerate(self._items):
            left, right = self._segment_box(index)
            selected = key == self._value
            if selected:
                fill = theme.accent
                color = theme.accent_text
            elif index == self._hover:
                fill = theme.btn_hover
                color = theme.text
            else:
                fill = ""
                color = theme.text_dim
            if fill:
                rounded_rect(self, left + 1, 3, right - 1, height - 3, 7,
                             fill=fill, outline="")
            self.create_text((left + right) / 2, height / 2, text=label,
                             font=self._font, fill=color)


class ToggleSwitch(tk.Canvas):
    """Тумблер 40×22; переключается сразу, без промежуточных кадров."""

    WIDTH = 40
    HEIGHT = 22

    def __init__(self, master: tk.Misc, theme: Theme, value: bool = False,
                 command=None, bg: str | None = None):
        self.theme = theme
        self._value = bool(value)
        self.command = command
        self._bg = bg or theme.card
        self._pos = 1.0 if value else 0.0
        self._hover = False
        super().__init__(master, width=self.WIDTH, height=self.HEIGHT, bg=self._bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self._draw()

    # -- состояние ---------------------------------------------------------
    def get(self) -> bool:
        return self._value

    def set(self, value: bool, notify: bool = False) -> None:
        value = bool(value)
        self._value = value
        self._pos = 1.0 if value else 0.0
        self._draw()
        if notify and self.command is not None:
            self.command(value)

    # -- события -----------------------------------------------------------
    def _click(self, _event) -> None:
        self.set(not self._value, notify=True)

    def _enter(self, _event) -> None:
        self._hover = True
        self._draw()

    def _leave(self, _event) -> None:
        self._hover = False
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        theme = self.theme
        width, height = self.WIDTH, self.HEIGHT
        if self._value:
            track = theme.accent
            if self._hover:
                track = theme.accent_hover
        else:
            # Дорожка выключенного тумблера — цвет рамки кнопок: на светлой
            # карточке она заметно темнее, белый кружок не «висит в воздухе».
            track = theme.btn_border
            if self._hover:
                track = blend(theme.text, theme.card, 0.26)
        rounded_rect(self, 1, 1, width - 1, height - 1, (height - 2) / 2,
                     fill=track, outline="")
        knob = height - 6
        left = 3 + self._pos * (width - knob - 6)
        self.create_oval(left, 3, left + knob, 3 + knob, fill="#FFFFFF",
                         outline=blend(theme.text, theme.card, 0.25))


class HoverRow(tk.Frame):
    """Строка с подсветкой фона при наведении и курсором-рукой."""

    def __init__(self, master: tk.Misc, theme: Theme, command=None,
                 bg: str | None = None, hover: str | None = None, pad: int = 0):
        self.theme = theme
        self._base = bg or theme.card
        self._hover_color = hover or theme.card_hover
        super().__init__(master, bg=self._base,
                         cursor="hand2" if command is not None else "")
        self.command = command
        self._pad = pad
        if command is not None:
            self.bind("<Enter>", self._enter)
            self.bind("<Leave>", self._leave)
            self.bind("<Button-1>", self._click)

    def track(self, widget: tk.Misc) -> tk.Misc:
        """Включает дочерний виджет в клик и (если строка кликабельна) подсветку."""
        if self.command is None:
            return widget
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<Button-1>", self._click, add="+")
        return widget

    def _click(self, _event=None) -> None:
        if self.command is not None:
            self.command()

    def _paint(self, widget: tk.Misc, color: str) -> None:
        try:
            widget.configure(bg=color)
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._paint(child, color)

    def _enter(self, _event=None) -> None:
        self._paint(self, self._hover_color)

    def _leave(self, _event=None) -> None:
        # <Leave> приходит и при переходе на дочерний виджет — проверяем,
        # не остался ли курсор внутри строки.
        try:
            under = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
        except tk.TclError:
            under = None
        while under is not None:
            if under is self:
                return
            under = getattr(under, "master", None)
        self._paint(self, self._base)


class AnimatedSection(tk.Canvas):
    """Секция-раскрывашка без анимации.

    Canvas — viewport: ``expand`` сразу ставит высоту по содержимому,
    ``collapse`` — 0 и прячет item. Промежуточных кадров нет.
    """

    def __init__(self, master: tk.Misc, theme: Theme, bg: str | None = None):
        self.theme = theme
        self._bg = bg or theme.card
        self._open = False
        self._pack_info: dict | None = None
        super().__init__(master, bg=self._bg, bd=0, highlightthickness=0,
                         height=0)
        self.content = tk.Frame(self, bg=self._bg)
        self._item = self.create_window(0, 0, anchor="nw", window=self.content)
        self.itemconfigure(self._item, state="hidden")
        self.bind("<Configure>", self._on_configure)

    def is_open(self) -> bool:
        return self._open

    def _on_configure(self, event) -> None:
        try:
            self.itemconfigure(self._item, width=max(1, event.width))
        except tk.TclError:
            pass

    def expand(self, on_height=None) -> None:
        self._open = True
        try:
            if not self.winfo_manager() and self._pack_info:
                info = {key: value for key, value in self._pack_info.items()
                        if key not in ("in", "in_")}
                self.pack(**info)
            self.itemconfigure(self._item, state="normal")
            self.update_idletasks()
            target = max(1, self.content.winfo_reqheight())
            self.configure(height=target)
            self.update_idletasks()
        except tk.TclError:
            return
        if on_height is not None:
            on_height()

    def collapse(self, on_height=None) -> None:
        self._open = False
        try:
            self.configure(height=0)
            self.itemconfigure(self._item, state="hidden")
            if self.winfo_manager() == "pack":
                self._pack_info = self.pack_info()
                self.pack_forget()
            self.update_idletasks()
        except tk.TclError:
            return
        if on_height is not None:
            on_height()


class ScrollBody(tk.Frame):
    """Прокручиваемое тело панели: canvas + внутренний фрейм.

    Колесо работает только когда курсор над областью; ширина содержимого
    всегда равна ширине canvas, поэтому горизонтальной прокрутки нет.
    """

    def __init__(self, master: tk.Misc, theme: Theme, bg: str | None = None,
                 min_height: int = 120, scrollbar: bool = False):
        self.theme = theme
        self._bg = bg or theme.bg
        self._min_height = max(12, int(min_height))
        super().__init__(master, bg=self._bg)
        self.canvas = tk.Canvas(self, bg=self._bg, highlightthickness=0, bd=0,
                                yscrollincrement=30)
        #: Признак владельца ScrollBody нужен, чтобы вложенный список не
        #: перехватывал колесо у внешней прокрутки.
        self.canvas._scroll_owner = self  # type: ignore[attr-defined]
        self.inner = tk.Frame(self.canvas, bg=self._bg)
        self._window = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar: ttk.Scrollbar | None = None
        if scrollbar:
            self.scrollbar = ttk.Scrollbar(self, orient="vertical",
                                           command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)

    # -- геометрия ---------------------------------------------------------
    def content_height(self) -> int:
        return self.inner.winfo_reqheight()

    def set_height(self, height: int) -> None:
        self.canvas.configure(height=max(self._min_height, int(height)))
        self._update_scrollbar()

    def _on_inner(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._update_scrollbar()

    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=max(1, event.width))
        self._update_scrollbar()

    def _update_scrollbar(self) -> None:
        """Показывает полосу прокрутки, только если содержимое не влезает."""
        if self.scrollbar is None:
            return
        try:
            needed = self.content_height() > self.canvas.winfo_height() + 1
        except tk.TclError:
            return
        shown = bool(self.scrollbar.winfo_manager())
        if needed and not shown:
            self.scrollbar.pack(side="right", fill="y")
        elif not needed and shown:
            self.scrollbar.pack_forget()

    # -- колесо ------------------------------------------------------------
    def bind_tree(self, widget: tk.Misc | None = None) -> None:
        """Вешает обработчик колеса на область и всех вложенных детей.

        ``bind_all`` не используется: он живёт на интерпретаторе и копится при
        каждом пересоздании окна. Здесь привязки принадлежат конкретным
        виджетам и исчезают вместе с ними; повторный вызов не дублирует
        обработчик (проверяется существующая привязка).
        """
        target = widget if widget is not None else self
        self.bind_wheel(target)
        try:
            children = target.winfo_children()
        except tk.TclError:
            return
        for child in children:
            self.bind_tree(child)

    def bind_wheel(self, widget: tk.Misc) -> None:
        """Вешает колесо на один виджет; повторно обработчик не дублирует."""
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                if not widget.bind(sequence):
                    widget.bind(sequence, self._wheel, add="+")
            except tk.TclError:
                continue

    def _wheel_step(self, event) -> int:
        number = getattr(event, "num", None)
        if number == 4:
            return -1
        if number == 5:
            return 1
        return -1 if getattr(event, "delta", 0) > 0 else 1

    def _wheel(self, event) -> None:
        try:
            px, py = self.winfo_pointerxy()
            left = self.canvas.winfo_rootx()
            top = self.canvas.winfo_rooty()
        except tk.TclError:
            return
        if not (left <= px <= left + self.canvas.winfo_width()
                and top <= py <= top + self.canvas.winfo_height()):
            return
        # Если курсор над вложенной прокручиваемой областью, её колесо
        # обработает само — иначе внешняя и внутренняя прокрутятся разом.
        try:
            under = self.winfo_containing(px, py)
        except tk.TclError:
            under = None
        node = under
        while node is not None and node is not self.canvas:
            if getattr(node, "_scroll_owner", None) not in (None, self):
                return
            node = getattr(node, "master", None)
        if self.content_height() <= self.canvas.winfo_height():
            return
        self.canvas.yview_scroll(self._wheel_step(event), "units")

    def release(self) -> None:
        """Забывает ссылки; чужие привязки колеса не снимаем.

        Обработчики привязаны к своим виджетам и умирают вместе с ними —
        глобальных ``bind_all`` больше нет.
        """
        self._window = 0

