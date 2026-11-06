"""Маркер перебора стратегий: постановка, TTL и пропуск проверок.

Сеть и winws не трогаются: только файл маркера во временном каталоге данных.
Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import config, guard, health, strategy  # noqa: E402


class TrialMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        config.set_config_dir(self.data)
        self.addCleanup(config.set_config_dir, None)
        strategy.finish_trial()
        self.addCleanup(strategy.finish_trial)

    def test_start_and_finish(self) -> None:
        self.assertFalse(strategy.trial_active())
        self.assertTrue(strategy.start_trial())
        self.assertTrue(strategy.trial_active())
        info = strategy.trial_info()
        self.assertEqual(info.get("pid"), strategy.os.getpid())
        strategy.finish_trial()
        self.assertFalse(strategy.trial_active())

    def test_stale_marker_expires(self) -> None:
        now = time.time()
        strategy._write_trial({"pid": 1, "started": now - strategy.TRIAL_TTL - 5,
                               "heartbeat": now - strategy.TRIAL_TTL - 5})
        self.assertFalse(strategy.trial_active(now))
        # Свежий heartbeat в том же окне — активен.
        strategy._write_trial({"pid": 1, "started": now, "heartbeat": now})
        self.assertTrue(strategy.trial_active(now))

    def test_health_and_guard_skip_during_trial(self) -> None:
        strategy.start_trial()
        report = health.collect({})
        self.assertEqual(report.get("skipped"), "strategy_trial")
        self.assertEqual(len(report.get("items") or []), 1)
        self.assertIn("перебор", report["items"][0]["detail"])
        self.assertEqual(guard.heal({"guard_enabled": True}, {}, {}), [])


if __name__ == "__main__":
    unittest.main()
