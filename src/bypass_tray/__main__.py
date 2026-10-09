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


def enable_autostart() -> bool:
    """Кладёт ярлык на run.bat в автозапуск (права администратора не нужны)."""
    import os
    import subprocess
    from pathlib import Path

    project = Path(__file__).resolve().parents[2]
    run_bat = project / "run.bat"
    if not run_bat.is_file():
        return False
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" \
        / "Programs" / "Startup"
    if not startup.is_dir():
        return False
    target = startup / "bypass-tray.lnk"
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        + repr(str(target)).replace("'", '"') + ");"
        "$s.TargetPath = " + repr(str(run_bat)).replace("'", '"') + ";"
        "$s.WorkingDirectory = " + repr(str(project)).replace("'", '"') + ";"
        "$s.Description = 'bypass-tray';$s.Save()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=30)
        return target.is_file()
    except Exception:  # noqa: BLE001
        return False


def run_setup(autostart: bool = True) -> int:
    """Установка в один шаг: компоненты, пути, автозапуск, запуск виджета."""
    from . import components, config

    print("=" * 62)
    print("bypass-tray: установка")
    print("=" * 62)

    cfg = config.load_config()
    print("\nСкачиваю компоненты обхода из официальных релизов…")
    result = components.install_all(
        lambda stage, text, percent: print(f"  {text}", flush=True))
    state = result.get("components") or {}

    zapret = state.get("zapret") or {}
    tgws = state.get("tgws") or {}
    if zapret.get("ok"):
        cfg["zapret_dir"] = zapret["dir"]
        print(f"  zapret {zapret.get('version')}: {zapret['dir']}")
    else:
        print(f"  zapret: не установлен ({zapret.get('error')})")
    if tgws.get("ok"):
        cfg["tgws_exe"] = tgws["exe"]
        print(f"  tg-ws-proxy {tgws.get('version')}: {tgws['exe']}")
    else:
        print(f"  tg-ws-proxy: не установлен ({tgws.get('error')})")
    config.save_config(cfg)

    if autostart:
        ok = enable_autostart()
        print("\nАвтозапуск: " + ("включён" if ok else "не удалось включить"))

    print("\nЗапускаю виджет — он появится в трее.")
    print("Ссылку на подписку VPN укажите в «Настройки» → «VPN».")
    print("=" * 62)

    from .app import TrayApp

    TrayApp().start()
    return 0


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
    parser.add_argument("--setup", action="store_true",
                        help="установить zapret и tg-ws-proxy, включить автозапуск и запустить виджет")
    parser.add_argument("--no-autostart", action="store_true",
                        help="при --setup не прописывать автозапуск")
    args = parser.parse_args()

    from . import config, status

    if args.setup:
        return run_setup(autostart=not args.no_autostart)

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
