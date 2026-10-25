"""Проверка обновлений компонентов через GitHub Releases и «Обновить всё»."""

from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from . import actions, activity, components, config, status, xray

REPOS = {
    "zapret": "Flowseal/zapret-discord-youtube",
    "tgwsproxy": "Flowseal/tg-ws-proxy",
    "xray": "XTLS/Xray-core",
}

#: Строка ``xray -version``: «Xray 25.6.8 (Xray, Penetration Test Build)».
_VERSION_RE = re.compile(r"Xray\s+([\w.\-]+)")
#: Запасной разбор pyproject.toml, если tomllib недоступен (Python < 3.11).
_PYPROJECT_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)


def _latest_tag(repo: str) -> str:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "bypass-tray"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return str(json.loads(response.read()).get("tag_name") or "")
    except Exception:  # noqa: BLE001
        return ""


def _latest_tags() -> dict:
    """Последние теги всех репозиториев — параллельно, чтобы не ждать трижды."""
    tags: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(REPOS)) as pool:
        futures = {pool.submit(_latest_tag, repo): name for name, repo in REPOS.items()}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                tags[name] = future.result()
            except Exception:  # noqa: BLE001
                tags[name] = ""
    return tags


def _is_outdated(installed: str, latest: str) -> bool:
    """Отличается ли установленная версия от последнего тега.

    ``v`` в начале и регистр не важны: ``xray -version`` печатает «25.6.8»,
    а тег релиза — «v25.6.8».
    """
    have = str(installed or "").strip().lower().lstrip("v")
    want = str(latest or "").strip().lower().lstrip("v")
    if not have or not want:
        return False
    return have != want


# ---- установленные версии -------------------------------------------------


def _xray_binary() -> str:
    try:
        path = xray.xray_path()
    except Exception:  # noqa: BLE001
        return "xray.exe"
    return str(path) if path.is_file() else "xray.exe"


