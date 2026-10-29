"""Тесты маскирования секретов: ссылки, параметры, структуры.

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray.secrets import (  # noqa: E402
    MASK, redact_link, redact_obj, redact_text,
)

UUID = "550e8400-e29b-41d4-a716-446655440000"


class LinkTests(unittest.TestCase):
    """Секрет уходит, схема и хост остаются читаемыми."""

    def assert_masked(self, text: str, secret: str, host: str) -> str:
        result = redact_text(text)
        self.assertNotIn(secret, result)
        self.assertIn(host, result)
        self.assertIn(MASK, result)
        return result

    def test_vless(self) -> None:
        link = (f"vless://{UUID}@vpn.example.com:443"
                "?type=ws&security=reality&pbk=BIGPUBLICKEYBIGPUBLICKEY"
                "&sid=ab12cd34&sni=vpn.example.com#Германия")
        result = self.assert_masked(link, UUID, "vpn.example.com")
        self.assertTrue(result.startswith("vless://"))
        self.assertNotIn("ab12cd34", result)

    def test_vmess(self) -> None:
        payload = {"v": "2", "ps": "Япония", "add": "vm.example.net",
                   "port": "443", "id": UUID, "net": "ws", "tls": "tls"}
        body = base64.b64encode(
            json.dumps(payload).encode("utf-8")).decode("ascii")
        result = self.assert_masked(f"vmess://{body}", UUID, "vm.example.net")
        self.assertIn("Япония", result)
        self.assertNotIn(body, result)

    def test_trojan(self) -> None:
        link = "trojan://secretpass123@trojan.example.org:443?sni=t.example#Т"
        self.assert_masked(link, "secretpass123", "trojan.example.org")

    def test_ss(self) -> None:
        credentials = base64.b64encode(
            b"aes-256-gcm:secretpass456").decode("ascii")
        link = f"ss://{credentials}@ss.example.io:8388#SS"
        result = self.assert_masked(link, credentials, "ss.example.io")
        self.assertIn("8388", result)

    def test_redact_link_direct(self) -> None:
        result = redact_link(f"vless://{UUID}@host.name:443")
        self.assertEqual(result, f"vless://{MASK}@host.name:443")


class TextTests(unittest.TestCase):
    """Маскирование внутри произвольного текста."""

    def test_uuid_alone(self) -> None:
        result = redact_text(f"ключ сервера {UUID} недействителен")
        self.assertNotIn(UUID, result)
        self.assertEqual(result, f"ключ сервера {MASK} недействителен")

    def test_password_param(self) -> None:
        result = redact_text("подключение password=secret123 отклонено")
        self.assertEqual(
            result, f"подключение password={MASK} отклонено")
        self.assertNotIn("secret123", result)

    def test_plain_domain_and_url_unchanged(self) -> None:
        for text in ("example.com", "https://example.com/some/path?x=1",
                     "проверка www.instagram.com прошла",
                     "verylongsubdomainnameexample.com"):
            with self.subTest(text=text):
                self.assertEqual(redact_text(text), text)

    def test_idempotent(self) -> None:
        text = (f"vless://{UUID}@vpn.example.com:443?pbk=ABCDEFGHIJKLMNOP"
                "QRSTUVWXYZ012345&password=secret123#Имя")
        once = redact_text(text)
        self.assertEqual(redact_text(once), once)

    def test_quoted_and_spaced_params(self) -> None:
        """Пробелы, ``=``/``:`` и кавычки: форма строки не ломается."""
        cases = (
            ('password = "spacesecret7"', "spacesecret7"),
            ("PASSWORD=Secret1", "Secret1"),
            ("password = Secret2", "Secret2"),
            ('"password": "jsonsecret42"', "jsonsecret42"),
            ('{"api_key": "apikeysecret11"}', "apikeysecret11"),
            ("token: TOKCOLON99", "TOKCOLON99"),
        )
        for text, secret in cases:
            with self.subTest(text=text):
                result = redact_text(text)
                self.assertNotIn(secret, result)
                self.assertEqual(result, text.replace(secret, MASK))

    def test_reality_params_in_text(self) -> None:
        """Ключи Reality маскируются и вне ссылки."""
        text = ("pswd=PSWDKEY123 sid=SIDVALUE99 "
                "pbk=PBKVALUE99 spx=SPXVALUE99")
        result = redact_text(text)
        expected = text
        for secret in ("PSWDKEY123", "SIDVALUE99", "PBKVALUE99", "SPXVALUE99"):
            self.assertNotIn(secret, result)
            expected = expected.replace(secret, MASK)
        self.assertEqual(result, expected)

    def test_plain_text_untouched(self) -> None:
        """Обычный текст журнала: домены, пути, имена и порты целы."""
        for text in ("example.com", "https://example.com/path?x=1",
                     "проверил youtube.com", "Германия", "порт 443"):
            with self.subTest(text=text):
                self.assertEqual(redact_text(text), text)

    def test_ss_plugin_password(self) -> None:
        """ss с obfs: пароль плагина в percent-encoded виде тоже прячется."""
        credentials = base64.b64encode(
            b"aes-256-gcm:secretpass456").decode("ascii")
        plugin = "obfs-local%3Bobfs%3Dhttp%3Bobfs-password%3DPLUGPASS"
        link = f"ss://{credentials}@ss.example.io:8388?plugin={plugin}#SS"
        result = redact_text(link)
        self.assertNotIn("PLUGPASS", result)
        self.assertNotIn(credentials, result)
        self.assertIn("obfs-password", result)
        self.assertIn("ss.example.io:8388", result)

    def test_vmess_idempotent_fragment(self) -> None:
        """Повторный проход не теряет имя сервера после ``#``."""
        payload = {"v": "2", "ps": "Япония", "add": "vm.example.net",
                   "port": "443", "id": UUID, "net": "ws"}
        body = base64.b64encode(
            json.dumps(payload).encode("utf-8")).decode("ascii")
        once = redact_text(f"vmess://{body}#Япония")
        self.assertIn("vm.example.net:443", once)
        self.assertIn("Япония", once)
        self.assertNotIn(body, once)
        self.assertEqual(redact_text(once), once)

    def test_long_base64(self) -> None:
        blob = base64.b64encode(b"x" * 40).decode("ascii")
        result = redact_text(f"токен {blob} в логе")
        self.assertNotIn(blob, result)
        self.assertEqual(result, f"токен {MASK} в логе")


