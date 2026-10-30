"""Проверка обновлений компонентов через GitHub Releases и «Обновить всё»."""

from __future__ import annotations

import concurrent.futures
import ctypes
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import actions, activity, blocked, components, config, status, xray

REPOS = {
    "zapret": "Flowseal/zapret-discord-youtube",
    "tgwsproxy": "Flowseal/tg-ws-proxy",
    "xray": "XTLS/Xray-core",
}

#: Репозиторий самой программы: релизы ``v*`` собирает GitHub Actions.
APP_REPO = "Blazeeers/bypass-tray"
APP_RELEASES_URL = f"https://github.com/{APP_REPO}/releases/latest"

#: Человеческие названия для уведомлений и журнала.
LABELS = {
    "zapret": "zapret",
    "tgwsproxy": "tg-ws-proxy",
    "xray": "ядро Xray",
    "refilter": "Re:filter",
}

#: Строка ``xray -version``: «Xray 25.6.8 (Xray, Penetration Test Build)».
_VERSION_RE = re.compile(r"Xray\s+([\w.\-]+)")
#: Начало версии в теге: «v1.10.4», «1.11.2.0», «0.6.9».
_VERSION_START_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")
#: Запасной разбор pyproject.toml, если tomllib недоступен (Python < 3.11).
_PYPROJECT_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)


def label(name: str) -> str:
    return LABELS.get(str(name), str(name))


