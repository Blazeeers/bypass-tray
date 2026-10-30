"""Цветовые схемы, типографика и сетка отступов панели.

Схема выбирается автоматически по системной теме Windows (светлая/тёмная),
но её можно зафиксировать в конфиге полем ``theme``: ``auto`` | ``light`` | ``dark``.

Палитра — макет «B — Дашборд»: тёмная как основная, teal-акцент, приглушённые
поверхности. Смысловой текст (``text``, ``text_dim``) держит контраст WCAG AA
не ниже ``MIN_CONTRAST`` (4.5:1) на своём фоне в обеих схемах; проверять —
``contrast_ratio(theme.text_dim, theme.card)``. Исключение — ``text_faint``:
это декоративные подписи (11-12 px, дублируют соседний текст), для них
норма AA не выполняется, поэтому смысловое в них класть нельзя.
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
#: heading 11pt≈15px, display 16pt≈20px, hero 18pt≈24px,
#: value 20pt≈27px (крупные числа плиток).
TYPE: dict[str, int] = {
    "caption": 8,
    "body": 9,
    "title": 10,
    "heading": 11,
    "display": 16,
    "hero": 18,
    "value": 20,
}

#: Светлая схема: страница чуть темнее карточек, акцент — тот же teal, но
#: темнее: на белом ``#2DD4BF`` даёт 2.0:1 и текст на нём не читается.
LIGHT: dict[str, str] = {
    "bg": "#F5F6F8",
    "card": "#FFFFFF",
    "card_border": "#E4E7EC",
    "card_hover": "#F9FAFB",
    "hairline": "#EAECF0",
    "header": "#F5F6F8",
    "header_text": "#101828",
    "header_dim": "#667085",
    "text": "#101828",
    "text_dim": "#667085",
    "text_faint": "#98A2B3",
    "accent": "#0F766E",
    "accent_text": "#FFFFFF",
    "accent_hover": "#115E59",
    "accent_pressed": "#134E4A",
    # Подложка-тон; текст на ней — accent_ink (тот же accent: 5.5:1 на белом).
    "accent_soft": "#E7F6F4",
    "accent_ink": "#0F766E",
    "ok": "#0F766E",
    "ok_soft": "#E7F6F4",
    "warn": "#B54708",
    "warn_soft": "#FEF3E2",
    "bad": "#B42318",
    "bad_soft": "#FEE4E2",
    "btn": "#F9FAFB",
    "btn_hover": "#F2F4F7",
    "btn_border": "#E4E7EC",
    "btn_text": "#101828",
    "track": "#E4E7EC",
    "dim": "#667085",
    "shadow": "#98A2B3",
    "shadow_soft": "#D0D5DD",
    "focus": "#0F766E",
    "focus_soft": "#D5F0EC",
    "overlay": "#101828",
    "skeleton": "#EEF0F3",
}

#: Тёмная схема — основная.
DARK: dict[str, str] = {
    "bg": "#0B0F15",
    "card": "#121A24",
    "card_border": "#1D2836",
    "card_hover": "#18222E",
    "hairline": "#1D2836",
    # Шапка сливается со страницей, как на макете: отдельной полосы нет.
    "header": "#0B0F15",
    "header_text": "#E8EEF5",
    "header_dim": "#8A98A8",
    "text": "#E8EEF5",
    "text_dim": "#8A98A8",
    # Декоративные подписи: 3.3:1 на карточке — ниже AA, но дублируют текст.
    "text_faint": "#5E6C7C",
    # На заливке #2DD4BF белый даёт 2.0:1, поэтому текст кнопки тёмный (9.9:1).
    "accent": "#2DD4BF",
    "accent_text": "#04221E",
    "accent_hover": "#25BFAE",
    # Текст акцента тёмный, поэтому «нажатие» подсвечивает заливку.
    "accent_pressed": "#3FE0CE",
    "accent_soft": "#0F2A28",
    "accent_ink": "#2DD4BF",
    "ok": "#2DD4BF",
    "ok_soft": "#0F2A28",
    "warn": "#F59E0B",
    "warn_soft": "#2E2110",
    "bad": "#F87171",
    "bad_soft": "#33191B",
    "btn": "#16202B",
    "btn_hover": "#1C2836",
    "btn_border": "#1D2836",
    "btn_text": "#E8EEF5",
    "track": "#1D2836",
    "dim": "#8A98A8",
    # Тень в тёмной теме светлее фона: тёмное пятно на нём не видно вовсе.
    "shadow": "#243040",
    "shadow_soft": "#1A2431",
    "focus": "#2DD4BF",
    "focus_soft": "#0F2A28",
    "overlay": "#000000",
    "skeleton": "#1A2431",
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

    def hero(self, weight: str = "bold") -> tkfont.Font:
        """Название панели в шапке, 24 px."""
        return self.font(TYPE["hero"], weight)

    def value(self, weight: str = "bold") -> tkfont.Font:
        """Крупное число плитки (51 мс, 83%) — semibold, 27 px.

        Tk знает только normal/bold; bold у Segoe UI — ближайшее к semibold,
        цифры при этом остаются табличными (tabular figures шрифта).
        """
        return self.font(TYPE["value"], weight)


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
