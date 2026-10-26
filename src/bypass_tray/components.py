"""Установка компонентов обхода: zapret и tg-ws-proxy.

Оба берутся из официальных релизов на GitHub, поэтому в репозитории не нужно
хранить чужие бинарники, а версии всегда свежие. Ставим в профиль пользователя
(`%LOCALAPPDATA%\\bypass-tray`) — права администратора для скачивания не нужны.
"""

from __future__ import annotations

import json
import os
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
    """Скачивает файл, сообщая о ходе через ``progress(percent, mb_done, mb_total)``.

    Пишет рядом ``<target>.part`` и подменяет цель только после успешной
    загрузки: оборванный файл не должен выглядеть установленным.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "bypass-tray"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as handle:
                while True:
                    chunk = response.read(262144)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if progress is not None and total:
                        progress(int(done * 100 / total), done // 1048576, total // 1048576)
        os.replace(part, target)
    except Exception:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise
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


def install_zapret(progress=None, target: Path | None = None) -> dict:
    """Скачивает и распаковывает zapret, не задевая рабочую копию до проверки.

    Архив качается рядом и распаковывается во временную папку; только после
    проверки ``general*.bat`` каталог подменяется. При обрыве прежняя
    установка остаётся на месте.
    """
    asset = latest_asset(ZAPRET_REPO, suffixes=ZAPRET_SUFFIXES)
    target = Path(target) if target is not None else zapret_dir()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f"zapret-new-{os.getpid()}"
    old = parent / f"zapret-old-{os.getpid()}"
    archive = parent / f"zapret-{os.getpid()}.zip"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    try:
        download(asset["url"], archive, progress)
        staging.mkdir(parents=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(staging)
        _flatten(staging)

        bats = sorted(p.name for p in staging.glob("general*.bat"))
        if not bats:
            raise ValueError("в архиве нет general*.bat")

        moved_old = False
        try:
            if target.exists():
                os.replace(target, old)
                moved_old = True
            os.replace(staging, target)
        except OSError:
            if moved_old and not target.exists():
                os.replace(old, target)
            raise
        shutil.rmtree(old, ignore_errors=True)
        return {"ok": True, "dir": str(target), "version": asset["tag"],
                "bats": len(bats), "error": ""}
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(staging, ignore_errors=True)
        return {"ok": False, "dir": str(target) if target.exists() else "",
                "version": asset.get("tag", ""), "bats": 0,
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass


def install_tgws(progress=None, target: Path | None = None) -> dict:
    """Скачивает tg-ws-proxy и подменяет бинарник только после проверки.

    Скачивание идёт в ``.new``: битый или недокачанный файл не станет рабочим.
    """
    asset = latest_asset(TGWS_REPO, exact=TGWS_ASSET)
    target = Path(target) if target is not None else tgws_exe()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + ".new")
    try:
        download(asset["url"], staged, progress)
        size = staged.stat().st_size if staged.is_file() else 0
        if size <= 1024 * 1024:
            raise ValueError("файл не скачался или слишком мал")
        os.replace(staged, target)
        return {"ok": True, "exe": str(target), "version": asset["tag"], "error": ""}
    except Exception as exc:  # noqa: BLE001
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "exe": str(target), "version": asset.get("tag", ""),
                "error": f"{type(exc).__name__}: {exc}"}


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
    zapret_raw = str(cfg.get("zapret_dir") or "").strip()
    tgws_raw = str(cfg.get("tgws_exe") or "").strip()
    zapret_path = Path(zapret_raw) if zapret_raw else None
    tgws_path = Path(tgws_raw) if tgws_raw else None
    return {
        "zapret": bool(zapret_path and zapret_path.is_dir()
                       and list(zapret_path.glob("general*.bat"))),
        "zapret_dir": str(zapret_path) if zapret_path and zapret_path.is_dir() else "",
        "tgws": bool(tgws_path and tgws_path.is_file()),
        "tgws_exe": str(tgws_path) if tgws_path and tgws_path.is_file() else "",
    }
