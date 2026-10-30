"""Ключ сети и выбранная стратегия по сети.

Сеть определяется без PowerShell и без UAC: SSID — из ``netsh wlan show
interfaces``, шлюз — из реестра TCP/IP, MAC — из ``arp -a``. Ключ сети нужен,
чтобы при смене Wi-Fi или провода виджет вспоминал, какая стратегия обхода
работала именно здесь.

Всё, что можно проверить без машины (разбор вывода), вынесено в чистые
функции: ``parse_ssid``, ``parse_gateway_registry``, ``parse_arp_mac``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from .actions import CREATE_NO_WINDOW, _decode
from .config import atomic_write, config_dir

_INTERFACES_KEY = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"

#: Строка SSID, но не BSSID: ``^\s*SSID`` не совпадёт с «BSSID».
_SSID_RE = re.compile(r"^\s*SSID\s*:\s*(?P<name>.+?)\s*$")

#: Шесть байт через "-" или ":" — формат вывода ``arp -a``.
_MAC_RE = re.compile(
    r"\b([0-9a-f]{2})[-:]([0-9a-f]{2})[-:]([0-9a-f]{2})"
    r"[-:]([0-9a-f]{2})[-:]([0-9a-f]{2})[-:]([0-9a-f]{2})\b",
    re.IGNORECASE,
)

#: Строка шлюза в ``ipconfig`` на английской и русской Windows.
_IPCONFIG_GATEWAY_RE = re.compile(
    r"(?:Default Gateway|Основной шлюз)[^:]*:\s*(\S+)", re.IGNORECASE
)

#: Таймаут внешней команды: сеть нужна для выбора стратегии, а не для отчёта,
#: и её нельзя ждать по 10 с внутри цикла трея.
_RUN_TIMEOUT = 2.0
#: Сколько секунд держать результат определения сети. ``current()`` зовут из
#: цикла трея, а каждый вызов — до трёх внешних процессов.
_CACHE_SECONDS = 45.0
_CACHED: dict = {}
_CACHED_AT = 0.0


# ---- чистые парсеры -------------------------------------------------------


def parse_ssid(text: str) -> str:
    """Имя беспроводной сети из вывода ``netsh wlan show interfaces``."""
    for line in (text or "").splitlines():
        match = _SSID_RE.match(line)
        if match:
            return match.group("name")
    return ""


def _flatten(values) -> list:
    """Разворачивает REG_SZ / REG_MULTI_SZ в плоский список значений."""
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        return [values]
    result: list = []
    for item in values:
        if isinstance(item, (list, tuple)):
            result.extend(item)
        else:
            result.append(item)
    return result


def parse_gateway_registry(values) -> str:
    """Первый непустой адрес шлюза из значений реестра.

    ``DhcpDefaultGateway`` и ``DefaultGateway`` приходят как REG_MULTI_SZ
    (список) или REG_SZ (строка). Пустые адреса и ``0.0.0.0`` пропускаем:
    Windows хранит их у неактивных интерфейсов.
    """
    for value in _flatten(values):
        text = str(value or "").strip()
        if text and text not in ("0.0.0.0", "::", "::1"):
            return text
    return ""


def parse_arp_mac(text: str) -> str:
    """MAC из вывода ``arp -a`` в виде ``aa:bb:cc:dd:ee:ff``."""
    match = _MAC_RE.search(text or "")
    if not match:
        return ""
    return ":".join(part.lower() for part in match.groups())


def parse_ipconfig_gateway(text: str) -> str:
    """Шлюз из вывода ``ipconfig`` — запасной путь, если реестр пуст."""
    match = _IPCONFIG_GATEWAY_RE.search(text or "")
    return match.group(1).strip() if match else ""


# ---- определение сети -----------------------------------------------------


def _run(args: list[str], timeout: float = _RUN_TIMEOUT) -> str:
    try:
        completed = subprocess.run(
            args, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return _decode(completed.stdout)


def _wifi_ssid() -> str:
    return parse_ssid(_run(["netsh", "wlan", "show", "interfaces"]))


def _interface_gateway(winreg_module, interfaces, name: str) -> str:
    try:
        with winreg_module.OpenKey(interfaces, name) as key:
            for value_name in ("DhcpDefaultGateway", "DefaultGateway"):
                try:
                    raw = winreg_module.QueryValueEx(key, value_name)[0]
                except OSError:
                    continue
                found = parse_gateway_registry(raw)
                if found:
                    return found
    except OSError:
        return ""
    return ""


def _gateway() -> str:
    """Шлюз из реестра TCP/IP, при неудаче — из ``ipconfig``."""
    if sys.platform.startswith("win"):
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _INTERFACES_KEY) as interfaces:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(interfaces, index)
                    except OSError:
                        break
                    index += 1
                    found = _interface_gateway(winreg, interfaces, name)
                    if found:
                        return found
        except OSError:
            pass
    return parse_ipconfig_gateway(_run(["ipconfig"]))


def _mac(ip: str) -> str:
    if not ip:
        return ""
    return parse_arp_mac(_run(["arp", "-a", ip]))


def network_key(gateway: str, ssid: str, mac: str) -> str:
    """Один устойчивый ключ сети: шлюз, иначе SSID, иначе MAC.

    Раньше ключом был MAC, а он пуст сразу после загрузки (``arp -a`` ещё не
    знает шлюз): запись и чтение получали разные ключи, сохранённая стратегия
    не находилась, а ``remember`` плодил дубли на одну сеть. Шлюз из реестра
    есть всегда и не меняется, пока сеть та же.
    """
    for value in (gateway, ssid, mac):
        text = str(value or "").strip()
        if text:
            return text
    return "default"


def current(force: bool = False) -> dict:
    """Текущая сеть: ключ, SSID, шлюз, MAC и человеческое имя.

    Результат держится ``_CACHE_SECONDS``: вызов идёт из цикла трея, а это до
    трёх внешних процессов (``netsh``, реестр/``ipconfig``, ``arp``).
    ``force=True`` — перечитать немедленно.
    """
    global _CACHED, _CACHED_AT
    if _CACHED and not force and time.monotonic() - _CACHED_AT <= _CACHE_SECONDS:
        return dict(_CACHED)
    ssid = _wifi_ssid()
    gateway = _gateway()
    mac = _mac(gateway) if gateway else ""
    key = network_key(gateway, ssid, mac)
    if ssid:
        name = ssid
    elif gateway:
        name = f"Проводная сеть {gateway}"
    else:
        name = "Сеть"
    net = {"key": key, "ssid": ssid, "gateway": gateway, "mac": mac, "name": name}
    _CACHED, _CACHED_AT = dict(net), time.monotonic()
    return net


# ---- стратегия по сети ----------------------------------------------------


def path() -> Path:
    return config_dir() / "networks.json"


def all() -> dict:
    """Сохранённые стратегии по сетям: ``{ключ: {"bat", "at", "ssid", "gateway"}}``."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    networks = data.get("networks") if isinstance(data, dict) else None
    return networks if isinstance(networks, dict) else {}


