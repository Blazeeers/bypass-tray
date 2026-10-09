"""Точка входа для сборки PyInstaller.

PyInstaller не любит пакетные ``__main__.py`` как entry-point, поэтому здесь
тонкая обёртка: импортирует настоящий ``main`` и вызывает его.
"""

from __future__ import annotations

import sys

from bypass_tray.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
