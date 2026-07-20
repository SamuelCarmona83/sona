"""Tests for rock-radio cold fill."""

from __future__ import annotations

import collections
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src import fm_seed_radio


class ColdFillTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_fill_from_library(self) -> None:
        guild = MagicMock()
        guild.id = 55
        vc = MagicMock()
        vc.is_playing.return_value = False
        vc.is_paused.return_value = False
        channel = MagicMock()

        session = MagicMock()
        session.queue = collections.deque()
        session.now_playing = None

        local = [
            {
                "title": "Rock Song",
                "artist": "Band",
                "url": "/tmp/x.m4a",
                "local": True,
                "requester": "📻 Radio (local)",
            }
        ]

        fm_seed_radio._cold_fill_done.discard(55)
        with patch("src.radio.is_radio_active", return_value=True), patch(
            "src.radio.get_mood", return_value="rock-radio"
        ), patch(
            "src.playback.guild_session", return_value=session
        ), patch(
            "src.playback.queues", {}
        ), patch(
            "src.playback.play_next", new_callable=AsyncMock
        ) as play_next, patch(
            "src.library.get_radio_candidates", new_callable=AsyncMock, return_value=local
        ), patch.object(
            fm_seed_radio, "FM_SEED_COLD_FILL_COUNT", 2
        ), patch(
            "src.youtube.is_youtube_rate_limited", return_value=True
        ):
            n = await fm_seed_radio.cold_fill_stream_seed(guild, vc, channel)
            self.assertEqual(n, 1)
            self.assertEqual(len(session.queue), 1)
            self.assertTrue(session.queue[0].get("from_cold_start"))
            play_next.assert_awaited_once()
            # Second call in same session is a no-op
            n2 = await fm_seed_radio.cold_fill_stream_seed(guild, vc, channel)
            self.assertEqual(n2, 0)

    async def test_cold_fill_skips_when_not_stream_mood(self) -> None:
        guild = MagicMock()
        guild.id = 1
        with patch("src.radio.is_radio_active", return_value=True), patch(
            "src.radio.get_mood", return_value="rock"
        ):
            n = await fm_seed_radio.cold_fill_stream_seed(guild, None, None)
            self.assertEqual(n, 0)

    async def test_contingency_respects_cooldown(self) -> None:
        guild = MagicMock()
        guild.id = 77
        session = MagicMock()
        session.queue = collections.deque()
        session.now_playing = None
        fm_seed_radio._cold_fill_done.add(77)
        fm_seed_radio._last_contingency_at[77] = __import__("time").time()
        with patch("src.radio.is_radio_active", return_value=True), patch(
            "src.radio.get_mood", return_value="rock-radio"
        ), patch("src.playback.guild_session", return_value=session), patch.object(
            fm_seed_radio, "FM_SEED_CONTINGENCY_COOLDOWN_SEC", 60.0
        ), patch.object(
            fm_seed_radio, "RADIO_QUEUE_REFILL_THRESHOLD", 4
        ), patch(
            "src.fm_seed_radio._enqueue_genre_fallback_tracks",
            new_callable=AsyncMock,
        ) as enq:
            n = await fm_seed_radio.maybe_contingency_fill(guild, None, None)
            self.assertEqual(n, 0)
            enq.assert_not_called()


if __name__ == "__main__":
    unittest.main()