def _record(key: str) -> dict:
    """Запись сети: прямо по ключу, иначе по сохранённому в ней шлюзу.

    Старые версии писали ключом MAC, а он сразу после загрузки пуст — тогда
    шлюз лежал внутри записи, и найденное по нему подходит для чтения.
    """
    networks = all()
    info = networks.get(key)
    if isinstance(info, dict) and info:
        return info
    for saved in networks.values():
        if isinstance(saved, dict) and str(saved.get("gateway") or "") == key:
            return saved
    return {}


def strategy_for(key: str) -> str:
    """Какая стратегия zapret работала в этой сети (пусто — неизвестно)."""
    return str(_record(key).get("bat") or "")


def server_for(key: str) -> str:
    """Какой сервер запомнен за этой сетью (пусто — не выбран).

    Старые записи поля не имеют и читаются как «сервер не выбран».
    """
    return str(_record(key).get("server") or "")


def remember(key: str, bat: str, info: dict | None = None,
             server: str | None = None) -> None:
    """Запоминает стратегию и сервер для сети. ``info`` — из ``current()``.

    ``server=None`` — не трогать уже сохранённый сервер: подбор стратегии
    зовёт ``remember`` без него и не должен стирать выбор человека. Пустая
    строка сервер очищает.
    """
    if not key:
        return
    networks = all()
    source = info or {}
    gateway = str(source.get("gateway") or "")
    previous = networks.get(key)
    if not isinstance(previous, dict):
        previous = {}
    if server is None:
        saved_server = str(previous.get("server") or "")
    else:
        saved_server = str(server or "").strip()
    networks[key] = {
        "bat": str(bat or ""),
        "server": saved_server,
        "at": time.time(),
        "ssid": str(source.get("ssid") or ""),
        "gateway": gateway,
    }
    if gateway:
        # Дубль от старой версии (ключом был MAC, шлюз тот же) больше не нужен.
        for old_key, saved in list(networks.items()):
            if old_key == key or not isinstance(saved, dict):
                continue
            if str(saved.get("gateway") or "") == gateway:
                del networks[old_key]
    atomic_write(path(), json.dumps({"networks": networks}, ensure_ascii=False, indent=2))
