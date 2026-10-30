"""Тесты истории здоровья: сводка за сутки и жизнь файла ``health_history.json``.

Сводка проверяется на синтетике чистой функцией ``summarise`` (без файлов и
системного времени), запись/чтение — на временном каталоге.

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import activity, config, health, history  # noqa: E402

NOW = 1_700_000_000.0
HOUR = 3600.0


def _row(at: float, level: str, items: dict | None = None) -> dict:
    return {"at": at, "level": level, "verdict": "ok", "items": items or {}}


class SummariseTests(unittest.TestCase):
    """Сводка по готовым записям, без обращения к диску."""

    def test_uptime_is_share_of_ok(self) -> None:
        rows = [
            _row(NOW - 3 * HOUR, "ok"),
            _row(NOW - 2 * HOUR, "ok"),
            _row(NOW - 1 * HOUR, "down"),
            _row(NOW, "ok"),
        ]
        data = history.summarise(rows, [], 24, NOW)
        self.assertEqual(data["uptime_pct"], 75)
        self.assertEqual(data["reports"], 4)

    def test_incidents_count_transitions(self) -> None:
        # Три подряд ``down`` — один переход, ``down → ok → down`` — ещё один.
        rows = [
            _row(NOW - 5 * HOUR, "ok"),
            _row(NOW - 4 * HOUR, "down"),
            _row(NOW - 3 * HOUR, "down"),
            _row(NOW - 2 * HOUR, "down"),
            _row(NOW - 1 * HOUR, "ok"),
            _row(NOW, "down"),
        ]
        data = history.summarise(rows, [], 24, NOW)
        self.assertEqual(data["incidents"], 2)
        self.assertEqual(data["worst"], "down")

    def test_window_cuts_old_rows(self) -> None:
        rows = [
            _row(NOW - 25 * HOUR, "down"),
            _row(NOW - 1 * HOUR, "ok"),
        ]
        data = history.summarise(rows, [], 24, NOW)
        self.assertEqual(data["reports"], 1)
        self.assertEqual(data["worst"], "ok")
        self.assertEqual(data["incidents"], 0)
        self.assertEqual(data["uptime_pct"], 100)

    def test_by_service_counts_bad_and_warn(self) -> None:
        rows = [
            _row(NOW - 3 * HOUR, "down", {"vpn": "bad", "youtube": "warn"}),
            _row(NOW - 2 * HOUR, "down", {"vpn": "bad", "deepseek": "ok"}),
            _row(NOW - 1 * HOUR, "ok", {"vpn": "ok"}),
        ]
        data = history.summarise(rows, [], 24, NOW)
        self.assertEqual(data["by_service"], {"vpn": 2, "youtube": 1})

    def test_activity_counts_switches_and_heals(self) -> None:
        acts = [
            {"at": NOW - 3 * HOUR, "text": "переключил сервер: Рим", "kind": "ok"},
            {"at": NOW - 2 * HOUR, "text": "выбрал сервер: Милан", "kind": "ok"},
            {"at": NOW - 1 * HOUR, "text": "подобрал лучший сервер: Берлин",
             "kind": "ok"},
            {"at": NOW - 1 * HOUR, "text": "обновил список серверов: 7",
             "kind": "ok"},
            {"at": NOW - 2 * HOUR, "text": "tg-ws-proxy упал — перезапустил",
             "kind": "ok"},
            {"at": NOW - 1 * HOUR,
             "text": "tg-ws-proxy упал — перезапустить не удалось",
             "kind": "warn"},
        ]
        data = history.summarise([], acts, 24, NOW)
        self.assertEqual(data["server_switches"], 3)
        # Неудачная попытка лечением не считается.
        self.assertEqual(data["heals"], 1)

    def test_empty_data_gives_none_uptime(self) -> None:
        data = history.summarise([], [], 24, NOW)
        self.assertIsNone(data["uptime_pct"])
        self.assertIsNone(data["worst"])
        self.assertIsNone(data["first_at"])
        self.assertIsNone(data["last_at"])
        self.assertEqual(data["reports"], 0)
        self.assertEqual(data["incidents"], 0)
        self.assertEqual(data["by_service"], {})
        self.assertEqual(data["server_switches"], 0)
        self.assertEqual(data["heals"], 0)


class FileTests(unittest.TestCase):
    """Файл истории: запись, чтение, обрезка хвоста, битый JSON."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.object(history, "config_dir", return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Реальный журнал сюда не примешиваем: сводка читает activity отдельно.
        activity_patcher = mock.patch.object(activity, "recent", return_value=[])
        activity_patcher.start()
        self.addCleanup(activity_patcher.stop)

    def test_append_reads_back_and_keeps_tail(self) -> None:
        for index in range(5):
            history.append({"checked": NOW + index, "level": "ok",
                            "verdict": "ok",
                            "items": [{"id": "vpn", "state": "ok"}]})
        with mock.patch.object(history, "MAX_ROWS", 3):
            history.append({"checked": NOW + 5, "level": "down",
                            "verdict": "fail",
                            "items": [{"id": "vpn", "state": "bad"}]})
        got = history.rows(24, now=NOW + 5)
        self.assertEqual(len(got), 3)
        self.assertEqual(got[-1]["level"], "down")
        self.assertEqual(got[-1]["items"], {"vpn": "bad"})
        # Хвост старее окна хранилища при записи отсекается.
        self.assertTrue((self.dir / "health_history.json").is_file())

    def test_append_drops_rows_older_than_window(self) -> None:
        stale = NOW - (history.MAX_AGE_HOURS + 2) * HOUR
        history.append({"checked": stale, "level": "down", "verdict": "fail",
                        "items": []})
        history.append({"checked": NOW, "level": "ok", "verdict": "ok",
                        "items": []})
        got = history.rows(history.MAX_AGE_HOURS,
                           now=NOW)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["level"], "ok")

    def test_broken_file_is_empty(self) -> None:
        (self.dir / "health_history.json").write_text("{ не json", encoding="utf-8")
        self.assertEqual(history.rows(24, now=NOW), [])
        data = history.summary(24, now=NOW)
        self.assertIsNone(data["uptime_pct"])
        self.assertEqual(data["reports"], 0)

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(history.rows(24, now=NOW), [])
        self.assertIsNone(history.summary(24, now=NOW)["uptime_pct"])


