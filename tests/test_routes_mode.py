"""Тесты режима ``all``: аудит маршрутов, правила Xray, проверки здоровья.

Регрессии ревью фазы 2: в режиме «всё, кроме РФ, через VPN» прямой маршрут
аудита уводил трафик мимо VPN, а health проверял YouTube/Discord напрямую.

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

from bypass_tray import health, routes, xray  # noqa: E402

#: Минимальный сервер: ``build_config`` читает только ``outbound``.
SERVER = {
    "name": "test",
    "host": "example.com",
    "port": 443,
    "outbound": {"tag": "proxy", "protocol": "vless",
                 "settings": {"vnext": []}},
}

PORT = 10899


def _audit(*items: tuple[str, str], full: bool = True) -> dict:
    return {
        "at": 100.0,
        "full": full,
        "items": [{"domain": domain, "route": route} for domain, route in items],
    }


class AuditModeTests(unittest.TestCase):
    """``all`` не копит direct в оверрайдах, ``blocked`` — пишет как раньше."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(routes, "config_dir",
                                    return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_mode_does_not_save_audit_direct(self) -> None:
        applied = routes.apply_audit(
            _audit(("secret.example", "direct"), ("blocked.example", "proxy")),
            {"routing_mode": "all"})
        self.assertTrue(applied["saved"])
        self.assertNotIn("secret.example", applied["overrides"])
        self.assertNotIn("secret.example", routes.load_overrides())
        # proxy-решение сохраняется: оно и нужно режиму ``all``.
        self.assertEqual(applied["overrides"]["blocked.example"]["route"],
                         "proxy")

    def test_all_mode_drops_stale_direct_from_file(self) -> None:
        # Файл остался от сессии в ``blocked``: защита нужна и при записи.
        routes.save_overrides(
            {"secret.example": {"route": "direct", "since": 1, "checked": 1}},
            at=1.0)
        routes.apply_audit(_audit(("secret.example", "direct")),
                           {"routing_mode": "all"})
        self.assertNotIn("secret.example", routes.load_overrides())

    def test_blocked_mode_saves_audit_direct(self) -> None:
        applied = routes.apply_audit(_audit(("secret.example", "direct")),
                                     {"routing_mode": "blocked"})
        self.assertEqual(applied["overrides"]["secret.example"]["route"],
                         "direct")
        self.assertEqual(routes.direct_domains(), ["secret.example"])

    def test_all_mode_keeps_user_direct(self) -> None:
        cfg = {"routing_mode": "all",
               "xray_routes": {"mine.example": "direct"}}
        applied = routes.apply_audit(_audit(("mine.example", "direct")), cfg)
        # Пользовательское правило не переписывается аудитом ни в каком режиме.
        self.assertNotIn("mine.example", applied["overrides"])


class BuildConfigModeTests(unittest.TestCase):
    """Аудит-direct не становится правилом в ``all``, пользовательский — да."""

    def _direct_domains(self, mode: str, **kwargs) -> list[str]:
        cfg = xray.build_config(SERVER, PORT, routing_mode=mode, **kwargs)
        return [entry for rule in cfg["routing"]["rules"]
                if rule.get("outboundTag") == "direct"
                for entry in (rule.get("domain") or [])]

    def test_all_mode_ignores_audit_direct(self) -> None:
        domains = self._direct_domains("all",
                                       direct_domains=["secret.example"])
        self.assertNotIn("domain:secret.example", domains)
        # А домен при этом не теряется: catch-all ведёт его в VPN.
        self.assertEqual(
            xray.build_config(SERVER, PORT, routing_mode="all",
                              direct_domains=["secret.example"]
                              )["outbounds"][0]["tag"], "proxy")

    def test_blocked_mode_keeps_audit_direct(self) -> None:
        domains = self._direct_domains("blocked",
                                       direct_domains=["secret.example"])
        self.assertIn("domain:secret.example", domains)

    def test_all_mode_keeps_user_direct(self) -> None:
        domains = self._direct_domains("all",
                                       direct_domains=["secret.example"],
                                       user_routes={"mine.example": "direct"})
        self.assertIn("domain:mine.example", domains)
        self.assertNotIn("domain:secret.example", domains)


class StreamContextTests(unittest.TestCase):
    """В ``all`` YouTube/Discord проверяются через прокси, как при флаге."""

    PROXY = {"running": True, "port": 10818, "routing_mode": "all"}

    def test_all_mode_matches_youtube_via_vpn(self) -> None:
        all_mode = health._stream_context({"routing_mode": "all"}, self.PROXY)
        explicit = health._stream_context(
            {"youtube_via_vpn": True}, {"running": True, "port": 10818})
        self.assertEqual(all_mode, (True, 10818))
        self.assertEqual(all_mode, explicit)

    def test_all_mode_without_proxy_needs_vpn(self) -> None:
        state = health._stream_context({"routing_mode": "all"},
                                       {"running": False})
        self.assertEqual(state, (True, None))

    def test_blocked_mode_unchanged(self) -> None:
        state = health._stream_context(
            {"routing_mode": "blocked"}, {"running": True, "port": 10818})
        self.assertEqual(state, (False, None))


class ZapretCheckTests(unittest.TestCase):
    """В ``all`` остановленный zapret — предупреждение, а не сбой."""

    STOPPED = {"zapret": {"running": False, "dir": "C:/zapret",
                          "strategy": ""}}
    PROXY = {"running": True, "port": 10818, "routing_mode": "all"}

    def test_all_mode_is_warning_not_failure(self) -> None:
        item = health._check_zapret(self.STOPPED, {"routing_mode": "all"},
                                    self.PROXY)
        self.assertEqual(item["state"], "warn")
        self.assertIn("через VPN", item["detail"])
        # Плюс остальные проверки в порядке — уровень не «сбой».
        items = [item, {"id": "vpn", "state": "ok"},
                 {"id": "youtube", "state": "ok"},
                 {"id": "discord", "state": "ok"},
                 {"id": "deepseek", "state": "ok"}]
        self.assertEqual(health.level(items, {"routing_mode": "all"},
                                      self.PROXY), "degraded")

    def test_all_mode_without_proxy_is_still_failure(self) -> None:
        # VPN не поднят: резерва нет, сбой настоящий.
        item = health._check_zapret(self.STOPPED, {"routing_mode": "all"},
                                    {"running": False})
        self.assertEqual(item["state"], "bad")

    def test_blocked_mode_unchanged(self) -> None:
        item = health._check_zapret(
            self.STOPPED, {"routing_mode": "blocked"},
            {"running": True, "port": 10818, "routing_mode": "blocked"})
        self.assertEqual(item["state"], "bad")


class BypassCountTests(unittest.TestCase):
    """Счётчик исключений: без служебных записей и без двойного счёта."""

    def test_keeps_one_name_per_domain(self) -> None:
        entries = ["<local>", "127.*", "x.com", "*.x.com"]
        self.assertEqual(xray.count_bypass_domains(entries), 1)

    def test_ignores_local_addresses_and_bare_wildcards(self) -> None:
        entries = ["localhost", "10.*", "192.168.*", "*", "a.com",
                   "*.a.com", "b.com", "::1"]
        self.assertEqual(xray.count_bypass_domains(entries), 2)


if __name__ == "__main__":
    unittest.main()
