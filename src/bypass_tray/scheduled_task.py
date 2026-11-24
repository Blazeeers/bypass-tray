"""Задача Планировщика Windows для перебора стратегий без постоянного UAC.

Перебору нужны права администратора (он останавливает и перенастраивает
службу zapret), а само приложение работает без них. Один раз регистрируем
задачу с ``RunLevel=HighestAvailable`` через UAC; дальше ``schtasks /run``
запускает её с повышенными правами молча.

Регистрация идёт через ``schtasks /create /xml``: у аргумента ``/tr`` есть
лимит ~262 символа, и под кодовой страницей консоли он портит не-ASCII
(кириллица в профиле пользователя превращается в мусор). XML пишется в
UTF-16, а сам ``schtasks`` элевируется без промежуточного .bat, поэтому
кодировка консоли на путь не влияет.

Действие задачи фиксировано (интерпретатор + режим перебора). Если команда
или рабочий каталог изменились, реальная задача сравнивается с ожидаемой
по ``schtasks /query /xml``, и задачу перерегистрируем.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from . import actions, activity, config, strategy

#: Имя задачи в планировщике: одно на пользователя.
TASK_NAME = "bypass-tray-sweep"

#: Файл с отпечатком зарегистрированной команды рядом с конфигом.
#: Отдельный файл, а не state.json: приложение сохраняет свой ``state``
#: целиком и могло бы затереть отпечаток.
SIGNATURE_FILE = "sweep-task.json"

#: Временный XML для ``schtasks /create /xml``; удаляется после регистрации.
TASK_XML_FILE = "sweep-task.xml"

#: Сколько ждать, пока зарегистрируется задача после ответа на UAC.
#: С запасом: пользователь может не сразу нажать «Да».
REGISTER_WAIT = 30.0

#: Сколько ждать признака, что перебор действительно стартовал.
#: С запасом на холодный старт интерпретатора и импорт пакета.
START_WAIT = 8.0

#: Ограничение CreateProcess на командную строку; у XML-регистрации лимита
#: ``/tr`` (~262) нет, но совсем длинную команду всё равно не запустить.
MAX_ACTION_LENGTH = 8000

#: ``SE_ERR_ACCESSDENIED``: пользователь нажал «Нет» в запросе UAC.
UAC_DENIED = 5

_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

#: Сообщение последнего отказа — его показывает трей.
_last_error = ""
_last_code = 0


def task_name() -> str:
    return TASK_NAME


def sweep_files() -> tuple[Path, Path]:
    """Пути отчёта и прогресса перебора — те же, что у приложения."""
    base = config.config_dir()
    return base / "sweep-result.json", base / "sweep-progress.json"


def working_dir() -> Path:
    """Рабочий каталог задачи.

    В dev-режиме это ``src``: ``python -m bypass_tray`` находит пакет, даже
    если он не установлен (``pip install -e .``). У frozen-сборки — папка exe.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def task_command() -> list[str]:
    """Командная строка действия задачи (как в ``TrayApp.start_sweep``)."""
    result_path, progress_path = sweep_files()
    if getattr(sys, "frozen", False):
        return [
            sys.executable, "--sweep-strategies",
            "--out", str(result_path), "--report", str(progress_path),
        ]
    return [
        sys.executable, "-m", "bypass_tray", "--sweep-strategies",
        "--out", str(result_path), "--report", str(progress_path),
    ]


def task_arguments() -> str:
    """Аргументы действия: всё после интерпретатора, одной строкой."""
    return subprocess.list2cmdline(task_command()[1:])


def task_command_line() -> str:
    """Действие задачи одной строкой — для отпечатка и проверки длины."""
    return subprocess.list2cmdline(task_command())


def expected_action() -> tuple[str, str, str]:
    """Ожидаемое действие задачи: (Command, Arguments, WorkingDirectory)."""
    return task_command()[0], task_arguments(), str(working_dir())


