"""Встроенный список заблокированных в РФ доменов (Re:filter).

Файл ``data/geosite-refilter.dat`` собран проектом Re-filter-lists (MIT) и
содержит категорию ``REFILTER`` с очищенным списком заблокированных доменов.
При первом запуске он копируется рядом с ядром Xray и подключается правилом
``ext:<файл>:REFILTER`` — домены идут через VPN, но пользовательские правила,
аудит и YouTube/Discord (под zapret, пока он работает) остаются выше списка.

Файл проверяется минимальным разбором protobuf (GeoSiteList): одного размера
и сигнатуры мало — обрезанный файл роняет Xray. После скачивания обновления
дополнительно запускается ``xray.exe -test`` на временном конфиге.

Сеть используется только в :func:`update`; :func:`ensure` работает офлайн,
кроме явного автоскачивания при отсутствии поставки.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from . import config, xray
from .actions import CREATE_NO_WINDOW

FILE_NAME = "geosite-refilter.dat"
CATEGORY = "REFILTER"
SOURCE_REPO = "1andrevich/Re-filter-lists"
SOURCE_URL = "https://github.com/1andrevich/Re-filter-lists/releases/latest/download/geosite.dat"
#: Запасное значение для интерфейса, если файл ещё не разобран.
DOMAIN_COUNT = 81061
SOURCE_TAG = "19102026"

#: Минимальные требования к файлу.
MIN_SIZE = 1_000_000
MIN_COUNT = 50_000
#: Повторная попытка автоскачивания — не чаще раза в час.
DOWNLOAD_INTERVAL = 3600

_VALID_CACHE: dict[str, tuple[int, int, bool]] = {}


# ---- protobuf GeoSiteList -------------------------------------------------


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("varint за пределами файла")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("слишком длинный varint")


def _take(data: bytes, pos: int) -> tuple[bytes, int]:
    """Length-delimited поле: возвращает содержимое и новую позицию."""
    length, pos = _read_varint(data, pos)
    end = pos + length
    if end > len(data):
        raise ValueError("длина поля выходит за файл")
    return data[pos:end], end


def _skip_field(data: bytes, pos: int, wire: int) -> int:
    if wire == 0:
        _value, pos = _read_varint(data, pos)
        return pos
    if wire == 1:
        return pos + 8
    if wire == 2:
        _blob, pos = _take(data, pos)
        return pos
    if wire == 5:
        return pos + 4
    raise ValueError(f"неизвестный wire-тип {wire}")


def _parse_site(blob: bytes) -> tuple[str, int]:
    """Одна запись GeoSite: код категории и число доменов."""
    pos = 0
    country = ""
    domains = 0
    size = len(blob)
    while pos < size:
        key, pos = _read_varint(blob, pos)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ValueError("нулевое поле")
        if field == 1 and wire == 2:
            raw, pos = _take(blob, pos)
            country = raw.decode("utf-8", "replace")
        elif field == 2 and wire == 2:
            _raw, pos = _take(blob, pos)
            domains += 1
        else:
            pos = _skip_field(blob, pos, wire)
            if pos > size:
                raise ValueError("поле выходит за запись")
    return country, domains


def _parse(path: Path) -> dict:
    """Минимальный разбор GeoSiteList: категория и число доменов.

    Никогда не бросает наружу: при любой ошибке возвращает ``ok=False``.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return {"ok": False, "count": 0, "category": ""}
    try:
        pos = 0
        total = 0
        category = ""
        size = len(data)
        while pos < size:
            key, pos = _read_varint(data, pos)
            field, wire = key >> 3, key & 7
            if field == 0:
                raise ValueError("нулевое поле")
            if field == 1 and wire == 2:
                blob, pos = _take(data, pos)
                country, domains = _parse_site(blob)
                if country == CATEGORY:
                    category = country
                    total += domains
            else:
                pos = _skip_field(data, pos, wire)
                if pos > size:
                    raise ValueError("поле выходит за файл")
        return {"ok": True, "count": total, "category": category}
    except Exception:  # noqa: BLE001
        return {"ok": False, "count": 0, "category": ""}


