"""Иконки трея и панели.

Рисуются кодом (Pillow) в 4× разрешении и уменьшаются с сглаживанием, поэтому
выглядят чётко на любом DPI. Иконки панели прозрачные (цвет задаёт вызывающий),
иконки трея — цветной кружок с белым глифом.
"""

from __future__ import annotations

import io
import math
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

#: Вид глифа внутри кружка трея для каждого состояния.
STATE_GLYPHS = {
    "ok": "check",
    "warn": "alert",
    "bad": "cross",
    "busy": "busy",
    "off": "shield",
    "shield": "shield",
    "update": "down",
    # Трёхцветный уровень: degraded и down обязаны отличаться пикселями.
    "degraded": "alert",
    "down": "cross",
    # Совместимость со старыми ключами иконки трея.
    "zapret": "shield",
    "tgws": "letter:T",
    "proxy": "letter:X",
    "idle": "clock",
}

_FONT_CANDIDATES = ("segoeuib.ttf", "segoeui.ttf", "arialbd.ttf", "arial.ttf")
_SCALE = 4  # рисуем крупно и уменьшаем — даёт сглаживание
_WHITE = "#FFFFFF"

#: Щит в нормализованных координатах (0…1).
_SHIELD = [(0.5, 0.04), (0.88, 0.18), (0.88, 0.5), (0.5, 0.96),
           (0.12, 0.5), (0.12, 0.18)]


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


def _points(big: int, coords, scale: float = 1.0) -> list[tuple[float, float]]:
    """Нормализованные точки → пиксели, со сжатием к центру."""
    result = []
    for x, y in coords:
        result.append((
            (0.5 + (x - 0.5) * scale) * big,
            (0.5 + (y - 0.5) * scale) * big,
        ))
    return result


def _draw_check(draw: ImageDraw.ImageDraw, big: int, color: str, width: int,
                scale: float = 1.0) -> None:
    draw.line(_points(big, [(0.28, 0.52), (0.44, 0.68), (0.74, 0.34)], scale),
              fill=color, width=width, joint="curve")


def _draw_shield(draw: ImageDraw.ImageDraw, big: int, color: str,
                 scale: float = 1.0) -> None:
    draw.polygon(_points(big, _SHIELD, scale), fill=color)


# ---- глифы иконки трея ----------------------------------------------------


def _draw_state_glyph(draw: ImageDraw.ImageDraw, big: int, kind: str,
                      width: int) -> None:
    if kind == "check":
        _draw_check(draw, big, _WHITE, width, scale=0.86)
    elif kind == "alert":
        draw.line(_points(big, [(0.5, 0.24), (0.5, 0.6)], 0.72),
                  fill=_WHITE, width=width)
        dot = 0.045 * big
        cx, cy = 0.5 * big, 0.73 * big
        draw.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=_WHITE)
    elif kind == "cross":
        # Икс вместо «!»: сбой должен читаться иначе, чем «внимание».
        draw.line(_points(big, [(0.3, 0.3), (0.7, 0.7)], 0.86),
                  fill=_WHITE, width=width)
        draw.line(_points(big, [(0.7, 0.3), (0.3, 0.7)], 0.86),
                  fill=_WHITE, width=width)
    elif kind == "clock":
        box = (big * 0.2, big * 0.2, big * 0.8, big * 0.8)
        draw.ellipse(box, outline=_WHITE, width=width)
        draw.line((big * 0.5, big * 0.5, big * 0.5, big * 0.32),
                  fill=_WHITE, width=width)
        draw.line((big * 0.5, big * 0.5, big * 0.63, big * 0.6),
                  fill=_WHITE, width=width)
    elif kind == "shield":
        _draw_shield(draw, big, _WHITE, scale=0.62)
    elif kind == "down":
        draw.rectangle((big * 0.42, big * 0.18, big * 0.58, big * 0.56), fill=_WHITE)
        draw.polygon(_points(big, [(0.3, 0.5), (0.7, 0.5), (0.5, 0.8)], 0.86),
                     fill=_WHITE)
    elif kind == "busy":
        box = (big * 0.22, big * 0.22, big * 0.78, big * 0.78)
        draw.arc(box, 200, 340, fill=_WHITE, width=width)
        draw.arc(box, 20, 160, fill=_WHITE, width=width)
        draw.polygon(_points(big, [(0.62, 0.14), (0.82, 0.24), (0.6, 0.34)], 0.86),
                     fill=_WHITE)
        draw.polygon(_points(big, [(0.38, 0.86), (0.18, 0.76), (0.4, 0.66)], 0.86),
                     fill=_WHITE)
    elif kind.startswith("letter:"):
        glyph = kind.split(":", 1)[1]
        font = _load_font(int(big * 0.56))
        box = draw.textbbox((0, 0), glyph, font=font)
        draw.text(
            ((big - (box[2] - box[0])) / 2 - box[0],
             (big - (box[3] - box[1])) / 2 - box[1]),
            glyph, font=font, fill=_WHITE,
        )


