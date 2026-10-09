"""Готовит .ico для сборки .exe из того же рисунка, что рисует приложение.

Запуск (нужен Pillow):  python packaging/make_icon.py
Результат:              packaging/bypass-tray.ico
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import icons  # noqa: E402

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    target = ROOT / "packaging" / "bypass-tray.ico"
    image = icons.make_app_icon(256)
    image.save(target, format="ICO", sizes=SIZES)
    print(f"Иконка записана: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