def task_signature() -> str:
    """Отпечаток команды: меняется вместе с путём к python/exe и каталогом."""
    payload = task_command_line() + "\n" + str(working_dir())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _signature_path() -> Path:
    return config.config_dir() / SIGNATURE_FILE


def signature_matches() -> bool:
    """Совпадает ли сохранённая команда задачи с текущей."""
    try:
        data = json.loads(_signature_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return (data.get("name") == task_name()
            and data.get("hash") == task_signature())


def remember_task() -> bool:
    """Запоминает отпечаток подтверждённой задачи."""
    payload = {
        "name": task_name(),
        "command": task_command_line(),
        "workdir": str(working_dir()),
        "hash": task_signature(),
        "at": time.time(),
    }
    return config.atomic_write(
        _signature_path(), json.dumps(payload, ensure_ascii=False, indent=2))


def forget_task() -> None:
    """Сбрасывает отпечаток (задача удалена или команда изменилась)."""
    try:
        _signature_path().unlink(missing_ok=True)
    except OSError:
        pass


def last_error() -> str:
    """Почему последний запуск не удался (для сообщения в трее)."""
    return _last_error


def _refuse(message: str, code: int = 0) -> bool:
    global _last_error, _last_code
    _last_error = message
    _last_code = code
    activity.log(message, kind="warn")
    return False


def _q(tag: str) -> str:
    return f"{{{_TASK_NS}}}{tag}"


def task_xml(run_level: str = "HighestAvailable") -> str:
    """XML задачи для ``schtasks /create /xml``.

    Команда, аргументы и рабочий каталог лежат в XML-элементах, а не в
    ``/tr``: там нет ни лимита длины, ни порчи кириллицы кодовой страницей.
    """
    ET.register_namespace("", _TASK_NS)
    root = ET.Element(_q("Task"), {"version": "1.2"})
    info = ET.SubElement(root, _q("RegistrationInfo"))
    ET.SubElement(info, _q("Description")).text = (
        "Перебор стратегий zapret для bypass-tray (создаётся автоматически)")
    # Триггеров нет: задача запускается только по требованию (schtasks /run).
    ET.SubElement(root, _q("Triggers"))
    principal = ET.SubElement(
        ET.SubElement(root, _q("Principals")), _q("Principal"),
        {"id": "Author"})
    ET.SubElement(principal, _q("LogonType")).text = "InteractiveToken"
    ET.SubElement(principal, _q("RunLevel")).text = run_level
    settings = ET.SubElement(root, _q("Settings"))
    for tag, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        # Ложные «запустилось» из-за питания и простоя недопустимы: после
        # /run приложение ждёт появления прогресса.
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "false"),
        ("RunOnlyIfNetworkAvailable", "false"),
    ):
        ET.SubElement(settings, _q(tag)).text = value
    idle = ET.SubElement(settings, _q("IdleSettings"))
    ET.SubElement(idle, _q("StopOnIdleEnd")).text = "false"
    ET.SubElement(idle, _q("RestartOnIdle")).text = "false"
    for tag, value in (
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true"),
        ("Hidden", "false"),
        ("RunOnlyIfIdle", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT2H"),
        ("Priority", "7"),
    ):
        ET.SubElement(settings, _q(tag)).text = value
    exec_node = ET.SubElement(
        ET.SubElement(root, _q("Actions"), {"Context": "Author"}), _q("Exec"))
    ET.SubElement(exec_node, _q("Command")).text = task_command()[0]
    ET.SubElement(exec_node, _q("Arguments")).text = task_arguments()
    ET.SubElement(exec_node, _q("WorkingDirectory")).text = str(working_dir())
    return ('<?xml version="1.0" encoding="UTF-16"?>\n'
            + ET.tostring(root, encoding="unicode"))


def xml_dir() -> Path:
    """Каталог временного XML: ``%ProgramData%\\bypass-tray``.

    Пользовательский ``%APPDATA%`` писать может, а читает XML уже
    элевированный ``schtasks``: обычный процесс успел бы подменить файл
    и зарегистрировать произвольную задачу с ``HighestAvailable`` (TOCTOU).
    Каталог в ProgramData создаёт только элевированный процесс.
    """
    base = os.environ.get("PROGRAMDATA")
    if config.is_windows() and base:
        return Path(base) / "bypass-tray"
    return config.config_dir()


def xml_path() -> Path:
    return xml_dir() / TASK_XML_FILE


def write_task_xml(run_level: str = "HighestAvailable") -> Path | None:
    """Пишет XML в UTF-16 (с BOM) — ``schtasks`` читает его без кодировок."""
    path = xml_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(task_xml(run_level), encoding="utf-16")
    except OSError as exc:
        activity.log(f"не записал XML задачи: {exc}", kind="warn")
        return None
    return path


def _drop_xml(path: Path | None = None) -> None:
    try:
        (path or xml_path()).unlink(missing_ok=True)
    except OSError:
        pass


def _register_script(xml_text: str) -> str:
    """PowerShell-сценарий элевированной регистрации.

    XML приходит base64-строкой в аргументе сценария, а не файлом в
    user-writable каталоге: подменить его между записью и чтением нельзя.
    Каталог в ProgramData должен принадлежать администраторам — если его
    заранее создал обычный пользователь, регистрация отменяется (код 6).
    """
    xml_b64 = base64.b64encode(xml_text.encode("utf-16-le")).decode("ascii")
    return (
        "$ErrorActionPreference='Stop';"
        "$d=Join-Path $env:ProgramData 'bypass-tray';"
        "if(-not(Test-Path -LiteralPath $d)){"
        "New-Item -ItemType Directory -Path $d|Out-Null};"
        # Get-Acl требует автозагрузки Microsoft.PowerShell.Security: из
        # терминала PowerShell 7 модуль не грузится и регистрация падала с
        # exit 1. Вызов .NET не зависит от модулей.
        "$o=([System.IO.Directory]::GetAccessControl($d)).Owner;"
        "$s=(New-Object -TypeName System.Security.Principal.NTAccount "
        "-ArgumentList $o).Translate("
        "[System.Security.Principal.SecurityIdentifier]).Value;"
        "if($s -ne 'S-1-5-32-544' -and $s -ne 'S-1-5-18'){exit 6};"
        f"$f=Join-Path $d '{TASK_XML_FILE}';"
        "[IO.File]::WriteAllText($f,[Text.Encoding]::Unicode.GetString("
        f"[Convert]::FromBase64String('{xml_b64}')),[Text.Encoding]::Unicode);"
        f"& schtasks.exe /create /f /tn '{TASK_NAME}' /xml $f;"
        "$c=$LASTEXITCODE;"
        "Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue;"
        "exit $c"
    )


def elevated_create_args(xml_text: str) -> list[str]:
    """Аргументы UAC-запуска, который пишет XML и регистрирует задачу."""
    encoded = base64.b64encode(
        _register_script(xml_text).encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive",
            "-EncodedCommand", encoded]


def _run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess | None:
    """Запуск ``schtasks``; ``None`` — утилиту запустить не удалось."""
    try:
        return subprocess.run(
            ["schtasks", *args], capture_output=True, timeout=timeout,
            close_fds=True, creationflags=actions.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _code(args: list[str], timeout: int = 20) -> int | None:
    completed = _run(args, timeout)
    return None if completed is None else int(completed.returncode)


def _decode(raw: bytes | None) -> str:
    """Текст вывода ``schtasks``: UTF-16 с BOM, затем UTF-8, затем OEM.

    ``/query /xml`` на русской Windows отдаёт UTF-8, а обычный вывод —
    cp866; UTF-16 встречается у некоторых сборок.
    """
    data = raw or b""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "cp866", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def parse_action(xml_text: str) -> tuple[str, str, str] | None:
    """Действие из XML задачи: (Command, Arguments, WorkingDirectory).

    ``ElementTree`` сам раскрывает XML-эскейпинг (``&quot;``, ``&amp;``),
    поэтому строки сравниваются с исходными, а не с текстом XML.
    """
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError):
        return None
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "Exec":
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "")
                  for child in node}
        if fields.get("Command"):
            return (fields["Command"], fields.get("Arguments", ""),
                    fields.get("WorkingDirectory", ""))
    return None


