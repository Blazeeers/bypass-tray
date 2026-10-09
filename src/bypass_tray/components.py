"""Установка компонентов обхода: zapret и tg-ws-proxy.

Оба берутся из официальных релизов на GitHub, поэтому в репозитории не нужно
хранить чужие бинарники, а версии всегда свежие. Ставим в профиль пользователя
(`%LOCALAPPDATA%\\bypass-tray`) — права администратора для скачивания не нужны.
"""

from __future__ import annotations

import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

from . import runtime

ZAPRET_REPO = "Flowseal/zapret-discord-youtube"
TGWS_REPO = "Flowseal/tg-ws-proxy"

#: Какие файлы релиза подходят под нашу систему.
ZAPRET_SUFFIXES = (".zip",)
TGWS_ASSET = "TgWsProxy_windows.exe"


def base_dir() -> Path:
    """Постоянная папка приложения (переживает перезапуск и обновление .exe)."""
    return runtime.data_dir()


def zapret_dir() -> Path:
    return base_dir() / "zapret"


def tgws_dir() -> Path:
    return base_dir() / "tgws"


def tgws_exe() -> Path:
    return tgws_dir() / TGWS_ASSET


# ---- GitHub ---------------------------------------------------------------


def _api(url: str, timeout: int = 25) -> dict:
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "bypass-tray"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def latest_asset(repo: str, suffixes: tuple[str, ...] | None = None,
                 exact: str | None = None) -> dict:
    """Находит нужный файл в последнем релизе репозитория."""
    release = _api(f"https://api.github.com/repos/{repo}/releases/latest")
    assets = release.get("assets") or []
    chosen = None
    if exact:
        chosen = next((a for a in assets if a.get("name") == exact), None)
    if chosen is None and suffixes:
        chosen = next(
            (a for a in assets if str(a.get("name", "")).lower().endswith(suffixes)), None
        )
    if chosen is None:
        raise LookupError(f"в релизе {repo} не найден подходящий файл")
    return {
        "name": chosen["name"],
        "url": chosen["browser_download_url"],
        "size": int(chosen.get("size") or 0),
        "tag": release.get("tag_name", ""),
        "repo": repo,
    }


def download(url: str, target: Path, progress=None) -> Path:
    """Скачивает файл, сообщая о ходе через ``progress(percent, mb_done, mb_total)``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "bypass-tray"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(target, "wb") as handle:
            while True:
                chunk = response.read(262144)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if progress is not None and total:
                    progress(int(done * 100 / total), done // 1048576, total // 1048576)
    return target


# ---- установка ------------------------------------------------------------


def _flatten(root: Path) -> None:
    """Если архив распаковался в одну вложенную папку — поднимаем содержимое."""
    entries = [p for p in root.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        nested = entries[0]
        for item in nested.iterdir():
            shutil.move(str(item), str(root / item.name))
        nested.rmdir()


def install_zapret(progress=None) -> dict:
    """Скачивает и распаковывает zapret в профиль пользователя."""
    asset = latest_asset(ZAPRET_REPO, suffixes=ZAPRET_SUFFIXES)
    target = zapret_dir()
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    archive = target / asset["name"]
    download(asset["url"], archive, progress)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(target)
    archive.unlink(missing_ok=True)
    _flatten(target)

    bats = sorted(p.name for p in target.glob("general*.bat"))
    ok = bool(bats)
    return {"ok": ok, "dir": str(target), "version": asset["tag"],
            "bats": len(bats),
            "error": "" if ok else "в архиве нет general*.bat"}


def install_tgws(progress=None) -> dict:
    """Скачивает tg-ws-proxy."""
    asset = latest_asset(TGWS_REPO, exact=TGWS_ASSET)
    target = tgws_exe()
    download(asset["url"], target, progress)
    ok = target.is_file() and target.stat().st_size > 1024
    return {"ok": ok, "exe": str(target), "version": asset["tag"],
            "error": "" if ok else "файл не скачался"}


def install_all(progress=None) -> dict:
    """Ставит оба компонента. ``progress(этап, текст, процент)``."""

    def say(stage: str, text: str, percent: int = 0) -> None:
        if progress is not None:
            progress(stage, text, percent)

    result: dict = {"ok": True, "components": {}}

    say("zapret", "Скачиваю zapret…", 0)
    try:
        zapret = install_zapret(
            lambda p, done, total: say("zapret", f"zapret: {done} из {total} МБ", p))
    except Exception as exc:  # noqa: BLE001
        zapret = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result["components"]["zapret"] = zapret

    say("tgws", "Скачиваю tg-ws-proxy…", 0)
    try:
        tgws = install_tgws(
            lambda p, done, total: say("tgws", f"tg-ws-proxy: {done} из {total} МБ", p))
    except Exception as exc:  # noqa: BLE001
        tgws = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result["components"]["tgws"] = tgws

    result["ok"] = bool(zapret.get("ok")) and bool(tgws.get("ok"))
    return result


def installed(cfg: dict | None = None) -> dict:
    """Что уже стоит (без обращения к сети)."""
    from . import config as config_module

    cfg = cfg or config_module.load_config()
    zapret_path = Path(cfg.get("zapret_dir") or "")
    tgws_path = Path(cfg.get("tgws_exe") or "")
    return {
        "zapret": bool(zapret_path.is_dir() and list(zapret_path.glob("general*.bat"))),
        "zapret_dir": str(zapret_path) if zapret_path.is_dir() else "",
        "tgws": tgws_path.is_file(),
        "tgws_exe": str(tgws_path) if tgws_path.is_file() else "",
    }
