"""Точка входа PyInstaller: без консоли, только GUI-трей."""

from __future__ import annotations

import sys

from bypass_tray.__main__ import main


if __name__ == "__main__":
    sys.exit(main())
