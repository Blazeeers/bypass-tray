"""Собирает packaging/bypass-tray.ico из иконки приложения."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import icons  # noqa: E402


def main() -> int:
    source = icons.ensure_ico()
    target = Path(__file__).resolve().parent / "bypass-tray.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(target)
    print(f"{target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
