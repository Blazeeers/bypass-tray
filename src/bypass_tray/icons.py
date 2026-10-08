"""Отрисовка иконок трея.

Иконки рисуются кодом (Pillow) в повышенном разрешении и уменьшаются
с сглаживанием, поэтому выглядят чётко на любом DPI.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

#: Коды состояний иконки: буква либо «галочка».
GLYPHS = {
    "happ": "H",
    "zapret": "Z",
    "tgws": "T",
    "update": "U",
    "ok": None,   # рисуется галочкой
    "idle": "…",
}

_FONT_CANDIDATES = ("segoeuib.ttf", "segoeui.ttf", "arialbd.ttf", "arial.ttf")
_SCALE = 4  # рисуем крупно и уменьшаем — даёт сглаживание


def _font_path() -> str | None:
    if not sys.platform.startswith("win"):
        return None
    fonts = Path(r"C:\Windows\Fonts")
    for name in _FONT_CANDIDATES:
        candidate = fonts / name
        if candidate.is_file():
            return str(candidate)
    return None


def _load_font(size: int) -> ImageFont.ImageFont:
    path = _font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _draw_check(draw: ImageDraw.ImageDraw, size: int, color: str, width: int) -> None:
    """Галочка по центру квадрата ``size``."""
    draw.line(
        [(size * 0.28, size * 0.52), (size * 0.44, size * 0.68), (size * 0.74, size * 0.34)],
        fill=color,
        width=width,
        joint="curve",
    )


def make_icon(state: str, color: str, size: int = 64) -> Image.Image:
    """Иконка состояния: цветной кружок с буквой или галочкой.

    ``state`` — ключ из :data:`GLYPHS`; ``color`` — цвет заливки.
    Рисуется с полем прозрачности, чтобы смотреться на любой панели задач.
    """
    big = size * _SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = big * 0.06
    # Мягкая подложка — тонкое кольцо вокруг основного круга.
    draw.ellipse(
        (margin * 0.35, margin * 0.35, big - margin * 0.35, big - margin * 0.35),
        fill=color + "55",
    )
    draw.ellipse((margin, margin, big - margin, big - margin), fill=color)

    glyph = GLYPHS.get(state, "?")
    if glyph is None:
        _draw_check(draw, big, "#FFFFFF", width=max(2, int(big * 0.085)))
    else:
        font = _load_font(int(big * 0.56))
        box = draw.textbbox((0, 0), glyph, font=font)
        draw.text(
            ((big - (box[2] - box[0])) / 2 - box[0], (big - (box[3] - box[1])) / 2 - box[1]),
            glyph,
            font=font,
            fill="#FFFFFF",
        )

    return image.resize((size, size), Image.LANCZOS)


def make_app_icon(size: int = 64) -> Image.Image:
    """Иконка приложения: щит с галочкой (для окна панели)."""
    big = size * _SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = big * 0.07
    draw.rounded_rectangle(
        (margin, margin, big - margin, big - margin),
        radius=big * 0.24,
        fill="#2F6FED",
    )
    _draw_check(draw, big, "#FFFFFF", width=max(2, int(big * 0.09)))
    return image.resize((size, size), Image.LANCZOS)