def make_icon(state: str, color: str, size: int = 64) -> Image.Image:
    """Иконка состояния для трея: цветной кружок с белым глифом."""
    big = size * _SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = big * 0.06
    draw.ellipse((margin * 0.35, margin * 0.35, big - margin * 0.35, big - margin * 0.35),
                 fill=color + "55")
    draw.ellipse((margin, margin, big - margin, big - margin), fill=color)
    _draw_state_glyph(draw, big, STATE_GLYPHS.get(state, "letter:?"),
                      width=max(2, int(big * 0.075)))
    return image.resize((size, size), Image.LANCZOS)


# ---- иконки панели --------------------------------------------------------


def _gear_contour(center: float, radius: float, teeth: int = 8,
                  root: float = 0.74) -> list[tuple[float, float]]:
    """Замкнутый зубчатый контур шестерёнки.

    Восемь зубцов: площадка на внешнем радиусе, скосы и дуга впадины на
    ``root`` от внешнего радиуса. Раньше шестерёнка рисовалась кругом с
    восемью лучами — при 20 px это читалось как солнце.
    """
    points: list[tuple[float, float]] = []
    sector = 2 * math.pi / teeth
    for index in range(teeth):
        start = sector * index
        # Впадина → левый скос → площадка зуба → правый скос.
        for offset, scale in ((0.0, root), (0.16, 1.0), (0.40, 1.0),
                              (0.56, root)):
            angle = start + sector * offset
            points.append((center + radius * scale * math.cos(angle),
                           center + radius * scale * math.sin(angle)))
        # Дуга впадины до начала следующего зуба.
        for step in range(1, 3):
            angle = start + sector * (0.56 + 0.44 * step / 2)
            points.append((center + radius * root * math.cos(angle),
                           center + radius * root * math.sin(angle)))
    return points


def _panel_gear(draw: ImageDraw.ImageDraw, big: int, color: str, width: int) -> None:
    """Шестерёнка: зубчатый контур и круглое отверстие по центру.

    Штрих здесь свой, а не общий ``width``: макет просит 2 px при 20 px, то
    есть ровно десятую часть размера. Общая толщина (8.5 %) на этом контуре
    с восемью зубцами сливала бы впадины.
    """
    stroke = max(2, round(big * 0.10))
    center = big / 2
    contour = _gear_contour(center, big * 0.46)
    draw.line(contour + [contour[0]], fill=color, width=stroke, joint="curve")
    hole = big * 0.17
    draw.ellipse((center - hole, center - hole, center + hole, center + hole),
                 outline=color, width=stroke)


def _panel_refresh(draw: ImageDraw.ImageDraw, big: int, color: str, width: int) -> None:
    draw.arc((big * 0.16, big * 0.16, big * 0.84, big * 0.84),
             start=35, end=300, fill=color, width=width)
    draw.polygon(_points(big, [(0.58, 0.05), (0.92, 0.12), (0.78, 0.4)]), fill=color)


