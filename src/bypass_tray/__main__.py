"""Запуск: python -m bypass_tray [--status]"""

from __future__ import annotations

import argparse
import json

from . import config, status
from .app import TrayApp


def main() -> int:
    parser = argparse.ArgumentParser(prog="bypass-tray")
    parser.add_argument("--status", action="store_true",
                        help="напечатать текущий статус в JSON и выйти (для тестов)")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status.collect(config.load_config()), ensure_ascii=False, indent=2))
        return 0
    TrayApp().start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
