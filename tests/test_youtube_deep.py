"""Глубокая проверка YouTube: watch-страница, playability и поток.

Сеть не используется: ``_fetch_page``/``_fetch_range`` подменяются.
Запуск: ``python -m unittest discover -s tests -p "test_*.py"``.
"""

from __future__ import annotations

import gzip
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import health  # noqa: E402

WATCH_OK = (
    '... "playabilityStatus":{"status":"OK","reason":""} ... '
    '"url":"https:\\/\\/rr1---sn-abc.googlevideo.com\\/videoplayback'
    '?expire=1\\u0026sig=abc" ...'
)


def _http(ok: bool = True, ms: int = 20) -> dict:
    return {"ok": ok, "status": 200 if ok else 0, "ms": ms, "error": ""}


class YouTubeVideoProbeTests(unittest.TestCase):
    """Вердикт ``_youtube_video`` по странице, playability и потоку."""

    def probe(self, *, body: str = WATCH_OK, status: int = 200,
              stream: dict | None = None) -> dict:
        page = {"status": status, "body": body, "ms": 30,
                "truncated": False, "error": "" if status else "TimeoutError"}
        with mock.patch.object(health, "_fetch_page", return_value=page), \
                mock.patch.object(health, "_fetch_range",
                                  return_value=stream or {}):
            return health._youtube_video(None, 3.0)

    def test_watch_ok_and_stream_206(self) -> None:
        result = self.probe(stream={
            "status": 206, "bytes": health.VIDEO_MIN_BYTES, "ms": 50,
            "expired": False, "error": ""})
        self.assertTrue(result["ok"])
        self.assertTrue(result["stream"])
        self.assertEqual(result["status"], 206)

    def test_playability_ok_stream_403_is_not_failure(self) -> None:
        result = self.probe(stream={
            "status": 403, "bytes": 0, "ms": 40,
            "expired": False, "error": ""})
        self.assertTrue(result["ok"])
        self.assertTrue(result["signature"])
        self.assertFalse(result["stream"])

    def test_stream_timeout_is_failure(self) -> None:
        result = self.probe(stream={
            "status": 0, "bytes": 0, "ms": 40,
            "expired": False, "error": "TimeoutError"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["checked"])

    def test_playability_error_is_failure(self) -> None:
        body = ('... "playabilityStatus":{"status":"ERROR",'
                '"reason":"Video unavailable"} ...')
        result = self.probe(body=body)
        self.assertFalse(result["ok"])
        self.assertTrue(result["checked"])
        self.assertEqual(result["playability"], "ERROR")

    def test_watch_500_is_failure(self) -> None:
        result = self.probe(status=500)
        self.assertFalse(result["ok"])
        self.assertTrue(result["checked"])
        self.assertFalse(result["page"])

    def test_watch_timeout_is_failure(self) -> None:
        result = self.probe(status=0)
        self.assertFalse(result["ok"])
        self.assertTrue(result["checked"])

    def test_watch_429_is_warn_not_bad(self) -> None:
        result = self.probe(status=429)
        self.assertFalse(result["ok"])
        self.assertTrue(result["warn"])
        self.assertIn("429", result["reason"])

    def test_watch_404_is_warn_not_bad(self) -> None:
        result = self.probe(status=404)
        self.assertTrue(result["warn"])
        self.assertIn("404", result["reason"])

    def test_page_retry_after_disconnect(self) -> None:
        pages = [
            {"status": 0, "body": "", "ms": 5, "truncated": False,
             "error": "TimeoutError"},
            {"status": 200, "body": WATCH_OK, "ms": 20,
             "truncated": False, "error": ""},
        ]
        with mock.patch.object(health, "_fetch_page", side_effect=pages) as page, \
                mock.patch.object(health, "_fetch_range", return_value={
                    "status": 403, "bytes": 0, "ms": 30,
                    "expired": False, "error": ""}):
            result = health._youtube_video(None, 3.0)
        self.assertEqual(page.call_count, 2)
        self.assertTrue(result["ok"])
        self.assertTrue(result["signature"])

    def test_playability_ok_without_stream_url_is_ok(self) -> None:
        body = '... "playabilityStatus":{"status":"OK"} ...'
        result = self.probe(body=body)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["stream"])

    def test_nothing_found_is_unchecked(self) -> None:
        result = self.probe(body="<html>ничего</html>")
        self.assertFalse(result["ok"])
        self.assertFalse(result["checked"])