def _xray_version() -> str:
    try:
        completed = subprocess.run(
            [_xray_binary(), "-version"], capture_output=True, timeout=5,
            creationflags=actions.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = actions._decode(completed.stdout) + actions._decode(completed.stderr)
    match = _VERSION_RE.search(text)
    return match.group(1) if match else ""


def widget_version() -> str:
    """Версия виджета: сначала пакет (работает и в frozen), затем pyproject."""
    try:
        from . import __version__

        if __version__:
            return str(__version__)
    except Exception:  # noqa: BLE001
        pass
    path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        import tomllib
    except ImportError:  # pragma: no cover — Python 3.10
        match = _PYPROJECT_VERSION_RE.search(text)
        return match.group(1) if match else ""
    try:
        data = tomllib.loads(text)
    except ValueError:
        return ""
    project = data.get("project") or {}
    return str(project.get("version") or "")


def installed_versions(cfg: dict, state: dict | None = None) -> dict:
    """Что установлено сейчас: zapret/tgws — из state, xray — живым запуском."""
    state = state if state is not None else config.load_state()
    known = state.get("installed") or {}
    return {
        "zapret": str(known.get("zapret") or ""),
        "tgwsproxy": str(known.get("tgws") or ""),
        "xray": _xray_version(),
        "widget": widget_version(),
    }


# ---- проверка -------------------------------------------------------------


def check_all(cfg: dict, state: dict) -> dict:
    """Сверяет установленные версии с последними релизами.

    Для zapret/tgws версия установки известна не всегда — тогда обновление
    не считается нужным, но тег запоминается в ``seen_versions``.
    """
    latest = _latest_tags()
    installed = installed_versions(cfg, state)
    seen = state.setdefault("seen_versions", {})
    found: dict[str, dict] = {}
    outdated: list[str] = []
    for name in REPOS:
        tag = latest.get(name, "")
        have = installed.get(name, "")
        is_outdated = _is_outdated(have, tag) if have else False
        if tag:
            seen[name] = tag
        found[name] = {"latest": tag, "installed": have, "outdated": bool(is_outdated)}
        if is_outdated:
            outdated.append(name)
    result = {"checked": time.time(), "components": found, "outdated": outdated}
    state["updates"] = result
    return result


# ---- обновление всего -----------------------------------------------------


def _install_progress(progress, stage: str):
    """Обёртка ``progress(stage, text, percent)`` для конкретного компонента."""

    def report(percent: int, done: int, total: int) -> None:
        if progress is None:
            return
        try:
            progress(stage, f"{stage}: {done} из {total} МБ", percent)
        except Exception:  # noqa: BLE001
            pass

    return report


def update_all(cfg: dict, state: dict, progress=None) -> dict:
    """Обновляет компоненты; службу zapret не переустанавливает."""
    def say(stage: str, text: str, percent: int = 0) -> None:
        if progress is None:
            return
        try:
            progress(stage, text, percent)
        except Exception:  # noqa: BLE001
            pass

    result: dict = {"ok": True, "components": {}, "errors": [], "warnings": []}
    was_running = False
    was_port = None
    server = None
    try:
        was_running = xray.is_running()
        if was_running:
            server = xray.saved_server()
            was_port = xray.status().get("port")
    except Exception:  # noqa: BLE001
        was_running = False

    if was_running:
        say("stop", "Останавливаю прокси…", 0)
        try:
            xray.stop(port=was_port)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"прокси не остановлен: {exc}")

    tags: dict[str, str] = {}
    try:
        service_zapret = bool(status.service_image_path())
    except Exception:  # noqa: BLE001
        service_zapret = False

    if service_zapret:
        # Служба обновляется вручную: установщик portable в LOCALAPPDATA
        # не должен подменять службу и путь пользователя.
        say("zapret", "zapret установлен службой — пропускаю", 0)
        result["components"]["zapret"] = {
            "ok": True, "version": "", "error": "", "service": True}
        result["warnings"].append(
            "zapret установлен службой, обновите вручную")
    else:
        say("zapret", "Скачиваю zapret…", 0)
        existing_dir = str(cfg.get("zapret_dir") or "")
        target = Path(existing_dir) if existing_dir else components.zapret_dir()
        try:
            zapret = components.install_zapret(
                _install_progress(progress, "zapret"), target=target)
        except Exception as exc:  # noqa: BLE001
            zapret = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result["components"]["zapret"] = {
            "ok": bool(zapret.get("ok")),
            "version": str(zapret.get("version") or ""),
            "error": str(zapret.get("error") or ""),
        }
        if zapret.get("ok"):
            tags["zapret"] = str(zapret.get("version") or "")
            # Не меняем пользовательский путь без нужды.
            cfg["zapret_dir"] = existing_dir or str(zapret.get("dir") or "")
            installed_dir = Path(cfg["zapret_dir"])
            bats = sorted(p.name for p in installed_dir.glob("general*.bat"))
            old_bat = str(cfg.get("zapret_bat") or "")
            if bats and old_bat not in bats:
                cfg["zapret_bat"] = bats[0]
                result["warnings"].append(
                    f"стратегия {old_bat or '(не задана)'} не найдена — выбрана {bats[0]}")
        else:
            result["errors"].append(
                "zapret: " + (zapret.get("error") or "не обновился"))

    say("tgws", "Скачиваю tg-ws-proxy…", 0)
    tgws_was_running = False
    try:
        tgws_was_running = actions.tgws_running()
    except Exception:  # noqa: BLE001
        pass
    if tgws_was_running:
        try:
            actions.stop_tgws()
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"tg-ws-proxy не остановлен: {exc}")
    existing_tgws = str(cfg.get("tgws_exe") or "")
    tgws_target = Path(existing_tgws) if existing_tgws else components.tgws_exe()
    try:
        tgws = components.install_tgws(
            _install_progress(progress, "tgws"), target=tgws_target)
    except Exception as exc:  # noqa: BLE001
        tgws = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result["components"]["tgwsproxy"] = {
        "ok": bool(tgws.get("ok")),
        "version": str(tgws.get("version") or ""),
        "error": str(tgws.get("error") or ""),
    }
    if tgws.get("ok"):
        tags["tgws"] = str(tgws.get("version") or "")
        # Пользовательский путь сохраняем; LOCALAPPDATA — только для новой установки.
        cfg["tgws_exe"] = existing_tgws or str(tgws.get("exe") or "")
    else:
        result["errors"].append(
            "tgwsproxy: " + (tgws.get("error") or "не обновился"))
    if tgws_was_running:
        try:
            actions.start_tgws(cfg)
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"tg-ws-proxy не запущен: {exc}")

    say("xray", "Обновляю ядро Xray…", 0)
    try:
        core = xray.ensure_core(force=True)
    except Exception:  # noqa: BLE001
        core = None
    if core is None:
        message = "ядро не обновилось (файл занят или нет сети)"
        result["components"]["xray"] = {"ok": False, "version": "", "error": message}
        result["errors"].append("xray: " + message)
    else:
        version = _xray_version()
        tags["xray"] = version
        result["components"]["xray"] = {"ok": True, "version": version, "error": ""}

    known = state.get("installed")
    known = known if isinstance(known, dict) else {}
    state["installed"] = {**known, **{name: value for name, value in tags.items() if value}}
    if not config.save_config(cfg):
        result["warnings"].append("config.json не записан")
    config.save_state(state)

    if was_running:
        say("restart", "Возвращаю прокси…", 90)
        try:
            restarted = xray.switch_to(server, cfg) if server is not None else xray.start(cfg)
        except Exception as exc:  # noqa: BLE001
            restarted = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not restarted.get("ok"):
            result["errors"].append(
                "прокси не перезапустился: " + str(restarted.get("error") or "?"))

    names = [name for name, info in result["components"].items() if info.get("ok")]
    activity.log("обновил компоненты: " + (", ".join(names) if names else "нет изменений"))
    result["ok"] = not result["errors"]
    config.save_state(state)
    return result


# ---- совместимость с app.py ----------------------------------------------


def check(state: dict) -> dict:
    """Прежний формат ``{'result', 'new'}`` — его ждёт ``app.py``."""
    result = check_all(config.load_config(), state)
    new = [
        f"{name} → {info['latest']}"
        for name, info in result["components"].items()
        if info.get("outdated") and info.get("latest")
    ]
    return {"result": result, "new": new}
