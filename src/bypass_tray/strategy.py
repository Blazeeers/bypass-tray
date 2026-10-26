"""Работа со стратегиями zapret: извлечение команды, проверка связи, перебор.

Идея автоподбора: перебрать `general*.bat`, для каждого поднять `winws.exe`,
проверить, открываются ли YouTube и Discord, и установить службу с лучшей
стратегией. Всё это требует прав администратора (остановка/создание службы),
поэтому перебор запускается отдельным процессом с UAC.

Команда `winws` не парсится вручную: рядом с `.bat` кладётся его копия, в
которой `start ... winws.exe` заменён на `echo`, а проверка обновлений
(`call service.bat check_updates`, она открывает браузер) вырезана; у
запущенного сценария забирается уже полностью раскрытая строка. Так
учитываются переменные `%BIN%`, `%LISTS%`, `%GameFilterTCP%` и т.п.
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

from . import actions, config, status

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
    # Пробный прогон 22 стратегий тоже не должен открывать вкладки: проверку
    # обновлений вырезаем так же, как в сценарии, который идёт в запуск.
    # `load_game_filter`/`load_user_lists` остаются — без них в команде не
    # будет значений GameFilter и списков.
    probe_text, _cut = actions._strip_check_updates(probe_text)

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


def probe_url(url: str, timeout: float = 6.0, limit: int = 524288,
              budget: float | None = None) -> dict:
    """Скачивает первые ``limit`` байт и измеряет скорость.

    DPI-троттлинг часто виден только на объёме: страница открывается, а видео
    грузится по 2 КБ. ``ok`` — ответ 2xx/3xx; обрыв или таймаут дают ``ok=False``.

    ``budget`` — жёсткий предел на весь запрос, а не на одну сокет-операцию:
    при троттлинге 10 КБ/с таймаут сокета не срабатывает, 512 КБ ползут около
    50 с, перебор выходит за дедлайн UI, а дочерний процесс продолжает
    сдёргивать ``winws``. По исчерпании бюджета чтение прекращается, и замер
    считается несостоявшимся (``error="timeout"``): нужный объём не пришёл.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    secure = parts.scheme != "http"
    started = time.perf_counter()
    expire = started + (float(budget) if budget is not None
                        else max(float(timeout) * 2.0, 4.0))
    connection = None
    data = b""
    status = 0
    expired = False
    try:
        port = parts.port or (443 if secure else 80)
        if secure:
            connection = http.client.HTTPSConnection(
                host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.request("GET", path, headers={"User-Agent": USER_AGENT})
        response = connection.getresponse()
        status = response.status
        while len(data) < limit:
            left = expire - time.perf_counter()
            if left <= 0:
                expired = True
                break
            sock = getattr(connection, "sock", None)
            if sock is not None:
                try:
                    sock.settimeout(max(0.05, min(left, timeout)))
                except OSError:
                    pass
            try:
                # read1 отдаёт то, что уже пришло: read(n) ждал бы весь кусок,
                # и таймаут сокета обнулялся бы на каждом байте троттлинга.
                chunk = response.read1(min(65536, limit - len(data)))
            except TimeoutError:
                # Сокет-таймаут внутри бюджета — тот же троттлинг: объём
                # нужных байт не пришёл, замер не состоялся.
                expired = True
                break
            if not chunk:
                break
            data += chunk
        elapsed = max((time.perf_counter() - started), 1e-6)
        result = {
            "ok": 200 <= status < 400 and not expired,
            "status": status,
            "ms": round(elapsed * 1000),
            "bytes": len(data),
            "kbps": round(len(data) * 8 / 1000 / elapsed),
        }
        if expired:
            result["error"] = "timeout"
            result["truncated"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        elapsed = max((time.perf_counter() - started), 1e-6)
        return {
            "ok": False,
            "status": status,
            "ms": round(elapsed * 1000),
            "bytes": len(data),
            "kbps": round(len(data) * 8 / 1000 / elapsed),
            "error": type(exc).__name__,
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass


#: Сколько нужно скачать, чтобы цель считалась рабочей.
MIN_DOWNLOAD_BYTES = 64 * 1024


def _target_ok(result: dict) -> bool:
    """Цель засчитана только при ответе и достаточном объёме данных."""
    return bool(result.get("ok")) and int(result.get("bytes") or 0) >= MIN_DOWNLOAD_BYTES


def _rank_candidates(candidates: list[dict]) -> list[dict]:
    """Лучшие: балл, затем суммарный объём, затем меньшее время."""
    return sorted(
        candidates,
        key=lambda entry: (
            entry["score"],
            sum(target.get("bytes", 0)
                for target in entry.get("targets", {}).values()),
            -sum(target["ms"]
                 for target in entry.get("targets", {}).values()),
        ),
        reverse=True,
    )


def measure(timeout: float = 6.0, budget: float | None = None) -> dict:
    """Проверяет целевые адреса и контрольный, измеряя реальную загрузку.

    ``budget`` ограничивает каждый замер по времени (см. ``probe_url``):
    суммарно один ``measure`` не может тянуться дольше трёх бюджетов.
    """
    targets = {name: probe_url(url, timeout, budget=budget) for name, url in TARGETS}
    control_name, control_url = CONTROL
    control = probe_url(control_url, timeout, budget=budget)
    score = sum(1 for result in targets.values() if _target_ok(result))
    total_bytes = sum(result.get("bytes", 0) for result in targets.values())
    total_kbps = sum(result.get("kbps", 0) for result in targets.values())
    return {
        "targets": targets,
        "control": {control_name: control},
        "score": score,
        "total": len(targets),
        "control_ok": control["ok"],
        "bytes": total_bytes,
        "kbps": total_kbps,
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


def _winws_running() -> bool:
    """Поднят ли ``winws`` — службой или запуском из ``.bat``."""
    return bool(actions._pids("winws"))


def _wait_winws(seconds: float = 4.0) -> bool:
    """Ждёт появления ``winws``: запуск из ``.bat`` не мгновенный."""
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if _winws_running():
            return True
        time.sleep(0.25)
    return _winws_running()


def _service_start(emit) -> bool:
    """Запускает службу zapret и подтверждает, что она поднялась."""
    try:
        state = actions.control_service("start")
    except (OSError, subprocess.SubprocessError, PermissionError) as exc:
        emit(f"Служба не запустилась: {exc}")
        return False
    return state == "running"


def _restore_obhod(cfg: dict, original: str, bat: str | None, emit,
                   *, installed: bool = False) -> bool:
    """Возвращает обход в работу: служба, иначе запуск из ``.bat``.

    Службы может не быть вовсе — zapret часто поднимают из ``general*.bat``.
    Тогда восстановление это повторный запуск того же сценария: ``sc start``
    в несуществующую службу съедал откат, и обход лежал до ручного запуска.
    """
    if installed:
        # Служба только что перенастроена на лучшую стратегию — её и стартуем.
        if _service_start(emit):
            return True
    elif original:
        try:
            if install_service(original) and _service_start(emit):
                return True
        except (OSError, subprocess.SubprocessError, PermissionError) as exc:
            emit(f"Служба не восстановлена: {exc}")
    if bat:
        name = Path(bat).name
        try:
            actions.start_zapret(cfg, name)
        except (OSError, PermissionError, FileNotFoundError) as exc:
            emit(f"Запуск «{name}» не удался: {exc}")
        else:
            if _wait_winws():
                return True
            emit(f"«{name}» запущен, но winws не появился.")
    return False


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
    Отчёт формируется всегда: сбой внутри (таймаут ``sc.exe``, отказ UAC)
    попадает в ``error``, а не роняет процесс — иначе UI ждал бы результат
    все 900 секунд, а winws остался бы убитым.
    """
    zapret_dir = Path(zapret_dir)
    try:
        cfg = dict(config.load_config() or {})
    except (OSError, ValueError):
        cfg = {}
    # Запуск из .bat при откате должен идти из той папки, которую перебираем.
    cfg["zapret_dir"] = str(zapret_dir)
    bat = str(cfg.get("zapret_bat") or "").strip() or None
    try:
        original = status.service_image_path()
    except Exception:  # noqa: BLE001
        original = ""
    report: dict = {
        "started": time.time(),
        "original_command": original,
        "bat": bat,
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
    restored = False
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
            target_values = list(result["targets"].values())
            entry = {
                "name": name,
                "command": command,
                "score": result["score"],
                "total": result["total"],
                "control_ok": result["control_ok"],
                "working": result["working"],
                "bytes": sum(t.get("bytes", 0) for t in target_values),
                "kbps": sum(t.get("kbps", 0) for t in target_values),
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
            candidates = _rank_candidates(candidates)
            best = candidates[0]
            report["best"] = best["name"]
            report["best_command"] = best["command"]
            emit(f"Лучшая стратегия: {best['name']} ({best['score']}/{best['total']}, "
                 f"{best.get('bytes', 0) // 1024} КБ)")
            if install_best:
                try:
                    ok = install_service(best["command"])
                except (OSError, subprocess.SubprocessError, PermissionError) as exc:
                    ok = False
                    emit(f"sc.exe не отработал: {exc}")
                report["installed"] = ok
                emit("Служба перенастроена." if ok else "Не удалось перенастроить службу.")
        else:
            emit("Ни одна стратегия не дала результата — возвращаю исходную.")

        emit("Запускаю обход…")
        report["restored"] = _restore_obhod(
            cfg, original, (report["best"] if report["installed"] else bat) or None,
            emit, installed=bool(report["installed"]))
        restored = True
        emit("Обход работает." if report["restored"]
             else "Обход не поднялся — нужен ручной запуск.")
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        emit(f"Перебор прерван: {type(exc).__name__}: {exc}")
    finally:
        if not restored:
            _kill_winws()
            emit("Возвращаю обход в работу…")
            report["restored"] = _restore_obhod(
                cfg, original, (report["best"] if report["installed"] else bat) or None,
                emit, installed=bool(report["installed"]))
        report["finished"] = time.time()
    return report


def default_bats(zapret_dir: Path) -> list[str]:
    """Стратегии, которые имеет смысл перебирать (все general*.bat)."""
    directory = Path(zapret_dir)
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.glob("general*.bat"))


def write_report(report: dict, path: Path) -> bool:
    """Пишет отчёт перебора. ``False`` — записать не удалось."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return False
    return True


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
    try:
        report = sweep(zapret_dir, names, log=lambda m: print(m, flush=True))
    except Exception as exc:  # noqa: BLE001
        # Отчёт нужен даже после катастрофы: без файла UI ждёт 900 секунд.
        report = {"started": time.time(), "finished": time.time(),
                  "results": [], "best": "", "installed": False,
                  "restored": False, "error": f"{type(exc).__name__}: {exc}"}
        print(f"Перебор не выполнен: {exc}", flush=True)
    return 0 if write_report(report, path) else 1