class CollectTests(unittest.TestCase):
    """``collect`` пишет историю, но не падает, если та сломалась."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config.set_config_dir(Path(self.tmp.name))
        self.addCleanup(config.set_config_dir, None)

    @staticmethod
    def _ok(item_id: str) -> dict:
        return health._item(item_id, item_id, "ok", "", 0)

    def _patched(self):
        ok = self._ok
        return [
            mock.patch.object(health, "_check_youtube",
                              lambda *a, **k: ok("youtube")),
            mock.patch.object(health, "_check_discord",
                              lambda *a, **k: ok("discord")),
            mock.patch.object(health, "_check_deepseek",
                              lambda *a, **k: ok("deepseek")),
            mock.patch.object(health, "_check_telegram",
                              lambda *a, **k: ok("telegram")),
            mock.patch.object(health, "_check_vpn",
                              lambda *a, **k: ok("vpn")),
            mock.patch.object(health, "_check_zapret",
                              lambda *a, **k: ok("zapret")),
            mock.patch.object(health, "_check_tgws",
                              lambda *a, **k: ok("tgwsproxy")),
        ]

    def test_collect_appends_history_row(self) -> None:
        with mock.patch.object(history, "append") as append:
            for patcher in self._patched():
                patcher.start()
                self.addCleanup(patcher.stop)
            result = health.collect({}, {}, {"running": False, "port": 0})
        self.assertEqual(result["level"], "ok")
        append.assert_called_once()
        self.assertEqual(append.call_args[0][0]["items"][0]["id"], "zapret")

    def test_collect_survives_history_failure(self) -> None:
        boom = mock.Mock(side_effect=OSError("диск недоступен"))
        with mock.patch.object(history, "append", boom):
            for patcher in self._patched():
                patcher.start()
                self.addCleanup(patcher.stop)
            result = health.collect({}, {}, {"running": False, "port": 0})
        self.assertEqual(result["level"], "ok")
        boom.assert_called_once()


if __name__ == "__main__":
    unittest.main()
