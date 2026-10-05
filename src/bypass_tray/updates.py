"""Проверка обновлений компонентов через GitHub Releases."""

from __future__ import annotations

import json
import time
import urllib.request

REPOS = {
    "zapret": "Flowseal/zapret-discord-youtube",
    "tgwsproxy": "Flowseal/tg-ws-proxy",
}


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


def check(state: dict) -> dict:
    """Сравнивает последние теги релизов с ранее увиденными.

    Возвращает {'result': …, 'new': ['zapret → vX', …]}.
    """
    seen = state.setdefault("seen_versions", {})
    components = {}
    new_releases = []
    for name, repo in REPOS.items():
        tag = _latest_tag(repo)
        previous = seen.get(name, "")
        is_new = bool(tag and previous and tag != previous)
        components[name] = {"latest": tag, "previous": previous, "new": is_new}
        if tag and not previous:
            seen[name] = tag          # первый запуск: просто запоминаем
        if is_new:
            new_releases.append(f"{name} → {tag}")
            seen[name] = tag
    result = {"checked": time.time(), "components": components}
    state["updates"] = result
    return {"result": result, "new": new_releases}
