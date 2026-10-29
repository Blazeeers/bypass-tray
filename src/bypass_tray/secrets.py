"""Маскирование секретов в строках и структурах.

Конфиги серверов содержат UUID, пароли и ключи, а журнал панели, JSON для CLI
и отчёт диагностики видит пользователь — и может переслать их дальше. Поэтому
всё, что выходит наружу, проходит через эти функции.

Модуль чистый: без файлов, сети и глобального состояния. Схему ссылки и хост
функции сохраняют — иначе журнал станет нечитаемым; маскируется только то,
что является секретом.
"""

from __future__ import annotations

import base64
import json
import re

#: Чем заменяется секрет. Многоточие не входит в base64 и не ломает JSON.
MASK = "…"

#: Ссылки поддерживаемых протоколов целиком (тело — до пробела или кавычки).
#: Запятая и точка с запятой не входят в тело: так имя сервера с запятой
#: («#Сервер, Германия») остаётся вне маски и не искажается.
_LINK_RE = re.compile(
    r"(?P<scheme>vless|vmess|trojan|ss|ssr|hysteria2|hy2|tuic)://"
    r"(?P<body>[^\s\"'<>`,;]+)",
    re.IGNORECASE,
)

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

#: Параметры-секреты вида ``ключ = значение``, ``ключ: значение``,
#: ``"ключ": "значение"`` вне ссылки. Кавычку ключа (``"password":``) и кавычки
#: значения сохраняем в выводе: маскируется только само значение.
_PARAM_RE = re.compile(
    r"(?P<key>\b(?:password|passwd|psk|publicKey|privateKey|shortId|pswd|"
    r"sid|pbk|spx|obfs[-_]?password|token|secret|apikey|api_key|uuid)\b)"
    r"(?P<kq>[\"']?)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?:(?P<q>[\"'])(?P<quoted>[^\"'\n]+)(?P=q)"
    r"|(?P<plain>[^\s&,;}\"'<>`#]+))",
    re.IGNORECASE,
)

#: Пароль внутри ss-плагина: ``obfs-password=…`` и percent-encoded
#: ``obfs-password%3D…``; значение кончается на ``;`` или ``%3B``.
_PLUGIN_SECRET_RE = re.compile(
    r"(?P<key>obfs[-_]?password|password|passwd)"
    r"(?P<sep>%3[Dd]|=)"
    r"(?P<value>(?:(?!%3[bB])[^&;\s])*)",
    re.IGNORECASE,
)

#: Длинный base64-блок. Точка в просмотрах с обеих сторон оставляет в покое
#: домены и пути: иначе «verylongsubdomainname» в ``verylongsubdomainname.com``
#: считался бы base64.
_B64_RE = re.compile(
    r"(?<![A-Za-z0-9+/=._-])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=._-])"
)

#: Секретные параметры ссылки. Ключ нормализуется: регистр и разделители
#: не важны, поэтому ``shortId`` и ``shortid`` — одно и то же.
_SECRET_QUERY = frozenset({
    "pbk", "sid", "spx", "password", "passwd", "pswd", "psk", "publickey",
    "privatekey", "shortid", "uuid", "auth", "token", "secret",
    "key", "obfspassword", "obfs",
})

#: Ключи структур, значение которых — секрет по определению. Сравниваются
#: через ``_norm_key``, поэтому регистр, ``-`` и ``_`` не важны: ``API-Key``,
#: ``api_key`` и ``apikey`` — один и тот же ключ.
_SECRET_KEYS = frozenset({
    "uuid", "password", "passwd", "pswd", "psk", "publickey", "privatekey",
    "shortid", "pbk", "sid", "spx", "obfspassword",
    "apikey", "accesstoken", "refreshtoken", "bearer", "session", "cookie",
})

#: Неоднозначные ключи: в структуре ими зовут и секрет, и обычное имя или
#: режим (``{"id": "zapret"}``, ``{"auth": "noauth"}``, ``{"key": "wlan"}``).
#: Маскируем только значение, которое само похоже на секрет.
_AMBIGUOUS_KEYS = frozenset({"id", "key", "auth", "token", "secret"})

#: От этой длины строка маскируется, даже если цифр в ней нет: секрет такой
#: длины почти наверняка не человеческое слово.
_SECRET_MIN_LEN = 20

