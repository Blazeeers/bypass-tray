"""Запуск: python -m bypass_tray [--status | --sweep-strategies]

`--sweep-strategies` — служебный режим: перебор стратегий zapret. Требует прав
администратора и запускается приложением отдельным процессом с UAC.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(prog="bypass-tray")
    parser.add_argument("--status", action="store_true",
                        help="напечатать текущий статус в JSON и выйти (для тестов)")
    parser.add_argument("--sweep-strategies", action="store_true",
                        help="перебрать general*.bat и поставить лучшую (нужен админ)")
    parser.add_argument("--out", default="",
                        help="куда записать отчёт о переборе (JSON)")
    parser.add_argument("--report", default="",
                        help="дополнительный файл с ходом перебора")
    parser.add_argument("--bats", default="",
                        help="список стратегий через ';' (по умолчанию — все general*.bat)")
    parser.add_argument("--happ-watchdog", action="store_true",
                        help="сторож: вернуть настройки Happ, если их не подтвердят")
    parser.add_argument("--seconds", type=float, default=150.0,
                        help="сколько секунд ждать подтверждения (сторож)")
    parser.add_argument("--confirm", default="",
                        help="файл-подтверждение для сторожа")
    args = parser.parse_args()

    from . import config, status

    if args.happ_watchdog:
        from . import happ

        return happ.run_watchdog(args.seconds, args.confirm)

    if args.status:
        print(json.dumps(status.collect(config.load_config()), ensure_ascii=False, indent=2))
        return 0

    if args.sweep_strategies:
        from . import strategy

        cfg = config.load_config()
        zapret_dir = Path(cfg.get("zapret_dir") or "")
        out = Path(args.out) if args.out else Path(config.config_dir() / "sweep-result.json")
        names = [n for n in args.bats.split(";") if n] or None
        report_path = Path(args.report) if args.report else None
        progress_state: dict = {}

        def write_progress() -> None:
            if report_path is None:
                return
            try:
                report_path.write_text(
                    json.dumps(progress_state, ensure_ascii=False), encoding="utf-8"
                )
            except OSError:
                pass

        def log(message: str) -> None:
            print(message, flush=True)
            progress_state["message"] = message
            progress_state["at"] = time.time()
            write_progress()

        def progress(payload: dict) -> None:
            progress_state.update(payload)
            progress_state["at"] = time.time()
            write_progress()

        if not zapret_dir.is_dir():
            print(f"Папка zapret не найдена: {zapret_dir}", flush=True)
            return 2
        if not strategy.actions.is_admin():
            print("Требуются права администратора.", flush=True)
            return 3

        report = strategy.sweep(
            zapret_dir,
            names or strategy.default_bats(zapret_dir),
            log=log,
            progress=progress,
        )
        strategy.write_report(report, out)
        progress_state["message"] = "Готово"
        progress_state["done"] = True
        write_progress()
        return 0

    from .app import TrayApp

    TrayApp().start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
