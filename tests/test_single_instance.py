"""Единственный экземпляр трея: мьютекс не должен давать ложный отказ.

WinAPI подменяется: проверяем именно сброс последней ошибки и реакцию на
``ERROR_ALREADY_EXISTS``/нулевой handle, без запуска приложения.
Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray.__main__ import _acquire_single_instance  # noqa: E402


class FakeUser32:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def MessageBoxW(self, *args) -> int:
        self.calls.append(args)
        return 0


class FakeKernel32:
    """Минимальный kernel32: ``error`` меняется только через SetLastError."""

    def __init__(self, handle: int = 1, error: int = 0) -> None:
        self.error = error
        self.last_error_set: int | None = None
        self.closed: list = []
        self.CreateMutexW = lambda *args: handle
        self.GetLastError = lambda: self.error
        self.SetLastError = lambda value: self._set_last_error(value)
        self.CloseHandle = lambda handle: self.closed.append(handle)

    def _set_last_error(self, value: int) -> None:
        self.last_error_set = value
        self.error = value


class AcquireTests(unittest.TestCase):
    def test_stale_error_does_not_block_start(self) -> None:
        # Прошлый вызов WinAPI оставил 183, мьютекса на самом деле нет.
        kernel = FakeKernel32(handle=42, error=183)
        user = FakeUser32()
        handle, first = _acquire_single_instance(
            kernel32=kernel, user32=user)
        self.assertEqual(kernel.last_error_set, 0)
        self.assertTrue(first)
        self.assertEqual(handle, 42)
        self.assertEqual(user.calls, [])

    def test_existing_instance_is_busy(self) -> None:
        kernel = FakeKernel32(handle=42, error=0)
        user = FakeUser32()

        def create(*_args):
            kernel.error = 183
            return 42

        kernel.CreateMutexW = create
        handle, first = _acquire_single_instance(
            kernel32=kernel, user32=user)
        self.assertFalse(first)
        self.assertIsNone(handle)
        self.assertEqual(user.calls, [])
        self.assertEqual(len(kernel.closed), 1)

    def test_failed_create_is_not_busy(self) -> None:
        # Нулевой handle — провал WinAPI, а не занятость: запуск не блокируем.
        kernel = FakeKernel32(handle=0, error=183)
        user = FakeUser32()
        handle, first = _acquire_single_instance(
            kernel32=kernel, user32=user)
        self.assertTrue(first)
        self.assertIsNone(handle)
        self.assertEqual(user.calls, [])


if __name__ == "__main__":
    unittest.main()