#: С этой длины значение маскируется, только если в нём есть цифра: так
#: ``TOKCOLON99`` прячется, а ``zapret``, ``noauth`` и ``wlan`` — нет.
_SECRET_DIGIT_MIN_LEN = 8


def _norm_key(key: object) -> str:
    """Имя ключа без регистра и разделителей: ``publicKey`` → ``publickey``."""
    return re.sub(r"[-_\s]", "", str(key or "")).lower()


def _looks_secret(value: object) -> bool:
    """Похоже ли значение на секрет, а не на имя или режим.

    Числа, флаги и ``None`` секретом не бывают никогда: это порты, счётчики и
    переключатели. UUID маскируется всегда. Строка маскируется, если она
    длинная (от ``_SECRET_MIN_LEN``) или если она хотя бы ``_SECRET_DIGIT_MIN_LEN``
    символов и содержит цифру. Человеческие слова — ``zapret``, ``noauth``,
    ``wlan``, ``direct`` — остаются читаемыми.
    """
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return False
    if isinstance(value, (bytes, bytearray)):
        text = value.decode("utf-8", "replace")
    else:
        text = str(value)
    text = text.strip()
    if not text:
        return False
    if _UUID_RE.fullmatch(text):
        return True
    if len(text) >= _SECRET_MIN_LEN:
        return True
    return (len(text) >= _SECRET_DIGIT_MIN_LEN
            and any(ch.isdigit() for ch in text))


def _b64_text(value: str) -> str:
    """Мягкий base64 → UTF-8; мусор даёт пустую строку."""
    raw = "".join((value or "").split())
    if not raw:
        return ""
    raw = raw.replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.b64decode(raw).decode("utf-8", "replace")
    except (TypeError, ValueError):
        return ""


def _json_from_b64(body: str) -> dict | None:
    """vmess-тело: base64 с JSON внутри; не разобралось — ``None``."""
    try:
        data = json.loads(_b64_text(body))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _split_fragment(body: str) -> tuple[str, str]:
    """Делит тело ссылки на часть до решётки и имя сервера после неё."""
    if "#" in body:
        body, fragment = body.split("#", 1)
        return body, fragment
    return body, ""


def _mask_query(query: str) -> str:
    """Маскирует значения секретных параметров, остальные оставляет как есть."""
    if not query:
        return query
    pairs = []
    for part in query.split("&"):
        key, sep, value = part.partition("=")
        if sep and value and _norm_key(key) in _SECRET_QUERY:
            pairs.append(f"{key}={MASK}")
        else:
            pairs.append(part)
    return "&".join(pairs)


def _redact_generic(scheme: str, body: str, fragment: str) -> str:
    """vless/trojan/hysteria2/tuic: маска на userinfo и секретные параметры."""
    rest = body
    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)
    if "@" in rest:
        # rsplit: в пароле может встретиться «@», хост — всегда справа.
        _, host = rest.rsplit("@", 1)
        rest = f"{MASK}@{host}"
    out = f"{scheme}://{rest}"
    if query:
        out += "?" + _mask_query(query)
    if fragment:
        out += "#" + fragment
    return out


def _redact_vmess(body: str, fragment: str) -> str:
    """vmess: тело — base64 JSON; из него читаем хост и имя, их и показываем."""
    data = _json_from_b64(body)
    if data is not None:
        host = str(data.get("add") or "").strip()
        port = str(data.get("port") or "").strip()
        name = str(data.get("ps") or "").strip() or fragment
        tail = ""
        if host:
            tail = f"@{host}" + (f":{port}" if port else "")
        if name:
            tail += "#" + name
        return f"vmess://{MASK}{tail}"
    # Уже замаскированное тело второй раз не разбирается — хвост сохраняем,
    # иначе повторный проход терял бы хост. Имя сервера живёт во фрагменте и
    # потерялось бы вместе с ним — поэтому дописываем его обратно.
    if "@" in body:
        _, tail = body.rsplit("@", 1)
        out = f"vmess://{MASK}@{tail}"
    else:
        out = f"vmess://{MASK}"
    if fragment:
        out += "#" + fragment
    return out