def task_action() -> tuple[str, str, str] | None:
    """Реальное действие зарегистрированной задачи (или ``None``)."""
    completed = _run(["/query", "/tn", task_name(), "/xml"])
    if completed is None or completed.returncode != 0:
        return None
    return parse_action(_decode(completed.stdout))


def action_matches() -> bool:
    """Совпадает ли действие в Планировщике с ожидаемым."""
    actual = task_action()
    return actual is not None and actual == expected_action()


def parse_run_level(xml_text: str) -> str | None:
    """``RunLevel`` из XML задачи (``HighestAvailable``/``LeastPrivilege``)."""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError):
        return None
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == "RunLevel":
            return (node.text or "").strip()
    return None


def task_matches() -> bool:
    """Задача совпадает с ожидаемой: и действие, и ``RunLevel``.

    Только действие мало: задача с тем же Exec, но ``LeastPrivilege``
    выглядела бы годной, а ``/run`` пошёл бы без прав — перебор умер бы
    на первом же гашении службы.
    """
    completed = _run(["/query", "/tn", task_name(), "/xml"])
    if completed is None or completed.returncode != 0:
        return False
    text = _decode(completed.stdout)
    return (parse_action(text) == expected_action()
            and (parse_run_level(text) or "").lower() == "highestavailable")


