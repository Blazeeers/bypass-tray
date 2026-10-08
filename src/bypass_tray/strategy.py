"""Работа со стратегиями zapret: извлечение команды, проверка связи, перебор.

Идея автоподбора: перебрать `general*.bat`, для каждого поднять `winws.exe`,
проверить, открываются ли YouTube и Discord, и установить службу с лучшей
стратегией. Всё это требует прав администратора (остановка/создание службы),
поэтому перебор запускается отдельным процессом с UAC.

Команда `winws` не парсится вручную: рядом с `.bat` кладётся его копия, в
которой `start ... winws.exe` заменён на `echo`, и у запущенного сценария
забирается уже полностью раскрытая строка. Так учитываются переменные
`%BIN%`, `%LISTS%`, `%GameFilterTCP%` и т.п.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import time
from pathlib import Path

from . import actions, status

PROBE_MARKER = "ZAPRET_CMD"
PROBE_PREFIX = "_bypass_probe_"

#: Что считаем признаком рабочего обхода. Контрольный адрес должен открываться
#: всегда — если он не открывается, сеть недоступна и замер недействителен.
TARGETS: tuple[tuple[str, str], ...] = (
    ("youtube", "https://www.youtube.com/"),
    ("discord", "https://discord.com/"),
)
CONTROL: tuple[str, str] = ("control", "https://ya.ru/")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


# ---- извлечение команды ---------------------------------------------------


def extract_command(bat_path: Path, timeout: int = 40) -> str:
    """Возвращает раскрытую командную строку ``winws.exe`` из сценария.

    Требует права на запись в папку zapret (копия кладётся рядом, иначе
    ``%~dp0`` в сценарии укажет не туда и пути в аргументах будут неверными).
    """
    bat_path = Path(bat_path)
    if not bat_path.is_file():
        return ""
    try:
        text = bat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    # `start "заголовок" /min "..."` → `echo ZAPRET_CMD "..."`.
    probe_text, replaced = re.subn(
        r'(?mi)^(\s*)start\s+"[^"]*"\s*(?:/min\s*)?',
        rf'\1echo {PROBE_MARKER} ',
        text,
        count=1,
    )
    if not replaced:
        return ""

    probe_path = bat_path.parent / f"{PROBE_PREFIX}{bat_path.stem}.bat"
    try:
        probe_path.write_text(probe_text, encoding="utf-8")
    except OSError:
        return ""
    try:
        completed = subprocess.run(
            ["cmd.exe", "/c", str(probe_path)],
            capture_output=True, timeout=timeout, cwd=str(bat_path.parent),
            stdin=subprocess.DEVNULL, creationflags=actions.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        try:
            probe_path.unlink()
        except OSError:
            pass

    stdout = actions._decode(completed.stdout)
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(PROBE_MARKER):
            command = line[len(PROBE_MARKER):].strip()
            if "winws.exe" in command.lower():
                return command
    return ""


def split_command(command: str) -> tuple[str, str]:
    """Делит строку на путь к exe и остаток аргументов (в исходном виде)."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end == -1:
            return command, ""
        return command[1:end], command[end + 1:].strip()
    parts = command.split(" ", 1)
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


# ---- проверка связи -------------------------------------------------------