def _version_key(text: str) -> tuple[int, ...] | None:
    """Числовая версия строкой-кортежем; ``None`` — если это не версия.

    Сравнивать «0.6.10» и «0.6.9» строками нельзя: как тексты они дают
    обратный порядок. Хвостовые нули отбрасываются, потому что версия из
    ресурсов exe — «1.11.2.0», а тег релиза — «v1.11.2».
    """
    match = _VERSION_START_RE.match(str(text or "").strip())
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


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
    Ключ ``app`` — последний релиз самой программы, чтобы панель и трей
    могли сказать «Доступна версия X».
    """
    repos = dict(REPOS)
    repos["app"] = APP_REPO
    tags: dict[str, str] = {}
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(repos)) as pool:
        futures = {pool.submit(_latest_tag, repo): name for name, repo in repos.items()}
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
    """Установленная версия старше последнего тега.

    ``v`` в начале, регистр и хвостовые нули не важны: ``xray -version``
    печатает «25.6.8», тег релиза — «v25.6.8», а версия из ресурсов exe —
    «1.11.2.0». Неразборчивые теги (например, «zapret-new») сравниваются
    строками: иначе проверка молча считала бы их равными.
    """
    have_raw = str(installed or "").strip()
    want_raw = str(latest or "").strip()
    if not have_raw or not want_raw:
        return False
    have = _version_key(have_raw)
    want = _version_key(want_raw)
    if have is not None and want is not None:
        return have < want
    return have_raw.lower().lstrip("v") != want_raw.lower().lstrip("v")


def _needs_update(installed: str, latest: str, present: bool) -> bool:
    """Стоит ли ставить компонент: отсутствует или версия не подтверждена.

    Пустая версия при живых файлах — не «актуально», а «неизвестно»:
    zapret, поставленный службой, и tg-ws-proxy, скопированный вручную,
    в ``state`` своей версии не оставили. Один раз обновляем — установка
    запишет версию, и дальше сверка пойдёт по ней.
    """
    if not str(latest or "").strip():
        return False  # сверять не с чем — не трогаем без сети
    if not present:
        return True
    if not str(installed or "").strip():
        return True
    return _is_outdated(installed, latest)


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


def _exe_version(path) -> str:
    """Версия из ресурсов exe (``FileVersion``) — без сторонних библиотек.

    tg-ws-proxy не помнит свою версию, если его файл скопировали вручную:
    читаем её из самого бинарника. Ресурс даёт «1.11.2.0», тег релиза —
    «v1.11.2»; ``_version_key`` считает их равными.
    """
    if not sys.platform.startswith("win"):
        return ""
    target = str(path or "").strip()
    if not target or not Path(target).is_file():
        return ""
    try:
        from ctypes import wintypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(target, None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(target, 0, size, buffer):
            return ""
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
                buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return ""
        # VS_FIXEDFILEINFO: подпись, версия структуры, старшая и младшая
        # половины номера файла.
        fixed = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
        high, low = int(fixed[2]), int(fixed[3])
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return ""
    if not high and not low:
        return ""
    return f"{high >> 16}.{high & 0xFFFF}.{low >> 16}.{low & 0xFFFF}"


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
    """Что установлено сейчас: zapret/tgws — из state, xray — живым запуском.

    Если в state версии нет, tg-ws-proxy читается из ресурсов своего exe:
    файл, скопированный вручную, иначе навсегда оставался «неизвестным».
    """
    state = state if state is not None else config.load_state()
    known = state.get("installed") or {}
    tgws = str(known.get("tgws") or "")
    raw_tgws = str(cfg.get("tgws_exe") or "").strip()
    if not tgws and raw_tgws:
        # Пустой путь в конфиге значит «не установлено» (так же думает
        # present_components) — на диск по умолчанию не подменяем.
        tgws = _exe_version(raw_tgws)
    return {
        "zapret": str(known.get("zapret") or ""),
        "tgwsproxy": tgws,
        "xray": _xray_version(),
        "widget": widget_version(),
    }


def _app_block(tag: str) -> dict:
    """Свежий релиз программы: ``installed/latest/outdated/url``.

    Пустой ``latest`` — не «актуально», а «не проверено»: GitHub мог не
    ответить или у репозитория ещё нет релизов. Это не ошибка проверки,
    но и не повод писать «обновлений нет».
    """
    installed = widget_version()
    block = {
        "installed": installed,
        "latest": str(tag or ""),
        "outdated": bool(tag) and _is_outdated(installed, tag),
        "url": APP_RELEASES_URL,
    }
    block["checked"] = bool(tag)
    if not tag:
        block["note"] = "не проверено"
    return block


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
        # Служба zapret: её файлы не наши — версию не ведём и не «обновляем».
        "service": service,
    }


def check_all(cfg: dict, state: dict) -> dict:
    """Сверяет установленные версии с последними релизами.

    ``missing`` — компоненты, которых нет ни в state, ни на диске (для
    zapret учитывается и служба Windows). ``network_error`` появляется,
    только когда GitHub не ответил ни по одному репозиторию: тогда пустой
    ``outdated`` не означает «всё актуально». ``app`` — отдельный блок про
    версию самой программы.
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
        here = bool(present.get(name))
        if name == "zapret" and present.get("service"):
            # Версию службы мы не знаем и менять её файлы нельзя: не врём
            # ни «устарело», ни «актуально».
            is_outdated = False
        elif here:
            is_outdated = _needs_update(have, tag, True)
        else:
            is_outdated = False
        if tag:
            seen[name] = tag
        info = {"latest": tag, "installed": have, "outdated": bool(is_outdated)}
        if name == "zapret" and present.get("service"):
            info["service"] = True
        found[name] = info
        if is_outdated:
            outdated.append(name)
        if not have and not here:
            missing.append(name)
    if not present.get("refilter"):
        missing.append("refilter")
    # Версии, прочитанные с диска (tgws из ресурсов exe), фиксируем сразу:
    # иначе «неизвестная» версия снова приведёт к лишней переустановке.
    known = state.get("installed")
    known = dict(known) if isinstance(known, dict) else {}
    for name, key in (("zapret", "zapret"), ("tgwsproxy", "tgws"), ("xray", "xray")):
        value = str(installed.get(name) or "")
        if value and value != known.get(key):
            known[key] = value
    if known:
        state["installed"] = known
    result = {"checked": time.time(), "components": found,
              "outdated": outdated, "missing": missing,
              "app": _app_block(str(latest.get("app") or ""))}
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
        can_update = _needs_update(known.get("zapret", ""), latest.get("zapret", ""),
                                   was_there)
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
    have_tgws = str(known.get("tgws") or "") or _exe_version(
        str(cfg.get("tgws_exe") or "").strip())
    can_update = _needs_update(have_tgws, latest.get("tgwsproxy", ""), was_there)
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
            if not version:
                version = _exe_version(cfg.get("tgws_exe") or tgws.get("exe") or "")
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
    can_update = _needs_update(have_xray, latest.get("xray", ""), was_there)
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
    # Версию, прочитанную с диска, тоже фиксируем: компонент, поставленный
    # вручную, иначе навсегда останется «неизвестным» и будет переставляться.
    if not known.get("tgws"):
        found_tgws = _exe_version(str(cfg.get("tgws_exe") or "").strip())
        if found_tgws:
            known["tgws"] = found_tgws
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
