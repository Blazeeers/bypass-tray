"""Задача планировщика для перебора: XML, сверка действия, запуск и fallback.

Всё офлайн: ``schtasks`` и элевация подменяются заглушками, каталог данных —
временный. Живой UAC/планировщик тестами не трогаем.

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import config, scheduled_task  # noqa: E402


def completed(code: int, stdout: bytes = b"", stderr: bytes = b"") -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def task_xml_bytes(command_line: str | None = None) -> bytes:
    """Вывод ``schtasks /query /xml``: XML задачи (UTF-8, как на Windows)."""
    text = scheduled_task.task_xml()
    if command_line is not None:
        text = text.replace(scheduled_task.task_arguments(), command_line)
    return text.encode("utf-8")


class TempConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Пробел в пути проверяет кавычки в команде задачи. ``resolve`` —
        # потому что ``config.set_config_dir`` тоже разрешает путь (8.3 → полный).
        self.data = (Path(self._tmp.name) / "data dir").resolve()
        self.data.mkdir()
        config.set_config_dir(self.data)
        self.addCleanup(config.set_config_dir, None)
        # XML регистрации теперь живёт в %ProgramData%; в тестах — во временном
        # каталоге, чтобы не трогать настоящий ProgramData.
        self.programdata = self.data / "ProgramData"
        self.programdata.mkdir()
        env = mock.patch.dict(os.environ, {"PROGRAMDATA": str(self.programdata)})
        env.start()
        self.addCleanup(env.stop)

    def use_cyrillic_dir(self) -> Path:
        path = (Path(self._tmp.name) / "проба кириллицы"
                / "данные по стратегиям").resolve()
        config.set_config_dir(path)
        self.addCleanup(config.set_config_dir, self.data)
        return path


class CommandTests(TempConfig):
    def test_sweep_files_in_config_dir(self) -> None:
        result, progress = scheduled_task.sweep_files()
        self.assertEqual(result, self.data / "sweep-result.json")
        self.assertEqual(progress, self.data / "sweep-progress.json")

    def test_dev_command_uses_module(self) -> None:
        with mock.patch.object(sys, "frozen", False, create=True):
            command = scheduled_task.task_command()
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ["-m", "bypass_tray"])
        self.assertIn("--sweep-strategies", command)
        self.assertIn(str(self.data / "sweep-result.json"), command)
        self.assertIn(str(self.data / "sweep-progress.json"), command)

    def test_frozen_command_uses_exe(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable",
                                  r"C:\Apps\bypass-tray.exe"):
            command = scheduled_task.task_command()
        self.assertEqual(command, [
            r"C:\Apps\bypass-tray.exe", "--sweep-strategies",
            "--out", str(self.data / "sweep-result.json"),
            "--report", str(self.data / "sweep-progress.json"),
        ])

    def test_command_line_quotes_paths_with_spaces(self) -> None:
        result = str(self.data / "sweep-result.json")
        progress = str(self.data / "sweep-progress.json")
        line = scheduled_task.task_command_line()
        self.assertIn(f'"{result}"', line)
        self.assertIn(f'"{progress}"', line)

    def test_working_dir_lets_module_import(self) -> None:
        with mock.patch.object(sys, "frozen", False, create=True):
            workdir = scheduled_task.working_dir()
        self.assertTrue((workdir / "bypass_tray" / "__init__.py").is_file())


class XmlTests(TempConfig):
    def test_xml_action_matches_expected(self) -> None:
        self.assertEqual(scheduled_task.parse_action(scheduled_task.task_xml()),
                         scheduled_task.expected_action())

    def test_xml_is_highest_and_starts_on_battery(self) -> None:
        text = scheduled_task.task_xml()
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", text)
        self.assertIn("<DisallowStartIfOnBatteries>false"
                      "</DisallowStartIfOnBatteries>", text)
        self.assertIn("<WorkingDirectory>", text)

    def test_xml_keeps_cyrillic_paths(self) -> None:
        path = self.use_cyrillic_dir()
        text = scheduled_task.task_xml()
        action = scheduled_task.expected_action()
        self.assertIn("данные по стратегиям", text)
        self.assertIn(str(path / "sweep-result.json"), action[1])
        self.assertEqual(scheduled_task.parse_action(text), action)

    def test_xml_escapes_ampersand_in_path(self) -> None:
        path = (Path(self._tmp.name) / "данные & отчёты").resolve()
        config.set_config_dir(path)
        self.addCleanup(config.set_config_dir, self.data)
        text = scheduled_task.task_xml()
        self.assertIn("&amp;", text)
        self.assertEqual(scheduled_task.parse_action(text),
                         scheduled_task.expected_action())

    def test_parse_action_unescapes_quotes(self) -> None:
        xml = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            '<Actions Context="Author"><Exec>'
            r"<Command>C:\Program Files\Python\python.exe</Command>"
            "<Arguments>-m bypass_tray --out "
            "&quot;C:\\данные &amp; отчёты\\sweep-result.json&quot;</Arguments>"
            r"<WorkingDirectory>C:\Проекты\src</WorkingDirectory>"
            "</Exec></Actions></Task>"
        )
        self.assertEqual(scheduled_task.parse_action(xml), (
            r"C:\Program Files\Python\python.exe",
            '-m bypass_tray --out "C:\\данные & отчёты\\sweep-result.json"',
            r"C:\Проекты\src",
        ))

    def test_parse_action_without_exec(self) -> None:
        self.assertIsNone(scheduled_task.parse_action(
            '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"/>'))
        self.assertIsNone(scheduled_task.parse_action("не xml"))

    def test_write_task_xml_is_utf16_with_bom(self) -> None:
        path = self.use_cyrillic_dir()
        xml_file = scheduled_task.write_task_xml()
        self.assertIsNotNone(xml_file)
        self.assertEqual(Path(xml_file).parent,
                         self.programdata / "bypass-tray")
        raw = Path(xml_file).read_bytes()
        self.assertEqual(raw[:2], b"\xff\xfe")
        text = raw.decode("utf-16")
        self.assertIn("данные по стратегиям", text)
        self.assertIn(str(path / "sweep-result.json"),
                      scheduled_task.parse_action(text)[1])

    def test_elevated_args_carry_xml_inline(self) -> None:
        # XML едет в аргументе сценария, а не файлом в user-writable каталоге.
        args = scheduled_task.elevated_create_args(scheduled_task.task_xml())
        self.assertEqual(args[0], "powershell.exe")
        self.assertIn("-EncodedCommand", args)
        script = base64.b64decode(args[-1]).decode("utf-16-le")
        payload = base64.b64encode(
            scheduled_task.task_xml().encode("utf-16-le")).decode("ascii")
        self.assertIn(payload, script)
        self.assertIn("schtasks.exe /create", script)
        self.assertIn("$env:ProgramData", script)
        self.assertNotIn(str(scheduled_task.xml_path()), script)

    def test_elevation_script_does_not_need_security_module(self) -> None:
        # Get-Acl живёт в Microsoft.PowerShell.Security: из терминала PS7
        # модуль не автозагружается и элевация падала с exit 1.
        args = scheduled_task.elevated_create_args(scheduled_task.task_xml())
        script = base64.b64decode(args[-1]).decode("utf-16-le")
        self.assertNotIn("Get-Acl", script)
        self.assertIn("[System.IO.Directory]::GetAccessControl($d)", script)
        for command in ("Set-Acl", "Get-AuthenticodeSignature",
                        "Get-ExecutionPolicy", "Set-ExecutionPolicy",
                        "Get-Credential", "ConvertTo-SecureString",
                        "Get-PfxCertificate"):
            self.assertNotIn(command, script)
        # Проверка владельца осталась прежней: Administrators или SYSTEM, иначе 6.
        self.assertIn("$s -ne 'S-1-5-32-544' -and $s -ne 'S-1-5-18'", script)
        self.assertIn("exit 6", script)


class ActionTests(TempConfig):
    def test_matches_real_task(self) -> None:
        with mock.patch.object(scheduled_task, "_run",
                               return_value=completed(0, task_xml_bytes())):
            self.assertTrue(scheduled_task.action_matches())

    def test_mismatch_when_command_differs(self) -> None:
        other = '--sweep-strategies --out "C:\\другой\\result.json"'
        with mock.patch.object(scheduled_task, "_run",
                               return_value=completed(0, task_xml_bytes(other))):
            self.assertFalse(scheduled_task.action_matches())

    def test_mismatch_when_task_missing(self) -> None:
        with mock.patch.object(scheduled_task, "_run",
                               return_value=completed(1)):
            self.assertFalse(scheduled_task.action_matches())

    def test_xml_frozen_exe_with_spaces(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable",
                                  r"C:\Program Files\bypass-tray\bypass-tray.exe"):
            self.assertEqual(
                scheduled_task.parse_action(scheduled_task.task_xml()),
                scheduled_task.expected_action())


class WaitRegisteredTests(TempConfig):
    def test_accepts_matching_task(self) -> None:
        with mock.patch.object(scheduled_task, "_run",
                               return_value=completed(0, task_xml_bytes())):
            self.assertTrue(scheduled_task.wait_registered(timeout=0.3))

    def test_rejects_existing_task_with_other_command(self) -> None:
        # Старая задача существует, но команда другая: это не успех.
        other = '--sweep-strategies --out "C:\\старый\\path\\result.json"'
        with mock.patch.object(scheduled_task, "_run",
                               return_value=completed(0, task_xml_bytes(other))):
            self.assertFalse(scheduled_task.wait_registered(timeout=0.3))

    def test_rejects_task_without_highest_run_level(self) -> None:
        # Тот же Exec, но LeastPrivilege: /run пошёл бы без прав.
        xml = scheduled_task.task_xml(run_level="LeastPrivilege")
        with mock.patch.object(scheduled_task, "_run",
                               return_value=completed(0, xml.encode("utf-8"))):
            self.assertFalse(scheduled_task.task_matches())
            self.assertTrue(scheduled_task.action_matches())


class RunningTests(TempConfig):
    """task_running разбирает строку статуса, а не весь вывод /query /v."""

    def output(self, text: str) -> SimpleNamespace:
        return completed(0, text.encode("utf-8"))

    def test_ready_task_is_not_running(self) -> None:
        # Главная ловушка: поле «Повторять: остановить, если выполняется»
        # есть в выводе всегда, но задача при этом «Готово».
        text = ('Имя задачи: bypass-tray-sweep\n'
                'Повторять: остановить, если выполняется\n'
                'Состояние: Готово\n')
        with mock.patch.object(scheduled_task, "_run",
                               return_value=self.output(text)):
            self.assertFalse(scheduled_task.task_running())

    def test_running_task_is_detected(self) -> None:
        text = ('Повторять: остановить, если выполняется\n'
                'Состояние: Выполняется\n')
        with mock.patch.object(scheduled_task, "_run",
                               return_value=self.output(text)):
            self.assertTrue(scheduled_task.task_running())

    def test_english_status(self) -> None:
        with mock.patch.object(scheduled_task, "_run",
                               return_value=self.output("Status: Running\n")):
            self.assertTrue(scheduled_task.task_running())
        with mock.patch.object(scheduled_task, "_run",
                               return_value=self.output("Status: Ready\n")):
            self.assertFalse(scheduled_task.task_running())

    def test_missing_query_is_not_running(self) -> None:
        with mock.patch.object(scheduled_task, "_run",
                               return_value=completed(1)):
            self.assertFalse(scheduled_task.task_running())


class EnsureTests(TempConfig):
    def patch_register(self):
        stack = [
            mock.patch.object(scheduled_task, "environment_ready",
                              return_value=True),
            mock.patch.object(scheduled_task, "write_task_xml",
                              return_value=self.data / scheduled_task.TASK_XML_FILE),
            mock.patch.object(scheduled_task, "_drop_xml"),
            mock.patch.object(scheduled_task.activity, "log"),
        ]
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_matching_task_needs_no_uac(self) -> None:
        with mock.patch.object(scheduled_task, "task_matches",
                               return_value=True), \
                mock.patch.object(scheduled_task.actions, "_elevate") as elevate, \
                mock.patch.object(scheduled_task.activity, "log"):
            self.assertTrue(scheduled_task.ensure_task())
        elevate.assert_not_called()
        self.assertTrue(scheduled_task.signature_matches())

    def test_registration_confirmed_remembers(self) -> None:
        self.patch_register()
        with mock.patch.object(scheduled_task, "task_matches",
                               side_effect=[False, True]), \
                mock.patch.object(scheduled_task, "task_exists",
                                  return_value=False), \
                mock.patch.object(scheduled_task, "wait_registered",
                                  return_value=True), \
                mock.patch.object(scheduled_task.actions, "_elevate",
                                  return_value=(True, 42)) as elevate:
            self.assertTrue(scheduled_task.ensure_task())
        self.assertEqual(elevate.call_args.kwargs.get("attempts"), 1)
        self.assertTrue(scheduled_task.signature_matches())

    def test_stale_task_refused_does_not_remember(self) -> None:
        # Блокер: старая задача существует, новая не зарегистрировалась.
        self.patch_register()
        with mock.patch.object(scheduled_task, "task_matches",
                               return_value=False), \
                mock.patch.object(scheduled_task, "task_exists",
                                  return_value=True), \
                mock.patch.object(scheduled_task, "delete_task",
                                  return_value=False), \
                mock.patch.object(scheduled_task, "wait_registered",
                                  return_value=False), \
                mock.patch.object(scheduled_task, "remember_task") as remember, \
                mock.patch.object(scheduled_task.actions, "_elevate",
                                  return_value=(True, 42)):
            self.assertFalse(scheduled_task.ensure_task())
        remember.assert_not_called()
        self.assertFalse(scheduled_task.signature_matches())
        self.assertIn("другим действием", scheduled_task.last_error())

    def test_uac_denied_is_final_and_not_retried(self) -> None:
        self.patch_register()
        with mock.patch.object(scheduled_task, "task_matches",
                               return_value=False), \
                mock.patch.object(scheduled_task, "task_exists",
                                  return_value=False), \
                mock.patch.object(scheduled_task, "remember_task") as remember, \
                mock.patch.object(scheduled_task.actions, "_elevate",
                                  return_value=(False, 5)) as elevate:
            self.assertFalse(scheduled_task.ensure_task())
        self.assertEqual(elevate.call_count, 1)
        remember.assert_not_called()
        self.assertEqual(scheduled_task.last_error(), "Запрос прав отклонён")

    def test_long_command_refused(self) -> None:
        with mock.patch.object(scheduled_task, "task_matches",
                               return_value=False), \
                mock.patch.object(scheduled_task, "task_command_line",
                                  return_value="x" * 9000), \
                mock.patch.object(scheduled_task.actions, "_elevate") as elevate, \
                mock.patch.object(scheduled_task.activity, "log") as log:
            self.assertFalse(scheduled_task.ensure_task())
        elevate.assert_not_called()
        self.assertIn("слишком длинная", scheduled_task.last_error())
        log.assert_called_once()

    def test_missing_package_refused_before_uac(self) -> None:
        with mock.patch.object(scheduled_task, "task_matches",
                               return_value=False), \
                mock.patch.object(scheduled_task, "environment_ready",
                                  return_value=False), \
                mock.patch.object(scheduled_task.actions, "_elevate") as elevate, \
                mock.patch.object(scheduled_task.activity, "log"):
            self.assertFalse(scheduled_task.ensure_task())
        elevate.assert_not_called()
        self.assertIn("не находит пакет", scheduled_task.last_error())

    def test_environment_check_drops_pythonpath(self) -> None:
        old = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = "чужой путь"
        self.addCleanup(
            lambda: os.environ.pop("PYTHONPATH", None) if old is None
            else os.environ.__setitem__("PYTHONPATH", old))
        with mock.patch.object(scheduled_task.subprocess, "run",
                               return_value=completed(0)) as run:
            self.assertTrue(scheduled_task.environment_ready())
        kwargs = run.call_args.kwargs
        self.assertNotIn("PYTHONPATH", {key.upper() for key in kwargs["env"]})
        self.assertEqual(kwargs["cwd"], str(scheduled_task.working_dir()))


class StartTests(TempConfig):
    def test_start_detected_by_progress_file(self) -> None:
        _, progress = scheduled_task.sweep_files()
        progress.write_text("{}", encoding="utf-8")
        with mock.patch.object(scheduled_task, "task_running",
                               return_value=False):
            self.assertTrue(scheduled_task.wait_started(time.time()))

    def test_stale_progress_is_not_a_start(self) -> None:
        _, progress = scheduled_task.sweep_files()
        progress.write_text("{}", encoding="utf-8")
        old = time.time() - 120
        os.utime(progress, (old, old))
        with mock.patch.object(scheduled_task, "task_running",
                               return_value=False):
            self.assertFalse(scheduled_task.wait_started(time.time(),
                                                         timeout=0.3))

    def test_missing_progress_and_not_running_is_failure(self) -> None:
        with mock.patch.object(scheduled_task, "task_running",
                               return_value=False):
            self.assertFalse(scheduled_task.wait_started(time.time(),
                                                         timeout=0.3))

    def test_running_task_counts_as_start(self) -> None:
        with mock.patch.object(scheduled_task, "task_running",
                               return_value=True):
            self.assertTrue(scheduled_task.wait_started(time.time(),
                                                        timeout=0.3))

    def test_task_launch_confirms_start(self) -> None:
        with mock.patch.object(scheduled_task, "ensure_task",
                               return_value=True), \
                mock.patch.object(scheduled_task, "run_task",
                                  return_value=True), \
                mock.patch.object(scheduled_task, "wait_started",
                                  return_value=True), \
                mock.patch.object(scheduled_task.actions,
                                  "run_elevated") as fallback, \
                mock.patch.object(scheduled_task.activity, "log"):
            outcome = scheduled_task.launch(["python"])
        self.assertEqual(outcome, "task")
        fallback.assert_not_called()

    def test_launch_falls_back_when_task_does_not_start(self) -> None:
        with mock.patch.object(scheduled_task, "ensure_task",
                               return_value=True), \
                mock.patch.object(scheduled_task, "run_task",
                                  return_value=True), \
                mock.patch.object(scheduled_task, "wait_started",
                                  return_value=False), \
                mock.patch.object(scheduled_task, "task_running",
                                  return_value=False), \
                mock.patch.object(scheduled_task.strategy, "trial_active",
                                  return_value=False), \
                mock.patch.object(scheduled_task.actions, "run_elevated",
                                  return_value=True) as fallback, \
                mock.patch.object(scheduled_task.activity, "log"):
            outcome = scheduled_task.launch(["python"])
        self.assertEqual(outcome, "fallback")
        fallback.assert_called_once_with(["python"])

    def test_no_fallback_when_task_started_late(self) -> None:
        # Блокер: wait_started опоздал (холодный старт), но задача идёт —
        # второй перебор не нужен.
        with mock.patch.object(scheduled_task, "ensure_task",
                               return_value=True), \
                mock.patch.object(scheduled_task, "run_task",
                                  return_value=True), \
                mock.patch.object(scheduled_task, "wait_started",
                                  return_value=False), \
                mock.patch.object(scheduled_task, "task_running",
                                  return_value=True), \
                mock.patch.object(scheduled_task.strategy, "trial_active",
                                  return_value=False), \
                mock.patch.object(scheduled_task.actions,
                                  "run_elevated") as fallback, \
                mock.patch.object(scheduled_task.activity, "log"):
            outcome = scheduled_task.launch(["python"])
        self.assertEqual(outcome, "task")
        fallback.assert_not_called()

    def test_no_fallback_when_trial_marker_is_alive(self) -> None:
        # Задача стартовала, но статус ещё не сменился — маркер уже есть.
        with mock.patch.object(scheduled_task, "ensure_task",
                               return_value=True), \
                mock.patch.object(scheduled_task, "run_task",
                                  return_value=True), \
                mock.patch.object(scheduled_task, "wait_started",
                                  return_value=False), \
                mock.patch.object(scheduled_task, "task_running",
                                  return_value=False), \
                mock.patch.object(scheduled_task.strategy, "trial_active",
                                  return_value=True), \
                mock.patch.object(scheduled_task.actions,
                                  "run_elevated") as fallback, \
                mock.patch.object(scheduled_task.activity, "log"):
            outcome = scheduled_task.launch(["python"])
        self.assertEqual(outcome, "task")
        fallback.assert_not_called()

    def test_run_failure_falls_back(self) -> None:
        with mock.patch.object(scheduled_task, "ensure_task",
                               return_value=True), \
                mock.patch.object(scheduled_task, "run_task",
                                  return_value=False), \
                mock.patch.object(scheduled_task, "task_running",
                                  return_value=False), \
                mock.patch.object(scheduled_task.strategy, "trial_active",
                                  return_value=False), \
                mock.patch.object(scheduled_task.actions, "run_elevated",
                                  return_value=True) as fallback, \
                mock.patch.object(scheduled_task.activity, "log"):
            outcome = scheduled_task.launch(["python"])
        self.assertEqual(outcome, "fallback")
        fallback.assert_called_once()

    def test_uac_denied_is_failed_without_fallback(self) -> None:
        self.patch_denied()
        outcome = scheduled_task.launch(["python"])
        self.assertEqual(outcome, "failed")
        self.assertEqual(scheduled_task.last_error(), "Запрос прав отклонён")
        self.fallback.assert_not_called()

    def patch_denied(self) -> None:
        self.fallback = mock.patch.object(
            scheduled_task.actions, "run_elevated").start()
        self.addCleanup(self.fallback.stop)
        stack = [
            mock.patch.object(scheduled_task, "environment_ready",
                              return_value=True),
            mock.patch.object(scheduled_task, "write_task_xml",
                              return_value=self.data / scheduled_task.TASK_XML_FILE),
            mock.patch.object(scheduled_task, "_drop_xml"),
            mock.patch.object(scheduled_task, "task_matches",
                              return_value=False),
            mock.patch.object(scheduled_task, "task_exists",
                              return_value=False),
            mock.patch.object(scheduled_task.actions, "_elevate",
                              return_value=(False, 5)),
            mock.patch.object(scheduled_task.activity, "log"),
        ]
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_uac_denied_does_not_start_second_prompt(self) -> None:
        self.patch_denied()
        scheduled_task.launch(["python"])
        self.fallback.assert_not_called()


class SignatureTests(TempConfig):
    def test_signature_roundtrip(self) -> None:
        self.assertFalse(scheduled_task.signature_matches())
        self.assertTrue(scheduled_task.remember_task())
        self.assertTrue(scheduled_task.signature_matches())
        scheduled_task.forget_task()
        self.assertFalse(scheduled_task.signature_matches())

    def test_signature_tracks_interpreter_change(self) -> None:
        self.assertTrue(scheduled_task.remember_task())
        with mock.patch.object(sys, "executable", r"C:\Other\python.exe"):
            self.assertFalse(scheduled_task.signature_matches())

    def test_broken_signature_file_is_mismatch(self) -> None:
        (self.data / scheduled_task.SIGNATURE_FILE).write_text(
            "{не json", encoding="utf-8")
        self.assertFalse(scheduled_task.signature_matches())

    def test_signature_keeps_command_readable(self) -> None:
        self.assertTrue(scheduled_task.remember_task())
        data = json.loads((self.data / scheduled_task.SIGNATURE_FILE)
                          .read_text(encoding="utf-8"))
        self.assertEqual(data["command"], scheduled_task.task_command_line())
        self.assertEqual(data["workdir"], str(scheduled_task.working_dir()))


class RegisterTaskTests(TempConfig):
    def test_register_task_remembers_on_success(self) -> None:
        with mock.patch.object(scheduled_task, "environment_ready",
                               return_value=True), \
                mock.patch.object(scheduled_task.subprocess, "run",
                                  side_effect=[
                                      completed(0),
                                      completed(0, task_xml_bytes()),
                                  ]):
            self.assertTrue(scheduled_task.register_task())
        self.assertTrue(scheduled_task.signature_matches())

    def test_register_task_reports_failure(self) -> None:
        with mock.patch.object(scheduled_task, "environment_ready",
                               return_value=True), \
                mock.patch.object(scheduled_task.subprocess, "run",
                                  return_value=completed(1)):
            self.assertFalse(scheduled_task.register_task())
        self.assertFalse(scheduled_task.signature_matches())

    def test_register_task_reports_unconfirmed_action(self) -> None:
        other = '--sweep-strategies --out "C:\\старый\\result.json"'
        with mock.patch.object(scheduled_task, "environment_ready",
                               return_value=True), \
                mock.patch.object(scheduled_task.subprocess, "run",
                                  side_effect=[
                                      completed(0),
                                      completed(0, task_xml_bytes(other)),
                                  ]):
            self.assertFalse(scheduled_task.register_task())
        self.assertFalse(scheduled_task.signature_matches())


class ExistsTests(TempConfig):
    def test_exists_true_on_zero(self) -> None:
        with mock.patch.object(scheduled_task.subprocess, "run",
                               return_value=completed(0)) as run:
            self.assertTrue(scheduled_task.task_exists())
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["schtasks", "/query", "/tn"])
        self.assertIn(scheduled_task.task_name(), args)

    def test_exists_false_on_nonzero(self) -> None:
        with mock.patch.object(scheduled_task.subprocess, "run",
                               return_value=completed(1)):
            self.assertFalse(scheduled_task.task_exists())

    def test_exists_false_when_schtasks_missing(self) -> None:
        with mock.patch.object(scheduled_task.subprocess, "run",
                               side_effect=OSError("schtasks не найден")):
            self.assertFalse(scheduled_task.task_exists())


if __name__ == "__main__":
    unittest.main()