# ---- пути и состояние -----------------------------------------------------


def packaged_path() -> Path:
    """Файл из поставки (в PyInstaller — распакованные данные)."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "data" / FILE_NAME
    return Path(__file__).resolve().parents[2] / "data" / FILE_NAME


def bin_path() -> Path:
    return xray.bin_dir() / FILE_NAME


def state_path() -> Path:
    return config.config_dir() / "blocked-list.json"


def _read_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(patch: dict) -> dict:
    state = _read_state()
    state.update(patch)
    try:
        config.atomic_write(state_path(),
                            json.dumps(state, ensure_ascii=False, indent=2))
    except Exception:  # noqa: BLE001
        pass
    return state


def _valid(path: Path) -> bool:
    """Файл — целый GeoSiteList с категорией REFILTER и достаточным числом."""
    try:
        stat = path.stat()
    except OSError:
        return False
    if not path.is_file() or stat.st_size <= MIN_SIZE:
        return False
    cached = _VALID_CACHE.get(str(path))
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    parsed = _parse(path)
    good = bool(parsed["ok"] and parsed["category"] == CATEGORY
                and parsed["count"] > MIN_COUNT)
    _VALID_CACHE[str(path)] = (stat.st_mtime_ns, stat.st_size, good)
    return good


# ---- установка и обновление ----------------------------------------------


def _copy_atomic(source: Path, dest: Path) -> bool:
    tmp = dest.with_name(dest.name + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, tmp)
        os.replace(tmp, dest)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _download_allowed() -> bool:
    state = _read_state()
    try:
        last = float(state.get("last_attempt") or 0)
    except (TypeError, ValueError):
        last = 0.0
    return time.time() - last >= DOWNLOAD_INTERVAL


def ensure(source: Path | None = None, dest: Path | None = None,
           auto_download: bool = True) -> Path | None:
    """Гарантирует рабочий файл рядом с ядром.

    Валидный файл в ``bin`` не перезаписывается. Если его нет — копируется
    поставка. Если и поставки нет, при ``auto_download`` пробуем скачать
    (не чаще раза в час). Возвращает ``None``, если валидного файла нет.
    """
    target = Path(dest) if dest is not None else bin_path()
    if _valid(target):
        return target
    origin = Path(source) if source is not None else packaged_path()
    if _valid(origin):
        if _copy_atomic(origin, target) and _valid(target):
            return target
        return None
    if source is None and auto_download and _download_allowed():
        update()
        if _valid(target):
            return target
    return None


def _xray_test(data_path: Path) -> tuple[bool, str]:
    """Проверяет файл через ``xray.exe -test`` на временном конфиге.

    Возвращает ``(ok, message)``. Если ядро недоступно — доверяем разбору
    (``ok=True`` с предупреждением).
    """
    try:
        exe = xray.xray_path()
    except Exception:  # noqa: BLE001
        exe = Path("xray.exe")
    if not exe.is_file():
        return True, "xray.exe не найден — проверен только разбор файла"
    try:
        with tempfile.TemporaryDirectory(prefix="bypass-tray-refilter-") as tmp:
            folder = Path(tmp)
            shutil.copyfile(data_path, folder / FILE_NAME)
            config_data = {
                "log": {"loglevel": "warning"},
                "inbounds": [{
                    "tag": "test",
                    "listen": "127.0.0.1",
                    "port": 18099,
                    "protocol": "socks",
                    "settings": {"auth": "noauth", "udp": False},
                }],
                "outbounds": [{"tag": "direct", "protocol": "freedom"}],
                "routing": {"rules": [{
                    "type": "field", "outboundTag": "direct",
                    "domain": [f"ext:{FILE_NAME}:{CATEGORY}"],
                }]},
            }
            config_path = folder / "config.json"
            config_path.write_text(json.dumps(config_data, ensure_ascii=False),
                                   encoding="utf-8")
            env = dict(os.environ, XRAY_LOCATION_ASSET=str(folder))
            completed = subprocess.run(
                [str(exe), "-test", "-c", str(config_path)],
                cwd=str(exe.parent), env=env, capture_output=True, timeout=60,
                creationflags=CREATE_NO_WINDOW,
            )
            output = ((completed.stdout or b"") + (completed.stderr or b""))
            text = output.decode("utf-8", "replace")
            if completed.returncode == 0:
                return True, ""
            return False, text[-600:].strip() or f"код {completed.returncode}"
    except subprocess.TimeoutExpired:
        return False, "xray -test не завершился за 60 с"
    except OSError as exc:
        return True, f"xray -test не запустился: {type(exc).__name__}"


def update(progress=None) -> dict:
    """Скачивает свежий список, проверяет и атомарно заменяет файл.

    ``progress(percent, mb_done, mb_total)`` вызывается по мере загрузки.
    При ошибке временный файл удаляется, старый список не трогается;
    время попытки пишется в состояние, чтобы не долбить сеть каждый старт.
    """
    target = bin_path()
    part = target.with_name(target.name + ".part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(SOURCE_URL,
                                         headers={"User-Agent": "bypass-tray"})
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
                        try:
                            progress(int(done * 100 / total), done // 1048576,
                                     total // 1048576)
                        except Exception:  # noqa: BLE001
                            pass
        if not _valid(part):
            raise ValueError("файл не прошёл разбор GeoSiteList/REFILTER")
        tested, message = _xray_test(part)
        if not tested:
            raise RuntimeError("xray -test: " + (message or "ошибка"))
        parsed = _parse(part)
        os.replace(part, target)
        at = time.time()
        size = target.stat().st_size
        state = _write_state({
            "at": at,
            "size": size,
            "count": int(parsed.get("count") or 0),
            "source": SOURCE_REPO,
            "category": CATEGORY,
            "version": time.strftime("%d%m%Y", time.localtime(at)),
            "date": time.strftime("%Y-%m-%d", time.localtime(at)),
            "last_attempt": at,
        })
        result = {"ok": True, "size": size, "updated": state["at"],
                  "count": state["count"], "path": str(target)}
        if message:
            result["warning"] = message
        return result
    except Exception as exc:  # noqa: BLE001
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        _write_state({"last_attempt": time.time(),
                      "error": f"{type(exc).__name__}: {exc}"[:300]})
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def info(cfg: dict | None = None) -> dict:
    """Состояние списка: включён ли, установлен ли, сколько доменов и когда."""
    if cfg is None:
        try:
            cfg = config.load_config()
        except Exception:  # noqa: BLE001
            cfg = {}
    path = bin_path()
    installed = _valid(path)
    parsed = _parse(path) if installed else {"ok": False, "count": 0, "category": ""}
    state = _read_state()
    try:
        state_count = int(state.get("count") or 0)
    except (TypeError, ValueError):
        state_count = 0
    count = int(parsed.get("count") or 0) if installed else state_count
    if not count:
        count = DOMAIN_COUNT
    size = 0
    updated = 0.0
    if installed:
        try:
            size = path.stat().st_size
            updated = path.stat().st_mtime
        except OSError:
            installed = False
    try:
        state_at = float(state.get("at") or 0)
    except (TypeError, ValueError):
        state_at = 0.0
    if state_at:
        updated = state_at
    return {
        "enabled": bool((cfg or {}).get("blocked_list_enabled", True)),
        "installed": installed,
        "size": int(size),
        "updated": float(updated),
        "source": SOURCE_REPO,
        "category": str(parsed.get("category") or state.get("category") or CATEGORY),
        "count": int(count),
        "version": str(state.get("version") or SOURCE_TAG),
        "date": str(state.get("date") or ""),
        # Время последнего успешного обновления (0, если обновлений не было).
        "state_at": float(state_at),
    }
