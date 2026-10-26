"""Проверка обновлений компонентов через GitHub Releases и «Обновить всё»."""

from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from . import actions, activity, blocked, components, config, status, xray

REPOS = {
    "zapret": "Flowseal/zapret-discord-youtube",
    "tgwsproxy": "Flowseal/tg-ws-proxy",
    "xray": "XTLS/Xray-core",
}

#: Человеческие названия для уведомлений и журнала.
LABELS = {
    "zapret": "zapret",
    "tgwsproxy": "tg-ws-proxy",
    "xray": "ядро Xray",
    "refilter": "Re:filter",
}

#: Строка ``xray -version``: «Xray 25.6.8 (Xray, Penetration Test Build)».
_VERSION_RE = re.compile(r"Xray\s+([\w.\-]+)")
#: Запасной разбор pyproject.toml, если tomllib недоступен (Python < 3.11).
_PYPROJECT_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)


def label(name: str) -> str:
    return LABELS.get(str(name), str(name))


def _latest_tag(repo: str) -> tuple[str, str]:
    """Последний тег релиза: ``(тег, ошибка)``.

    Пустой тег с непустой ошибкой — именно «нет связи», а не «обновления
    не нужны»: вызывающий код обязан различать эти состояния.
    """
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "bypass-tray"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            tag = str(json.loads(response.read()).get("tag_name") or "")
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"
    if not tag:
        return "", "GitHub вернул пустой тег"
    return tag, ""


def _latest_tags() -> tuple[dict, str]:
    """Теги всех репозиториев — параллельно, чтобы не ждать трижды.

    Второе значение — текст ошибки, если **ни один** репозиторий не ответил.
    Часовой сбой одного репозитория не делает офлайном всю проверку.
    """
    tags: dict[str, str] = {}
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(REPOS)) as pool:
        futures = {pool.submit(_latest_tag, repo): name for name, repo in REPOS.items()}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                tag, error = future.result()
            except Exception as exc:  # noqa: BLE001
                tag, error = "", f"{type(exc).__name__}: {exc}"
            tags[name] = tag
            if error:
                failures.append(f"{name}: {error}")
    answered = any(tags.values())
    return tags, ("" if answered else ("; ".join(failures[:2]) or "нет ответа GitHub"))


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


def present_components(cfg: dict) -> dict:
    """Что уже лежит на диске — без единого запроса в сеть.

    Нужно, чтобы отличить «не установлено» от «версия неизвестна»: ядро и
    список Re:filter в релизном zip уже на месте, и скачивать их только
    ради проверки нельзя.
    """
    try:
        local = components.installed(cfg)
    except Exception:  # noqa: BLE001
        local = {}
    try:
        service = bool(status.service_image_path())
    except Exception:  # noqa: BLE001
        service = False
    try:
        core = bool(xray.xray_path().is_file())
    except Exception:  # noqa: BLE001
        core = False
    try:
        refilter = bool(blocked.info(cfg).get("installed"))
    except Exception:  # noqa: BLE001
        refilter = False
    return {
        "zapret": bool(local.get("zapret")) or service,
        "tgwsproxy": bool(local.get("tgws")),
        "xray": core,
        "refilter": refilter,
    }