def panel_icon(name: str, color: str, size: int = 20) -> Image.Image:
    """Иконка панели на прозрачном фоне: ``name`` из списка ниже.

    shield, zap, youtube, discord, spark, telegram, update, check, warning,
    gear, chevron, clock, refresh, moon, sun, list, route, download.
    """
    big = size * _SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    width = max(2, int(big * 0.085))

    if name == "shield":
        _draw_shield(draw, big, color, scale=0.94)
    elif name == "zap":
        draw.polygon(_points(big, [(0.58, 0.03), (0.22, 0.56), (0.46, 0.56),
                                   (0.4, 0.97), (0.8, 0.42), (0.54, 0.42)]),
                     fill=color)
    elif name == "youtube":
        draw.rounded_rectangle((big * 0.05, big * 0.2, big * 0.95, big * 0.8),
                               radius=big * 0.18, outline=color, width=width)
        draw.polygon(_points(big, [(0.42, 0.34), (0.42, 0.66), (0.68, 0.5)]),
                     fill=color)
    elif name == "discord":
        # Морда-геймпад: скруглённый корпус, два «ушка» сверху и два глаза.
        mask = Image.new("L", (big, big), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle((big * 0.05, big * 0.24, big * 0.95, big * 0.86),
                                    radius=big * 0.26, fill=255)
        for cx in (0.23, 0.77):
            r = 0.12
            mask_draw.ellipse((big * (cx - r), big * (0.14 - r),
                               big * (cx + r), big * (0.14 + r)), fill=255)
        for cx in (0.34, 0.66):
            r = 0.062
            mask_draw.ellipse((big * (cx - r), big * (0.5 - r),
                               big * (cx + r), big * (0.5 + r)), fill=0)
        image.paste(Image.new("RGBA", (big, big), color), (0, 0), mask)
    elif name == "spark":
        # Четырёхлучевая звезда с вогнутыми сторонами.
        draw.polygon(_points(big, [(0.5, 0.02), (0.565, 0.435), (0.98, 0.5),
                                   (0.565, 0.565), (0.5, 0.98), (0.435, 0.565),
                                   (0.02, 0.5), (0.435, 0.435)]), fill=color)
    elif name == "telegram":
        draw.polygon(_points(big, [(0.05, 0.47), (0.95, 0.1), (0.72, 0.92),
                                   (0.47, 0.6), (0.3, 0.76)]), fill=color)
    elif name == "update":
        draw.rectangle((big * 0.42, big * 0.08, big * 0.58, big * 0.56), fill=color)
        draw.polygon(_points(big, [(0.24, 0.5), (0.76, 0.5), (0.5, 0.92)]), fill=color)
    elif name == "check":
        _draw_check(draw, big, color, max(2, int(width * 1.2)), scale=0.95)
    elif name == "warning":
        draw.polygon(_points(big, [(0.5, 0.08), (0.94, 0.9), (0.06, 0.9)]),
                     outline=color, width=width)
        draw.line((big * 0.5, big * 0.38, big * 0.5, big * 0.62),
                  fill=color, width=width)
        r = 0.045 * big
        draw.ellipse((big * 0.5 - r, big * 0.72 - r, big * 0.5 + r, big * 0.72 + r),
                     fill=color)
    elif name == "gear":
        _panel_gear(draw, big, color, width)
    elif name == "chevron":
        draw.line(_points(big, [(0.38, 0.22), (0.68, 0.5), (0.38, 0.78)]),
                  fill=color, width=max(2, int(width * 1.2)), joint="curve")
    elif name == "chevron_down":
        draw.line(_points(big, [(0.22, 0.38), (0.5, 0.68), (0.78, 0.38)]),
                  fill=color, width=max(2, int(width * 1.2)), joint="curve")
    elif name == "chevron_left":
        draw.line(_points(big, [(0.7, 0.22), (0.4, 0.5), (0.7, 0.78)]),
                  fill=color, width=max(2, int(width * 1.2)), joint="curve")
    elif name == "clock":
        draw.ellipse((big * 0.1, big * 0.1, big * 0.9, big * 0.9),
                     outline=color, width=width)
        draw.line((big * 0.5, big * 0.5, big * 0.5, big * 0.28),
                  fill=color, width=width)
        draw.line((big * 0.5, big * 0.5, big * 0.66, big * 0.6),
                  fill=color, width=width)
    elif name == "refresh":
        _panel_refresh(draw, big, color, width)
    elif name == "moon":
        # Полумесяц: круг минус смещённый круг (альфа-маска Tk не умеет).
        mask = Image.new("L", (big, big), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((big * 0.10, big * 0.08, big * 0.90, big * 0.88), fill=255)
        mask_draw.ellipse((big * 0.36, big * 0.00, big * 1.16, big * 0.74), fill=0)
        image.paste(Image.new("RGBA", (big, big), color), (0, 0), mask)
    elif name == "sun":
        radius = 0.17
        draw.ellipse((big * (0.5 - radius), big * (0.5 - radius),
                      big * (0.5 + radius), big * (0.5 + radius)),
                     outline=color, width=width)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0),
                       (0.7, -0.7), (0.7, 0.7), (-0.7, -0.7), (-0.7, 0.7)):
            draw.line((big * (0.5 + dx * 0.29), big * (0.5 + dy * 0.29),
                       big * (0.5 + dx * 0.44), big * (0.5 + dy * 0.44)),
                      fill=color, width=width)
    elif name == "list":
        for row in (0.26, 0.5, 0.74):
            draw.ellipse((big * 0.10, big * (row - 0.05),
                          big * 0.20, big * (row + 0.05)), fill=color)
            draw.line((big * 0.30, big * row, big * 0.90, big * row),
                      fill=color, width=width)
    elif name == "route":
        draw.line(_points(big, [(0.18, 0.84), (0.5, 0.20), (0.82, 0.84)]),
                  fill=color, width=width, joint="curve")
        for cx, cy in ((0.18, 0.84), (0.82, 0.84)):
            r = 0.08
            draw.ellipse((big * (cx - r), big * (cy - r),
                          big * (cx + r), big * (cy + r)), fill=color)
    elif name == "download":
        draw.line((big * 0.5, big * 0.10, big * 0.5, big * 0.62),
                  fill=color, width=width)
        draw.line(_points(big, [(0.28, 0.44), (0.5, 0.66), (0.72, 0.44)]),
                  fill=color, width=width, joint="curve")
        draw.line((big * 0.16, big * 0.88, big * 0.84, big * 0.88),
                  fill=color, width=width)
    else:
        draw.ellipse((big * 0.2, big * 0.2, big * 0.8, big * 0.8),
                     outline=color, width=width)
    return image.resize((size, size), Image.LANCZOS)


