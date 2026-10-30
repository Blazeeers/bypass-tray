"""Проверки сервисов по конфигу ``health_items``: модель, движок и уровень.

Сеть не используется: HTTP-примитивы и сокеты подменяются, записи отчёта идут
во временный каталог данных. Запуск:
``python -m unittest discover -s tests -p "test_*.py"``.
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

from bypass_tray import config, health  # noqa: E402

BUILTIN_ORDER = ["zapret", "youtube", "discord", "deepseek",
                 "telegram", "tgwsproxy", "vpn"]


def _https(item_id: str = "svc_site", target: str = "https://example.test/",
           level: str = "minor", expect: dict | None = None,
           via: str = "auto", **extra) -> dict:
    record = {
        "id": item_id,
        "title": item_id,
        "kind": "https",
        "target": target,
        "expect": expect or {"status": "any", "contains": ""},
        "via": via,
        "level": level,
        "check": True,
        "show": True,
        "timeout": 1,
    }
    record.update(extra)
    return record


def _tcp(item_id: str = "svc_port", target: str = "example.test:443",
         level: str = "minor", **extra) -> dict:
    record = {
        "id": item_id,
        "title": item_id,
        "kind": "tcp",
        "target": target,
        "expect": {},
        "via": "auto",
        "level": level,
        "check": True,
        "show": True,
        "timeout": 1,
    }
    record.update(extra)
    return record


def _response(status: int = 200, body: str = "", error: str = "") -> dict:
    return {"status": status, "body": body, "ms": 5, "error": error}


class _TempData(unittest.TestCase):
    """Временный каталог данных: ``health.json`` и история не трогают APPDATA."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        config.set_config_dir(self.data)
        self.addCleanup(config.set_config_dir, None)

    def cfg(self, records: list[dict]) -> dict:
        return {"xray_port": 10818, "health_items": records}


class ConfigModelTests(_TempData):
    """Модель: умолчания, миграция старого конфига, чистка мусора."""

    def test_defaults_keep_builtin_order(self) -> None:
        records = config._default_health_items()
        self.assertEqual([record["id"] for record in records], BUILTIN_ORDER)
        self.assertTrue(all(record["kind"] == "builtin" for record in records))
        self.assertEqual(records[4]["level"], "minor")
        self.assertEqual(records[0]["level"], "critical")

    def test_old_config_without_key_gets_builtins(self) -> None:
        config.CONFIG_FILE.write_text(json.dumps({
            "zapret_dir": "C:/Zapret",
            "tgws_exe": "C:/TgWsProxy.exe",
            "xray_routes": {},
        }, ensure_ascii=False), encoding="utf-8")
        cfg = config.load_config()
        records = cfg["health_items"]
        self.assertEqual([record["id"] for record in records], BUILTIN_ORDER)
        self.assertTrue(all(record["kind"] == "builtin" for record in records))
        self.assertEqual(config.health_items_report(), [])

    def test_garbage_is_dropped_with_report(self) -> None:
        items, problems = config.normalise_health_items([
            {"id": "", "title": "без id", "kind": "tcp", "target": "h:1"},
            {"id": "no_title", "kind": "tcp", "target": "h:2"},
            {"id": "bad_kind", "title": "x", "kind": "ftp", "target": "h:3"},
            {"id": "bad_https", "title": "x", "kind": "https", "target": "not a url"},
            {"id": "bad_tcp", "title": "x", "kind": "tcp", "target": "host:70000"},
            _https("keep"),
            _https("keep"),
        ])
        self.assertEqual([item["id"] for item in items], ["keep"])
        self.assertEqual(len(problems), 6, problems)

    def test_custom_checks_are_capped(self) -> None:
        records = [_tcp(f"svc_{index}", target=f"host.test:{1000 + index}")
                   for index in range(12)]
        items, problems = config.normalise_health_items(records)
        self.assertEqual(len(items), config.HEALTH_MAX_CUSTOM)
        self.assertEqual([item["id"] for item in items],
                         [f"svc_{index}" for index in range(10)])
        self.assertTrue(any("больше" in problem for problem in problems), problems)

    def test_present_broken_list_is_normalised_with_backup(self) -> None:
        config.CONFIG_FILE.write_text(json.dumps({
            "zapret_dir": "C:/Zapret",
            "tgws_exe": "C:/TgWsProxy.exe",
            "xray_routes": {},
            "health_items": [_https("keep"),
                             {"id": "keep", "title": "dup", "kind": "tcp",
                              "target": "h:1"}],
        }, ensure_ascii=False), encoding="utf-8")
        cfg = config.load_config()
        self.assertEqual([item["id"] for item in cfg["health_items"]], ["keep"])
        self.assertTrue(config.health_items_report())
        self.assertTrue(list(self.data.glob("config.json.bak-*")))

    def test_expect_and_timeout_are_clamped(self) -> None:
        items, _ = config.normalise_health_items([
            _https("a", expect={"status": "418", "contains": "x"}, timeout=99),
            _https("b", expect={"status": "мусор"}, timeout=0),
        ])
        self.assertEqual(items[0]["expect"], {"status": 418, "contains": "x"})
        self.assertEqual(items[0]["timeout"], 15)
        self.assertEqual(items[1]["expect"]["status"], "any")
        self.assertEqual(items[1]["timeout"], 1)


