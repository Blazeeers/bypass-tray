"""Глубокая проверка YouTube: страница отдельно, реальное видео отдельно.

Сеть не используется: HTTP-примитивы и видео-проба подменяются.
Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import health  # noqa: E402


def _http(ok: bool = True, ms: int = 20) -> dict:
    return {"ok": ok, "status": 200 if ok else 0, "ms": ms, "error": ""}


class YouTubeStatusTests(unittest.TestCase):
    def check(self, *, page: bool, video: dict, cdn: bool = True) -> dict:
        def fake_retry(url, *args, **kwargs):
            return _http(cdn if "ytimg" in url else page)

        with mock.patch.object(health, "_http_retry", side_effect=fake_retry), \
                mock.patch.object(health, "_youtube_video", return_value=video):
            return health._check_youtube({}, {}, 3.0)

    def test_page_and_video_ok(self) -> None:
        item = self.check(page=True, video={
            "ok": True, "checked": True, "ms": 120, "error": ""})
        self.assertEqual(item["state"], "ok")
        self.assertIn("страница и видео", item["detail"])

    def test_page_ok_video_missing(self) -> None:
        # Сорванное видео — явный bad: только он даёт право health-триггеру
        # уводить маршрут через VPN.
        item = self.check(page=True, video={
            "ok": False, "checked": True, "ms": 90, "error": "timeout"})
        self.assertEqual(item["state"], "bad")
        self.assertIn("видео нет", item["detail"])

    def test_page_ok_video_not_checked(self) -> None:
        item = self.check(page=True, cdn=True, video={
            "ok": False, "checked": False, "ms": 40, "error": "HTTP 403"})
        self.assertEqual(item["state"], "warn")
        self.assertIn("видео не проверено", item["detail"])

    def test_page_ok_video_not_checked_cdn_broken(self) -> None:
        # Даже при мёртвом CDN без видео-пробы это неопределённость (warn),
        # а не доказанный сбой.
        item = self.check(page=True, cdn=False, video={
            "ok": False, "checked": False, "ms": 40, "error": "HTTP 403"})
        self.assertEqual(item["state"], "warn")
        self.assertIn("видео не проверено", item["detail"])

    def test_page_broken(self) -> None:
        item = self.check(page=False, video={
            "ok": False, "checked": False, "ms": 0, "error": "timeout"})
        self.assertEqual(item["state"], "bad")

    def test_player_url_extraction(self) -> None:
        payload = {"streamingData": {
            "formats": [{"url": "https://rr1---sn.example.googlevideo.com/videoplayback?x=1"}],
            "adaptiveFormats": [{"url": "https://example.invalid/"}],
        }}
        url = health._player_stream_url(payload)
        self.assertIn("googlevideo.com", url)
        self.assertEqual(health._player_stream_url({"streamingData": {}}), "")


if __name__ == "__main__":
    unittest.main()
