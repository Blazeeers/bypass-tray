"""Сверка ProxyOverride при уже включённом нашем прокси.

Реестр подменяется: проверяем, что старый обход обновляется, а совпадающий
не переписывается. Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

from contextlib import ExitStack
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import xray  # noqa: E402


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = 1
    REG_SZ = 1

    def __init__(self, values: dict) -> None:
        self.values = dict(values)
        self.writes: list[str] = []

    def CreateKeyEx(self, *_args):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def SetValueEx(self, _key, name, _reserved, _kind, value) -> None:
        self.values[name] = value
        self.writes.append(value)


class BypassRefreshTests(unittest.TestCase):
    #: Желаемый список: YouTube через VPN, Discord напрямую. «corp.example»
    #: сюда не входит — это чужая запись, её нужно сохранить.
    DESIRED = "localhost;discord.com;*.discord.com"

    def _actual(self, fake: FakeWinreg) -> list[str]:
        raw = str(fake.values.get("ProxyOverride") or "")
        return [part.strip() for part in raw.split(";") if part.strip()]

    def _patches(self, fake: FakeWinreg):
        return (
            mock.patch.dict(sys.modules, {"winreg": fake}),
            mock.patch.object(xray, "_proxy_bypass_list",
                              return_value=self.DESIRED),
            mock.patch.object(xray, "system_proxy_bypass",
                              side_effect=lambda: self._actual(fake)),
            mock.patch.object(xray, "_refresh_wininet"),
        )

    def test_stale_bypass_is_updated(self) -> None:
        fake = FakeWinreg({
            "ProxyOverride": "localhost;youtube.com;*.youtube.com;"
                             "discord.com;corp.example",
        })
        with ExitStack() as stack:
            for patcher in self._patches(fake):
                stack.enter_context(patcher)
            result = xray.refresh_system_proxy_bypass(True, False)
        items = self._actual(fake)
        self.assertTrue(result.get("changed"))
        self.assertNotIn("youtube.com", items)
        self.assertNotIn("*.youtube.com", items)
        self.assertIn("discord.com", items)
        self.assertIn("*.discord.com", items)
        self.assertIn("corp.example", items)

    def test_matching_bypass_is_not_rewritten(self) -> None:
        fake = FakeWinreg({
            "ProxyOverride": "corp.example;*.discord.com;discord.com;localhost",
        })
        with ExitStack() as stack:
            for patcher in self._patches(fake):
                stack.enter_context(patcher)
            result = xray.refresh_system_proxy_bypass(True, False)
        self.assertTrue(result.get("ok"))
        self.assertFalse(result.get("changed"))
        self.assertEqual(fake.writes, [])

    def test_second_call_does_not_write(self) -> None:
        fake = FakeWinreg({"ProxyOverride": "localhost;youtube.com"})
        with ExitStack() as stack:
            for patcher in self._patches(fake):
                stack.enter_context(patcher)
            first = xray.refresh_system_proxy_bypass(True, False)
            second = xray.refresh_system_proxy_bypass(True, False)
        self.assertTrue(first.get("changed"))
        self.assertFalse(second.get("changed"))
        self.assertEqual(len(fake.writes), 1)


if __name__ == "__main__":
    unittest.main()