class EngineTests(_TempData):
    """Движок: какие записи выполняются и что попадает в отчёт."""

    def test_check_false_is_skipped_entirely(self) -> None:
        records = config._default_health_items()
        for record in records:
            if record["id"] == "youtube":
                record["check"] = False
        ok = lambda *a, **k: health._item("x", "x", "ok", "", 0)  # noqa: E731
        with mock.patch.object(health, "_check_youtube") as youtube, \
                mock.patch.object(health, "_check_discord", lambda *a, **k: ok()), \
                mock.patch.object(health, "_check_deepseek", lambda *a, **k: ok()), \
                mock.patch.object(health, "_check_telegram", lambda *a, **k: ok()), \
                mock.patch.object(health, "_check_vpn", lambda *a, **k: ok()):
            result = health.collect(self.cfg(records), {}, {"running": False})
        youtube.assert_not_called()
        ids = [item["id"] for item in result["items"]]
        self.assertNotIn("youtube", ids)
        self.assertEqual(len(ids), len(BUILTIN_ORDER) - 1)

    def test_show_false_is_checked_and_counted(self) -> None:
        record = _https("svc_hidden", show=False)
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(200)) as fetch:
            result = health.collect(self.cfg([record]), {}, {"running": False})
        fetch.assert_called_once()
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], "svc_hidden")
        self.assertEqual(result["items"][0]["state"], "ok")
        self.assertEqual(result["level"], "ok")

    def test_report_order_follows_config(self) -> None:
        records = [_tcp("first", target="a.test:1"),
                   _https("second", target="https://b.test/"),
                   _tcp("third", target="c.test:3")]
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(200)):
            def fake_connect(address, timeout=None):
                if address[1] == 1:
                    raise ConnectionRefusedError("closed")
                return mock.MagicMock()

            with mock.patch("socket.create_connection", fake_connect):
                result = health.collect(self.cfg(records), {}, {"running": False})
        self.assertEqual([item["id"] for item in result["items"]],
                         ["first", "second", "third"])
        self.assertEqual(result["items"][0]["state"], "warn")
        self.assertEqual(result["items"][2]["state"], "ok")


class HttpsProbeTests(_TempData):
    """Своя HTTPS-проверка: ожидания, состояние и ``via``."""

    def test_200_and_contains_is_ok(self) -> None:
        record = _https(expect={"status": "2xx", "contains": "готово"})
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(200, "всё готово")):
            item = health._check_https(record, None, 1)
        self.assertEqual(item["state"], "ok")

    def test_wrong_status_is_warn(self) -> None:
        record = _https(expect={"status": 200})
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(404)):
            item = health._check_https(record, None, 1)
        self.assertEqual(item["state"], "warn")
        self.assertIn("HTTP 404", item["detail"])

    def test_missing_substring_is_warn(self) -> None:
        record = _https(expect={"status": "2xx", "contains": "ok-marker"})
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(200, "другой текст")):
            item = health._check_https(record, None, 1)
        self.assertEqual(item["state"], "warn")
        self.assertIn("нет ожидаемого текста", item["detail"])

    def test_critical_failure_is_bad(self) -> None:
        record = _https(level="critical", expect={"status": "2xx"})
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(500)):
            item = health._check_https(record, None, 1)
        self.assertEqual(item["state"], "bad")

    def test_any_accepts_4xx_but_not_5xx(self) -> None:
        record = _https()
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(404)):
            self.assertEqual(health._check_https(record, None, 1)["state"], "ok")
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(503)):
            self.assertEqual(health._check_https(record, None, 1)["state"], "warn")


class TcpProbeTests(_TempData):
    """Своя TCP-проверка: открытый и закрытый порт."""

    def test_open_port_is_ok(self) -> None:
        record = _tcp()
        with mock.patch("socket.create_connection",
                        return_value=mock.MagicMock()):
            item = health._check_tcp(record, 1)
        self.assertEqual(item["state"], "ok")
        self.assertIn("example.test:443", item["detail"])

    def test_closed_port_follows_level(self) -> None:
        with mock.patch("socket.create_connection",
                        side_effect=ConnectionRefusedError("closed")):
            minor = health._check_tcp(_tcp(), 1)
            critical = health._check_tcp(_tcp(level="critical"), 1)
        self.assertEqual(minor["state"], "warn")
        self.assertEqual(critical["state"], "bad")


