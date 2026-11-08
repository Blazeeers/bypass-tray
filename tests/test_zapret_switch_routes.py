"""Тумблер zapret: маршруты YouTube/Discord при выключении и включении.

Сеть, Xray и health подменяются; ``route_overrides.json`` — во временном
каталоге. Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import app, routes  # noqa: E402


class ZapretSwitchRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(routes, "config_dir",
                                    return_value=Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        routes.save_overrides({})
        self.logs: list[tuple[str, str]] = []
        self.restarts: list[dict] = []
        self.restart_kwargs: list[dict] = []
        self.patches = [
            mock.patch.object(app.activity, "log",
                              side_effect=lambda text, kind="info":
                              self.logs.append((text, kind))),
            mock.patch.object(app.xray, "restart",
                              side_effect=self._restart),
            mock.patch.object(app.xray, "status",
                              return_value={"running": True, "stream": False}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.youtube = "ok"
        self.discord = "ok"
        self.health_patches = [
            mock.patch.object(app.health, "_check_youtube",
                              side_effect=lambda *a, **k: {
                                  "state": self.youtube}),
            mock.patch.object(app.health, "_check_discord",
                              side_effect=lambda *a, **k: {
                                  "state": self.discord}),
        ]
        for item in self.health_patches:
            item.start()
            self.addCleanup(item.stop)

    def _restart(self, cfg=None, **kwargs):
        self.restarts.append(dict(cfg or {}))
        self.restart_kwargs.append(dict(kwargs))
        return {"ok": True}

    def controller(self, *, vpn: bool = True) -> object:
        tray = app.TrayApp.__new__(app.TrayApp)
        tray.cfg = {}
        tray.proxy = {"running": vpn, "busy": False}
        tray._signature = None
        tray.refresh_activity = lambda: None
        tray.update_icon = lambda: None
        return tray

    def test_stop_routes_only_broken_youtube(self) -> None:
        self.youtube = "bad"
        self.discord = "ok"
        self.controller()._after_zapret_stopped()
        overrides = routes.load_overrides()
        self.assertEqual(overrides["youtube.com"]["source"], "zapret_off")
        self.assertEqual(overrides["youtube.com"]["route"], "proxy")
        self.assertNotIn("discord.com", overrides)
        self.assertEqual(len(self.restarts), 1)

    def test_stop_routes_both_broken(self) -> None:
        self.youtube = "bad"
        self.discord = "bad"
        self.controller()._after_zapret_stopped()
        overrides = routes.load_overrides()
        self.assertEqual(overrides["youtube.com"]["source"], "zapret_off")
        self.assertEqual(overrides["discord.com"]["source"], "zapret_off")
        self.assertEqual(len(self.restarts), 1)

    def test_stop_healthy_services_stay_direct(self) -> None:
        self.youtube = "ok"
        self.discord = "ok"
        self.controller()._after_zapret_stopped()
        self.assertEqual(routes.load_overrides(), {})
        self.assertEqual(len(self.restarts), 1)

    def test_stop_without_vpn_does_not_route(self) -> None:
        self.youtube = "bad"
        self.discord = "bad"
        self.controller(vpn=False)._after_zapret_stopped()
        self.assertEqual(routes.load_overrides(), {})
        self.assertEqual(self.restarts, [])
        self.assertTrue(any(kind == "warn" and "VPN не поднят" in text
                            for text, kind in self.logs))

    def test_start_removes_only_zapret_off(self) -> None:
        routes.save_overrides({
            "youtube.com": {"route": "proxy", "source": "zapret_off",
                            "since": 1.0, "checked": 1.0},
            "googlevideo.com": {"route": "proxy", "source": "escalation",
                                "since": 1.0, "checked": 1.0},
            "direct.example": {"route": "direct", "source": "audit",
                               "since": 1.0, "checked": 1.0},
        })
        self.controller()._after_zapret_started()
        overrides = routes.load_overrides()
        self.assertNotIn("youtube.com", overrides)
        self.assertEqual(overrides["googlevideo.com"]["source"], "escalation")
        self.assertEqual(overrides["direct.example"]["route"], "direct")
        self.assertEqual(len(self.restarts), 1)

    def test_start_without_route_changes_still_rebuilds(self) -> None:
        routes.save_overrides({
            "googlevideo.com": {"route": "proxy", "source": "escalation",
                                "since": 1.0, "checked": 1.0},
        })
        self.controller()._after_zapret_started()
        self.assertEqual(len(self.restarts), 1)
        self.assertIn("googlevideo.com", routes.load_overrides())

    def test_zapret_off_survives_full_audit(self) -> None:
        routes.save_overrides({
            "youtube.com": {"route": "proxy", "source": "zapret_off",
                            "since": 1.0, "checked": 1.0},
        })
        applied = routes.apply_audit(
            {"full": True, "items": [{"domain": "other.example",
                                      "route": "proxy"}]}, {})
        self.assertTrue(applied.get("saved"), applied)
        self.assertEqual(routes.load_overrides()["youtube.com"]["source"],
                         "zapret_off")

    def test_confirm_stop_sets_flag_and_routes(self) -> None:
        self.youtube = "bad"
        self.discord = "ok"
        tray = self.controller()
        tray.status = {"zapret": {"running": False}}
        tray._zapret_user_off = False
        tray._confirm_zapret(False)
        self.assertTrue(tray._zapret_user_off)
        self.assertEqual(routes.load_overrides()["youtube.com"]["source"],
                         "zapret_off")

    def test_manual_route_is_not_overwritten(self) -> None:
        self.youtube = "bad"
        self.discord = "ok"
        tray = self.controller()
        tray.cfg = {"xray_routes": {"geosite:youtube": "direct"}}
        tray._after_zapret_stopped()
        self.assertEqual(routes.load_overrides(), {})
        self.assertEqual(len(self.restarts), 1)


if __name__ == "__main__":
    unittest.main()
