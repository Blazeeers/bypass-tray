"""Перебор стратегий и Discord: подтверждение и подавление рестарта.

Процессы, Планировщик и диалог подменяются: живой Discord и настоящий
перебор не трогаются. Запуск:
``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import app  # noqa: E402


class SweepStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        self.logs: list[str] = []
        self.tray = app.TrayApp.__new__(app.TrayApp)
        self.tray.cfg = {}
        self.tray.state = {}
        self.tray.sweep = {"running": False, "message": ""}
        self.tray.busy = {}
        self.tray._signature = None
        self.tray.update_icon = lambda: None
        self.tray.refresh_activity = lambda: None
        self.tray.sweep_files = lambda: (
            self.data / "sweep-result.json",
            self.data / "sweep-progress.json",
        )

    def start(self, *, pids: list[int], answer: bool):
        launched = threading.Event()

        def fake_launch(command: str) -> str:
            launched.set()
            return "failed"

        confirm = mock.Mock(return_value=answer)
        self.tray._confirm_sweep_discord = confirm
        with mock.patch.object(app.actions, "discord_pids",
                               return_value=pids), \
                mock.patch.object(app.strategy, "trial_active",
                                  return_value=False), \
                mock.patch.object(app.scheduled_task, "task_command",
                                  return_value="bypass-tray --sweep-strategies"), \
                mock.patch.object(app.scheduled_task, "launch",
                                  side_effect=fake_launch), \
                mock.patch.object(
                    app.activity, "log",
                    side_effect=lambda text, kind="info":
                    self.logs.append(f"{kind}:{text}")):
            started = self.tray.start_sweep()
            if started:
                self.assertTrue(launched.wait(2.0),
                                "задача перебора не запустилась")
            else:
                self.assertFalse(launched.wait(0.2),
                                 "при отмене задача запускаться не должна")
        return started, confirm

    def test_no_discord_starts_without_question(self) -> None:
        started, confirm = self.start(pids=[], answer=False)
        self.assertTrue(started)
        confirm.assert_not_called()

    def test_discord_and_no_cancels(self) -> None:
        started, confirm = self.start(pids=[1], answer=False)
        self.assertFalse(started)
        confirm.assert_called_once()
        self.assertEqual(self.tray.sweep["message"],
                         "Тест отменён: запущен Discord")
        self.assertTrue(any("запущен Discord" in text for text in self.logs))

    def test_discord_and_yes_starts(self) -> None:
        started, confirm = self.start(pids=[1], answer=True)
        self.assertTrue(started)
        confirm.assert_called_once()


class SweepConfirmTextTests(unittest.TestCase):
    def test_question_mentions_voice_call(self) -> None:
        tray = app.TrayApp.__new__(app.TrayApp)
        ask = mock.Mock(return_value=False)
        tray.panel = SimpleNamespace(ask_yesno=ask)
        self.assertFalse(tray._confirm_sweep_discord())
        title, question = ask.call_args.args[:2]
        self.assertEqual(title, "Тест стратегий")
        self.assertIn("Discord запущен", question)
        self.assertIn("голосовой звонок", question)


class DiscordSwitchDuringSweepTests(unittest.TestCase):
    def controller(self, *, sweep_running: bool) -> app.TrayApp:
        tray = app.TrayApp.__new__(app.TrayApp)
        tray.cfg = {"discord_switch_on_zapret": True}
        tray.proxy = {"running": True, "busy": False, "port": 10818}
        tray.sweep = {"running": sweep_running}
        tray.refresh_activity = lambda: None
        return tray

    def call(self, tray, *, trial: bool):
        with mock.patch.object(app.strategy, "trial_active",
                               return_value=trial), \
                mock.patch.object(app.actions, "discord_pids",
                                  return_value=[1]), \
                mock.patch.object(app.actions, "discord_forced_proxy",
                                  return_value=False), \
                mock.patch.object(app.actions, "restart_discord",
                                  return_value={"ok": True,
                                                "action": "restarted"}) as restart, \
                mock.patch.object(app.activity, "log"):
            tray._switch_discord(True)
        return restart

    def test_trial_marker_suppresses_restart(self) -> None:
        tray = self.controller(sweep_running=False)
        restart = self.call(tray, trial=True)
        restart.assert_not_called()

    def test_running_sweep_suppresses_restart(self) -> None:
        tray = self.controller(sweep_running=True)
        restart = self.call(tray, trial=False)
        restart.assert_not_called()

    def test_after_sweep_restarts_as_usual(self) -> None:
        tray = self.controller(sweep_running=False)
        restart = self.call(tray, trial=False)
        restart.assert_called_once_with(True, 10818)


if __name__ == "__main__":
    unittest.main()