def _redact_ss(body: str, fragment: str) -> str:
    """ss: ``base64(метод:пароль)@хост`` или ``base64(вся ссылка)``."""
    location = ""
    if "@" in body:
        _, location = body.rsplit("@", 1)
    else:
        decoded = _b64_text(body)
        if "@" in decoded:
            _, location = decoded.rsplit("@", 1)
    if not location:
        return f"ss://{MASK}"
    rest, _, query = location.partition("?")
    out = f"ss://{MASK}@{rest}"
    if query:
        # Плагин obfs несёт пароль внутри своего значения; в percent-encoded
        # виде его видно как ``obfs-password%3D…``, поэтому чистим обе формы.
        query = _PLUGIN_SECRET_RE.sub(
            lambda m: f"{m.group('key')}{m.group('sep')}{MASK}",
            _mask_query(query))
        out += "?" + query
    if fragment:
        out += "#" + fragment
    return out


def redact_link(link: str) -> str:
    """Маскирует секреты одной ссылки: схема, хост и имя остаются.

    ``vless://uuid@host:443?pbk=…&sid=…#имя`` → ``vless://…@host:443?pbk=…``.
    Строку без схемы обрабатывает ``redact_text``.
    """
    text = link.strip() if isinstance(link, str) else ""
    if "://" not in text:
        return redact_text(text)
    scheme, body = text.split("://", 1)
    scheme = scheme.strip().lower()
    body, fragment = _split_fragment(body)
    if scheme == "vmess":
        return _redact_vmess(body, fragment)
    if scheme == "ss":
        return _redact_ss(body, fragment)
    if scheme == "ssr":
        # У ssr секрет — всё тело base64, читаемого хоста в нём нет.
        return f"{scheme}://{MASK}"
    return _redact_generic(scheme, body, fragment)


def _mask_param(match: re.Match) -> str:
    """Ключ, кавычки и разделитель остаются, значение заменяется маской."""
    quote = match.group("q") or ""
    return (f"{match.group('key')}{match.group('kq')}"
            f"{match.group('sep')}{quote}{MASK}{quote}")


def redact_text(text: str) -> str:
    """Маскирует секреты в произвольной строке, домены и пути не трогает.

    Идемпотентна: повторный вызов на замаскированной строке ничего не меняет.
    """
    if not isinstance(text, str) or not text:
        return text
    result = _LINK_RE.sub(
        lambda m: redact_link(f"{m.group('scheme')}://{m.group('body')}"), text)
    result = _UUID_RE.sub(MASK, result)
    result = _PARAM_RE.sub(_mask_param, result)
    result = _B64_RE.sub(MASK, result)
    return result


def _scalar(value) -> bool:
    """Значение, которое можно целиком заменить маской."""
    return isinstance(value, (str, bytes, bytearray, int, float))


def redact_obj(obj):
    """Рекурсивно маскирует секреты в dict/list по ключу и по значению.

    Структура сохраняется: dict остаётся dict, list — list. ``None`` в
    секретном поле остаётся ``None`` (секрета в нём нет), числа и байты
    не ломают обход. Циклические структуры обходятся: повторный контейнер
    отдаётся уже маскированной копией, а не оригиналом.
    """
    return _redact(obj, {})


def _redact(obj, seen: dict):
    if isinstance(obj, dict):
        marker = id(obj)
        if marker in seen:
            return seen[marker]
        out: dict = {}
        # Кладём результат до обхода: ссылка на самого себя разрешится в него
        # же, и второй (общий, не циклический) экземпляр не утечёт секретом.
        seen[marker] = out
        for key, value in obj.items():
            if _scalar(value) and value is not None:
                name = _norm_key(key)
                if name in _SECRET_KEYS:
                    out[key] = MASK
                    continue
                if name in _AMBIGUOUS_KEYS:
                    # ``id`` пункта health — "zapret", ``auth`` — "noauth":
                    # маскируем, только если значение само похоже на секрет.
                    out[key] = MASK if _looks_secret(value) else value
                    continue
            out[key] = _redact(value, seen)
        return out
    if isinstance(obj, list):
        marker = id(obj)
        if marker in seen:
            return seen[marker]
        out = []
        seen[marker] = out
        for item in obj:
            out.append(_redact(item, seen))
        return out
    if isinstance(obj, tuple):
        if id(obj) in seen:
            return obj
        seen[id(obj)] = obj
        return tuple(_redact(item, seen) for item in obj)
    if isinstance(obj, str):
        return redact_text(obj)
    return obj
