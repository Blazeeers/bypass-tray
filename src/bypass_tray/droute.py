"""Управление Droute — SOCKS5-проксификатором Discord (TCP+UDP).

Droute ставит ``version.dll`` и ``droute.dll`` в каталог ``app-*`` Discord;
голос и демонстрация экрана идут через SOCKS Xray (inbound ``droute``).
Модуль только управляет установкой: сеть, GitHub и ``droute.exe`` вызываются
явно, все ошибки возвращаются словарём, без падений.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

API_URL = "https://api.github.com/repos/snowluwu/droute/releases/latest"
UA = "bypass-tray"
BRANCHES = ("stable", "canary", "ptb")
_BRANCH_DIRS = {
    "stable": "Discord",
    "canary": "DiscordCanary",
    "ptb": "DiscordPTB",
}
_CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def download_url() -> tuple[str, str]:
    """Последний релиз Droute: ``(tag, url_asset)``; ошибки → ``("", "")``."""
    request = urllib.request.Request(
        API_URL, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    tag = str(data.get("tag_name") or "")
    url = ""
    for asset in data.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("name") or "") == "droute.zip":
            url = str(asset.get("browser_download_url") or "")
            break
    return (tag, url) if tag and url else ("", "")


def discord_bases() -> list[Path]:
    """Существующие каталоги Discord (Stable/Canary/PTB) с ``Update.exe``."""
    if not _is_windows():
        return []
    root = Path(os.environ.get("LOCALAPPDATA")
                or (Path.home() / "AppData" / "Local"))
    found: list[Path] = []
    for name in _BRANCH_DIRS.values():
        base = root / name
        if (base / "Update.exe").is_file():
            found.append(base)
    return found


def discord_branch(base: Path | str) -> str:
    """Ветка по имени каталога: ``Discord`` → stable и т.п."""
    name = Path(base).name
    for branch, directory in _BRANCH_DIRS.items():
        if name.lower() == directory.lower():
            return branch
    return ""


def latest_app_dir(base: Path | str) -> Path | None:
    """Самый новый ``app-*`` в каталоге Discord."""
    directory = Path(base)
    candidates: list[tuple[int, Path]] = []
    try:
        entries = list(directory.glob("app-*"))
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        suffix = entry.name[4:]
        if suffix.isdigit():
            candidates.append((int(suffix), entry))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def installed(base: Path | str) -> bool:
    """Есть ли в latest ``app-*`` обе DLL Droute."""
    app = latest_app_dir(base)
    if app is None:
        return False
    return (app / "version.dll").is_file() and (app / "droute.dll").is_file()


def _file_version(path: Path) -> str:
    """FileVersion из DLL через ctypes; пустая строка при любой ошибке."""
    if not _is_windows():
        return ""
    try:
        import ctypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(
                str(path), 0, size, buffer):
            return ""
        for language in (0x040904B0, 0x040904E4, 0x04090000):
            pointer = ctypes.c_void_p()
            length = ctypes.c_uint()
            query = f"\\StringFileInfo\\{language:08x}\\FileVersion"
            if ctypes.windll.version.VerQueryValueW(
                    buffer, query, ctypes.byref(pointer),
                    ctypes.byref(length)):
                value = ctypes.wstring_at(pointer.value, length.value)
                return value.strip("\x00 ").strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def installed_version(base: Path | str) -> str:
    """Версия установленной ``droute.dll``; ``""`` — не установлена."""
    app = latest_app_dir(base)
    if app is None:
        return ""
    return _file_version(app / "droute.dll")


def _registry_values(key) -> dict:
    import winreg

    values: dict = {}
    index = 0
    while True:
        try:
            name, value, _kind = winreg.EnumValue(key, index)
        except OSError:
            break
        except Exception:  # noqa: BLE001
            break
        values[str(name)] = value
        index += 1
    return values


def _branch_registry_values(branch: str) -> tuple[dict, bool]:
    """Значения подраздела ветки и признак, что он существует."""
    try:
        import winreg
    except ImportError:
        return {}, False
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                rf"Software\droute\{branch}") as key:
            return _registry_values(key), True
    except OSError:
        return {}, False


def _root_registry_values() -> dict:
    try:
        import winreg
    except ImportError:
        return {}
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\droute") as key:
            return _registry_values(key)
    except OSError:
        return {}


def _parse_port(value) -> int | None:
    """Порт из значения: int, чистое число или host:port; иначе None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 65535 else None
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{1,5}", text):
        port = int(text)
        return port if 1 <= port <= 65535 else None
    # host:port принимается только для имени с буквой или IPv4: «10:45»,
    # даты и время — не адрес.
    match = re.fullmatch(
        r"(?:[A-Za-z][^:\s]*|(?:\d{1,3}\.){3}\d{1,3}):(\d{1,5})", text)
    if match:
        port = int(match.group(1))
        return port if 1 <= port <= 65535 else None
    return None


def configured_port(branch: str = "stable") -> int | None:
    """Порт Droute из подраздела ветки; ``None`` — неизвестно.

    Значение принимается только по имени с ``port`` и только как int,
    чистое число или ``host:port``. Даты/время и прочие строки игнорируются;
    при нескольких разных кандидатах — ``None`` (не угадываем).
    """
    if not _is_windows():
        return None
    branch = _branch_name(branch)
    values, exists = _branch_registry_values(branch)
    if not exists:
        # Root — только запасной вариант, если подраздела ветки нет.
        values = _root_registry_values()
    ports: set[int] = set()
    for name, value in values.items():
        if "port" not in str(name).lower():
            continue
        port = _parse_port(value)
        if port is not None:
            ports.add(port)
    return ports.pop() if len(ports) == 1 else None