class ObjectTests(unittest.TestCase):
    """redact_obj: структура и типы не ломаются."""

    def test_nested_structures(self) -> None:
        payload = {
            "server": {"address": "vpn.example.com", "port": 443,
                       "uuid": UUID, "password": "secret123"},
            "items": [{"id": "zapret", "state": "ok", "token": "abc"},
                      {"id": UUID, "ms": 12.5}],
            "empty": None,
            "raw": b"\x01\x02",
            "count": 7,
        }
        result = redact_obj(payload)
        self.assertEqual(result["server"]["uuid"], MASK)
        self.assertEqual(result["server"]["password"], MASK)
        # address/port — не секреты.
        self.assertEqual(result["server"]["address"], "vpn.example.com")
        self.assertEqual(result["server"]["port"], 443)
        # Короткий id пункта health остаётся читаемым, UUID в id — нет.
        self.assertEqual(result["items"][0]["id"], "zapret")
        self.assertEqual(result["items"][1]["id"], MASK)
        # "abc" — не токен: неоднозначный ключ маскируется по значению.
        self.assertEqual(result["items"][0]["token"], "abc")
        self.assertEqual(result["items"][1]["ms"], 12.5)
        self.assertIsNone(result["empty"])
        self.assertEqual(result["raw"], b"\x01\x02")
        self.assertEqual(result["count"], 7)
        self.assertIsInstance(result["items"], list)
        self.assertIsInstance(result["server"], dict)
        # bytes в структуре json не сериализует, поэтому проверяем целиком.
        self.assertNotIn(UUID, repr(result))

    def test_scalars_do_not_raise(self) -> None:
        for value in (None, 0, 3.5, b"bytes", [1, None], {"a": None}):
            with self.subTest(value=value):
                self.assertEqual(redact_obj(value), value)

    def test_value_redacted_by_text(self) -> None:
        payload = {"detail": f"сервер {UUID} недоступен"}
        result = redact_obj(payload)
        self.assertNotIn(UUID, result["detail"])

    def test_reality_keys_masked_in_obj(self) -> None:
        """pbk/sid/spx/pswd — секреты по определению, маска всегда."""
        payload = {"pbk": "PBKVALUE99", "sid": "SIDVALUE99",
                   "spx": "SPXVALUE99", "pswd": "PSWDKEY123"}
        result = redact_obj(payload)
        for key in ("pbk", "sid", "spx", "pswd"):
            with self.subTest(key=key):
                self.assertEqual(result[key], MASK)

    def test_ambiguous_keys_keep_plain_values(self) -> None:
        """``id``/``auth``/``key``/``token``: маска только для секрета."""
        plain = {"id": "zapret", "auth": "noauth", "key": "wlan"}
        self.assertEqual(redact_obj(plain), plain)

        secret = {"id": UUID, "auth": "abc123def456ghi789",
                  "token": "s3cr3tT0ken0123456789", "key": "a1b2c3d4e5f6a7b8"}
        masked = redact_obj(secret)
        for name in ("id", "auth", "token", "key"):
            with self.subTest(key=name):
                self.assertEqual(masked[name], MASK)

    def test_cycle_does_not_raise(self) -> None:
        """Циклический dict обходится без RecursionError."""
        data: dict = {"password": "secret123", "self": None}
        data["self"] = data
        result = redact_obj(data)
        self.assertEqual(result["password"], MASK)
        self.assertIs(result["self"], result)

    def test_shared_dict_still_masked(self) -> None:
        """Один и тот же вложенный dict в двух местах маскируется везде."""
        inner = {"pbk": "PBKVALUE99"}
        result = redact_obj({"a": inner, "b": inner})
        self.assertEqual(result["a"]["pbk"], MASK)
        self.assertEqual(result["b"]["pbk"], MASK)