class ViaTests(_TempData):
    """Маршрут проверки: ``direct``, ``proxy`` и ``auto``."""

    def test_direct_never_uses_proxy(self) -> None:
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(200)) as direct, \
                mock.patch.object(health, "_fetch_via_proxy") as proxied:
            item = health._check_https(_https(via="direct"), 10818, 1)
        self.assertEqual(item["state"], "ok")
        direct.assert_called_once()
        proxied.assert_not_called()

    def test_proxy_uses_local_port(self) -> None:
        with mock.patch.object(health, "_fetch_direct") as direct, \
                mock.patch.object(health, "_fetch_via_proxy",
                                  return_value=_response(200)) as proxied:
            item = health._check_https(_https(via="proxy"), 10818, 1)
        self.assertEqual(item["state"], "ok")
        direct.assert_not_called()
        self.assertEqual(proxied.call_args[0][1], 10818)

    def test_proxy_without_vpn_is_reported(self) -> None:
        with mock.patch.object(health, "_fetch_via_proxy") as proxied:
            item = health._check_https(_https(via="proxy"), None, 1)
        proxied.assert_not_called()
        self.assertEqual(item["state"], "warn")
        self.assertIn("нужен VPN", item["detail"])

    def test_auto_tries_direct_first_then_proxy(self) -> None:
        calls: list[str] = []

        def direct(url, timeout):
            calls.append("direct")
            return _response(0, error="TimeoutError")

        def proxied(url, port, timeout):
            calls.append("proxy")
            return _response(200)

        with mock.patch.object(health, "_fetch_direct", direct), \
                mock.patch.object(health, "_fetch_via_proxy", proxied):
            item = health._check_https(_https(via="auto"), 10818, 1)
        self.assertEqual(calls, ["direct", "proxy"])
        self.assertEqual(item["state"], "ok")


class DetailTests(_TempData):
    """``detail`` показывает схему, хост и путь, но не query и не фрагмент."""

    def test_success_shows_path_but_hides_query_and_token(self) -> None:
        target = "https://api.example.test/v1/status?token=secret&key=abc"
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(200)):
            item = health._check_https(_https(target=target), None, 1)
        self.assertIn("https://api.example.test/v1/status", item["detail"])
        for secret in ("token", "secret", "key=abc"):
            self.assertNotIn(secret, item["detail"])

    def test_failure_shows_path_but_hides_query_and_token(self) -> None:
        target = "https://api.example.test/v1/status?token=secret"
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(0, error="TimeoutError")):
            item = health._check_https(_https(target=target), None, 1)
        self.assertIn("https://api.example.test/v1/status", item["detail"])
        for secret in ("token", "secret"):
            self.assertNotIn(secret, item["detail"])

    def test_fragment_is_dropped_too(self) -> None:
        target = "https://api.example.test/api/health#section-2"
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(200)):
            item = health._check_https(_https(target=target), None, 1)
        self.assertIn("https://api.example.test/api/health", item["detail"])
        self.assertNotIn("section-2", item["detail"])


class LevelTests(_TempData):
    """Уровень учитывает ``level`` записи и не ломает встроенные правила."""

    def test_bad_minor_does_not_mean_down(self) -> None:
        self.assertEqual(health.level([{"id": "telegram", "state": "bad"}]),
                         "degraded")
        self.assertEqual(health.level([{"id": "tgwsproxy", "state": "bad"}]),
                         "degraded")

    def test_bad_critical_keeps_old_rules(self) -> None:
        self.assertEqual(health.level([{"id": "vpn", "state": "bad"}]), "down")
        self.assertEqual(health.level([{"id": "deepseek", "state": "bad"}]),
                         "down")
        self.assertEqual(health.level([{"id": "youtube", "state": "bad"}]),
                         "degraded")

    def test_custom_minor_failure_is_attention(self) -> None:
        records = [_https("svc_site", level="minor")]
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(500)):
            result = health.collect(self.cfg(records), {}, {"running": False})
        self.assertEqual(result["items"][0]["state"], "warn")
        self.assertEqual(result["level"], "degraded")
        self.assertEqual(result["verdict"], "warn")

    def test_custom_critical_bad_counts_as_critical(self) -> None:
        records = [_https("svc_site", level="critical")]
        with mock.patch.object(health, "_fetch_direct",
                               return_value=_response(500)):
            result = health.collect(self.cfg(records), {}, {"running": False})
        self.assertEqual(result["items"][0]["state"], "bad")
        # Один критичный — «требует внимания», как одиночный bad встроенных.
        self.assertEqual(result["level"], "degraded")
        # Второй критичный доводит до «есть сбой».
        items = result["items"] + [{"id": "youtube", "state": "bad"}]
        self.assertEqual(health.level(items, self.cfg(records)), "down")


class BudgetTests(_TempData):
    """Общий бюджет: медленная проба не задерживает остальные."""

    def test_slow_probe_does_not_block_fast_one(self) -> None:
        def fetch(url, timeout):
            time.sleep(1.0)
            return _response(200)

        records = [_https("slow", target="https://slow.test/", timeout=1),
                   _https("fast", target="https://fast.test/", timeout=1)]
        started = time.monotonic()
        with mock.patch.object(health, "_fetch_direct", fetch):
            result = health.collect(self.cfg(records), {}, {"running": False},
                                     timeout=1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5, f"сбор занял {elapsed:.1f} с")
        self.assertEqual([item["id"] for item in result["items"]],
                         ["slow", "fast"])
        self.assertTrue(all(item["state"] == "ok" for item in result["items"]))


if __name__ == "__main__":
    unittest.main()
