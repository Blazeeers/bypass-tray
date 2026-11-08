"""Health с учётом proxy-оверрайдов и ручного выключения zapret.

Сеть, Xray и CLI-вывод подменяются; ``route_overrides.json`` — в temp.
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

from bypass_tray import config, health, routes  # noqa: E402
from bypass_tray import status as status_module  # noqa: E402
from bypass_tray import xray as xray_module  # noqa: E402
from bypass_tray import __main__ as cli  # noqa: E402


class HealthOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        config.set_config_dir(self.data)
        self.addCleanup(config.set_config_dir, None)
        patcher = mock.patch.object(routes, "config_dir", return_value=self.data)
        patcher.start()
        self.addCleanup(patcher.stop)
        routes.save_overrides({})

    def test_any_proxy_override_switches_context(self) -> None:
        routes.save_overrides({
            "discord.com": {"route": "proxy", "source": "escalation"},
        })
        self.assertEqual(
            health._stream_context({}, {"running": True, "port": 10818},
                                   "discord"),
            (True, 10818))
        routes.save_overrides({
            "youtube.com": {"route": "proxy"},  # ручной/без source
        })
        self.assertEqual(
            health._stream_context({}, {"running": True, "port": 10818},
                                   "youtube"),
            (True, 10818))

    def test_no_override_keeps_direct(self) -> None:
        self.assertEqual(
            health._stream_context({}, {"running": True, "port": 10818},
                                   "discord"),
            (False, None))

    def test_discord_probe_goes_through_vpn(self) -> None:
        routes.save_overrides({
            "discord.com": {"route": "proxy", "source": "escalation"},
        })
        ok = {"ok": True, "status": 200, "ms": 10, "error": ""}
        with mock.patch.object(health, "_http_retry", return_value=ok):
            item = health._check_discord(
                {}, {"running": True, "port": 10818}, 3.0)
        self.assertEqual(item["state"], "ok")
        self.assertIn("через VPN", item["detail"])

    def test_youtube_probe_goes_through_vpn(self) -> None:
        routes.save_overrides({
            "youtube.com": {"route": "proxy", "source": "escalation"},
        })
        ok = {"ok": True, "status": 200, "ms": 10, "error": ""}
        video = {"ok": True, "checked": True, "stream": True, "ms": 50}
        with mock.patch.object(health, "_http_retry", return_value=ok), \
                mock.patch.object(health, "_youtube_video",
                                  return_value=video):
            item = health._check_youtube(
                {}, {"running": True, "port": 10818}, 3.0)
        self.assertEqual(item["state"], "ok")
        self.assertIn("через VPN", item["detail"])

    def test_manual_zapret_off_is_not_degraded(self) -> None:
        items = [
            {"id": "zapret", "state": "off"},
            {"id": "youtube", "state": "ok"},
            {"id": "discord", "state": "ok"},
            {"id": "deepseek", "state": "ok"},
            {"id": "telegram", "state": "ok"},
            {"id": "tgwsproxy", "state": "ok"},
            {"id": "vpn", "state": "ok"},
        ]
        self.assertEqual(health.verdict(items), "ok")
        self.assertEqual(health.level(items, {}, {"running": True}), "ok")


class HealthCliTests(unittest.TestCase):
    def test_cli_passes_zapret_off_from_state(self) -> None:
        result = {"items": [], "verdict": "ok", "level": "ok"}
        with mock.patch.object(config, "load_config", return_value={}), \
                mock.patch.object(config, "load_state",
                                  return_value={"zapret_user_off": True}), \
                mock.patch.object(status_module, "collect", return_value={}), \
                mock.patch.object(xray_module, "status", return_value={}), \
                mock.patch.object(health, "collect", return_value=result) as collect, \
                mock.patch.object(cli, "_print_json"), \
                mock.patch.object(sys, "argv", ["bypass-tray", "--health"]):
            self.assertEqual(cli.main(), 0)
        self.assertTrue(collect.call_args.kwargs.get("zapret_off"))


if __name__ == "__main__":
    unittest.main()
