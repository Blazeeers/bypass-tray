"""Цветовые схемы, типографика и сетка отступов панели.

Схема выбирается автоматически по системной теме Windows (светлая/тёмная),
но её можно зафиксировать в конфиге полем ``theme``: ``auto`` | ``light`` | ``dark``.

Палитра подобрана под контраст WCAG AA: любой текст на своём фоне даёт
не меньше ``MIN_CONTRAST`` (4.5:1) в обеих схемах. Проверять —
``contrast_ratio(theme.text_faint, theme.card)``.
"""

from __future__ import annotations

import sys
import tkinter.font as tkfont

FONT_FAMILY = "Segoe UI"

#: Минимальный контраст текста по WCAG AA для обычного кегля.
MIN_CONTRAST = 4.5

#: Сетка отступов, пиксели.
SPACE: dict[str, int] = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}

#: Радиусы скруглений, пиксели.
RADIUS: dict[str, int] = {"sm": 8, "md": 12, "lg": 16}

#: Шкала кеглей в пунктах Tk (1 pt = 1.333 px при 96 DPI):
#: caption 8pt≈11px, body 9pt≈12px, title 10pt≈13px,
#: heading 11pt≈15px, display 16pt≈20px.
TYPE: dict[str, int] = {
    "caption": 8,
    "body": 9,
    "title": 10,
    "heading": 11,
    "display": 16,
}

#: Светлая схема — фон чуть глубже карточек, чтобы белые карточки читались.
LIGHT: dict[str, str] = {
    "bg": "#E9EDF4",
    "card": "#FFFFFF",
    "card_border": "#DCE3ED",
    "card_hover": "#F4F7FC",
    "hairline": "#E4EAF2",
    "header": "#16202C",
    "header_text": "#FFFFFF",
    "header_dim": "#A6B6C9",
    "text": "#101821",
    "text_dim": "#4F5E73",
    "text_faint": "#5A6879",
    "accent": "#2F6FED",
    "accent_text": "#FFFFFF",
    "accent_hover": "#255BC7",
    "accent_pressed": "#1E4FAE",
    # Подложка-тон; текст на ней берётся accent_hover: чистый accent на
    # светло-синем даёт только 4.1:1.
    "accent_soft": "#EEF4FF",
    "accent_ink": "#255BC7",
    "ok": "#0F7A52",
    "ok_soft": "#E3F5EC",
    "warn": "#96560A",
    "warn_soft": "#FBF0DC",
    "bad": "#B7202A",
    "bad_soft": "#FBE7E8",
    "btn": "#F0F3F9",
    "btn_hover": "#E3E9F3",
    "btn_border": "#D5DDE9",
    "btn_text": "#1B2735",
    "track": "#DFE6EF",
    "dim": "#5C6A80",
    "shadow": "#C9D3E1",
    "shadow_soft": "#DBE2EC",
    "focus": "#2F6FED",
    "focus_soft": "#D7E5FD",
    "overlay": "#0B1220",
    "skeleton": "#E4EAF2",
}

#: Тёмная схема.
DARK: dict[str, str] = {
    "bg": "#0B0F15",
    "card": "#151B24",
    "card_border": "#28313E",
    "card_hover": "#1C2431",
    "hairline": "#202834",
    "header": "#070A0E",
    "header_text": "#EAF0F8",
    "header_dim": "#8798AC",
    "text": "#EAF0F8",
    "text_dim": "#A7B4C6",
    "text_faint": "#8B99AC",
    # Белый на #4C8DFF даёт всего 3.2:1, поэтому в тёмной схеме текст
    # акцентной кнопки тёмный — так он читается (5.9:1) и остаётся в моде.
    "accent": "#4C8DFF",
    "accent_text": "#08101E",
    "accent_hover": "#3C79E6",
    # Текст акцента в тёмной схеме тёмный, поэтому «нажатие» подсвечивает
    # заливку (6.9:1); затемнение дало бы 3.6:1.
    "accent_pressed": "#5E9BFF",
    "accent_soft": "#16253E",
    # На тёмно-синей подложке читается сам accent (4.8:1).
    "accent_ink": "#4C8DFF",
    "ok": "#4ED0A0",
    "ok_soft": "#122A21",
    "warn": "#F2B43C",
    "warn_soft": "#2C2312",
    "bad": "#FF8288",
    "bad_soft": "#331619",
    "btn": "#1F2733",
    "btn_hover": "#29323F",
    "btn_border": "#33404F",
    "btn_text": "#DCE4EE",
    "track": "#262F3C",
    "dim": "#78879A",
    "shadow": "#05080C",
    "shadow_soft": "#070B10",
    "focus": "#7FB0FF",
    "focus_soft": "#1E3357",
    "overlay": "#000000",
    "skeleton": "#1E2734",
}

