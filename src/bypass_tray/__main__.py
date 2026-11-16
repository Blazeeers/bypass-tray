"""Запуск: python -m bypass_tray [--status | --sweep-strategies]

`--sweep-strategies` — служебный режим: перебор стратегий zapret. Требует прав
администратора и запускается приложением отдельным процессом с UAC.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _print_json(payload: dict) -> None:
    """Печатает JSON для диагностики.

    При перенаправлении вывода Python берёт локальную кодировку (cp1251 на
    русской Windows), а в detail встречаются символы вроде «·». Поэтому stdout
    переключается на UTF-8: на настоящей консоли это ничего не меняет,
    а в файле/пайпе JSON остаётся читаемым и печать не падает.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    from .secrets import redact_obj

    payload = redact_obj(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def enable_autostart() -> bool:
    """Кладёт ярлык на run.bat в автозапуск (обёртка над autostart.enable)."""
    from . import autostart

    return autostart.enable()


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

    # Без записанных версий check_all не может понять, что устарело.
    components.remember_install(result)

    if autostart:
        ok = enable_autostart()
        print("\nАвтозапуск: " + ("включён" if ok else "не удалось включить"))

    print("\nЗапускаю виджет — он появится в трее.")
    print("Ссылку на подписку VPN укажите в «Настройки» → «VPN».")
    print("=" * 62)

    return _run_tray()


def _acquire_single_instance(kernel32=None, user32=None) -> tuple[object | None, bool]:
    """Мьютекс — вспомогательный признак; окончательно решает PID-файл.

    ``first=False`` значит лишь, что объект с таким именем существует. Это
    может быть и настоящий второй экземпляр, и «залипший» handle, унаследованный
    дочерним процессом. Поэтому main() сверяет PID-файл, а MessageBox здесь не
    показывается — второй экземпляр выходит тихо.
    """
    if sys.platform != "win32":
        return None, True
    try:
        import ctypes
        from ctypes import wintypes

        if kernel32 is None:
            # use_last_error=True: иначе GetLastError() может вернуть чужой
            # код от предыдущего вызова WinAPI.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            injected = False
        else:
            injected = True
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.SetLastError(0)
        # lpMutexAttributes=NULL: handle не наследуется дочерними процессами
        # (в том числе парой «venv-лаунчер → базовый pythonw»).
        # Имя v2: у старого имени мог остаться унаследованный handle от
        # предыдущих версий («залипший» мьютекс), новый объект стартует чисто.
        handle = kernel32.CreateMutexW(None, False,
                                       "Local\\Blazeeers.BypassTray.v2")
        if not handle:
            # Провал создания — не признак занятости: не блокируем запуск.
            return None, True
        error = (kernel32.GetLastError() if injected
                 else ctypes.get_last_error())
        if error == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return None, False
        return handle, True
    except Exception:  # noqa: BLE001
        return None, True


def _release_single_instance(handle) -> None:
    if not handle or sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:  # noqa: BLE001
        pass


def _tray_pid_path() -> Path:
    from . import config

    return config.config_dir() / "tray.pid"


def _read_tray_pid() -> int | None:
    try:
        return int(_tray_pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _tray_pid_alive(pid: int | None) -> bool:
    """PID-файл указывает на живой процесс именно трея."""
    if not pid:
        return False
    try:
        import psutil

        cmdline = " ".join(psutil.Process(pid).cmdline() or []).lower()
        # В исходниках — «-m bypass_tray», в PyInstaller-сборке — имя exe
        # («Обходы.exe»). Имя exe проверяем только для сборки, иначе под него
        # попадёт любой чужой pythonw с тем же PID.
        exe = Path(sys.executable).name.lower()
        frozen = bool(getattr(sys, "frozen", False))
        return "bypass_tray" in cmdline or (frozen and bool(exe) and exe in cmdline)
    except Exception:  # noqa: BLE001
        return False


def _write_tray_pid(pid: int) -> None:
    from . import config

    config.atomic_write(_tray_pid_path(), str(pid))


def _clear_tray_pid(pid: int) -> None:
    path = _tray_pid_path()
    try:
        if path.read_text(encoding="utf-8").strip() == str(pid):
            path.unlink()
    except OSError:
        pass


def _run_tray() -> int:
    """Запускает GUI-трей, не давая стартовать второму живому экземпляру."""
    from .app import TrayApp

    pid = _read_tray_pid()
    if _tray_pid_alive(pid):
        return 0
    mutex, first = _acquire_single_instance()
    if not first:
        # Настоящий второй экземпляр (или честный ERROR_ALREADY_EXISTS):
        # выходим тихо, без MessageBox.
        return 0
    _write_tray_pid(os.getpid())
    try:
        TrayApp().start()
    finally:
        _clear_tray_pid(os.getpid())
        _release_single_instance(mutex)
    return 0


def _set_app_user_model_id() -> None:
    """Свой AppUserModelID: иконка и группа панели задач Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Blazeeers.BypassTray")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    # console=False: sys.stdout/stderr = None, и первый print() упал бы.
    from . import runtime

    runtime.ensure_streams()
    # DPI-awareness пока НЕ включаем: панель сверстана в пиксельных константах
    # (WINDOW_W, PAD, BODY_RATIO), и при 125-200% интерфейс стал бы мельче и
    # теснее, чем растянутый Windows'ом. Нужна отдельная задача со scaling.
    # runtime.enable_dpi_awareness()
    _set_app_user_model_id()
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
    parser.add_argument("--setup", action="store_true",
                        help="установить zapret и tg-ws-proxy, включить автозапуск и запустить виджет")
    parser.add_argument("--export", default="", metavar="PATH",
                        help="экспорт настроек в JSON; серверы (секреты) — только с --with-secrets")
    parser.add_argument("--with-secrets", action="store_true",
                        help="при --export включить в файл серверы (ключи доступа)")
    parser.add_argument("--import", dest="import_path", default="", metavar="PATH",
                        help="импорт настроек из JSON с резервной копией текущего конфига")
    parser.add_argument("--no-autostart", action="store_true",
                        help="при --setup не прописывать автозапуск")
    parser.add_argument("--health", action="store_true",
                        help="напечатать JSON проверки сервисов и выйти")
    parser.add_argument("--check", default="", metavar="TARGET",
                        help="разовая проверка адреса (URL или host:port): "
                             "JSON и код 0/1")
    parser.add_argument("--diagnose", action="store_true",
                        help="напечатать диагноз по последнему отчёту проверки и выйти")
    parser.add_argument("--history", action="store_true",
                        help="напечатать стабильность за сутки из истории здоровья и выйти")
    parser.add_argument("--route-audit", action="store_true",
                        help="напечатать JSON аудита маршрутов доменов и выйти")
    parser.add_argument("--apply", action="store_true",
                        help="при --route-audit применить найденные маршруты")
    parser.add_argument("--routes-plan", action="store_true",
                        help="напечатать JSON сухого прогона маршрутов и выйти")
    parser.add_argument("--routes-rollback", action="store_true",
                        help="откатить последнее изменение маршрутов и напечатать JSON")
    parser.add_argument("--updates", action="store_true",
                        help="напечатать JSON проверки обновлений и выйти")
    args = parser.parse_args()

    from . import config, status

    if args.setup:
        return run_setup(autostart=not args.no_autostart)

    if args.export:
        from . import transfer

        try:
            result = transfer.export_settings(
                config.load_config(), args.export,
                include_secrets=args.with_secrets)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _print_json(result)
        return 0 if result.get("ok") else 1

    if args.import_path:
        from . import transfer

        try:
            result = transfer.import_settings(args.import_path)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _print_json(result)
        return 0 if result.get("ok") else 1

    if args.check:
        from . import health

        try:
            result = health.check_target(args.check, config.load_config())
        except Exception as exc:  # noqa: BLE001
            result = {"id": "check", "title": args.check, "ok": False,
                      "state": "bad", "ms": 0,
                      "detail": f"{type(exc).__name__}: {exc}"}
        _print_json(result)
        return 0 if result.get("ok") else 1

    if args.health:
        from . import health, xray

        cfg = config.load_config()
        state = config.load_state()
        try:
            result = health.collect(
                cfg, status.collect(cfg), xray.status(),
                zapret_off=bool((state or {}).get("zapret_user_off")))
        except Exception as exc:  # noqa: BLE001
            result = {"checked": time.time(), "items": [], "verdict": "warn",
                      "error": f"{type(exc).__name__}: {exc}"}
        _print_json(result)
        return 0

    if args.diagnose:
        # Без сети: диагноз строится по последнему отчёту, как в панели и трее.
        from . import diagnose, health, history, xray

        cfg = config.load_config()
        try:
            result = diagnose.diagnose(
                health.load_cache(cfg) or {}, xray.status(), cfg,
                history.summary())
        except Exception as exc:  # noqa: BLE001
            result = {"code": "error", "level": "warn",
                      "title": "Диагноз не удался", "action": None,
                      "facts": [f"error: {type(exc).__name__}"]}
        _print_json(result)
        return 0

    if args.history:
        from . import history

        _print_json(history.summary())
        return 0

    if args.routes_plan:
        from . import routes

        cfg = config.load_config()
        try:
            result = routes.plan(cfg)
        except Exception as exc:  # noqa: BLE001
            result = {"at": time.time(), "changes": 0,
                      "error": f"{type(exc).__name__}: {exc}"}
        _print_json(result)
        return 0

    if args.routes_rollback:
        from . import routes

        cfg = config.load_config()
        try:
            result = routes.rollback_last(cfg)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _print_json(result)
        return 0 if result.get("ok") else 1

    if args.route_audit:
        from . import routes

        cfg = config.load_config()
        try:
            result = routes.audit(cfg)
            if args.apply:
                routes.apply_audit(result, cfg)
        except Exception as exc:  # noqa: BLE001
            result = {"at": time.time(), "proxy": False, "items": [], "broken": [],
                      "error": f"{type(exc).__name__}: {exc}"}
        _print_json(result)
        return 0

    if args.updates:
        from . import updates

        cfg = config.load_config()
        state = config.load_state()
        try:
            result = updates.check_all(cfg, state)
        except Exception as exc:  # noqa: BLE001
            result = {"checked": time.time(), "components": {}, "outdated": [],
                      "error": f"{type(exc).__name__}: {exc}"}
        config.save_state(state)
        _print_json(result)
        return 0

    if args.status:
        try:
            result = status.collect(config.load_config())
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{type(exc).__name__}: {exc}"}
        _print_json(result)
        # Ненулевой код — сигнал скриптам, что статус собрать не удалось.
        return 1 if result.get("error") else 0

    if args.sweep_strategies:
        from . import strategy

        cfg = config.load_config()
        zapret_raw = str(cfg.get("zapret_dir") or "").strip()
        if not zapret_raw:
            print("Папка zapret не задана в конфиге.", flush=True)
            return 2
        zapret_dir = Path(zapret_raw)
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

    return _run_tray()


if __name__ == "__main__":
    sys.exit(main())