def task_exists() -> bool:
    """Зарегистрирована ли задача (код возврата ``/query``)."""
    return _code(["/query", "/tn", task_name()]) == 0


#: Значения поля статуса, которые означают «задача идёт сейчас».
_RUNNING_STATES = ("выполняется", "running")


def task_running() -> bool:
    """Выполняется ли задача сейчас (для подтверждения старта).

    Разбираем именно строку статуса, а не весь вывод: в ``/fo LIST /v``
    всегда есть поле «Повторять: остановить, если выполняется», из-за
    которого поиск подстроки возвращал True и для готовой задачи.
    """
    completed = _run(["/query", "/tn", task_name(), "/fo", "LIST", "/v"])
    if completed is None or completed.returncode != 0:
        return False
    for line in _decode(completed.stdout).splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() in ("состояние", "status"):
            return value.strip().lower() in _RUNNING_STATES
    return False


def create_args(path: Path) -> list[str]:
    """Аргументы ``schtasks /create`` для XML-файла."""
    return ["/create", "/f", "/tn", task_name(), "/xml", str(path)]


def environment_ready() -> bool:
    """Сможет ли задача импортировать пакет без ``pip install -e .``.

    Проверяем тем же интерпретатором и с тем же рабочим каталогом, что будут
    у задачи, но без ``PYTHONPATH`` текущего процесса: у Планировщика его нет.
    """
    if getattr(sys, "frozen", False):
        return True
    env = {key: value for key, value in os.environ.items()
           if key.upper() != "PYTHONPATH"}
    try:
        completed = subprocess.run(
            [sys.executable, "-c", "import bypass_tray"],
            cwd=str(working_dir()), env=env, capture_output=True, timeout=30,
            close_fds=True, creationflags=actions.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def register_task() -> bool:
    """Прямая регистрация (вызывающий уже с правами администратора)."""
    if not environment_ready():
        return _refuse("интерпретатор задачи не находит пакет bypass_tray")
    path = write_task_xml()
    if path is None:
        return _refuse("не удалось записать XML задачи")
    try:
        if _code(create_args(path)) != 0:
            return _refuse("schtasks не создал задачу")
        if not task_matches():
            return _refuse("schtasks создал задачу с другим действием")
    finally:
        _drop_xml(path)
    remember_task()
    return True


def run_task() -> bool:
    """Запускает зарегистрированную задачу: HIGHEST без запроса UAC."""
    return _code(["/run", "/tn", task_name()]) == 0


def delete_task() -> bool:
    ok = _code(["/delete", "/f", "/tn", task_name()]) == 0
    if ok:
        forget_task()
    return ok


def wait_registered(timeout: float = REGISTER_WAIT) -> bool:
    """Ждёт задачу с ожидаемым действием, а не просто существующую.

    Старая задача тоже существует: если проверять только ``/query``,
    неудачная перерегистрация выглядит успехом, отпечаток записывается, и
    ``/run`` каждый раз запускает старую команду.
    """
    deadline = time.time() + max(0.0, float(timeout))
    while time.time() < deadline:
        if task_matches():
            return True
        time.sleep(0.25)
    return task_matches()


def wait_started(since: float, timeout: float = START_WAIT) -> bool:
    """Есть ли признак, что перебор действительно стартовал.

    ``schtasks /run`` отвечает кодом 0 и когда задаче запрещено стартовать
    (отключена, разряд батареи). Без этой проверки трей ждал бы результата
    900 секунд впустую. Признаки — свежий файл прогресса или статус Running.
    """
    _, progress_path = sweep_files()
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        try:
            # Допуск 1 с: файл мог быть записан чуть раньше отметки запуска.
            if progress_path.stat().st_mtime >= float(since) - 1.0:
                return True
        except OSError:
            pass
        if task_running():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def ensure_task() -> bool:
    """Задача есть и её действие актуально; иначе регистрирует через UAC.

    Возвращает False, если пользователь отклонил UAC или регистрация не
    удалась — отпечаток тогда не пишется, чтобы расхождение не потерялось.
    """
    global _last_error, _last_code
    _last_error, _last_code = "", 0
    if task_matches():
        if not signature_matches():
            remember_task()
        return True
    line = task_command_line()
    if len(line) > MAX_ACTION_LENGTH:
        return _refuse(f"команда задачи слишком длинная: {len(line)} символов")
    if not environment_ready():
        return _refuse("интерпретатор задачи не находит пакет bypass_tray")
    if task_exists():
        # Мёртвая задача с чужим путём или без HighestAvailable: сносим,
        # чтобы /run не подхватил её. Если не вышло — /create /f перезапишет.
        if delete_task():
            activity.log("удалил устаревшую задачу перебора", kind="info")
    # Одна попытка: код 5 — это отказ пользователя, повтор лишь добавит окно.
    ok, code = actions._elevate(elevated_create_args(task_xml()), attempts=1)
    if code == UAC_DENIED:
        return _refuse("Запрос прав отклонён", code)
    if not ok:
        return _refuse(f"UAC не запустил schtasks (код {code})", code)
    if not wait_registered():
        if task_exists():
            return _refuse("задача зарегистрировалась с другим действием")
        return _refuse("schtasks не создал задачу (нет прав или ProgramData)")
    remember_task()
    activity.log("зарегистрировал задачу планировщика для перебора",
                 kind="info")
    return True


def launch(fallback_command: list[str]) -> str:
    """Запускает перебор: сначала задача, при неудаче — прежний UAC-запуск.

    Возвращает ``"task"``, ``"fallback"`` или ``"failed"``.
    """
    global _last_error
    _last_error = ""
    if ensure_task():
        started = time.time()
        if run_task() and wait_started(started):
            activity.log("перебор запущен через планировщик", kind="info")
            return "task"
        # Проверка старта могла опоздать (холодный старт дольше ожидания):
        # если задача всё-таки идёт или маркер перебора уже поставлен,
        # второй запуск даст два перебора сразу — тогда fallback не делаем.
        if task_running() or strategy.trial_active():
            activity.log("перебор запущен через планировщик", kind="info")
            return "task"
        _last_error = "задача планировщика не запустилась"
        activity.log("задача планировщика не запустилась — запускаю напрямую",
                     kind="warn")
    elif _last_code == UAC_DENIED:
        # Пользователь уже отказал в правах: второй запрос — лишнее окно.
        return "failed"
    if actions.run_elevated(fallback_command):
        return "fallback"
    if not _last_error:
        _last_error = "перебор не запустился"
    return "failed"
