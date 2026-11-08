"""Остановка драйверов WinDivert вместе с zapret.

Сеть, sc.exe, UAC и процессы подменяются. Запуск:
``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import actions  # noqa: E402


def _fake_batch(store: list[list[str]], result=(True, 42), on_call=None):
    def inner(lines, attempts=3, delay=0.8):
        store.append(list(lines))
        if on_call is not None:
            on_call()
        return result
    return inner


class WindivertNamesTests(unittest.TestCase):
    def test_both_case_variants_found(self) -> None:
        out = ("SERVICE_NAME: WinDivert\n TYPE : 1 KERNEL_DRIVER\n"
               "SERVICE_NAME: windivert\n TYPE : 1 KERNEL_DRIVER\n"
               "SERVICE_NAME: other\n")
        with mock.patch.object(actions, "_run_capture", return_value=out):
            names = actions._windivert_services()
        self.assertIn("WinDivert", names)
        self.assertIn("windivert", names)
        self.assertNotIn("other", names)

    def test_known_names_when_sc_fails(self) -> None:
        with mock.patch.object(actions, "_run_capture", return_value=""):
            names = actions._windivert_services()
        self.assertIn("WinDivert", names)
        self.assertIn("windivert", names)


class StopWindivertTests(unittest.TestCase):
    def test_absent_and_stopped_are_idempotent(self) -> None:
        with mock.patch.object(actions, "_windivert_services",
                               return_value=["WinDivert", "windivert"]), \
                mock.patch.object(actions, "service_state",
                                  return_value="absent"), \
                mock.patch.object(actions, "_run_capture") as run:
            result = actions.stop_windivert()
        self.assertEqual(result["state"], "absent")
        run.assert_not_called()

        with mock.patch.object(actions, "_windivert_services",
                               return_value=["WinDivert", "windivert"]), \
                mock.patch.object(actions, "service_state",
                                  return_value="stopped"), \
                mock.patch.object(actions, "_run_capture") as run:
            result = actions.stop_windivert()
        self.assertEqual(result["state"], "not_running")
        run.assert_not_called()

    def test_admin_stops_each_running_driver(self) -> None:
        states = {"WinDivert": "running", "windivert": "running"}
        calls: list[list[str]] = []

        def run(args, timeout=10):
            calls.append(list(args))
            if len(args) > 1 and args[1] == "stop":
                states[args[2]] = "stopped"
            return "STOPPED"

        with mock.patch.object(actions, "_windivert_services",
                               return_value=list(states)), \
                mock.patch.object(actions, "service_state",
                                  side_effect=lambda name: states[name]), \
                mock.patch.object(actions, "is_admin", return_value=True), \
                mock.patch.object(actions, "_run_capture", side_effect=run):
            result = actions.stop_windivert()
        self.assertEqual(result["state"], "stopped")
        stopped = [args[2] for args in calls if len(args) > 2 and args[1] == "stop"]
        self.assertIn("WinDivert", stopped)
        self.assertIn("windivert", stopped)

    def test_non_admin_code5_but_factually_stopped(self) -> None:
        with mock.patch.object(actions, "_windivert_services",
                               return_value=["WinDivert"]), \
                mock.patch.object(actions, "service_state",
                                  return_value="running"), \
                mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_elevate_batch",
                                  return_value=(False, 5)), \
                mock.patch.object(actions, "_wait_services_stopped",
                                  return_value={"WinDivert": "stopped"}):
            result = actions.stop_windivert()
        self.assertEqual(result["state"], "stopped")

    def test_non_admin_uses_one_elevated_command(self) -> None:
        states = {"WinDivert": "running", "windivert": "running"}
        batches: list[list[str]] = []

        def stopped() -> None:
            states.update({"WinDivert": "stopped", "windivert": "stopped"})

        with mock.patch.object(actions, "_windivert_services",
                               return_value=list(states)), \
                mock.patch.object(actions, "service_state",
                                  side_effect=lambda name: states[name]), \
                mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_elevate_batch",
                                  side_effect=_fake_batch(
                                      batches, on_call=stopped)):
            result = actions.stop_windivert()
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(len(batches), 1)
        lines = batches[0]
        self.assertIn("sc.exe stop WinDivert", lines)
        self.assertIn("sc.exe stop windivert", lines)


class ElevatedCommandQuotingTests(unittest.TestCase):
    """C1: кавычки в имени службы ломаются экранированием list2cmdline."""

    def test_stop_all_command_has_no_quotes(self) -> None:
        command = actions._stop_all_command(True, ["WinDivert", "windivert"])
        self.assertIn("sc.exe stop zapret", command)
        self.assertIn("taskkill /f /im winws.exe", command)
        self.assertIn("sc.exe stop WinDivert", command)
        self.assertIn("sc.exe stop windivert", command)
        self.assertNotIn('"', command)
        params = subprocess.list2cmdline(["cmd", "/c", command])
        self.assertNotIn('\\"', params)

    def test_stop_windivert_batch_has_no_quotes(self) -> None:
        batches: list[list[str]] = []
        with mock.patch.object(actions, "_windivert_services",
                               return_value=["WinDivert", "windivert"]), \
                mock.patch.object(actions, "service_state",
                                  return_value="running"), \
                mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_elevate_batch",
                                  side_effect=_fake_batch(batches)), \
                mock.patch.object(actions, "_wait_services_stopped",
                                  return_value={"WinDivert": "stopped",
                                                "windivert": "stopped"}):
            actions.stop_windivert()
        lines = batches[0]
        self.assertIn("sc.exe stop WinDivert", lines)
        self.assertIn("sc.exe stop windivert", lines)
        self.assertFalse(any('"' in line for line in lines))

    def test_service_state_pending_is_not_absent(self) -> None:
        with mock.patch.object(actions, "_run_capture",
                               return_value="STATE : 3 STOP_PENDING"):
            self.assertEqual(actions.service_state("WinDivert"), "pending")


class ElevateBatchFileTests(unittest.TestCase):
    """C1: элевируется .bat-файл, команды лежат построчно."""

    def test_batch_file_content_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen: dict = {}

            def fake_elevate(args, cwd=None, attempts=3, delay=0.8):
                path = Path(args[0])
                seen["path"] = path
                seen["cwd"] = cwd
                seen["text"] = path.read_bytes()
                return True, 42

            with mock.patch.object(actions, "_batch_dir",
                                   return_value=Path(tmp)), \
                    mock.patch.object(actions, "_elevate",
                                      side_effect=fake_elevate):
                ok, code = actions._elevate_batch(
                    ["sc.exe stop zapret", "taskkill /f /im winws.exe"])
            self.assertTrue(ok)
            self.assertEqual(code, 42)
            self.assertTrue(seen["path"].name.endswith(".bat"))
            self.assertEqual(seen["path"].name != "cmd", True)
            self.assertIn(b"@echo off\r\n", seen["text"])
            self.assertIn(b"sc.exe stop zapret\r\n", seen["text"])
            self.assertIn(b"taskkill /f /im winws.exe\r\n", seen["text"])
            # Файл нужен elevated-процессу после возврата ShellExecute.
            self.assertTrue(seen["path"].exists())

    def test_stale_batches_cleaned_fresh_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            old = directory / "bypass-tray-elev-old.bat"
            fresh = directory / "bypass-tray-elev-fresh.bat"
            old.write_text("@echo off\r\n", encoding="utf-8")
            fresh.write_text("@echo off\r\n", encoding="utf-8")
            now = time.time()
            os.utime(old, (now - actions.BATCH_STALE_SECONDS - 5,)*2)
            os.utime(fresh, (now, now))

            generated: dict = {}

            def fake_elevate(args, cwd=None, attempts=3, delay=0.8):
                generated["path"] = Path(args[0])
                return True, 42

            with mock.patch.object(actions, "_batch_dir",
                                   return_value=directory), \
                    mock.patch.object(actions, "_elevate",
                                      side_effect=fake_elevate):
                actions._elevate_batch(["echo ok"])
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(generated["path"].exists())

    def test_stop_zapret_elevates_batch_path_not_cmd(self) -> None:
        seen: list[str] = []

        def fake_elevate(args, cwd=None, attempts=3, delay=0.8):
            seen.append(str(args[0]))
            return True, 42

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(actions, "_batch_dir",
                                  return_value=Path(tmp)), \
                mock.patch.object(actions, "_elevate",
                                  side_effect=fake_elevate), \
                mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_windivert_services",
                                  return_value=[]), \
                mock.patch.object(actions, "service_state",
                                  return_value="stopped"), \
                mock.patch.object(actions, "_pids", return_value=set()), \
                mock.patch.object(actions, "kill_process",
                                  return_value={"killed": 0, "denied": 0}), \
                mock.patch.object(actions, "_wait_services_stopped",
                                  return_value={"zapret": "stopped"}):
            actions.stop_zapret({})
        self.assertTrue(seen)
        self.assertTrue(seen[0].endswith(".bat"))
        self.assertNotEqual(Path(seen[0]).name.lower(), "cmd")


class ControlServiceStartTests(unittest.TestCase):
    """M2: старт службы без админа — один UAC и драйвер в той же команде."""

    def test_non_admin_start_is_one_uac(self) -> None:
        states = {"drv": "stopped", "zapret": "stopped"}
        batches: list[list[str]] = []

        def service_state(name):
            return states["zapret"] if name == actions.ZAPRET_SERVICE else states["drv"]

        def started() -> None:
            states["drv"] = "running"
            states["zapret"] = "running"

        with mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_windivert_services",
                                  return_value=["WinDivert"]), \
                mock.patch.object(actions, "service_state",
                                  side_effect=service_state), \
                mock.patch.object(actions, "_service_start_type",
                                  return_value="4"), \
                mock.patch.object(actions, "_elevate_batch",
                                  side_effect=_fake_batch(
                                      batches, on_call=started)):
            state = actions.control_service("start")
        self.assertEqual(state, "running")
        self.assertEqual(len(batches), 1)
        lines = batches[0]
        self.assertIn("sc.exe config WinDivert start= demand", lines)
        self.assertIn("sc.exe start WinDivert", lines)
        self.assertIn("sc.exe start zapret", lines)
        self.assertIn("sc.exe config WinDivert start= disabled", lines)
        self.assertLess(lines.index("sc.exe start WinDivert"),
                        lines.index("sc.exe start zapret"))
        self.assertLess(lines.index("sc.exe start zapret"),
                        lines.index("sc.exe config WinDivert start= disabled"))
        self.assertFalse(any('"' in line for line in lines))

    def test_restart_batch_stops_then_starts_service(self) -> None:
        states = {"drv": "running", "zapret": "running"}
        batches: list[list[str]] = []

        def service_state(name):
            return states["zapret"] if name == actions.ZAPRET_SERVICE else states["drv"]

        with mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_windivert_services",
                                  return_value=["WinDivert"]), \
                mock.patch.object(actions, "service_state",
                                  side_effect=service_state), \
                mock.patch.object(actions, "_elevate_batch",
                                  side_effect=_fake_batch(batches)):
            actions.control_service("restart")
        lines = batches[0]
        self.assertLess(lines.index("sc.exe stop zapret"),
                        lines.index("sc.exe start zapret"))
        self.assertFalse(any('"' in line for line in lines))


class ElevationRetryTests(unittest.TestCase):
    """Код ShellExecute транзиентный: решает фактическое состояние."""

    def test_elevate_retries_transient_5(self) -> None:
        calls = [5, 42]
        with mock.patch.object(actions, "_shell_execute",
                               side_effect=lambda *a, **k: calls.pop(0)):
            ok, code = actions._elevate(["cmd", "/c", "x"], attempts=3,
                                        delay=0.0)
        self.assertTrue(ok)
        self.assertEqual(code, 42)

    def test_stop_succeeds_by_fact_despite_code_5(self) -> None:
        with mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_windivert_services",
                                  return_value=["WinDivert"]), \
                mock.patch.object(actions, "service_state",
                                  return_value="stopped"), \
                mock.patch.object(actions, "_elevate_batch",
                                  return_value=(False, 5)), \
                mock.patch.object(actions, "_pids", return_value=set()), \
                mock.patch.object(actions, "kill_process",
                                  return_value={"killed": 0, "denied": 0}), \
                mock.patch.object(actions, "_wait_services_stopped",
                                  return_value={"zapret": "stopped",
                                                "WinDivert": "stopped"}):
            result = actions.stop_zapret({})
        self.assertEqual(result["windivert"], "not_running")

    def test_stop_raises_with_code_only_when_not_stopped(self) -> None:
        with mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_windivert_services",
                                  return_value=["WinDivert"]), \
                mock.patch.object(actions, "service_state",
                                  return_value="running"), \
                mock.patch.object(actions, "_elevate_batch",
                                  return_value=(True, 42)), \
                mock.patch.object(actions, "_pids", return_value={1}), \
                mock.patch.object(actions, "_wait_services_stopped",
                                  return_value={"zapret": "running",
                                                "WinDivert": "running"}), \
                mock.patch.object(actions, "_wait_process_gone"):
            with self.assertRaises(PermissionError) as caught:
                actions.stop_zapret({})
        self.assertIn("42", str(caught.exception))

    def test_start_succeeds_by_fact_despite_code_5(self) -> None:
        with mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_windivert_services",
                                  return_value=[]), \
                mock.patch.object(actions, "service_state",
                                  return_value="running"), \
                mock.patch.object(actions, "_elevate_batch",
                                  return_value=(False, 5)):
            self.assertEqual(actions.control_service("start"), "running")


class StopZapretOrderTests(unittest.TestCase):
    def test_admin_service_then_winws_then_drivers(self) -> None:
        order: list[str] = []
        states = {"WinDivert": "running", "windivert": "running"}

        def run(args, timeout=10):
            order.append(" ".join(args))
            if len(args) > 2 and args[1] == "stop" and args[2] in states:
                states[args[2]] = "stopped"
            return "STOPPED"

        with mock.patch.object(actions, "is_admin", return_value=True), \
                mock.patch.object(actions, "_windivert_services",
                                  return_value=list(states)), \
                mock.patch.object(actions, "service_state",
                                  side_effect=lambda name=actions.ZAPRET_SERVICE: (
                                      "running" if name == actions.ZAPRET_SERVICE
                                      else states[name])), \
                mock.patch.object(actions, "control_service",
                                  side_effect=lambda action, name=None:
                                  order.append(f"control:{action}") or "stopped"), \
                mock.patch.object(actions, "kill_process",
                                  side_effect=lambda prefix:
                                  order.append(f"kill:{prefix}")
                                  or {"killed": 1, "denied": 0}), \
                mock.patch.object(actions, "_pids", return_value=set()), \
                mock.patch.object(actions, "_run_capture", side_effect=run):
            result = actions.stop_zapret({})
        self.assertEqual(result["windivert"], "stopped")
        self.assertEqual(result["killed"], 1)
        control = next(i for i, item in enumerate(order)
                       if item.startswith("control:"))
        kill = next(i for i, item in enumerate(order) if item.startswith("kill:"))
        driver = next(i for i, item in enumerate(order)
                      if "sc.exe stop WinDivert" in item)
        self.assertLess(control, kill)
        self.assertLess(kill, driver)

    def test_non_admin_one_uac_for_everything(self) -> None:
        batches: list[list[str]] = []
        state = {"value": "running"}

        def stopped() -> None:
            state["value"] = "stopped"

        with mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_windivert_services",
                                  return_value=["WinDivert", "windivert"]), \
                mock.patch.object(actions, "service_state",
                                  side_effect=lambda name=actions.ZAPRET_SERVICE:
                                  state["value"]), \
                mock.patch.object(actions, "kill_process",
                                  return_value={"killed": 0, "denied": 0}), \
                mock.patch.object(actions, "_pids", return_value=set()), \
                mock.patch.object(actions, "_elevate_batch",
                                  side_effect=_fake_batch(
                                      batches, on_call=stopped)):
            result = actions.stop_zapret({})
        self.assertEqual(len(batches), 1)
        lines = batches[0]
        self.assertLess(lines.index("sc.exe stop zapret"),
                        lines.index("taskkill /f /im winws.exe"))
        self.assertLess(lines.index("taskkill /f /im winws.exe"),
                        lines.index("sc.exe stop WinDivert"))
        self.assertIn("sc.exe stop windivert", lines)
        self.assertEqual(result["windivert"], "stopped")

    def test_denied_uac_raises(self) -> None:
        with mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_windivert_services",
                                  return_value=["WinDivert", "windivert"]), \
                mock.patch.object(actions, "service_state",
                                  return_value="running"), \
                mock.patch.object(actions, "_pids", return_value={1}), \
                mock.patch.object(actions, "_wait_services_stopped",
                                  return_value={"zapret": "running",
                                                "WinDivert": "running",
                                                "windivert": "running"}), \
                mock.patch.object(actions, "_wait_process_gone"), \
                mock.patch.object(actions, "_elevate_batch",
                                  return_value=(False, 5)):
            with self.assertRaises(PermissionError) as caught:
                actions.stop_zapret({})
        self.assertIn("5", str(caught.exception))


class StartZapretTests(unittest.TestCase):
    def test_start_keeps_best_effort_driver_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "general.bat").write_text(
                "@echo off\n", encoding="utf-8")
            with mock.patch.object(actions, "kill_process",
                                   return_value={"killed": 0, "denied": 0}), \
                    mock.patch.object(actions, "_popen") as popen, \
                    mock.patch.object(actions, "_hide_later"), \
                    mock.patch.object(actions, "_pids", return_value={1}), \
                    mock.patch.object(actions, "_ensure_windivert",
                                      return_value="running") as ensure:
                result = actions.start_zapret(
                    {"zapret_dir": str(directory), "zapret_bat": "general.bat"})
        self.assertTrue(popen.called)
        ensure.assert_called_once()
        self.assertEqual(result["windivert"], "running")
        self.assertTrue(result["winws"])

    def test_ensure_windivert_does_not_raise(self) -> None:
        with mock.patch.object(actions, "service_state", return_value="stopped"), \
                mock.patch.object(actions, "is_admin", return_value=True), \
                mock.patch.object(actions, "_run_capture",
                                  return_value="") as run:
            self.assertEqual(actions._ensure_windivert(timeout=0.0), "stopped")
        self.assertTrue(any("start" in call.args[0]
                            for call in run.call_args_list))


class EnsureWindivertStartTests(unittest.TestCase):
    """A: DISABLED-драйвер временно переводится в demand и возвращается назад."""

    def test_disabled_is_set_demand_started_and_restored(self) -> None:
        state = {"value": "stopped"}
        commands: list[list[str]] = []

        def run(args, timeout=10):
            commands.append(list(args))
            if len(args) > 1 and args[1] == "start":
                state["value"] = "running"
            return ""

        with mock.patch.object(actions, "_windivert_services",
                               return_value=["WinDivert"]), \
                mock.patch.object(actions, "service_state",
                                  side_effect=lambda name: state["value"]), \
                mock.patch.object(actions, "_service_start_type",
                                  return_value="4"), \
                mock.patch.object(actions, "is_admin", return_value=True), \
                mock.patch.object(actions, "_run_capture", side_effect=run):
            result = actions._ensure_windivert(timeout=0.0)
        self.assertEqual(result, "running")
        self.assertIn(["sc.exe", "config", "WinDivert", "start=", "demand"],
                      commands)
        self.assertIn(["sc.exe", "start", "WinDivert"], commands)
        self.assertIn(["sc.exe", "config", "WinDivert", "start=", "disabled"],
                      commands)
        demand = commands.index(
            ["sc.exe", "config", "WinDivert", "start=", "demand"])
        start = commands.index(["sc.exe", "start", "WinDivert"])
        restore = commands.index(
            ["sc.exe", "config", "WinDivert", "start=", "disabled"])
        self.assertLess(demand, start)
        self.assertLess(start, restore)

    def test_non_admin_uses_one_elevated_batch(self) -> None:
        state = {"value": "stopped"}
        batches: list[list[str]] = []

        def started() -> None:
            state["value"] = "running"

        with mock.patch.object(actions, "_windivert_services",
                               return_value=["WinDivert", "windivert"]), \
                mock.patch.object(actions, "service_state",
                                  side_effect=lambda name: state["value"]), \
                mock.patch.object(actions, "_service_start_type",
                                  return_value="4"), \
                mock.patch.object(actions, "is_admin", return_value=False), \
                mock.patch.object(actions, "_elevate_batch",
                                  side_effect=_fake_batch(
                                      batches, on_call=started)):
            result = actions._ensure_windivert(timeout=0.0)
        self.assertEqual(result, "running")
        self.assertEqual(len(batches), 1)
        lines = batches[0]
        self.assertTrue(any("start= demand" in line for line in lines))
        self.assertTrue(any("start= disabled" in line for line in lines))
        self.assertTrue(any("WinDivert" in line for line in lines))
        self.assertTrue(any("windivert" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