#: Цвета состояний для иконки трея (одинаковы в обеих схемах).
#: Значения менять нельзя: по ним сверяются иконки, тесты и отчёты health.
STATE_COLORS = {
    "ok": "#22A06B",
    "warn": "#E0902B",
    "bad": "#DC3D43",
    "busy": "#2F6FED",
    "idle": "#8A98A8",
    "dim": "#8A98A8",
}


def _channels(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return (int(value[0:2], 16) / 255, int(value[2:4], 16) / 255,
            int(value[4:6], 16) / 255)


def _luminance(value: str) -> float:
    def channel(component: float) -> float:
        return (component / 12.92 if component <= 0.03928
                else ((component + 0.055) / 1.055) ** 2.4)

    red, green, blue = (channel(component) for component in _channels(value))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: str, background: str) -> float:
    """Контраст двух цветов по WCAG (1..21).

    Нужен, чтобы палитра проверялась расчётом, а не на глаз: у светлой схемы
    приглушённый текст когда-то давал 2.6:1 при норме 4.5:1.
    """
    high, low = _luminance(foreground), _luminance(background)
    if high < low:
        high, low = low, high
    return (high + 0.05) / (low + 0.05)


def system_is_dark() -> bool:
    """Тёмная ли системная тема приложений Windows."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def resolve_dark(preference: str = "auto") -> bool:
    """Превращает настройку ``theme_mode`` в конкретный режим.

    ``auto`` (и всё нераспознанное) спрашивает систему тем же
    ``system_is_dark``; ``light``/``dark`` применяются принудительно.
    Значение из конфига может оказаться числом или ``None`` — такие тоже
    считаются ``auto``, а не роняют старт панели.
    """
    preference = str(preference or "auto").strip().lower()
    if preference == "dark":
        return True
    if preference == "light":
        return False
    return system_is_dark()


class Theme:
    """Набор цветов, кеглей и отступов, разделяемый виджетами панели."""

    #: Те же сетки доступны и через экземпляр: ``theme.space["md"]``.
    space = SPACE
    radii = RADIUS
    type = TYPE
    min_contrast = MIN_CONTRAST

    def __init__(self, dark: bool = False):
        palette = DARK if dark else LIGHT
        for name, value in palette.items():
            setattr(self, name, value)
        self.dark = dark
        self._fonts: dict[tuple, tkfont.Font] = {}

    # -- типографика -------------------------------------------------------
    def font(self, size: int = TYPE["title"], weight: str = "normal") -> tkfont.Font:
        """Кэшированный шрифт Segoe UI (создаётся после появления Tk-корня)."""
        key = (size, weight)
        cached = self._fonts.get(key)
        if cached is None:
            cached = tkfont.Font(family=FONT_FAMILY, size=size, weight=weight)
            self._fonts[key] = cached
        return cached

    def caption(self, weight: str = "normal") -> tkfont.Font:
        """Подпись, 11 px."""
        return self.font(TYPE["caption"], weight)

    def body(self, weight: str = "normal") -> tkfont.Font:
        """Основной текст, 12 px."""
        return self.font(TYPE["body"], weight)

    def title(self, weight: str = "bold") -> tkfont.Font:
        """Заголовок блока, 13 px, semi-bold."""
        return self.font(TYPE["title"], weight)

    def heading(self, weight: str = "bold") -> tkfont.Font:
        """Крупный подзаголовок, 15 px."""
        return self.font(TYPE["heading"], weight)

    def display(self, weight: str = "bold") -> tkfont.Font:
        """Крупный заголовок, 20 px."""
        return self.font(TYPE["display"], weight)

    # -- прочее ------------------------------------------------------------
    def space_of(self, name: str, default: int = SPACE["md"]) -> int:
        """Отступ из сетки по имени: ``xs`` | ``sm`` | ``md`` | ``lg`` | ``xl``."""
        return SPACE.get(name, default)

    def radius_of(self, name: str, default: int = RADIUS["md"]) -> int:
        """Радиус из сетки по имени: ``sm`` | ``md`` | ``lg``."""
        return RADIUS.get(name, default)

    def soft_for(self, color: str) -> str:
        """Тональная подложка под цвет состояния (ok/warn/bad/accent)."""
        for key in ("ok", "warn", "bad", "accent"):
            if color.lower() == getattr(self, key).lower():
                return getattr(self, f"{key}_soft")
        return self.skeleton

    def state_color(self, level: str) -> str:
        return getattr(self, level, self.dim)

    def release(self) -> None:
        """Забывает шрифты Tk, привязанные к уничтоженному интерпретатору.

        Иначе объекты ``Font`` переживут окно и будут освобождены в чужом
        потоке — Python 3.14 сообщает об этом как об утечке интерпретатора Tcl.
        """
        self._fonts.clear()
