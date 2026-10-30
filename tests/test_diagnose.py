"""Тесты диагноза: чистая функция ``diagnose`` на синтетических отчётах.

Ни сети, ни файлов: на вход подаются те же словари, что собирают ``health``,
``xray.status`` и ``history.summary``.

Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import diagnose  # noqa: E402

NOW = 1_700_000_000.0
MINUTE = 60.0

#: Строка, похожая на UUID, и адрес сайта — их в выводе быть не должно.
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
URL_RE = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(?:com|ru|net|org)\b", re.I)

CFG = {"health_minutes": 10}


def item(item_id: str, state: str, detail: str = "") -> dict:
    return {"id": item_id, "title": item_id, "state": state, "detail": detail,
            "ms": 0}


def report(states: dict | None = None, level: str = "ok",
           age_minutes: float = 1.0) -> dict:
    """Отчёт ``health`` с заданными состояниями; по умолчанию всё ``ok``."""
    base = {item_id: "ok" for item_id in diagnose.FACT_ORDER}
    base.update(states or {})
    return {
        "checked": NOW - age_minutes * MINUTE,
        "level": level,
        "verdict": "ok",
        "items": [item(item_id, state) for item_id, state in base.items()],
    }


def proxy(running: bool = True, mode: str = "blocked") -> dict:
    return {"running": running, "routing_mode": mode, "port": 10818}


def summary(incidents: int = 0, uptime: float | None = 100) -> dict:
    return {"incidents": incidents, "uptime_pct": uptime, "reports": 20}


def run(rep: dict | None, prox: dict | None = None,
        cfg: dict | None = None, hist: dict | None = None) -> dict:
    return diagnose.diagnose(rep, prox if prox is not None else proxy(),
                             cfg or CFG, hist if hist is not None else summary(),
                             now=NOW)


class StaleTests(unittest.TestCase):
    def test_no_report(self) -> None:
        data = run(None)
        self.assertEqual(data["code"], "stale")
        self.assertEqual(data["action"]["kind"], "check_all")

    def test_empty_report(self) -> None:
        self.assertEqual(run({})["code"], "stale")

    def test_old_report(self) -> None:
        # Два периода проверки — уже устарело.
        data = run(report(age_minutes=21))
        self.assertEqual(data["code"], "stale")
        self.assertEqual(data["level"], "warn")

    def test_fresh_report_is_not_stale(self) -> None:
        self.assertEqual(run(report(age_minutes=15))["code"], "ok")

    def test_bad_checked_stamp(self) -> None:
        rep = report()
        rep["checked"] = "вчера"
        self.assertEqual(run(rep)["code"], "stale")


class VpnOffTests(unittest.TestCase):
    def test_vpn_off_with_blocked_services(self) -> None:
        data = run(report({"youtube": "bad", "discord": "bad"}, level="down"),
                   proxy(running=False))
        self.assertEqual(data["code"], "vpn_off")
        self.assertEqual(data["action"], {"label": "Включить VPN",
                                          "kind": "vpn_on"})

    def test_vpn_off_in_all_mode_when_services_off(self) -> None:
        # В режиме «all» без VPN YouTube и Discord помечены «off» (нужен VPN).
        data = run(report({"youtube": "off", "discord": "off"}, level="down"),
                   proxy(running=False, mode="all"))
        self.assertEqual(data["code"], "vpn_off")

    def test_vpn_off_but_zapret_works(self) -> None:
        # Обход работает через zapret, VPN выключен осознанно — это не диагноз.
        data = run(report(), proxy(running=False))
        self.assertNotEqual(data["code"], "vpn_off")


class TunnelTests(unittest.TestCase):
    def test_vpn_bad(self) -> None:
        data = run(report({"vpn": "bad"}, level="down"))
        self.assertEqual(data["code"], "tunnel_dead")
        self.assertEqual(data["level"], "down")
        self.assertEqual(data["action"]["kind"], "switch_server")


class ZapretTests(unittest.TestCase):
    def test_zapret_stopped(self) -> None:
        data = run(report({"zapret": "bad", "youtube": "bad", "discord": "bad"},
                          level="down"))
        self.assertEqual(data["code"], "zapret_stopped")
        self.assertEqual(data["action"]["kind"], "zapret_start")

    def test_all_mode_does_not_blame_zapret(self) -> None:
        # В режиме «all» YouTube идёт через VPN: остановленный zapret — не сбой.
        data = run(report({"zapret": "warn", "youtube": "ok", "discord": "ok"}),
                   proxy(mode="all"))
        self.assertNotEqual(data["code"], "zapret_stopped")
        self.assertEqual(data["code"], "ok")

    def test_all_mode_with_broken_stream_is_not_zapret(self) -> None:
        data = run(report({"zapret": "warn", "youtube": "bad", "discord": "bad"},
                          level="down"), proxy(mode="all"))
        self.assertNotEqual(data["code"], "zapret_stopped")
        self.assertEqual(data["level"], "down")

    def test_zapret_weak(self) -> None:
        data = run(report({"youtube": "bad"}, level="degraded"))
        self.assertEqual(data["code"], "zapret_weak")
        self.assertEqual(data["action"]["kind"], "strategy_pick")

    def test_zapret_weak_needs_tunnel(self) -> None:
        # Туннель не отвечает — там срабатывает правило туннеля, не стратегии.
        data = run(report({"vpn": "bad", "youtube": "bad"}, level="down"))
        self.assertEqual(data["code"], "tunnel_dead")


class ApiTests(unittest.TestCase):
    def test_deepseek_bad(self) -> None:
        data = run(report({"deepseek": "bad"}, level="down"))
        self.assertEqual(data["code"], "api_down")
        self.assertEqual(data["level"], "down")


class TelegramTests(unittest.TestCase):
    def test_telegram_bad_with_live_proxy(self) -> None:
        data = run(report({"telegram": "bad"}, level="degraded"))
        self.assertEqual(data["code"], "telegram_blocked")
        self.assertIsNone(data["action"])

    def test_tgws_bad(self) -> None:
        data = run(report({"tgwsproxy": "bad"}, level="degraded"))
        self.assertEqual(data["code"], "tgws_down")
        self.assertEqual(data["action"]["kind"], "tgws_restart")


class HistoryTests(unittest.TestCase):
    def test_many_incidents(self) -> None:
        data = run(report(), hist=summary(incidents=3))
        self.assertEqual(data["code"], "unstable")
        self.assertEqual(data["level"], "warn")
        self.assertIsNone(data["action"])

    def test_low_uptime(self) -> None:
        data = run(report(), hist=summary(incidents=1, uptime=65))
        self.assertEqual(data["code"], "unstable")

    def test_unstable_not_reported_when_current_is_bad(self) -> None:
        data = run(report({"deepseek": "bad"}, level="down"),
                   hist=summary(incidents=9, uptime=40))
        self.assertEqual(data["code"], "api_down")

    def test_single_degraded_report_is_not_unstable(self) -> None:
        # Один отчёт не повод держать «Нестабильно за сутки» на сутки вперёд.
        hist = {"incidents": 1, "uptime_pct": 0, "reports": 1}
        data = run(report({"youtube": "warn"}, level="degraded"), hist=hist)
        self.assertNotEqual(data["code"], "unstable")
        self.assertEqual(data["code"], "degraded")

    def test_low_uptime_with_few_reports_is_not_unstable(self) -> None:
        hist = {"incidents": 0, "uptime_pct": 0, "reports": 5}
        self.assertEqual(run(report(), hist=hist)["code"], "ok")
        # Шестой отчёт — данных достаточно, правило снова работает.
        hist = {"incidents": 0, "uptime_pct": 0, "reports": 6}
        self.assertEqual(run(report(), hist=hist)["code"], "unstable")


class OkTests(unittest.TestCase):
    def test_all_ok(self) -> None:
        data = run(report())
        self.assertEqual(data["code"], "ok")
        self.assertEqual(data["level"], "ok")
        self.assertIsNone(data["action"])
        self.assertEqual(data["facts"], [])

    def test_shape(self) -> None:
        data = run(report({"youtube": "bad"}, level="degraded"))
        self.assertEqual(set(data), {"code", "level", "title", "action", "facts"})
        self.assertIn(data["level"], ("ok", "warn", "down"))


class NoSecretsTests(unittest.TestCase):
    def test_no_uuid_or_url_in_title_and_facts(self) -> None:
        leak = "https://example.com/sub/0f8fad5b-d9cb-469f-a165-70867728950e"
        states = {"vpn": "bad", "zapret": "bad", "youtube": "bad",
                  "discord": "bad", "deepseek": "bad", "telegram": "bad",
                  "tgwsproxy": "bad"}
        rep = report(states, level="down")
        for entry in rep["items"]:
            entry["detail"] = leak
        data = run(rep)
        text = " ".join([str(data["title"])] + list(data["facts"]))
        self.assertIsNone(UUID_RE.search(text), text)
        self.assertIsNone(URL_RE.search(text), text)

    def test_facts_only_id_and_state(self) -> None:
        data = run(report({"youtube": "bad", "zapret": "bad"}, level="down"))
        for fact in data["facts"]:
            self.assertRegex(fact, r"^[\w]+: [a-z]+$")


if __name__ == "__main__":
    unittest.main()