class YouTubeStatusTests(unittest.TestCase):
    """``_check_youtube``: страница + вердикт видео без ложного warn."""

    def check(self, *, page: bool, video: dict) -> dict:
        with mock.patch.object(health, "_http_retry",
                               return_value=_http(page)), \
                mock.patch.object(health, "_youtube_video",
                                  return_value=video), \
                mock.patch.object(health, "_stream_context",
                                  return_value=(False, None)):
            return health._check_youtube({}, {}, 3.0)

    def test_page_and_video_ok(self) -> None:
        item = self.check(page=True, video={
            "ok": True, "checked": True, "stream": True, "ms": 120})
        self.assertEqual(item["state"], "ok")
        self.assertIn("страница и видео", item["detail"])

    def test_page_and_player_ok_without_stream(self) -> None:
        item = self.check(page=True, video={
            "ok": True, "checked": True, "stream": False,
            "signature": True, "ms": 90})
        self.assertEqual(item["state"], "ok")
        self.assertIn("плеер ок", item["detail"])

    def test_page_ok_video_missing(self) -> None:
        item = self.check(page=True, video={
            "ok": False, "checked": True, "stream": False, "ms": 90,
            "error": "timeout"})
        self.assertEqual(item["state"], "bad")
        self.assertIn("видео не идёт", item["detail"])

    def test_page_ok_video_not_checked(self) -> None:
        item = self.check(page=True, video={
            "ok": False, "checked": False, "ms": 40,
            "error": "playability и поток не найдены"})
        self.assertEqual(item["state"], "warn")
        self.assertIn("видео не проверено", item["detail"])

    def test_watch_4xx_is_warn_not_bad(self) -> None:
        item = self.check(page=True, video={
            "ok": False, "checked": True, "warn": True, "status": 429,
            "reason": "страница недоступна (429)", "ms": 30})
        self.assertEqual(item["state"], "warn")
        self.assertIn("429", item["detail"])

    def test_page_broken(self) -> None:
        item = self.check(page=False, video={
            "ok": False, "checked": False, "ms": 0, "error": "timeout"})
        self.assertEqual(item["state"], "bad")


class GzipPageTests(unittest.TestCase):
    """``_fetch_page``: gzip сжимает трафик, но не ломает разбор."""

    def fetch(self, raw: bytes, encoding: str) -> tuple[dict, object]:
        class Response:
            status = 200

            def __init__(self) -> None:
                self.data = raw
                self.headers = {"Content-Encoding": encoding}

            def read(self, size: int) -> bytes:
                chunk, self.data = self.data[:size], self.data[size:]
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Opener:
            def __init__(self) -> None:
                self.request = None

            def open(self, request, timeout=None):
                self.request = request
                return Response()

        opener = Opener()
        with mock.patch.object(health.urllib.request, "build_opener",
                               return_value=opener):
            page = health._fetch_page("https://www.youtube.com/watch", None,
                                      3.0, health.WATCH_BODY_LIMIT)
        return page, opener.request

    def test_gzip_body_is_decompressed(self) -> None:
        page, request = self.fetch(gzip.compress(WATCH_OK.encode()), "gzip")
        self.assertEqual(page["status"], 200)
        self.assertEqual(health._playability(page["body"])[0], "OK")
        self.assertIn("googlevideo.com", health._video_url(page["body"]))
        self.assertEqual(request.get_header("Accept-encoding"), "gzip")

    def test_plain_body_still_works(self) -> None:
        page, _request = self.fetch(WATCH_OK.encode(), "")
        self.assertEqual(health._playability(page["body"])[0], "OK")


class YouTubeParsingTests(unittest.TestCase):
    def test_playability_order_and_reason(self) -> None:
        body = ('"playabilityStatus":{"reason":"No video","status":"UNPLAYABLE"}')
        self.assertEqual(health._playability(body), ("UNPLAYABLE", "No video"))

    def test_video_url_unescaped(self) -> None:
        url = health._video_url(WATCH_OK)
        self.assertTrue(url.startswith("https://"))
        self.assertIn("googlevideo.com/videoplayback", url)
        self.assertIn("&sig=abc", url)
        self.assertEqual(health._video_url("<html></html>"), "")


if __name__ == "__main__":
    unittest.main()
