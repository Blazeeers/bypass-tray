"""Обратимость авторешений аудита: сухой прогон, снимок перед записью, откат.

Проверяется главный сценарий: надзиратель ночью что-то поменял, утром человек
хочет вернуть прежние маршруты — и до этого видит, что именно изменится.

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import routes  # noqa: E402


def _audit(*items: tuple[str, str], full: bool = True) -> dict:
    return {
        "at": 100.0,
        "full": full,
        "items": [{"domain": domain, "route": route} for domain, route in items],
    }


def _file_state(directory: Path) -> dict[str, bytes]:
    """Содержимое всех файлов каталога — для проверки «ничего не изменилось»."""
    state: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            state[str(path.relative_to(directory))] = path.read_bytes()
    return state


class PlanTests(unittest.TestCase):
    """Сухой прогон считает изменения и не трогает диск."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(routes, "config_dir",
                                    return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plan_creates_and_changes_nothing(self) -> None:
        routes.save_overrides({
            "a.example": {"route": "direct", "since": 1, "checked": 1},
            "b.example": {"route": "proxy", "since": 1, "checked": 1},
        }, at=1.0)
        before = _file_state(self.root)

        routes.plan({"routing_mode": "blocked"},
                    result=_audit(("a.example", "proxy"), ("c.example", "direct")))

        self.assertEqual(_file_state(self.root), before)
        self.assertFalse(routes.snapshots_dir().exists())

    def test_plan_classifies_changes(self) -> None:
        routes.save_overrides({
            "a.example": {"route": "direct", "since": 1, "checked": 1},
            "b.example": {"route": "proxy", "since": 1, "checked": 1},
            "gone.example": {"route": "proxy", "since": 1, "checked": 1},
        }, at=1.0)
        cfg = {"routing_mode": "blocked",
               "xray_routes": {"mine.example": "direct"}}
        plan = routes.plan(cfg, result=_audit(
            ("a.example", "proxy"),        # direct -> proxy
            ("b.example", "proxy"),        # без изменений
            ("c.example", "direct"),       # новый
            ("gone.example", "proxy"),     # остаётся
            ("mine.example", "proxy"),     # своё правило: аудит не трогает
            ("d.example", "proxy"),        # новый
        ))

        self.assertEqual([row["domain"] for row in plan["changed"]], ["a.example"])
        self.assertEqual(plan["changed"][0]["from"], "direct")
        self.assertEqual(plan["changed"][0]["to"], "proxy")
        self.assertEqual([row["domain"] for row in plan["added"]],
                         ["c.example", "d.example"])
        self.assertEqual(plan["removed"], [])
        self.assertEqual(plan["kept_user"], ["mine.example"])
        self.assertEqual(plan["changes"], 3)
        self.assertTrue(plan["restart_needed"])
        self.assertIn("изменятся 1", plan["summary"])
        self.assertIn("свои правила не трону", plan["summary"])

    def test_plan_shows_removal_of_stale_routes(self) -> None:
        routes.save_overrides({
            "gone.example": {"route": "proxy", "since": 1, "checked": 1},
        }, at=1.0)
        plan = routes.plan({"routing_mode": "blocked"},
                           result=_audit(("a.example", "proxy")))
        self.assertEqual(plan["removed"],
                         [{"domain": "gone.example", "route": "proxy"}])
        self.assertEqual(len(plan["added"]), 1)
        self.assertIn("удалятся 1", plan["summary"])

    def test_all_mode_does_not_add_audit_direct(self) -> None:
        plan = routes.plan({"routing_mode": "all"},
                           result=_audit(("x.example", "direct")))
        self.assertEqual(plan["added"], [])
        self.assertEqual(plan["changes"], 0)
        self.assertFalse(plan["restart_needed"])
        self.assertEqual(plan["summary"], "изменений нет")

    def test_all_mode_plans_removal_of_blocked_direct(self) -> None:
        # Остаток сессии в ``blocked``: в ``all`` он должен уйти.
        routes.save_overrides({
            "x.example": {"route": "direct", "since": 1, "checked": 1},
        }, at=1.0)
        plan = routes.plan({"routing_mode": "all"},
                           result=_audit(("x.example", "direct")))
        self.assertEqual(plan["removed"],
                         [{"domain": "x.example", "route": "direct"}])
        self.assertEqual(plan["changes"], 1)