class SecretKeyTests(unittest.TestCase):
    """Ключи-секреты маскируются всегда: значение не важно."""

    def test_always_masked_keys(self) -> None:
        """api_key/apikey/access_token/refresh_token и формы с дефисом и капсом."""
        keys = ("api_key", "apikey", "api-key", "API_KEY", "Api-Key",
                "access_token", "access-token", "ACCESS-TOKEN", "Access_Token",
                "refresh_token", "refresh-token", "REFRESH-TOKEN",
                "bearer", "session", "cookie", "Session", "COOKIE")
        for key in keys:
            with self.subTest(key=key):
                # "x1" слишком короток для правила «похоже на секрет»:
                # маска здесь может взяться только из набора ключей.
                self.assertEqual(redact_obj({key: "x1"})[key], MASK)

    def test_nested_secret_keys(self) -> None:
        payload = {"proxy": {"api_key": "apikeysecret11"},
                   "items": [{"access_token": "short"}]}
        result = redact_obj(payload)
        self.assertEqual(result["proxy"]["api_key"], MASK)
        self.assertEqual(result["items"][0]["access_token"], MASK)


class AmbiguousValueTests(unittest.TestCase):
    """Неоднозначные ключи: решает значение, а не имя ключа."""

    def test_short_secrets_masked(self) -> None:
        """10-14 символов с цифрами — уже секрет."""
        cases = ({"auth": "apikeysecret11"}, {"token": "TOKCOLON99"},
                 {"key": "a1b2c3d4e5f6a7b8"}, {"secret": "abc123def456ghi789"},
                 {"id": UUID})
        for payload in cases:
            with self.subTest(payload=payload):
                key = next(iter(payload))
                self.assertEqual(redact_obj(payload)[key], MASK)

    def test_plain_values_kept(self) -> None:
        """Значения из реальных структур проекта остаются читаемыми."""
        payload = {"id": "zapret", "auth": "noauth", "key": "wlan",
                   "route": "direct", "verdict": "ok", "port": 443}
        self.assertEqual(redact_obj(payload), payload)
        self.assertEqual(redact_obj({"route": "proxy"}), {"route": "proxy"})
        self.assertEqual(redact_obj({"key": "none"}), {"key": "none"})

    def test_numbers_and_flags_never_secret(self) -> None:
        """Порты, счётчики и флаги под неоднозначным ключом не маскируются."""
        for value in (443, 0, 3.5, True, False, None):
            with self.subTest(value=value):
                self.assertEqual(redact_obj({"token": value})["token"], value)

    def test_length_threshold(self) -> None:
        """От 20 символов маска без цифр; 19 букв — ещё слово."""
        self.assertEqual(redact_obj({"token": "a" * 20})["token"], MASK)
        self.assertEqual(redact_obj({"token": "a" * 19})["token"], "a" * 19)


class LegacySuiteTests(unittest.TestCase):
    """Старый набор (22 теста) на месте: регрессии не удалены."""

    def test_legacy_test_count_intact(self) -> None:
        legacy = (LinkTests, TextTests, ObjectTests)
        total = sum(len([name for name in dir(cls)
                         if name.startswith("test_")]) for cls in legacy)
        self.assertEqual(total, 22)


if __name__ == "__main__":
    unittest.main()