def _branch_name(branch: str) -> str:
    value = str(branch or "").strip().lower()
    return value if value in BRANCHES else "stable"


def _base_for_branch(branch: str) -> Path | None:
    for base in discord_bases():
        if discord_branch(base) == branch:
            return base
    return None


def _extract_installer(tag: str, url: str) -> tuple[Path | None, Path | None]:
    """Скачивает zip и возвращает ``(droute.exe, temp_dir)``."""
    if not url:
        return None, None
    directory = Path(tempfile.mkdtemp(prefix="droute-"))
    archive = directory / "droute.zip"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=60) as response:
            archive.write_bytes(response.read())
        unpacked = directory / "unpacked"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(unpacked)
        executable = next(unpacked.rglob("droute.exe"), None)
        return executable, directory
    except Exception:  # noqa: BLE001
        shutil.rmtree(directory, ignore_errors=True)
        return None, None


def _discord_running() -> bool:
    try:
        from . import actions

        return bool(actions.discord_pids())
    except Exception:  # noqa: BLE001
        return False


def _close_discord(callback=None) -> bool:
    """Закрывает Discord; True — закрытие инициировано."""
    if callable(callback):
        try:
            callback()
            return True
        except Exception:  # noqa: BLE001
            return False
    if not _is_windows():
        return False
    try:
        from . import actions

        pids = actions.discord_pids()
        if not pids:
            return False
        import psutil

        for pid in pids:
            try:
                psutil.Process(pid).terminate()
            except Exception:  # noqa: BLE001
                continue
        return True
    except Exception:  # noqa: BLE001
        return False


def _reopen_discord(callback) -> None:
    if callable(callback):
        try:
            callback()
        except Exception:  # noqa: BLE001
            pass


def _run_installer(executable: Path, args: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [str(executable), *args], capture_output=True, timeout=120,
            cwd=str(executable.parent), close_fds=True,
            creationflags=_CREATE_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    output = (completed.stdout or b"") + (completed.stderr or b"")
    text = output.decode("utf-8", "replace").strip()
    return completed.returncode == 0, text[-500:]


def install(branch: str = "stable", port: int = 10819,
            close_discord_cb=None, reopen_cb=None, opencd=None) -> dict:
    """Скачивает и устанавливает Droute в выбранную ветку Discord."""
    branch = _branch_name(branch)
    base = _base_for_branch(branch)
    if base is None:
        return {"ok": False, "action": "discord_absent", "version": "",
                "error": "Discord не установлен"}
    tag, url = download_url()
    if not url:
        return {"ok": False, "action": "download", "version": "",
                "error": "не удалось получить релиз Droute"}
    executable, temp_dir = _extract_installer(tag, url)
    if executable is None:
        return {"ok": False, "action": "extract", "version": "",
                "error": "droute.exe не найден в архиве"}
    was_running = _discord_running()
    closed = False
    try:
        closed = _close_discord(close_discord_cb)
        ok, output = _run_installer(executable, [
            "--install", "--branch", branch, "--host", "127.0.0.1",
            "--port", str(int(port)),
        ])
        if not ok or not installed(base):
            return {"ok": False, "action": "failed", "version": "",
                    "error": output or "DLL Droute не появились"}
        return {"ok": True, "action": "installed",
                "version": installed_version(base), "error": ""}
    finally:
        if was_running and closed:
            _reopen_discord(reopen_cb if callable(reopen_cb) else opencd)
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def uninstall(branch: str = "stable", close_discord_cb=None,
              reopen_cb=None) -> dict:
    """Удаляет Droute из выбранной ветки Discord."""
    branch = _branch_name(branch)
    base = _base_for_branch(branch)
    if base is None:
        return {"ok": False, "action": "discord_absent", "error": "Discord не установлен"}
    tag, url = download_url()
    if not url:
        return {"ok": False, "action": "download",
                "error": "не удалось получить релиз Droute"}
    executable, temp_dir = _extract_installer(tag, url)
    if executable is None:
        return {"ok": False, "action": "extract",
                "error": "droute.exe не найден в архиве"}
    was_running = _discord_running()
    closed = False
    try:
        closed = _close_discord(close_discord_cb)
        ok, output = _run_installer(executable, [
            "--uninstall", "--branch", branch,
        ])
        if installed(base):
            return {"ok": False, "action": "failed",
                    "error": output or "DLL Droute остались"}
        return {"ok": True, "action": "uninstalled", "error": ""}
    finally:
        if was_running and closed:
            _reopen_discord(reopen_cb)
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def ensure(port: int, branch: str = "stable", close_discord_cb=None,
           reopen_cb=None) -> dict:
    """Ставит Droute при отсутствии; переустанавливает только при явном порте."""
    branch = _branch_name(branch)
    base = _base_for_branch(branch)
    if base is None:
        return {"ok": False, "action": "discord_absent", "version": "",
                "error": "Discord не установлен"}
    if not installed(base):
        return install(branch, port, close_discord_cb=close_discord_cb,
                       reopen_cb=reopen_cb)
    configured = configured_port(branch)
    if configured is None or configured == int(port):
        # Порт неизвестен — не повод убивать Discord и переустанавливать.
        return {"ok": True, "action": "present",
                "version": installed_version(base), "error": "",
                "port_known": configured is not None}
    result = install(branch, port, close_discord_cb=close_discord_cb,
                     reopen_cb=reopen_cb)
    if result.get("ok"):
        result["action"] = "reinstalled"
    return result


def available_version() -> str:
    """Тег последнего релиза Droute; пусто — нет связи."""
    tag, _url = download_url()
    return tag