def probe_url(url: str, timeout: float = 6.0) -> dict:
    """Открывается ли адрес за отведённое время."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    path = parts.path or "/"
    started = time.perf_counter()
    context = ssl.create_default_context()
    connection = None
    try:
        connection = http.client.HTTPSConnection(host, timeout=timeout, context=context)
        connection.request("GET", path, headers={"User-Agent": USER_AGENT})
        response = connection.getresponse()
        response.read(2048)
        elapsed = (time.perf_counter() - started) * 1000
        ok = 200 <= response.status < 400
        return {"ok": ok, "status": response.status, "ms": round(elapsed)}
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - started) * 1000
        return {"ok": False, "status": 0, "ms": round(elapsed), "error": type(exc).__name__}
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass


def measure(timeout: float = 6.0) -> dict:
    """Проверяет целевые адреса и контрольный."""
    targets = {name: probe_url(url, timeout) for name, url in TARGETS}
    control_name, control_url = CONTROL
    control = probe_url(control_url, timeout)
    score = sum(1 for result in targets.values() if result["ok"])
    return {
        "targets": targets,
        "control": {control_name: control},
        "score": score,
        "total": len(targets),
        "control_ok": control["ok"],
        "working": score == len(targets) and control["ok"],
    }


# ---- установка службы -----------------------------------------------------


def install_service(command: str, name: str = status.ZAPRET_SERVICE) -> bool:
    """Прописывает команду в службу zapret (создаёт или перенастраивает)."""
    exists = actions.service_state(name) in ("running", "stopped")
    verb = "config" if exists else "create"
    args = ["sc.exe", verb, name, "binPath=", command]
    if not exists:
        args += ["DisplayName=", "zapret", "start=", "auto"]
    if actions.is_admin():
        completed = subprocess.run(
            args, capture_output=True, timeout=30, creationflags=actions.CREATE_NO_WINDOW
        )
        return completed.returncode == 0
    return actions.run_elevated(args)


# ---- перебор стратегий ----------------------------------------------------


def _kill_winws() -> None:
    actions.kill_process("winws")
    time.sleep(0.6)


def _launch(command: str, zapret_dir: Path) -> subprocess.Popen | None:
    exe, _ = split_command(command)
    cwd = Path(exe).parent if exe else zapret_dir
    try:
        return subprocess.Popen(
            command, cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=actions.CREATE_NO_WINDOW,
        )
    except OSError:
        return None


def sweep(
    zapret_dir: Path,
    bats: list[str],
    *,
    wait: float = 3.5,
    timeout: float = 6.0,
    log=print,
    progress=None,
    install_best: bool = True,
) -> dict:
    """Перебирает стратегии и ставит лучшую.

    Возвращает отчёт; `restored` показывает, удалось ли вернуть обход в работу.
    """
    zapret_dir = Path(zapret_dir)
    original = status.service_image_path()
    report: dict = {
        "started": time.time(),
        "original_command": original,
        "results": [],
        "baseline": None,
        "best": "",
        "installed": False,
        "restored": False,
    }

    def emit(message: str) -> None:
        log(message)

    def advance(index: int, total: int, name: str) -> None:
        if progress is not None:
            progress({"index": index, "total": total, "name": name})

    total = len(bats)
    try:
        emit("Останавливаю службу zapret…")
        try:
            actions.control_service("stop")
        except Exception as exc:  # noqa: BLE001
            emit(f"Не удалось остановить службу: {exc}")
        _kill_winws()

        emit("Замер без обхода (базовая линия)…")
        report["baseline"] = measure(timeout)
        base = report["baseline"]
        emit(
            f"  без обхода: {base['score']}/{base['total']}, "
            f"контроль {'ок' if base['control_ok'] else 'НЕ ОТВЕЧАЕТ'}"
        )
        if not base["control_ok"]:
            emit("Контрольный адрес недоступен — результат перебора будет недостоверным.")

        for index, name in enumerate(bats, start=1):
            advance(index, total, name)
            command = extract_command(zapret_dir / name)
            if not command:
                emit(f"[{index}/{total}] {name}: не удалось получить команду")
                report["results"].append({"name": name, "error": "extract"})
                continue
            _kill_winws()
            process = _launch(command, zapret_dir)
            time.sleep(wait)
            result = measure(timeout)
            _kill_winws()
            entry = {
                "name": name,
                "command": command,
                "score": result["score"],
                "total": result["total"],
                "control_ok": result["control_ok"],
                "working": result["working"],
                "targets": result["targets"],
                "started": bool(process),
            }
            report["results"].append(entry)
            emit(
                f"[{index}/{total}] {name}: {entry['score']}/{entry['total']}"
                + (" ✓" if entry["working"] else "")
            )

        candidates = [r for r in report["results"] if r.get("working")]
        if not candidates:
            candidates = [r for r in report["results"] if r.get("score", 0) > 0]
        if candidates:
            candidates.sort(
                key=lambda r: (r["score"], -sum(t["ms"] for t in r["targets"].values())),
                reverse=True,
            )
            best = candidates[0]
            report["best"] = best["name"]
            report["best_command"] = best["command"]
            emit(f"Лучшая стратегия: {best['name']} ({best['score']}/{best['total']})")
            if install_best:
                ok = install_service(best["command"])
                report["installed"] = ok
                emit("Служба перенастроена." if ok else "Не удалось перенастроить службу.")
        else:
            emit("Ни одна стратегия не дала результата — возвращаю исходную.")

        emit("Запускаю обход…")
        try:
            actions.control_service("start")
            report["restored"] = actions.service_state() == "running"
        except Exception as exc:  # noqa: BLE001
            emit(f"Не удалось запустить службу: {exc}")
    finally:
        _kill_winws()
        if not report["restored"] and original and not report["installed"]:
            # Аварийный откат: возвращаем исходную команду службы.
            emit("Откат к исходной команде службы…")
            try:
                install_service(original)
                actions.control_service("start")
                report["restored"] = actions.service_state() == "running"
            except Exception as exc:  # noqa: BLE001
                emit(f"Откат не удался: {exc}")
        report["finished"] = time.time()
    return report


def default_bats(zapret_dir: Path) -> list[str]:
    """Стратегии, которые имеет смысл перебирать (все general*.bat)."""
    directory = Path(zapret_dir)
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.glob("general*.bat"))


def write_report(report: dict, path: Path) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_report(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run_sweep(path: Path, zapret_dir: Path, bats: list[str] | None = None) -> int:
    """Точка входа процесса, запускаемого с UAC."""
    if not actions.is_admin():
        print("Требуются права администратора.", flush=True)
        return 2
    names = bats or default_bats(zapret_dir)
    report = sweep(zapret_dir, names, log=lambda m: print(m, flush=True))
    write_report(report, path)
    return 0
