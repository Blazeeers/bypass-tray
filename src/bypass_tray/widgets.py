"""Небольшой набор виджетов для аккуратной панели.

Tkinter не умеет скруглённые углы и «плоские» кнопки, поэтому карточки,
индикаторы и кнопки рисуются на ``tk.Canvas`` вручную.

Все размеры и цвета берутся из :mod:`bypass_tray.theme`: сетка ``SPACE``,
скругления ``RADIUS``, шкала кеглей ``TYPE`` и палитра схемы. Хардкодов
размеров здесь нет — поменяв тему, панель меняется целиком.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from .theme import RADIUS, SPACE, Theme

#: Высота элементов управления (кнопка, поле ввода), пиксели.
CONTROL_HEIGHT = SPACE["xl"] + SPACE["md"]

#: Радиус полей ввода: ровно середина между ``sm`` и ``md``.
FIELD_RADIUS = RADIUS["sm"] + (RADIUS["md"] - RADIUS["sm"]) // 2

#: Кадр анимации тумблера и секции, миллисекунды.
FRAME_MS = 16


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


def blend(foreground: str, background: str, alpha: float) -> str:
    """Смешивает цвета: Tk не понимает 8-значный hex с альфой."""
    def channels(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    fg = channels(foreground)
    bg = channels(background)
    mixed = tuple(round(fg[i] * alpha + bg[i] * (1 - alpha)) for i in range(3))
    return "#%02x%02x%02x" % mixed


def soft_shadow(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                radius: float, theme: Theme, base: str, steps: int = 4,
                spread: float = 4.0, tags: str = "shadow") -> None:
    """Мягкая тень: несколько полупрозрачных слоёв (у Tk нет alpha).

    Каждый следующий слой ближе к фигуре и темнее, поэтому край выглядит
    размытым. Рисуется до самой фигуры — она перекроет внутренние слои.
    Прозрачность слоёв не выше 10 % (макет): резче тень читалась бы как
    вторая рамка, а не как тень.
    """
    for step in range(steps, 0, -1):
        grow = spread * step / steps
        # 2.5 % у внешнего слоя … 10 % у внутреннего: на широком размытии
        # разница с фоном иначе не читается и тень выглядит отсутствующей.
        alpha = 0.025 + 0.075 * (steps - step) / max(1, steps - 1)
        color = blend(theme.shadow, base, alpha)
        rounded_rect(canvas, x1 - grow, y1 - grow + 1, x2 + grow, y2 + grow + 1,
                     radius + grow, fill=color, outline="", tags=tags)


def _chevron(canvas: tk.Canvas, cx: float, cy: float, size: float, color: str,
             down: bool, tags: str = "chevron") -> None:
    """Шеврон линией: Tk не умеет поворачивать текст."""
    half, arm = size / 2, size / 4
    if down:
        points = (cx - half, cy - arm, cx, cy + arm, cx + half, cy - arm)
    else:
        points = (cx - half, cy + arm, cx, cy - arm, cx + half, cy + arm)
    canvas.create_line(*points, fill=color, width=1.6, capstyle="round",
                       joinstyle="round", tags=tags)


class Card(tk.Canvas):
    """Карточка со скруглёнными углами; содержимое кладётся в ``self.inner``."""

    #: Место под тень под нижней кромкой карточки, пиксели.
    SHADOW = 3

    def __init__(self, master: tk.Misc, theme: Theme, radius: int | None = None,
                 pad: int | None = None, interactive: bool = False, **kwargs):
        super().__init__(master, highlightthickness=0, bd=0, bg=theme.bg, height=20, **kwargs)
        self.theme = theme
        self.radius = theme.radius_of("md") if radius is None else radius
        self.pad = theme.space_of("lg") if pad is None else pad
        self.interactive = bool(interactive)
        self._hover = False
        # Последний размер из <Configure>: во время первого события
        # winfo_width() ещё 0, и по нему карточка рисовалась бы 1x1.
        self._size = (0, 0)
        self.inner = tk.Frame(self, bg=theme.card)
        self._window = self.create_window(self.pad, self.pad, anchor="nw",
                                          window=self.inner)
        self.inner.bind("<Configure>", self._sync_height)
        self.bind("<Configure>", self._on_configure)
        self._idle = self.after_idle(self._draw)
        self.bind("<Destroy>", self._cancel_idle)
        if self.interactive:
            self.configure(cursor="hand2")
            self.bind("<Enter>", self._enter)
            self.bind("<Leave>", self._leave)

    def _on_configure(self, event) -> None:
        self._size = (int(event.width), int(event.height))
        self._draw()

    def _cancel_idle(self, _event=None) -> None:
        if self._idle is not None:
            self.after_cancel(self._idle)
            self._idle = None

    def _sync_height(self, _event=None) -> None:
        wanted = self.inner.winfo_reqheight() + 2 * self.pad + self.SHADOW
        if abs(self.winfo_height() - wanted) > 1:
            self.configure(height=wanted)
        self._draw()

    def _enter(self, _event=None) -> None:
        self._hover = True
        self._draw()

    def _leave(self, _event=None) -> None:
        self._hover = False
        self._draw()

    def _draw(self, *_args) -> None:
        width, height = self._size
        if width <= 1 or height <= 1:
            width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        self.delete("card")
        self.delete("shadow")
        body = max(2, height - self.SHADOW)
        soft_shadow(self, 2, 2, width - 2, body - 1, self.radius, self.theme,
                    self.theme.bg, tags="shadow")
        border = self.theme.card_border
        if self._hover:
            border = blend(self.theme.accent, self.theme.card_border, 0.55)
        rounded_rect(
            self, 1, 1, width - 1, body, self.radius,
            fill=self.theme.card, outline=border, tags="card",
        )
        # Тень — под карточкой, карточка — над тенью и под содержимым.
        # Опускать саму карточку нельзя: tag_lower("card") уводит её под
        # собственную тень, и вместо скруглённой рамки на экране остаётся
        # прямоугольник цвета фона. Содержимое — отдельное окно поверх всего.
        self.tag_lower("shadow")
        self.itemconfigure(self._window, width=max(1, width - 2 * self.pad))


class StatusDot(tk.Canvas):
    """Цветной индикатор состояния: ядро 8 px и мягкий ореол."""

    def __init__(self, master: tk.Misc, theme: Theme, size: int | None = None,
                 bg: str | None = None):
        self._bg = bg or theme.card
        self._size = size or SPACE["md"]
        super().__init__(master, width=self._size, height=self._size,
                         highlightthickness=0, bd=0, bg=self._bg)
        self.theme = theme
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
    """Кнопка-«таблетка» с состояниями hover/pressed/disabled/loading.

    Виды: ``primary`` (акцент), ``secondary`` (нейтральная), ``ghost``
    (только текст, подсветка при наведении). Публичный API прежний:
    ``set_text``, ``set_command``, ``set_enabled``, ``set_kind``.
    """

    #: Шагов анимации индикатора загрузки и период, миллисекунды.
    SPIN_MS = 180

    def __init__(self, master: tk.Misc, theme: Theme, text: str, command,
                 kind: str = "secondary", pad_x: int | None = None,
                 height: int | None = None, bg: str | None = None,
                 width: int | None = None, min_width: int | None = None):
        self.theme = theme
        self.kind = kind
        self.command = command
        self._text = text
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._loading = False
        self._spin_job: str | None = None
        self._spin_phase = 0
        self._bg = bg or theme.card
        self._pad_x = theme.space_of("lg") if pad_x is None else pad_x
        self._min_width = min_width or 0
        self._height = CONTROL_HEIGHT if height is None else int(height)
        self._font = self._font_for(kind)
        self._last_width = 0
        computed = width or (self._font.measure(text) + self._pad_x * 2)
        super().__init__(master, width=max(computed, self._min_width),
                         height=self._height, highlightthickness=0, bd=0,
                         bg=self._bg, cursor="hand2")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_click)
        self.bind("<Configure>", self._on_configure)
        self.bind("<Destroy>", self._stop_spin)
        self._render()

    def _font_for(self, kind: str) -> tkfont.Font:
        weight = "bold" if kind == "primary" else "normal"
        return self.theme.body(weight)

    # -- состояние ---------------------------------------------------------
    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        self._font = self._font_for(self.kind)
        self.configure(width=max(self._font.measure(text) + self._pad_x * 2,
                                 self._min_width))
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
        self._font = self._font_for(kind)
        self._render()

    def set_loading(self, loading: bool) -> None:
        """Индикатор «идёт работа»: текст заменяется бегущими точками."""
        loading = bool(loading)
        if loading == self._loading:
            return
        self._loading = loading
        if loading:
            self._spin_phase = 0
            self._schedule_spin()
        else:
            self._stop_spin()
        self._render()

    def is_loading(self) -> bool:
        return self._loading

    # -- события -----------------------------------------------------------
    def _on_enter(self, _event) -> None:
        if self._enabled and not self._loading:
            self._hover = True
            self._render()

    def _on_leave(self, _event) -> None:
        self._hover = False
        self._pressed = False
        self._render()

    def _on_press(self, _event) -> None:
        if self._enabled and not self._loading:
            self._pressed = True
            self._render()

    def _on_click(self, event=None) -> None:
        was_pressed = self._pressed
        self._pressed = False
        self._render()
        if not self._enabled or self._loading or self.command is None:
            return
        # Клик считается нажатием кнопки: отпускание мимо кнопки не срабатывает.
        if event is not None and was_pressed:
            try:
                inside = (0 <= event.x <= self.winfo_width()
                          and 0 <= event.y <= self.winfo_height())
            except tk.TclError:
                inside = True
            if not inside:
                return
        self.command()

    def _on_configure(self, event) -> None:
        if abs(event.width - self._last_width) <= 1:
            return
        self._last_width = event.width
        self._render()

    # -- загрузка ----------------------------------------------------------
    def _schedule_spin(self) -> None:
        try:
            self._spin_job = self.after(self.SPIN_MS, self._tick_spin)
        except tk.TclError:
            self._spin_job = None

    def _tick_spin(self) -> None:
        self._spin_job = None
        if not self._loading:
            return
        self._spin_phase = (self._spin_phase + 1) % 3
        self._render()
        self._schedule_spin()

    def _stop_spin(self, _event=None) -> None:
        if self._spin_job is not None:
            try:
                self.after_cancel(self._spin_job)
            except tk.TclError:
                pass
            self._spin_job = None

    # -- отрисовка ---------------------------------------------------------
    def _palette(self) -> tuple[str, str, str]:
        """(заливка, рамка, текст) под текущее состояние."""
        theme = self.theme
        if not self._enabled or self._loading:
            return theme.btn, theme.btn_border, theme.text_faint
        if self.kind == "primary":
            if self._pressed:
                fill = getattr(theme, "accent_pressed", theme.accent_hover)
            else:
                fill = theme.accent_hover if self._hover else theme.accent
            return fill, fill, theme.accent_text
        if self.kind == "ghost":
            if self._pressed:
                return theme.btn, theme.btn_border, theme.text
            if self._hover:
                return theme.btn_hover, theme.btn_border, theme.text
            return theme.card, theme.card, theme.text_dim
        fill = theme.btn_border if self._pressed else (
            theme.btn_hover if self._hover else theme.btn)
        return fill, theme.btn_border, theme.btn_text

    def _render(self) -> None:
        self.delete("all")
        fill, border, fg = self._palette()
        width = self._last_width if self._last_width > 1 else self.winfo_reqwidth()
        width = max(int(width), 1)
        height = self._height
        self.configure(bg=self._bg)
        rounded_rect(self, 1, 1, width - 1, height - 1, (height - 2) / 2,
                     fill=fill, outline=border)
        if self._loading:
            self._draw_spinner(width, height, fg, fill)
            return
        offset = 1 if self._pressed else 0
        self.create_text(width / 2, height / 2 + offset, text=self._text,
                         font=self._font, fill=fg)

    def _draw_spinner(self, width: float, height: float, fg: str, fill: str) -> None:
        middle = height / 2
        step = SPACE["sm"]
        for index in range(3):
            active = index == self._spin_phase
            radius = 2.2 if active else 1.7
            color = fg if active else blend(fg, fill, 0.45)
            cx = width / 2 + (index - 1) * step
            self.create_oval(cx - radius, middle - radius, cx + radius,
                             middle + radius, fill=color, outline="")


class ActionTile(PillButton):
    """Ячейка панели действий: иконка сверху, подпись снизу.

    Наследник ``PillButton`` намеренно: снаружи это та же кнопка —
    ``set_text``, ``set_command``, ``set_enabled``, ``_on_click`` и признак
    ``_text`` (по нему панель и проверки находят «Проверить всё»). Отличается
    только отрисовка: скруглённый прямоугольник во всю ячейку и иконка над
    подписью, а не «таблетка» с текстом в центре.
    """

    #: Центр иконки от верха ячейки, пиксели.
    ICON_Y = 19
    #: Отступ подписи от низа ячейки, пиксели.
    TEXT_BOTTOM = 16

    def __init__(self, master: tk.Misc, theme: Theme, text: str, command,
                 icon, kind: str = "ghost", **kwargs):
        self.icon = icon
        super().__init__(master, theme, text, command, kind=kind,
                         pad_x=SPACE["sm"], **kwargs)

    def _render(self) -> None:
        self.delete("all")
        fill, border, fg = self._palette()
        width = self._last_width if self._last_width > 1 else self.winfo_reqwidth()
        width = max(int(width), 1)
        height = self._height
        self.configure(bg=self._bg)
        rounded_rect(self, 1, 1, width - 1, height - 1, RADIUS["md"],
                     fill=fill, outline=border)
        if self._loading:
            self._draw_spinner(width, height, fg, fill)
            return
        offset = 1 if self._pressed else 0
        if self.icon is not None:
            self.create_image(width / 2, self.ICON_Y + offset, image=self.icon)
        self.create_text(width / 2, height - self.TEXT_BOTTOM + offset,
                         text=self._text, font=self._font, fill=fg)


class Divider(tk.Frame):
    """Тонкая разделительная линия."""

    def __init__(self, master: tk.Misc, theme: Theme, bg: str | None = None):
        super().__init__(master, height=1, bg=theme.hairline)
        if bg:
            self.configure(bg=bg)


class Chip(tk.Canvas):
    """Плашка состояния: точка 8 px + текст на тональной подложке."""

    def __init__(self, master: tk.Misc, theme: Theme, text: str, color: str,
                 bg: str | None = None, height: int | None = None,
                 soft: str | None = None):
        self.theme = theme
        self.color = color
        self._text = text
        self._soft = soft
        self._bg = bg or theme.card
        self._height = SPACE["xl"] - SPACE["xs"] if height is None else height
        self._font = theme.caption("bold")
        super().__init__(master, width=self._measure(), height=self._height,
                         highlightthickness=0, bd=0, bg=self._bg)
        self._draw()

    #: Отступ по краям и зазор между точкой и текстом, пиксели.
    PAD = SPACE["sm"] - 1
    GAP = SPACE["xs"] + 2

    def _measure(self) -> int:
        return int(self._font.measure(self._text)
                   + 2 * self.PAD + SPACE["sm"] + self.GAP)

    def set(self, text: str, color: str, soft: str | None = None) -> None:
        if text == self._text and color == self.color and soft == self._soft:
            return
        self._text = text
        self.color = color
        self._soft = soft
        self.configure(width=self._measure())
        self._draw()

    def _tone(self) -> str:
        if self._soft:
            return self._soft
        known = self.theme.soft_for(self.color)
        if known != self.theme.skeleton:
            return known
        return blend(self.color, self.theme.card, 0.10 if not self.theme.dark else 0.16)

    def _ink(self) -> str:
        """Текст плашки: чуть ближе к основному цвету текста ради контраста."""
        return blend(self.color, self.theme.text, 0.9 if not self.theme.dark else 0.85)

    def _draw(self) -> None:
        self.delete("all")
        width = int(self.winfo_reqwidth())
        height = self._height
        rounded_rect(self, 1, 1, width - 1, height - 1, (height - 2) / 2,
                     fill=self._tone(), outline="")
        dot = SPACE["sm"]
        cy = height / 2
        cx = self.PAD + dot / 2
        self.create_oval(cx - dot / 2, cy - dot / 2, cx + dot / 2, cy + dot / 2,
                         fill=self.color, outline="")
        self.create_text(cx + dot / 2 + self.GAP, cy, text=self._text,
                         font=self._font, fill=self._ink(), anchor="w")


class Tooltip:
    """Подсказка над кнопкой; создаётся только по наведению."""

    def __init__(self, widget: tk.Misc, text: str, delay: int = 450,
                 theme: Theme | None = None):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.theme = theme
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

    def _colors(self) -> tuple[str, str, str]:
        if self.theme is None:
            return "#16202C", "#FFFFFF", "#DCE3ED"
        return self.theme.header, self.theme.header_text, self.theme.card_border

    def show(self) -> None:
        if self._window is not None or not self.text:
            return
        widget = self.widget
        background, foreground, border = self._colors()
        try:
            window = tk.Toplevel(widget)
            window.wm_overrideredirect(True)
            window.attributes("-topmost", True)
            frame = tk.Frame(window, bg=border, padx=1, pady=1)
            frame.pack()
            font = self.theme.caption() if self.theme is not None else ("Segoe UI", 8)
            label = tk.Label(
                frame, text=self.text, bg=background, fg=foreground,
                font=font, padx=SPACE["sm"], pady=SPACE["xs"], bd=0,
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

    #: Точка-бейдж: диаметр и отступ от правого верхнего угла, пиксели.
    #: Мелкая точка вместо перекраски всей иконки: цвет иконки остаётся
    #: нейтральным, а «есть новое» читается уголком.
    BADGE_SIZE = 5
    BADGE_INSET = 3

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
        self._tooltip = Tooltip(self, tooltip, theme=theme)
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
        """Маленькая акцентная точка в правом верхнем углу (есть обновления)."""
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
            rounded_rect(self, 1, 1, size - 1, size - 1, RADIUS["sm"],
                         fill=fill, outline="")
        self.create_image(size / 2, size / 2, image=self._image)
        if self._badge:
            dot, inset = self.BADGE_SIZE, self.BADGE_INSET
            right, top = size - inset, inset
            self.create_oval(right - dot, top, right, top + dot,
                             fill=self._badge, outline="")


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
        self._font = theme.body("bold")
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
        rounded_rect(self, 1, 1, width - 1, height - 1, (height - 2) / 2,
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
                rounded_rect(self, left + 1, 3, right - 1, height - 3,
                             (height - 8) / 2, fill=fill, outline="")
            self.create_text((left + right) / 2, height / 2, text=label,
                             font=self._font, fill=color)


class Dropdown(tk.Canvas):
    """Компактный выбор из списка: плашка с текущим значением и выпадающий список.

    Нужен там, где вариантов много (серверы подписки) и ``Segmented`` уже не
    помещается. Плашка рисуется тем же треком, что и ``Segmented``; список —
    отдельным окном без рамки в цветах карточки, потому что ``tk.Menu`` на
    Windows игнорирует цвета темы и выбивается из окна.
    """

    WIDTH = 132

    def __init__(self, master: tk.Misc, theme: Theme, items: list[tuple[str, str]],
                 value: str | None = None, command=None, bg: str | None = None,
                 height: int = 30, width: int = WIDTH, placeholder: str = "—"):
        self.theme = theme
        self._items = [(str(key), str(label)) for key, label in items]
        keys = [key for key, _label in self._items]
        self._value = str(value) if value is not None and str(value) in keys \
            else (keys[0] if keys else "")
        self.command = command
        self._bg = bg or theme.card
        self._height = int(height)
        self._width = int(width)
        self._placeholder = placeholder
        self._hover = False
        self._popup: tk.Toplevel | None = None
        self._font = theme.body()
        super().__init__(master, width=self._width, height=self._height,
                         bg=self._bg, highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self.bind("<Destroy>", self._on_destroy)
        self._draw()

    # ---- значение ------------------------------------------------------
    def get(self) -> str:
        return self._value

    def set(self, value: str, notify: bool = False) -> None:
        value = str(value)
        if value not in [key for key, _label in self._items] or value == self._value:
            return
        self._value = value
        self._draw()
        if notify and self.command is not None:
            self.command(value)

    def _label(self) -> str:
        for key, label in self._items:
            if key == self._value:
                return label
        return self._placeholder

    # ---- отрисовка -----------------------------------------------------
    def _set_hover(self, value: bool) -> None:
        if self._hover != value:
            self._hover = value
            self._draw()

    def _draw(self) -> None:
        self.delete("all")
        theme = self.theme
        width, height = self._width, self._height
        rounded_rect(self, 1, 1, width - 1, height - 1, (height - 2) / 2,
                     fill=theme.btn_hover if self._hover else theme.btn,
                     outline=theme.btn_border)
        label = self._label()
        # Плашка узкая: длинное имя сервера обрезаем, полное — в подсказке.
        if len(label) > 18:
            label = label[:17].rstrip() + "…"
        self.create_text(10, height / 2, text=label, anchor="w",
                         font=self._font, fill=theme.text)
        _chevron(self, width - 13, height / 2, 8, theme.text_dim, down=True)

    # ---- выпадающий список ---------------------------------------------
    def _toggle(self, _event=None) -> None:
        if self._popup is not None:
            self._close()
            return
        self._open()

    def _open(self) -> None:
        if not self._items:
            return
        theme = self.theme
        try:
            popup = tk.Toplevel(self)
        except tk.TclError:
            return
        popup.overrideredirect(True)
        popup.configure(bg=theme.btn_border)
        columns = 1 if len(self._items) <= 14 else 2
        rows = (len(self._items) + columns - 1) // columns
        body = tk.Frame(popup, bg=theme.btn)
        body.pack(padx=1, pady=1)
        for index, (key, label) in enumerate(self._items):
            selected = key == self._value
            row = tk.Label(
                body, text=label, bg=theme.btn,
                fg=theme.accent if selected else theme.text,
                font=theme.body("bold") if selected else theme.body(),
                anchor="w", justify="left", padx=10, pady=4,
                width=16 if columns > 1 else 0)
            row.grid(row=index % max(rows, 1), column=index // max(rows, 1),
                     sticky="ew")
            row.bind("<Enter>", lambda _event, widget=row:
                     widget.configure(bg=theme.btn_hover))
            row.bind("<Leave>", lambda _event, widget=row:
                     widget.configure(bg=theme.btn))
            row.bind("<Button-1>", lambda _event, name=key: self._choose(name))
        popup.update_idletasks()
        popup_w = popup.winfo_reqwidth()
        popup_h = popup.winfo_reqheight()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 2
        # Окно не должно уходить за край экрана: при нехватке места снизу
        # список раскрывается вверх.
        if y + popup_h > self.winfo_screenheight():
            y = max(0, self.winfo_rooty() - popup_h - 2)
        x = min(x, max(0, self.winfo_screenwidth() - popup_w - 4))
        popup.geometry(f"+{int(x)}+{int(y)}")
        popup.bind("<Escape>", lambda _event: self._close())
        popup.bind("<FocusOut>", lambda _event: self._close())
        self._popup = popup
        try:
            popup.focus_force()
        except tk.TclError:
            pass

    def _choose(self, value: str) -> None:
        self.set(value, notify=True)
        self._close()

    def _close(self) -> None:
        popup, self._popup = self._popup, None
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass

    def _on_destroy(self, event) -> None:
        if event.widget is self:
            self._close()


class ToggleSwitch(tk.Canvas):
    """Тумблер: дорожка 40×22, кружок 18, плавный переезд за 128 мс.

    ``set()`` меняет состояние мгновенно (панель обновляет тумблеры из
    состояния конфига и не должна догонять анимацию), а клик пользователя
    анимируется. Атрибута ``_after`` у виджета нет намеренно — тесты
    проверяют, что в покое никаких отложенных кадров не остаётся.
    """

    WIDTH = 40
    HEIGHT = 22
    #: Кружок, пиксели.
    KNOB = 18
    #: Отступ под кольцо фокуса, пиксели.
    PAD = 2
    #: Кадров анимации переезда (16 мс × 8 ≈ 128 мс).
    STEPS = 8

    def __init__(self, master: tk.Misc, theme: Theme, value: bool = False,
                 command=None, bg: str | None = None):
        self.theme = theme
        self._value = bool(value)
        self.command = command
        self._bg = bg or theme.card
        self._pos = 1.0 if value else 0.0
        self._anim_target = self._pos
        self._anim_job: str | None = None
        self._hover = False
        self._focused = False
        self._enabled = True
        super().__init__(master, width=self.WIDTH + 2 * self.PAD,
                         height=self.HEIGHT + 2 * self.PAD, bg=self._bg,
                         highlightthickness=0, bd=0, cursor="hand2",
                         takefocus=1)
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self.bind("<space>", self._space)
        self.bind("<Destroy>", self._stop_anim)
        self._draw()

    # -- состояние ---------------------------------------------------------
    def get(self) -> bool:
        return self._value

    def set(self, value: bool, notify: bool = False) -> None:
        value = bool(value)
        self._stop_anim()
        self._value = value
        self._pos = 1.0 if value else 0.0
        self._anim_target = self._pos
        self._draw()
        if notify and self.command is not None:
            self.command(value)

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self._hover = False
        self.configure(cursor="hand2" if enabled else "arrow",
                       takefocus=1 if enabled else 0)
        self._draw()

    # -- события -----------------------------------------------------------
    def _click(self, _event) -> None:
        if not self._enabled:
            return
        self._flip()

    def _space(self, _event):
        if self._enabled:
            self._flip()
            return "break"
        return None

    def _flip(self) -> None:
        target = not self._value
        self._value = target
        self._anim_target = 1.0 if target else 0.0
        self._step_anim()
        if self.command is not None:
            self.command(target)

    def _enter(self, _event) -> None:
        if self._enabled:
            self._hover = True
            self._draw()

    def _leave(self, _event) -> None:
        self._hover = False
        self._draw()

    def _focus_in(self, _event) -> None:
        self._focused = True
        self._draw()

    def _focus_out(self, _event) -> None:
        self._focused = False
        self._draw()

    # -- анимация ----------------------------------------------------------
    def _step_anim(self) -> None:
        self._stop_anim()
        if abs(self._anim_target - self._pos) < 1e-6:
            self._pos = self._anim_target
            self._draw()
            return
        step = 1.0 / self.STEPS
        diff = self._anim_target - self._pos
        self._pos = (self._anim_target if abs(diff) <= step
                     else self._pos + (step if diff > 0 else -step))
        self._draw()
        if abs(self._anim_target - self._pos) >= 1e-6:
            try:
                self._anim_job = self.after(FRAME_MS, self._step_anim)
            except tk.TclError:
                self._anim_job = None
        else:
            self._anim_job = None

    def _stop_anim(self, _event=None) -> None:
        if self._anim_job is not None:
            try:
                self.after_cancel(self._anim_job)
            except tk.TclError:
                pass
            self._anim_job = None

    # -- отрисовка ---------------------------------------------------------
    def _draw(self) -> None:
        self.delete("all")
        theme = self.theme
        pad = self.PAD
        width, height = self.WIDTH, self.HEIGHT
        right = pad + width
        bottom = pad + height
        radius = height / 2

        if not self._enabled:
            track = blend(theme.track, theme.card, 0.55)
            knob_fill = blend(theme.card, theme.bg, 0.55)
            knob_edge = blend(theme.track, theme.card, 0.6)
        elif self._value:
            track = theme.accent_hover if self._hover else theme.accent
            knob_fill = "#FFFFFF"
            knob_edge = blend(theme.text, theme.card, 0.18)
        else:
            # Дорожка выключенного тумблера — цвет рамки кнопок: на светлой
            # карточке она заметно темнее, белый кружок не «висит в воздухе».
            track = theme.track
            if self._hover:
                track = blend(theme.text, theme.card, 0.26)
            knob_fill = "#FFFFFF"
            knob_edge = blend(theme.text, theme.card, 0.25)

        rounded_rect(self, pad, pad, right, bottom, radius,
                     fill=track, outline="")

        knob = self.KNOB
        travel = width - knob - 2 * (pad - 1)
        left = pad + 2 + self._pos * travel
        top = pad + (height - knob) / 2
        self.create_oval(left, top, left + knob, top + knob, fill=knob_fill,
                         outline=knob_edge)

        if self._focused and self._enabled:
            rounded_rect(self, pad - 1, pad - 1, right + 1, bottom + 1,
                         radius + 1, fill="", outline=theme.focus)


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
    """Секция-раскрывашка: Canvas-viewport поверх ``self.content``.

    По умолчанию ``expand``/``collapse`` срабатывают мгновенно: страница
    пересчитывает высоту сама, и промежуточные кадры только мешают. С
    ``animate=True`` высота едет к цели за 160 мс, а по завершении точно
    равна высоте содержимого — пересчёт высоты страницы не ломается.
    """

    #: Длительность анимации раскрытия, миллисекунды.
    DURATION = 160

    def __init__(self, master: tk.Misc, theme: Theme, bg: str | None = None,
                 animate: bool = False):
        self.theme = theme
        self._bg = bg or theme.card
        self._open = False
        self._pack_info: dict | None = None
        self._height = 0
        self._on_height = None
        self._animate = bool(animate)
        self._anim_job: str | None = None
        super().__init__(master, bg=self._bg, bd=0, highlightthickness=0,
                         height=0)
        self.content = tk.Frame(self, bg=self._bg)
        self._item = self.create_window(0, 0, anchor="nw", window=self.content)
        self.itemconfigure(self._item, state="hidden")
        self.bind("<Configure>", self._on_configure)
        # Открытая секция должна расти вместе с содержимым: после
        # «Обновить список» в ней появляются новые строки, а высота Canvas
        # сама по себе не меняется — строки оставались невидимыми.
        self.content.bind("<Configure>", self._on_content)

    def is_open(self) -> bool:
        return self._open

    def _on_configure(self, event) -> None:
        try:
            self.itemconfigure(self._item, width=max(1, event.width))
        except tk.TclError:
            pass

    def _on_content(self, _event=None) -> None:
        """Подогнать высоту секции под выросшее содержимое."""
        if not self._open or self._anim_job is not None:
            return
        try:
            target = max(1, self.content.winfo_reqheight())
        except tk.TclError:
            return
        if target == self._height:
            return
        self._height = target
        try:
            self.configure(height=target)
        except tk.TclError:
            return
        # Окно/страница тоже должны узнать о новой высоте.
        if callable(self._on_height):
            self._on_height()

    def expand(self, on_height=None) -> None:
        self._open = True
        self._on_height = on_height
        try:
            if not self.winfo_manager() and self._pack_info:
                info = {key: value for key, value in self._pack_info.items()
                        if key not in ("in", "in_")}
                self.pack(**info)
            self.itemconfigure(self._item, state="normal")
            self.update_idletasks()
            target = max(1, self.content.winfo_reqheight())
            if self._animate:
                self._animate_to(target)
                return
            self._height = target
            self.configure(height=target)
            self.update_idletasks()
        except tk.TclError:
            return
        if on_height is not None:
            on_height()

    def collapse(self, on_height=None) -> None:
        self._open = False
        self._height = 0
        if self._animate:
            self._animate_to(0, then=self._finish_collapse)
        try:
            if not self._animate:
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

    def _finish_collapse(self) -> None:
        try:
            self.configure(height=0)
            self.itemconfigure(self._item, state="hidden")
            if self.winfo_manager() == "pack":
                self._pack_info = self.pack_info()
                self.pack_forget()
        except tk.TclError:
            return

    # -- анимация ----------------------------------------------------------
    def _animate_to(self, target: int, then=None, frame: float = 0.0) -> None:
        """Ведёт высоту к ``target`` за ``DURATION`` мс (ease-out)."""
        if self._anim_job is not None:
            try:
                self.after_cancel(self._anim_job)
            except tk.TclError:
                pass
            self._anim_job = None
        start = self.winfo_height() if self._height else 0
        steps = max(1, self.DURATION // FRAME_MS)
        frame += 1
        progress = min(1.0, frame / steps)
        eased = 1 - (1 - progress) ** 2
        value = int(round(start + (target - start) * eased))
        try:
            self.configure(height=max(0, value))
        except tk.TclError:
            return
        self._height = max(0, value)
        if callable(self._on_height):
            self._on_height()
        if progress >= 1.0:
            self._anim_job = None
            try:
                self.configure(height=max(0, target))
            except tk.TclError:
                return
            self._height = max(0, target)
            if then is not None:
                then()
            if callable(self._on_height):
                self._on_height()
            return
        try:
            self._anim_job = self.after(
                FRAME_MS, lambda: self._animate_to(target, then, frame))
        except tk.TclError:
            self._anim_job = None


class Section(tk.Frame):
    """Заголовок 13/600 с шевроном и раскрываемое тело под ним.

    Тонкая обёртка над :class:`AnimatedSection`: строка заголовка кликабельна,
    тело — ``self.body``. Анимация выключена по умолчанию (мгновенный отклик).
    """

    def __init__(self, master: tk.Misc, theme: Theme, title: str,
                 bg: str | None = None, command=None, start_open: bool = False,
                 animate: bool = False):
        self.theme = theme
        self._bg = bg or theme.card
        self._title = title
        self._outside = command
        self._open = False
        self._hover = False
        super().__init__(master, bg=self._bg)
        self.header = tk.Canvas(self, bg=self._bg, bd=0, highlightthickness=0,
                                height=CONTROL_HEIGHT)
        self.header.pack(fill="x")
        self.header.bind("<Button-1>", self._toggle)
        self.header.bind("<Enter>", self._enter)
        self.header.bind("<Leave>", self._leave)
        self.header.bind("<Configure>", lambda _event: self._draw_header())
        self._draw_header()
        self.section = AnimatedSection(self, theme, bg=self._bg, animate=animate)
        self.section.pack(fill="x")
        self.body = self.section.content
        if start_open:
            self.open()

    # -- состояние ---------------------------------------------------------
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        if self._open:
            return
        self._open = True
        self._draw_header()
        self.section.expand()

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        self._draw_header()
        self.section.collapse()

    def toggle(self) -> None:
        self.close() if self._open else self.open()

    def set_title(self, title: str) -> None:
        self._title = title
        self._draw_header()

    # -- события и отрисовка -----------------------------------------------
    def _toggle(self, _event=None) -> None:
        self.toggle()
        if self._outside is not None:
            self._outside()

    def _enter(self, _event=None) -> None:
        self._hover = True
        self._draw_header()

    def _leave(self, _event=None) -> None:
        self._hover = False
        self._draw_header()

    def _draw_header(self) -> None:
        canvas = self.header
        canvas.delete("all")
        width = canvas.winfo_width()
        if width <= 1:
            width = canvas.winfo_reqwidth()
        height = CONTROL_HEIGHT
        canvas.configure(cursor="hand2")
        if self._hover:
            rounded_rect(canvas, 1, 1, width - 1, height - 1, RADIUS["sm"],
                         fill=self.theme.card_hover, outline="")
        canvas.create_text(SPACE["xs"], height / 2, text=self._title,
                           font=self.theme.title(), fill=self.theme.text,
                           anchor="w")
        _chevron(canvas, width - SPACE["md"], height / 2, SPACE["sm"],
                 self.theme.text_dim, down=self._open)


class ScrollBody(tk.Frame):
    """Прокручиваемое тело панели: canvas + внутренний фрейм.

    Колесо работает только когда курсор над областью; ширина содержимого
    всегда равна ширине canvas, поэтому горизонтальной прокрутки нет.
    Полоса прокрутки тонкая и видна только когда содержимое не влезает.
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
            style = self._slim_scrollbar_style()
            self.scrollbar = ttk.Scrollbar(self, orient="vertical",
                                           command=self.canvas.yview, style=style)
            self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)

    def _slim_scrollbar_style(self) -> str:
        """Тонкая полоса без стрелок; цвета — из темы."""
        name = "Slim.Vertical.TScrollbar"
        thumb = blend(self.theme.text, self._bg, 0.22)
        active = blend(self.theme.text, self._bg, 0.34)
        style = ttk.Style(self)
        try:
            style.layout(name, [(
                "Vertical.Scrollbar.trough",
                {"sticky": "ns", "children": [
                    ("Vertical.Scrollbar.thumb",
                     {"expand": "1", "sticky": "nswe"}),
                ]},
            )])
            style.configure(
                name, gripcount=0, arrowsize=0, width=SPACE["sm"],
                background=thumb, lightcolor=thumb, darkcolor=thumb,
                bordercolor=self._bg, troughcolor=self._bg,
                relief="flat", borderwidth=0,
            )
            style.map(name, background=[("active", active)])
        except tk.TclError:
            pass
        return name

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
        """Навесить колесо на виджет дерева; повторно обработчик не плодим.

        Проверяем именно свою привязку: чужая привязка на этом виджете не
        повод считать колесо навешенным (раньше из-за неё прокрутка над
        виджетом не работала).
        """
        bound = getattr(widget, "_scroll_wheel_ids", None)
        if bound is None:
            bound = {}
            try:
                widget._scroll_wheel_ids = bound  # type: ignore[attr-defined]
            except (AttributeError, tk.TclError):
                return
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                current = widget.bind(sequence)
            except tk.TclError:
                continue
            mine = bound.get(sequence)
            if mine and str(mine) in str(current):
                continue
            try:
                funcid = widget.bind(sequence, self._wheel, add="+")
            except tk.TclError:
                continue
            if funcid:
                bound[sequence] = funcid

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