def make_app_icon(size: int = 64) -> Image.Image:
    """Иконка приложения: белый щит с галочкой на акцентном фоне."""
    big = size * _SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = big * 0.05
    draw.rounded_rectangle((margin, margin, big - margin, big - margin),
                           radius=big * 0.23, fill="#2F6FED")
    _draw_shield(draw, big, _WHITE, scale=0.6)
    draw.line(_points(big, [(0.37, 0.5), (0.47, 0.61), (0.65, 0.4)], 0.6),
              fill="#2F6FED", width=max(2, int(big * 0.05)), joint="curve")
    return image.resize((size, size), Image.LANCZOS)

def ensure_ico(size: int = 256) -> Path:
    """Многоразмерный .ico в %LOCALAPPDATA%\bypass-tray; атомарно.

    Tk ``iconbitmap`` и панель задач Windows читают .ico лучше, чем
    PhotoImage. Файл перезаписывается только если содержимое изменилось.
    """
    image_size = max(256, int(size))
    sizes = [n for n in (16, 32, 48, 64, 128, 256) if n <= image_size]
    buffer = io.BytesIO()
    make_app_icon(image_size).save(
        buffer, format="ICO", sizes=[(n, n) for n in sizes])
    data = buffer.getvalue()
    base = Path(os.environ.get("LOCALAPPDATA")
                or (Path.home() / "AppData" / "Local")) / "bypass-tray"
    path = base / "bypass-tray.ico"
    try:
        if path.is_file() and path.read_bytes() == data:
            return path
    except OSError:
        pass
    base.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return path

