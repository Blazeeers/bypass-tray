"""Тесты проверки обновлений: версии программы, компоненты и запись версий.

Сеть не используется: ``_latest_tags`` подменяется, установка компонентов —
заглушками. Каталог конфигурации — временный, чтобы ``state.json`` теста не
попал в настоящий профиль.

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

from bypass_tray import components, config, updates  # noqa: E402

#: Пустой ответ «нет связи»: так выглядит живой офлайн-прогон.
OFFLINE = ({}, "URLError: нет сети")


class VersionKeyTests(unittest.TestCase):
    """Числовая версия: «0.6.10» больше «0.6.9», хотя строка короче."""

    def test_patch_number_compare(self) -> None:
        self.assertLess(updates._version_key("0.6.9"), updates._version_key("0.6.10"))
        self.assertGreater(updates._version_key("0.6.10"), updates._version_key("0.6.9"))

    def test_minor_number_compare(self) -> None:
        self.assertLess(updates._version_key("0.6.9"), updates._version_key("0.7.0"))

    def test_v_prefix_ignored(self) -> None:
        self.assertEqual(updates._version_key("v0.7.0"), updates._version_key("0.7.0"))

    def test_exe_version_equals_tag(self) -> None:
        # exe отдаёт «1.11.2.0», тег релиза — «v1.11.2».
        self.assertEqual(updates._version_key("1.11.2.0"),
                         updates._version_key("v1.11.2"))

    def test_garbage_is_not_version(self) -> None:
        self.assertIsNone(updates._version_key("zapret-new"))
        self.assertIsNone(updates._version_key(""))
        self.assertIsNone(updates._version_key(None))


class OutdatedTests(unittest.TestCase):
    """``_is_outdated`` — «установлена старше», а не «отличается»."""

    def test_older_is_outdated(self) -> None:
        self.assertTrue(updates._is_outdated("0.6.9", "v0.7.0"))
        self.assertTrue(updates._is_outdated("v1.11.1", "v1.11.2"))

    def test_equal_is_not_outdated(self) -> None:
        self.assertFalse(updates._is_outdated("v0.7.0", "0.7.0"))
        self.assertFalse(updates._is_outdated("1.11.2.0", "v1.11.2"))

    def test_newer_installed_is_not_outdated(self) -> None:
        # Откат не предлагаем: своя сборка новее тега.
        self.assertFalse(updates._is_outdated("0.7.1", "v0.7.0"))

    def test_unknown_versions_are_not_compared(self) -> None:
        self.assertFalse(updates._is_outdated("", "v0.7.0"))
        self.assertFalse(updates._is_outdated("0.6.9", ""))

    def test_non_numeric_tags_fall_back_to_text(self) -> None:
        self.assertTrue(updates._is_outdated("zapret-old", "zapret-new"))
        self.assertFalse(updates._is_outdated("zapret-new", "zapret-new"))


class NeedsUpdateTests(unittest.TestCase):
    """Пустая версия при живых файлах — «неизвестно», а не «актуально»."""

    def test_present_without_version_needs_update(self) -> None:
        self.assertTrue(updates._needs_update("", "v1.10.4", True))

    def test_missing_component_needs_update(self) -> None:
        self.assertTrue(updates._needs_update("1.10.4", "v1.10.4", False))

    def test_known_equal_versions_are_skipped(self) -> None:
        self.assertFalse(updates._needs_update("1.10.4", "v1.10.4", True))

    def test_known_older_version_needs_update(self) -> None:
        self.assertTrue(updates._needs_update("1.10.3", "v1.10.4", True))

    def test_no_network_tag_does_not_force_update(self) -> None:
        # Тег пуст — сверять не с чем, лишнюю установку не начинаем.
        self.assertFalse(updates._needs_update("", "", True))
        self.assertFalse(updates._needs_update("1.10.4", "", True))


class CheckAllTests(unittest.TestCase):
    """``check_all`` без сети и с новым релизом программы."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="bypass-tray-upd-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(config.set_config_dir, None)
        config.set_config_dir(Path(self._tmp.name))
        # Диск и ядро не трогаем: окружение теста не должно влиять на вывод.
        for name, value in (
            ("present_components", lambda cfg: {
                "zapret": False, "tgwsproxy": False, "xray": False,
                "refilter": False, "service": False}),
            ("_xray_version", lambda: ""),
            ("widget_version", lambda: "0.6.9"),
        ):
            patcher = mock.patch.object(updates, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_offline_gives_app_block_without_error(self) -> None:
        with mock.patch.object(updates, "_latest_tags", lambda: OFFLINE):
            report = updates.check_all({"zapret_dir": "", "tgws_exe": ""}, {})
        app = report.get("app") or {}
        self.assertIn("app", report)
        self.assertFalse(app.get("outdated"))
        self.assertTrue(app.get("note"))
        self.assertIn("не проверено", app["note"])
        self.assertEqual(app.get("installed"), "0.6.9")
        self.assertTrue(app.get("url"))

    def test_new_app_tag_marks_outdated_with_url(self) -> None:
        tags = {"app": "v0.7.0", "zapret": "", "tgwsproxy": "", "xray": ""}
        with mock.patch.object(updates, "_latest_tags", lambda: (tags, "")):
            report = updates.check_all({"zapret_dir": "", "tgws_exe": ""}, {})
        app = report["app"]
        self.assertTrue(app["outdated"])
        self.assertEqual(app["latest"], "v0.7.0")
        self.assertIn("github.com", app["url"])
        self.assertIn("/releases", app["url"])

    def test_same_version_is_not_outdated(self) -> None:
        tags = {"app": "v0.6.9", "zapret": "", "tgwsproxy": "", "xray": ""}
        with mock.patch.object(updates, "_latest_tags", lambda: (tags, "")):
            report = updates.check_all({"zapret_dir": "", "tgws_exe": ""}, {})
        self.assertFalse(report["app"]["outdated"])

    def test_present_component_without_version_is_outdated(self) -> None:
        tags = {"app": "", "zapret": "v1.10.4", "tgwsproxy": "", "xray": ""}
        with mock.patch.object(updates, "_latest_tags", lambda: (tags, "")), \
                mock.patch.object(updates, "present_components", lambda cfg: {
                    "zapret": True, "tgwsproxy": False, "xray": False,
                    "refilter": True, "service": False}):
            report = updates.check_all({"zapret_dir": "", "tgws_exe": ""}, {})
        self.assertIn("zapret", report["outdated"])

    def test_service_zapret_is_neither_outdated_nor_missing(self) -> None:
        tags = {"app": "", "zapret": "v1.10.4", "tgwsproxy": "", "xray": ""}
        with mock.patch.object(updates, "_latest_tags", lambda: (tags, "")), \
                mock.patch.object(updates, "present_components", lambda cfg: {
                    "zapret": True, "tgwsproxy": False, "xray": False,
                    "refilter": True, "service": True}):
            report = updates.check_all({"zapret_dir": "", "tgws_exe": ""}, {})
        self.assertNotIn("zapret", report["outdated"])
        self.assertNotIn("zapret", report["missing"])
        self.assertTrue(report["components"]["zapret"].get("service"))


class UpdateAllWritesVersionsTests(unittest.TestCase):
    """После установки версии обоих компонентов должны попасть в state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="bypass-tray-upd-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(config.set_config_dir, None)
        config.set_config_dir(Path(self._tmp.name))
        self.target = Path(self._tmp.name) / "zapret"
        self.target.mkdir()
        (self.target / "general.bat").write_text("@echo off", encoding="utf-8")

    def test_install_records_zapret_and_tgws_versions(self) -> None:
        tags = {"zapret": "v1.10.4", "tgwsproxy": "v1.11.2", "xray": "v26.10.29"}
        cfg = {"zapret_dir": str(self.target), "tgws_exe": ""}
        state: dict = {}
        with mock.patch.object(updates, "_latest_tags", lambda: (tags, "")), \
                mock.patch.object(updates.status, "service_image_path", lambda: ""), \
                mock.patch.object(updates, "present_components", lambda c: {
                    "zapret": False, "tgwsproxy": False, "xray": False,
                    "refilter": False, "service": False}), \
                mock.patch.object(updates.components, "install_zapret",
                                  lambda *a, **k: {"ok": True, "dir": str(self.target),
                                                   "version": "v1.10.4"}), \
                mock.patch.object(updates.components, "install_tgws",
                                  lambda *a, **k: {"ok": True, "version": "v1.11.2",
                                                   "exe": ""}), \
                mock.patch.object(updates.actions, "tgws_running", lambda: False), \
                mock.patch.object(updates.xray, "ensure_core", lambda force=False: Path("xray.exe")), \
                mock.patch.object(updates.xray, "is_running", lambda: False), \
                mock.patch.object(updates, "_xray_version", lambda: "26.10.29"), \
                mock.patch.object(updates.blocked, "update",
                                  lambda progress=None: {"ok": True, "size": 1}):
            result = updates.update_all(cfg, state)

        installed = state.get("installed") or {}
        self.assertEqual(installed.get("zapret"), "v1.10.4")
        self.assertEqual(installed.get("tgws"), "v1.11.2")
        self.assertTrue(result.get("ok"), str(result.get("errors")))
        self.assertIn("zapret", result.get("installed") or [])
        self.assertIn("tgwsproxy", result.get("installed") or [])

    def test_known_version_skips_install_and_keeps_state(self) -> None:
        tags = {"zapret": "v1.10.4", "tgwsproxy": "v1.11.2", "xray": "26.10.29"}
        cfg = {"zapret_dir": str(self.target), "tgws_exe": ""}
        state = {"installed": {"zapret": "v1.10.4", "tgws": "v1.11.2",
                               "xray": "26.10.29"}}
        calls: list[str] = []
        with mock.patch.object(updates, "_latest_tags", lambda: (tags, "")), \
                mock.patch.object(updates.status, "service_image_path", lambda: ""), \
                mock.patch.object(updates, "present_components", lambda c: {
                    "zapret": True, "tgwsproxy": True, "xray": True,
                    "refilter": True, "service": False}), \
                mock.patch.object(updates.components, "install_zapret",
                                  lambda *a, **k: calls.append("zapret")), \
                mock.patch.object(updates.components, "install_tgws",
                                  lambda *a, **k: calls.append("tgws")), \
                mock.patch.object(updates.xray, "ensure_core",
                                  lambda force=False: calls.append("xray")), \
                mock.patch.object(updates, "_xray_version", lambda: "26.10.29"), \
                mock.patch.object(updates.blocked, "update",
                                  lambda progress=None: {"ok": True, "size": 1}):
            result = updates.update_all(cfg, state)

        self.assertEqual(calls, [], str(calls))
        self.assertEqual(result.get("skipped"), ["zapret", "tgwsproxy", "xray"],
                         str(result.get("skipped")))
        self.assertEqual((state.get("installed") or {}).get("zapret"), "v1.10.4")


class UnusedComponentsGuard(unittest.TestCase):
    """``components`` импортирован для заморозки репозиториев — не мусор."""

    def test_repo_names_are_known(self) -> None:
        self.assertEqual(components.ZAPRET_REPO, updates.REPOS["zapret"])
        self.assertEqual(components.TGWS_REPO, updates.REPOS["tgwsproxy"])


if __name__ == "__main__":
    unittest.main()