class TextField(tk.Canvas):
    """Однострочное поле ввода: скруглённая рамка, фокус-акцент, placeholder.

    Внутри — обычный ``tk.Entry`` без своей рамки, поэтому ввод, выделение
    и вставка работают штатно, а рисуется всё на Canvas из темы.

    Подсказка (placeholder) — отдельный ``Label``: элементы Canvas всегда
    лежат под встроенными окнами, поэтому нарисованный на канве текст
    перекрывался бы самим ``Entry``.
    """

    def __init__(self, master: tk.Misc, theme: Theme, textvariable=None,
                 placeholder: str = "", height: int | None = None,
                 bg: str | None = None, width: int = 200, command=None):
        self.theme = theme
        self.placeholder = placeholder
        self.command = command
        self._bg = bg or theme.card
        self._height = CONTROL_HEIGHT if height is None else int(height)
        self._radius = FIELD_RADIUS
        self._focused = False
        self._error = False
        self._enabled = True
        self._width = int(width)
        if textvariable is None:
            textvariable = tk.StringVar(master=master)
        self.variable = textvariable
        super().__init__(master, width=width, height=self._height,
                         highlightthickness=0, bd=0, bg=self._bg)
        self.entry = tk.Entry(
            self, textvariable=textvariable, bd=0, highlightthickness=0,
            relief="flat", bg=self.theme.card, fg=self.theme.text,
            insertbackground=self.theme.text, selectbackground=self.theme.accent,
            selectforeground=self.theme.accent_text, font=self.theme.body(),
            disabledbackground=self.theme.btn, disabledforeground=self.theme.text_faint,
        )
        self._pad = SPACE["md"]
        self._window = self.create_window(self._pad, self._height / 2,
                                          anchor="w", window=self.entry)
        # Подсказка создаётся после Entry: окна Canvas держатся того же
        # порядка, поэтому Label окажется поверх поля ввода.
        self._hint = tk.Label(
            self, text=placeholder, anchor="w", bd=0, padx=0, pady=0,
            bg=self.theme.card, fg=self.theme.text_faint, cursor="xterm",
            font=self.theme.body(),
        )
        self._hint_window = self.create_window(self._pad, self._height / 2,
                                               anchor="w", window=self._hint)
        self._hint.bind("<Button-1>", self._hint_click)
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.entry.bind("<KeyRelease>", lambda _event: self._draw())
        self.bind("<Configure>", self._on_configure)
        self._trace = self.variable.trace_add("write", self._on_text)
        self.bind("<Destroy>", self._on_destroy)
        self._draw()

    # -- публичный API -----------------------------------------------------
    def get(self) -> str:
        return self.variable.get()

    def set(self, value: str) -> None:
        self.variable.set(value)
        self._draw()

    def focus_set(self) -> None:  # noqa: D401 - совпадает с Tk-именем
        self.entry.focus_set()

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self.entry.configure(state="normal" if enabled else "disabled")
        self._draw()

    def set_error(self, error: bool) -> None:
        error = bool(error)
        if error == self._error:
            return
        self._error = error
        self._draw()

    def set_command(self, command) -> None:
        self.command = command

    def on_enter(self, callback) -> None:
        """Enter в поле — обычно «применить»."""
        self.entry.bind("<Return>", lambda _event: callback())

    # -- события -----------------------------------------------------------
    def _focus_in(self, _event) -> None:
        self._focused = True
        self._draw()

    def _focus_out(self, _event) -> None:
        self._focused = False
        self._draw()

    def _on_text(self, *_args) -> None:
        self._draw()
        if self.command is not None:
            self.command(self.variable.get())

    def _on_destroy(self, _event=None) -> None:
        try:
            self.variable.trace_remove("write", self._trace)
        except (tk.TclError, ValueError):
            pass

    def _on_configure(self, event) -> None:
        self._width = max(1, int(event.width))
        try:
            # Поле ввода ужимается по вертикали, чтобы не накрыть рамку:
            # Entry — прямоугольник, и вплотную к бордеру он его съедает.
            self.itemconfigure(
                self._window,
                width=max(SPACE["md"], self._width - 2 * self._pad),
                height=max(1, self._height - 2 * SPACE["xs"]),
            )
        except tk.TclError:
            pass
        self._draw()

    def _hint_click(self, _event=None) -> None:
        """Клик по подсказке — это клик по полю: передаём фокус в Entry."""
        self.entry.focus_set()

    # -- отрисовка ---------------------------------------------------------
    def _border(self) -> str:
        if self._error:
            return self.theme.bad
        if self._focused:
            return self.theme.accent
        return self.theme.card_border

    def _hint_visible(self) -> bool:
        return bool(self.placeholder) and not self._focused and not self.variable.get()

    def _draw(self) -> None:
        self.delete("field")
        width = self._width
        if width <= 1:
            width = self.winfo_width()
        if width <= 1:
            width = self.winfo_reqwidth()
        height = self._height
        fill = self.theme.card if self._enabled else self.theme.btn
        rounded_rect(self, 1, 1, width - 1, height - 1, self._radius,
                     fill=fill, outline=self._border(), tags="field")
        if self._focused and not self._error:
            rounded_rect(self, 2, 2, width - 2, height - 2, self._radius - 1,
                         fill="", outline=self.theme.focus_soft, tags="field")
        # Рамка должна лежать под окном ввода, иначе перекроет текст.
        self.tag_lower("field")
        self.entry.configure(bg=fill)
        self._hint.configure(bg=fill, fg=self.theme.text_faint)
        self.itemconfigure(self._hint_window,
                           state="normal" if self._hint_visible() else "hidden")
        self.tag_raise(self._hint_window)
