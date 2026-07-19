"""Unit tests for FM stream song recognition (shazamio sidecar)."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.fm_recognizer import (
    match_key,
    parse_shazam_response,
    start_fm_recognizer,
    stop_fm_recognizer,
    is_running,
)


class ParseShazamResponseTests(unittest.TestCase):
    def test_full_track_payload(self) -> None:
        payload = {
            "matches": [{"offset": 0, "id": "x"}],
            "track": {
                "title": "Bohemian Rhapsody",
                "subtitle": "Queen",
                "url": "https://www.shazam.com/track/123",
                "images": {
                    "coverart": "https://example.com/cover.jpg",
                    "coverarthq": "https://example.com/cover_hq.jpg",
                },
                "sections": [
                    {
                        "type": "SONG",
                        "metadata": [
                            {"title": "Album", "text": "A Night at the Opera"},
                            {"title": "Label", "text": "EMI"},
                        ],
                    }
                ],
            },
        }
        match = parse_shazam_response(payload)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["title"], "Bohemian Rhapsody")
        self.assertEqual(match["artist"], "Queen")
        self.assertEqual(match["album"], "A Night at the Opera")
        self.assertEqual(match["cover_url"], "https://example.com/cover_hq.jpg")
        self.assertEqual(match["shazam_url"], "https://www.shazam.com/track/123")
        self.assertIn("recognized_at", match)

    def test_empty_or_no_track(self) -> None:
        self.assertIsNone(parse_shazam_response(None))
        self.assertIsNone(parse_shazam_response({}))
        self.assertIsNone(parse_shazam_response({"matches": []}))
        self.assertIsNone(parse_shazam_response({"track": {"title": ""}}))

    def test_artist_fallback_field(self) -> None:
        match = parse_shazam_response({"track": {"title": "Song", "artist": "Someone"}})
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["artist"], "Someone")


class MatchKeyTests(unittest.TestCase):
    def test_normalize_case_and_spaces(self) -> None:
        self.assertEqual(
            match_key("  Queen  ", "Bohemian   Rhapsody"),
            match_key("queen", "bohemian rhapsody"),
        )

    def test_different_tracks(self) -> None:
        self.assertNotEqual(
            match_key("Queen", "Don't Stop Me Now"),
            match_key("Queen", "Bohemian Rhapsody"),
        )


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        stop_fm_recognizer(42)
        await asyncio.sleep(0)

    async def test_start_stop_task(self) -> None:
        on_match = AsyncMock()
        active = MagicMock(return_value=True)

        with patch("src.fm_recognizer.FM_RECOGNIZER_ENABLED", True), patch(
            "src.fm_recognizer.sample_stream",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "src.fm_recognizer.recognize_clip",
            new_callable=AsyncMock,
            return_value=None,
        ):
            start_fm_recognizer(
                42,
                "http://example.com/stream.mp3",
                on_match=on_match,
                is_active=active,
            )
            self.assertTrue(is_running(42))
            stop_fm_recognizer(42)
            await asyncio.sleep(0.05)
            self.assertFalse(is_running(42))

    async def test_disabled_does_not_start(self) -> None:
        with patch("src.fm_recognizer.FM_RECOGNIZER_ENABLED", False):
            start_fm_recognizer(
                99,
                "http://example.com/stream.mp3",
                on_match=AsyncMock(),
                is_active=lambda: True,
            )
            self.assertFalse(is_running(99))

    async def test_on_match_called_for_new_song(self) -> None:
        on_match = MagicMock()
        match_seen = asyncio.Event()

        async def _on_match(gid: int, payload: dict) -> None:
            on_match(gid, payload)
            match_seen.set()

        active = MagicMock(return_value=True)

        match = {
            "title": "Song A",
            "artist": "Artist A",
            "album": None,
            "cover_url": None,
            "shazam_url": None,
            "recognized_at": 1.0,
        }

        real_sleep = asyncio.sleep

        async def fast_sleep(_delay=0, *_a, **_k):
            await real_sleep(0)

        with patch("src.fm_recognizer.FM_RECOGNIZER_ENABLED", True), patch(
            "src.fm_recognizer.FM_RECOGNIZER_ANNOUNCE", False
        ), patch(
            "src.fm_recognizer.sample_stream",
            new_callable=AsyncMock,
            return_value="/tmp/fake.wav",
        ), patch(
            "src.fm_recognizer.recognize_clip",
            new_callable=AsyncMock,
            return_value=match,
        ), patch(
            "src.fm_recognizer._safe_unlink",
        ), patch(
            "asyncio.sleep",
            side_effect=fast_sleep,
        ):
            start_fm_recognizer(
                7,
                "http://example.com/live.mp3",
                on_match=_on_match,
                is_active=active,
            )
            await asyncio.wait_for(match_seen.wait(), timeout=2.0)
            stop_fm_recognizer(7)
            await real_sleep(0)
            self.assertGreaterEqual(on_match.call_count, 1)
            args = on_match.call_args
            self.assertEqual(args.args[0], 7)
            self.assertEqual(args.args[1]["title"], "Song A")


if __name__ == "__main__":
    unittest.main()