def check_all(cfg: dict, state: dict) -> dict:
    """Сверяет установленные версии с последними релизами.

    ``missing`` — компоненты, которых нет ни в state, ни на диске (для
    zapret учитывается и служба Windows). ``network_error`` появляется,
    только когда GitHub не ответил ни по одному репозиторию: тогда пустой
    ``outdated`` не означает «всё актуально».
    """
    latest, network_error = _latest_tags()
    installed = installed_versions(cfg, state)
    present = present_components(cfg)
    seen = state.setdefault("seen_versions", {})
    found: dict[str, dict] = {}
    outdated: list[str] = []
    missing: list[str] = []
    for name in REPOS:
        tag = latest.get(name, "")
        have = installed.get(name, "")
        is_outdated = _is_outdated(have, tag)
        if tag:
            seen[name] = tag
        found[name] = {"latest": tag, "installed": have, "outdated": bool(is_outdated)}
        if is_outdated:
            outdated.append(name)
        if not have and not present.get(name):
            missing.append(name)
    if not present.get("refilter"):
        missing.append("refilter")
    result = {"checked": time.time(), "components": found,
              "outdated": outdated, "missing": missing}
    if network_error:
        result["network_error"] = network_error
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
    """Ставит отсутствующее, обновляет устаревшее, остальное пропускает.

    Результат честный: ``installed``/``updated`` — что реально сделано,
    ``skipped`` — что не трогали. Служба zapret пропускается: её файлы не
    наши, подмена portable-копией ничего бы не изменила.
    """
    def say(stage: str, text: str, percent: int = 0) -> None:
        if progress is None:
            return
        try:
            progress(stage, text, percent)
        except Exception:  # noqa: BLE001
            pass

    result: dict = {"ok": True, "components": {}, "installed": [], "updated": [],
                    "skipped": [], "errors": [], "warnings": []}
    known = state.get("installed")
    known = dict(known) if isinstance(known, dict) else {}
    present = present_components(cfg)
    latest, network_error = _latest_tags()
    if network_error:
        result["network_error"] = network_error
    versions: dict[str, str] = {}
    try:
        service_zapret = bool(status.service_image_path())
    except Exception:  # noqa: BLE001
        service_zapret = False

    # ---- zapret -----------------------------------------------------------
    if service_zapret:
        say("zapret", "zapret установлен службой — пропускаю", 0)
        result["skipped"].append("zapret")
        result["components"]["zapret"] = {
            "ok": True, "version": "", "error": "", "service": True, "skipped": True}
        result["warnings"].append("zapret установлен службой, обновите вручную")
    else:
        was_there = bool(present["zapret"])
        can_update = _is_outdated(known.get("zapret", ""), latest.get("zapret", ""))
        if not was_there or can_update:
            say("zapret", ("обновляю: zapret" if was_there else "устанавливаю: zapret"), 0)
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
                version = str(zapret.get("version") or "")
                if version:
                    versions["zapret"] = version
                (result["updated"] if was_there else result["installed"]).append("zapret")
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
                    "zapret: " + (zapret.get("error") or "не установился"))
        else:
            result["skipped"].append("zapret")
            result["components"]["zapret"] = {
                "ok": True, "version": str(known.get("zapret") or ""),
                "error": "", "skipped": True}

    # ---- tg-ws-proxy ------------------------------------------------------
    was_there = bool(present["tgwsproxy"])
    can_update = _is_outdated(known.get("tgws", ""), latest.get("tgwsproxy", ""))
    if not was_there or can_update:
        say("tgws", ("обновляю: tg-ws-proxy" if was_there else "устанавливаю: tg-ws-proxy"), 0)
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
            version = str(tgws.get("version") or "")
            if version:
                versions["tgws"] = version
            (result["updated"] if was_there else result["installed"]).append("tgwsproxy")
            # Пользовательский путь сохраняем; LOCALAPPDATA — только для новой установки.
            cfg["tgws_exe"] = existing_tgws or str(tgws.get("exe") or "")
        else:
            result["errors"].append(
                "tgwsproxy: " + (tgws.get("error") or "не установился"))
        if tgws_was_running:
            try:
                actions.start_tgws(cfg)
            except Exception as exc:  # noqa: BLE001
                result["warnings"].append(f"tg-ws-proxy не запущен: {exc}")
    else:
        result["skipped"].append("tgwsproxy")
        result["components"]["tgwsproxy"] = {
            "ok": True, "version": str(known.get("tgws") or ""),
            "error": "", "skipped": True}

    # ---- ядро Xray --------------------------------------------------------
    was_there = bool(present["xray"])
    have_xray = installed_versions(cfg, state).get("xray", "")
    can_update = _is_outdated(have_xray, latest.get("xray", ""))
    if not was_there or can_update:
        say("xray", ("обновляю: ядро Xray" if was_there else "устанавливаю: ядро Xray"), 0)
        was_running = False
        server = None
        was_port = None
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
        try:
            core = xray.ensure_core(force=was_there)
        except Exception:  # noqa: BLE001
            core = None
        if core is None:
            message = "ядро не установилось (файл занят или нет сети)"
            result["components"]["xray"] = {"ok": False, "version": "", "error": message}
            result["errors"].append("xray: " + message)
        else:
            version = _xray_version()
            if version:
                versions["xray"] = version
            (result["updated"] if was_there else result["installed"]).append("xray")
            result["components"]["xray"] = {"ok": True, "version": version, "error": ""}
        # Прокси возвращаем в любом случае: остановленным он быть не должен.
        if was_running:
            say("restart", "Возвращаю прокси…", 90)
            try:
                restarted = (xray.switch_to(server, cfg) if server is not None
                             else xray.start(cfg))
            except Exception as exc:  # noqa: BLE001
                restarted = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if not restarted.get("ok"):
                result["errors"].append(
                    "прокси не перезапустился: " + str(restarted.get("error") or "?"))
    else:
        result["skipped"].append("xray")
        result["components"]["xray"] = {
            "ok": True, "version": have_xray, "error": "", "skipped": True}

    # ---- список Re:filter -------------------------------------------------
    # Список обновляется отдельно: его отсутствие в сети — ошибка этого шага,
    # остальные компоненты уже разложены и её не замечают.
    say("refilter", "обновляю список Re:filter…", 0)
    try:
        refilter = blocked.update(_install_progress(progress, "refilter"))
    except Exception as exc:  # noqa: BLE001
        refilter = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if refilter.get("ok"):
        (result["updated"] if present["refilter"] else result["installed"]).append("refilter")
        result["components"]["refilter"] = {"ok": True, "version": "", "error": ""}
    else:
        message = str(refilter.get("error") or "не обновился")
        result["components"]["refilter"] = {"ok": False, "version": "", "error": message}
        result["errors"].append("Re:filter: " + message)

    if versions:
        known.update(versions)
    state["installed"] = known
    if not config.save_config(cfg):
        result["warnings"].append("config.json не записан")
    config.save_state(state)

    done = list(result["installed"]) + list(result["updated"])
    activity.log("обновил компоненты: " + (", ".join(done) if done else "нет изменений"))
    result["ok"] = not result["errors"]
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
