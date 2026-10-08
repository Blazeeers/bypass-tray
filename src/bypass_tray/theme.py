"""Цветовые схемы и шрифты панели.

Схема выбирается автоматически по системной теме Windows (светлая/тёмная),
но её можно зафиксировать в конфиге полем ``theme``: ``auto`` | ``light`` | ``dark``.
"""

from __future__ import annotations

import sys
import tkinter.font as tkfont

FONT_FAMILY = "Segoe UI"

#: Светлая схема — мягкий фон, белые карточки, тёмный шапка.
LIGHT: dict[str, str] = {
    "bg": "#EEF1F6",
    "card": "#FFFFFF",
    "card_border": "#DCE3EC",
    "header": "#1B2735",
    "header_text": "#FFFFFF",
    "header_dim": "#9FB0C4",
    "text": "#161F2B",
    "text_dim": "#6B7A8D",
    "text_faint": "#93A1B2",
    "accent": "#2F6FED",
    "accent_text": "#FFFFFF",
    "accent_hover": "#255BC7",
    "btn": "#F1F4F9",
    "btn_hover": "#E4EAF3",
    "btn_border": "#D3DBE6",
    "btn_text": "#22303F",
    "track": "#E4E9F0",
    "ok": "#1E9E6A",
    "warn": "#D98600",
    "bad": "#D93A40",
    "dim": "#8A98A8",
    "shadow": "#D8DEE7",
}

#: Тёмная схема.
DARK: dict[str, str] = {
    "bg": "#10141A",
    "card": "#181E27",
    "card_border": "#28313E",
    "header": "#0B0F14",
    "header_text": "#EAF0F7",
    "header_dim": "#8494A8",
    "text": "#E7EDF5",
    "text_dim": "#93A1B4",
    "text_faint": "#6E7C8E",
    "accent": "#4C8DFF",
    "accent_text": "#0B0F14",
    "accent_hover": "#3C79E6",
    "btn": "#222A35",
    "btn_hover": "#2C3644",
    "btn_border": "#333E4C",
    "btn_text": "#DCE4EE",
    "track": "#232C38",
    "ok": "#33C08A",
    "warn": "#E5A32B",
    "bad": "#F0575D",
    "dim": "#7C8A9C",
    "shadow": "#0A0D11",
}

#: Цвета состояний для иконки трея (одинаковы в обеих схемах).
STATE_COLORS = {
    "ok": "#22A06B",
    "warn": "#E0902B",
    "bad": "#DC3D43",
    "dim": "#8A98A8",
}


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
    """Превращает настройку ``theme`` в конкретный режим."""
    preference = (preference or "auto").lower()
    if preference == "dark":
        return True
    if preference == "light":
        return False
    return system_is_dark()


class Theme:
    """Набор цветов и шрифтов, разделяемый виджетами панели."""

    def __init__(self, dark: bool = False):
        palette = DARK if dark else LIGHT
        for name, value in palette.items():
            setattr(self, name, value)
        self.dark = dark
        self._fonts: dict[tuple, tkfont.Font] = {}

    def font(self, size: int = 10, weight: str = "normal") -> tkfont.Font:
        """Кэшированный шрифт Segoe UI (создаётся после появления Tk-корня)."""
        key = (size, weight)
        cached = self._fonts.get(key)
        if cached is None:
            cached = tkfont.Font(family=FONT_FAMILY, size=size, weight=weight)
            self._fonts[key] = cached
        return cached

    def state_color(self, level: str) -> str:
        return getattr(self, level, self.dim)

    def release(self) -> None:
        """Забывает шрифты Tk, привязанные к уничтоженному интерпретатору.

        Иначе объекты ``Font`` переживут окно и будут освобождены в чужом
        потоке — Python 3.14 сообщает об этом как об утечке интерпретатора Tcl.
        """
        self._fonts.clear()
