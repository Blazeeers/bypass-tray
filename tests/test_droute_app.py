"""Встроенный Droute: контроллер app, стартовая проверка и updates.

Сеть, droute.exe, Discord и Xray подменяются. Запуск:
``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import actions, app, config, droute, updates  # noqa: E402


def wait(predicate, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.01)
    return predicate()


def tray(cfg: dict | None = None) -> app.TrayApp:
    instance = app.TrayApp.__new__(app.TrayApp)
    instance.cfg = dict(cfg or {})
    instance.busy = {}
    instance._signature = None
    instance._droute_checked = False
    instance._droute_warned = False
    instance.refresh = lambda: None
    instance.refresh_activity = lambda: None
    instance.notify = lambda *args, **kwargs: None
    return instance


class DrouteControllerTests(unittest.TestCase):
    def test_toggle_on_calls_ensure_and_discord_callbacks(self) -> None:
        instance = tray({"droute_socks_port": 10818,
                         "droute_branch": "canary"})
        calls: list[tuple] = []

        def ensure(port, branch, close_discord_cb=None, reopen_cb=None):
            calls.append(("ensure", port, branch))
            close_discord_cb()
            reopen_cb()
            return {"ok": True, "action": "installed", "version": "1.0"}

        with mock.patch.object(app.config, "save_config", return_value=True), \
                mock.patch.object(app.droute, "ensure", side_effect=ensure), \
                mock.patch.object(app.actions, "stop_discord") as close, \
                mock.patch.object(app.actions, "restart_discord") as reopen, \
                mock.patch.object(app.activity, "log"):
            self.assertTrue(instance.set_droute_enabled(True))
            self.assertTrue(wait(lambda: not instance.busy))
        self.assertEqual(calls[0][:3], ("ensure", 10818, "canary"))
        close.assert_called_once()
        reopen.assert_called_once_with(False, 10818)
        self.assertTrue(instance.cfg["droute_enabled"])

    def test_toggle_on_failure_rolls_back_flag(self) -> None:
        instance = tray({"droute_socks_port": 10819})
        with mock.patch.object(app.config, "save_config", return_value=True), \
                mock.patch.object(app.droute, "ensure",
                                  return_value={"ok": False,
                                                "action": "download",
                                                "error": "no net"}), \
                mock.patch.object(app.activity, "log"):
            self.assertTrue(instance.set_droute_enabled(True))
            self.assertTrue(wait(lambda: not instance.busy))
        self.assertFalse(instance.cfg["droute_enabled"])

    def test_toggle_off_calls_uninstall(self) -> None:
        instance = tray({"droute_socks_port": 10819})
        calls: list[tuple] = []

        def uninstall(branch, close_discord_cb=None, reopen_cb=None):
            calls.append(("uninstall", branch))
            return {"ok": True, "action": "uninstalled"}

        with mock.patch.object(app.config, "save_config", return_value=True), \
                mock.patch.object(app.droute, "uninstall", side_effect=uninstall), \
                mock.patch.object(app.activity, "log"):
            self.assertTrue(instance.set_droute_enabled(False))
            self.assertTrue(wait(lambda: not instance.busy))
        self.assertEqual(calls, [("uninstall", "stable")])
        self.assertFalse(instance.cfg["droute_enabled"])

    def test_install_and_remove(self) -> None:
        instance = tray({"droute_socks_port": 10819})
        with mock.patch.object(app.config, "save_config", return_value=True), \
                mock.patch.object(app.droute, "install",
                                  return_value={"ok": True, "action": "installed"}
                                  ) as install, \
                mock.patch.object(app.activity, "log"):
            self.assertTrue(instance.install_droute())
            self.assertTrue(wait(lambda: not instance.busy))
        install.assert_called_once()

        with mock.patch.object(app.config, "save_config", return_value=True), \
                mock.patch.object(app.droute, "uninstall",
                                  return_value={"ok": True, "action": "uninstalled"}
                                  ) as uninstall, \
                mock.patch.object(app.activity, "log"):
            self.assertTrue(instance.remove_droute())
            self.assertTrue(wait(lambda: not instance.busy))
        uninstall.assert_called_once()
        self.assertFalse(instance.cfg["droute_enabled"])

    def test_startup_ensure_when_missing(self) -> None:
        instance = tray({"droute_enabled": True})
        with mock.patch.object(app.droute, "discord_bases",
                               return_value=[Path("C:/Discord")]), \
                mock.patch.object(app.droute, "discord_branch",
                                  return_value="stable"), \
                mock.patch.object(app.droute, "installed",
                                  return_value=False), \
                mock.patch.object(app.droute, "ensure",
                                  return_value={"ok": True, "action": "installed"}
                                  ) as ensure, \
                mock.patch.object(app.activity, "log"):
            instance._droute_startup_check()
        ensure.assert_called_once()

    def test_startup_present_does_nothing(self) -> None:
        instance = tray({"droute_enabled": True, "droute_socks_port": 10819})
        with mock.patch.object(app.droute, "discord_bases",
                               return_value=[Path("C:/Discord")]), \
                mock.patch.object(app.droute, "discord_branch",
                                  return_value="stable"), \
                mock.patch.object(app.droute, "installed", return_value=True), \
                mock.patch.object(app.droute, "configured_port",
                                  return_value=10819), \
                mock.patch.object(app.droute, "ensure") as ensure:
            instance._droute_startup_check()
        ensure.assert_not_called()

    def test_startup_disabled_does_nothing(self) -> None:
        instance = tray({"droute_enabled": False})
        with mock.patch.object(app.droute, "discord_bases") as bases, \
                mock.patch.object(app.droute, "ensure") as ensure:
            instance._droute_startup_check()
        bases.assert_not_called()
        ensure.assert_not_called()


class DrouteUpdatesTests(unittest.TestCase):
    def test_check_all_includes_droute(self) -> None:
        cfg = {"droute_enabled": True, "droute_branch": "stable"}
        with mock.patch.object(updates, "_latest_tags",
                               return_value=({"zapret": "", "tgwsproxy": "",
                                              "xray": ""}, "")), \
                mock.patch.object(updates, "present_components",
                                  return_value={"zapret": True,
                                                "tgwsproxy": True,
                                                "xray": True,
                                                "refilter": True,
                                                "service": False}), \
                mock.patch.object(updates, "installed_versions",
                                  return_value={"zapret": "1", "tgwsproxy": "1",
                                                "xray": "1"}), \
                mock.patch.object(updates.droute, "discord_bases",
                                  return_value=[Path("C:/Discord")]), \
                mock.patch.object(updates.droute, "discord_branch",
                                  return_value="stable"), \
                mock.patch.object(updates.droute, "installed",
                                  return_value=True), \
                mock.patch.object(updates.droute, "installed_version",
                                  return_value="1.0"), \
                mock.patch.object(updates.droute, "available_version",
                                  return_value="2.0"):
            result = updates.check_all(cfg, {})
        self.assertIn("droute", result["components"])
        self.assertTrue(result["components"]["droute"]["outdated"])
        self.assertIn("droute", result["outdated"])

    def test_update_all_installs_droute(self) -> None:
        cfg = {"droute_enabled": True, "droute_branch": "stable",
               "droute_socks_port": 10819}
        state = {"installed": {"zapret": "1", "tgws": "1", "xray": "1"}}
        with mock.patch.object(updates, "present_components",
                               return_value={"zapret": True, "tgwsproxy": True,
                                             "xray": True, "refilter": True,
                                             "service": False}), \
                mock.patch.object(updates, "_latest_tags",
                                  return_value=({"zapret": "", "tgwsproxy": "",
                                                 "xray": ""}, "")), \
                mock.patch.object(updates.status, "service_image_path",
                                  return_value=""), \
                mock.patch.object(updates.blocked, "update",
                                  return_value={"ok": True}), \
                mock.patch.object(updates.config, "save_config",
                                  return_value=True), \
                mock.patch.object(updates.config, "save_state",
                                  return_value=True), \
                mock.patch.object(updates.droute, "discord_bases",
                                  return_value=[Path("C:/Discord")]), \
                mock.patch.object(updates.droute, "discord_branch",
                                  return_value="stable"), \
                mock.patch.object(updates.droute, "installed",
                                  return_value=False), \
                mock.patch.object(updates.droute, "installed_version",
                                  return_value=""), \
                mock.patch.object(updates.droute, "available_version",
                                  return_value="2.0"), \
                mock.patch.object(updates.droute, "install",
                                  return_value={"ok": True, "version": "2.0",
                                                "action": "installed"}
                                  ) as install, \
                mock.patch.object(updates.activity, "log"):
            result = updates.update_all(cfg, state)
        install.assert_called_once()
        self.assertIn("droute", result["installed"])
        self.assertTrue(result["components"]["droute"]["ok"])


if __name__ == "__main__":
    unittest.main()