class SnapshotTests(unittest.TestCase):
    """Перед записью остаётся снимок прежнего состояния."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(routes, "config_dir",
                                    return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_snapshot_holds_previous_state(self) -> None:
        routes.save_overrides({
            "a.example": {"route": "direct", "since": 5, "checked": 5},
        }, at=5.0)
        routes.apply_audit(_audit(("a.example", "proxy")),
                           {"routing_mode": "blocked"})

        snapshot = routes.last_snapshot()
        self.assertTrue(snapshot)
        self.assertEqual(snapshot["reason"], "manual")
        self.assertEqual(snapshot["snapshot"]["routes"]["a.example"]["route"],
                         "direct")
        # Сам файл уже с новым решением.
        self.assertEqual(routes.load_overrides()["a.example"]["route"], "proxy")

    def test_reason_auto_from_guard(self) -> None:
        routes.apply_audit(_audit(("a.example", "proxy")),
                           {"routing_mode": "blocked"}, reason="auto")
        self.assertEqual(routes.last_snapshot()["reason"], "auto")

    def test_no_snapshot_without_changes(self) -> None:
        routes.save_overrides({
            "a.example": {"route": "proxy", "since": 5, "checked": 5},
        }, at=5.0)
        routes.apply_audit(_audit(("a.example", "proxy")),
                           {"routing_mode": "blocked"})
        self.assertEqual(routes.last_snapshot(), {})

    def test_chain_keeps_last_three(self) -> None:
        for index in range(5):
            routes.apply_audit(
                _audit((f"d{index}.example", "proxy")),
                {"routing_mode": "blocked", "xray_routes": {}})
        self.assertEqual(len(routes._snapshot_files()), routes.SNAPSHOT_LIMIT)

    def test_overrides_format_unchanged(self) -> None:
        routes.apply_audit(_audit(("a.example", "proxy")),
                           {"routing_mode": "blocked"})
        payload = json.loads(routes.overrides_path().read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"at", "routes"})
        self.assertEqual(set(payload["routes"]["a.example"]),
                         {"route", "since", "checked"})


class ApplyTests(unittest.TestCase):
    """Применение аудита: план и результат совпадают."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(routes, "config_dir",
                                    return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        running = mock.patch("bypass_tray.xray.is_running", return_value=False)
        running.start()
        self.addCleanup(running.stop)

    def test_all_mode_removal_is_applied(self) -> None:
        """Устаревший direct в ``all``: файл правится, снимок остаётся."""
        routes.save_overrides({
            "x.example": {"route": "direct", "since": 1, "checked": 1},
        }, at=1.0)
        cfg = {"routing_mode": "all"}
        report = _audit(("x.example", "direct"))

        plan = routes.plan(cfg, result=report)
        applied = routes.apply_audit(report, cfg)

        self.assertEqual(plan["changes"], 1)
        self.assertTrue(plan["restart_needed"])
        self.assertTrue(applied["saved"], applied)
        self.assertEqual(applied["changed"], ["x.example"])
        self.assertEqual(routes.load_overrides(), {})
        self.assertTrue(routes.last_snapshot())

    def test_all_mode_removal_asks_for_restart(self) -> None:
        routes.save_overrides({
            "x.example": {"route": "direct", "since": 1, "checked": 1},
        }, at=1.0)

        outcome = routes.apply_now({"routing_mode": "all"},
                                   result=_audit(("x.example", "direct")))

        self.assertEqual(outcome["changed"], 1)
        self.assertTrue(outcome["need_restart"])

    def test_broken_file_is_not_overwritten(self) -> None:
        """Неразбираемый файл: не пишем, байты копируем рядом."""
        path = routes.overrides_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = b'{"at": 1, "routes": {"a.example": '
        path.write_bytes(raw)

        outcome = routes.apply_audit(_audit(("a.example", "proxy")),
                                     {"routing_mode": "blocked"})

        self.assertFalse(outcome["saved"])
        self.assertEqual(outcome["changed"], [])
        self.assertIn("повреждён", outcome["error"])
        self.assertEqual(path.read_bytes(), raw)
        copies = list(path.parent.glob(path.name + ".broken-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), raw)
        self.assertFalse(routes.snapshots_dir().exists())

    def test_broken_file_plans_nothing(self) -> None:
        path = routes.overrides_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{oops")

        plan = routes.plan({"routing_mode": "blocked"},
                           result=_audit(("a.example", "proxy")))

        self.assertEqual(plan["changes"], 0)
        self.assertFalse(plan["restart_needed"])
        self.assertIn("повреждён", plan["summary"])


class RollbackTests(unittest.TestCase):
    """Откат возвращает ровно прежнее состояние."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(routes, "config_dir",
                                    return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Туннель не запущен: перезапуска быть не должно.
        running = mock.patch("bypass_tray.xray.is_running", return_value=False)
        running.start()
        self.addCleanup(running.stop)

    def test_rollback_restores_exact_previous_state(self) -> None:
        routes.save_overrides({
            "a.example": {"route": "direct", "since": 6, "checked": 6},
            "b.example": {"route": "proxy", "since": 7, "checked": 7},
        }, at=7.0)
        before = json.loads(json.dumps(routes.load_overrides()))

        routes.apply_audit(_audit(("a.example", "proxy"),
                                  ("c.example", "proxy")),
                           {"routing_mode": "blocked"})
        self.assertNotEqual(routes.load_overrides(), before)

        outcome = routes.rollback_last({"routing_mode": "blocked"})
        self.assertTrue(outcome["ok"], outcome)
        self.assertFalse(outcome["restarted"])
        self.assertEqual(routes.load_overrides(), before)
        self.assertEqual(outcome["changed"], 3)

    def test_rollback_without_snapshot_is_graceful(self) -> None:
        outcome = routes.rollback_last({})
        self.assertFalse(outcome["ok"])
        self.assertIn("снимков нет", outcome["error"])
        self.assertEqual(routes.load_overrides(), {})

    def test_rollback_does_not_touch_user_routes(self) -> None:
        cfg = {"routing_mode": "blocked",
               "xray_routes": {"mine.example": "direct"}}
        routes.save_overrides({}, at=1.0)
        routes.apply_audit(_audit(("mine.example", "proxy"),
                                  ("a.example", "proxy")), cfg)
        # Своё правило в маршруты не попало, но маршрут a.example появился.
        self.assertNotIn("mine.example", routes.load_overrides())
        self.assertIn("a.example", routes.load_overrides())

        routes.rollback_last(cfg)
        self.assertEqual(cfg["xray_routes"], {"mine.example": "direct"})
        self.assertNotIn("mine.example", routes.load_overrides())

    def test_rollback_consumes_snapshot(self) -> None:
        routes.apply_audit(_audit(("a.example", "proxy")),
                           {"routing_mode": "blocked"})
        self.assertTrue(routes.has_snapshot())
        routes.rollback_last({})
        self.assertFalse(routes.has_snapshot())

    def test_rollback_holds_route_audit_window(self) -> None:
        """«Ночью поменял, утром откатил»: откат не считается просроченным.

        Время из снимка старое (``at=1.0``); если записать его как есть,
        ``due(60)`` станет истинным сразу и надзиратель вернёт свои решения.
        """
        routes.save_overrides({
            "a.example": {"route": "direct", "since": 1, "checked": 1},
        }, at=1.0)
        routes.apply_audit(_audit(("a.example", "proxy")),
                           {"routing_mode": "blocked"})
        started = time.time()

        outcome = routes.rollback_last({"routing_mode": "blocked"})

        self.assertTrue(outcome["ok"], outcome)
        self.assertGreaterEqual(routes._load_file()["at"], started)
        self.assertFalse(routes.due(60))

    def test_rollback_reports_restart_failure(self) -> None:
        """Файл откатился, а ядро — нет: ответ обязан быть честным."""
        routes.save_overrides({
            "a.example": {"route": "direct", "since": 1, "checked": 1},
        }, at=1.0)
        routes.apply_audit(_audit(("a.example", "proxy")),
                           {"routing_mode": "blocked"})
        with mock.patch("bypass_tray.xray.is_running", return_value=True), \
                mock.patch("bypass_tray.xray.saved_server", return_value=None), \
                mock.patch("bypass_tray.xray.restart",
                           return_value={"ok": False, "error": "порт занят"}), \
                mock.patch("bypass_tray.activity.log") as log:
            outcome = routes.rollback_last({"routing_mode": "blocked"})

        self.assertFalse(outcome["ok"], outcome)
        self.assertFalse(outcome["restarted"])
        self.assertEqual(outcome["error"], "порт занят")
        self.assertEqual(outcome["changed"], 1)
        warnings = [call for call in log.call_args_list
                    if "откат записан" in str(call.args[0])
                    and call.kwargs.get("kind") == "warn"]
        self.assertEqual(len(warnings), 1, log.call_args_list)
        # Маршруты при этом действительно вернулись к снимку.
        self.assertEqual(routes.load_overrides()["a.example"]["route"], "direct")

    def test_rollback_without_changes_touches_nothing(self) -> None:
        """Нечего откатывать — файл, снимок и журнал остаются как были."""
        routes.save_overrides({
            "a.example": {"route": "direct", "since": 1, "checked": 1},
        }, at=1.0)
        routes.apply_audit(_audit(("a.example", "proxy")),
                           {"routing_mode": "blocked"})
        self.assertTrue(routes.has_snapshot())
        # Возвращаем файл ровно к состоянию снимка: откат уже не меняет ничего.
        routes.save_overrides({
            "a.example": {"route": "direct", "since": 1, "checked": 1},
        }, at=2.0)
        before = _file_state(self.root)

        with mock.patch("bypass_tray.activity.log") as log:
            outcome = routes.rollback_last({"routing_mode": "blocked"})

        self.assertEqual(outcome, {"ok": True, "changed": 0})
        self.assertEqual(_file_state(self.root), before)
        self.assertTrue(routes.has_snapshot(), "снимок израсходован без изменений")
        log.assert_not_called()

    def test_broken_snapshot_does_not_block_rollback(self) -> None:
        """Битый последний снимок пропускается: работает предыдущий."""
        routes.save_overrides({
            "a.example": {"route": "direct", "since": 1, "checked": 1},
        }, at=1.0)
        routes.apply_audit(_audit(("a.example", "proxy")),
                           {"routing_mode": "blocked"})
        broken = routes.snapshots_dir() / "9999999999999999999.json"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{не json", encoding="utf-8")

        outcome = routes.rollback_last({"routing_mode": "blocked"})

        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(routes.load_overrides()["a.example"]["route"], "direct")
        self.assertFalse(routes.has_snapshot())


if __name__ == "__main__":
    unittest.main()
