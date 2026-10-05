"""Сбор статусов: процессы Happ/winws/tg-ws-proxy, порт, список стратегий."""

from __future__ import annotations

import re
import socket
import subprocess
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

TGWS_RE = re.compile(r"tg[-_ ]?ws[-_ ]?proxy", re.I)
HAPP_RE = re.compile(r"^happ", re.I)


def _processes() -> list[tuple[str, str]]:
    if psutil is not None:
        result = []
        for proc in psutil.process_iter(["name", "cmdline"]):
            name = proc.info.get("name") or ""
            cmdline = " ".join(proc.info.get("cmdline") or [])
            result.append((name, cmdline))
        return result
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=10
        ).stdout
    except OSError:
        return []
    procs = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if parts:
            procs.append((parts[0], ""))
    return procs


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def strategy_digest(cmdline: str) -> str:
    modes = []
    for mode in re.findall(r"--dpi-desync=([^\s\"]+)", cmdline):
        if mode not in modes:
            modes.append(mode)
    return " ".join(modes[:3])


def collect(cfg: dict) -> dict:
    procs = _processes()
    winws = [(name, cmd) for name, cmd in procs if name.lower().startswith("winws")]
    happ = [name for name, _ in procs if HAPP_RE.match(name or "")]
    tgws = [(name, cmd) for name, cmd in procs if TGWS_RE.search(name or "") or TGWS_RE.search(cmd or "")]
    port = int(cfg.get("tgws_port") or 1443)
    zapret_dir = Path(cfg.get("zapret_dir") or "")
    bats = sorted(p.name for p in zapret_dir.glob("general*.bat")) if zapret_dir.is_dir() else []
    return {
        "happ": {"running": bool(happ), "process": happ[0] if happ else ""},
        "zapret": {
            "running": bool(winws),
            "process": winws[0][0] if winws else "",
            "digest": strategy_digest(winws[0][1]) if winws else "",
            "dir": str(zapret_dir) if zapret_dir.is_dir() else "",
            "bats": bats,
        },
        "tgwsproxy": {"running": bool(tgws), "port": port, "listening": port_open(port)},
    }
