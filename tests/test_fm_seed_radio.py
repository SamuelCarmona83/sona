"""Unit tests for stream-seeded radio mood (rock-radio)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.fm_seed_radio import (
    ROCK_RADIO_STATION,
    STREAM_SEEDED_MOODS,
    get_stream_seed_station,
    is_stream_seeded_mood,
    stream_seed_label,
    stop_fm_seed_listener,
    _already_queued_or_playing,
    _count_seed_tracks_in_queue,
)


class StreamSeededMoodTests(unittest.TestCase):
    def test_rock_radio_station_hardcoded_ar(self) -> None:
        self.assertEqual(ROCK_RADIO_STATION["countrycode"], "AR")
        self.assertIn("ROCKANDPOP", ROCK_RADIO_STATION["url"].upper())
        self.assertIn("rock-radio", STREAM_SEEDED_MOODS)
        self.assertTrue(is_stream_seeded_mood("rock-radio"))
        self.assertTrue(is_stream_seeded_mood("ROCK-RADIO"))
        self.assertFalse(is_stream_seeded_mood("rock"))
        st = get_stream_seed_station("rock-radio")
        self.assertIsNotNone(st)
        assert st is not None
        self.assertEqual(st["stationuuid"], ROCK_RADIO_STATION["stationuuid"])
        self.assertIn("Rock", stream_seed_label("rock-radio"))

    def test_url_override(self) -> None:
        with patch("src.fm_seed_radio.FM_SEED_ROCK_STREAM_URL", "https://example.com/custom.aac"):
            st = get_stream_seed_station("rock-radio")
            assert st is not None
            self.assertEqual(st["url"], "https://example.com/custom.aac")
            self.assertEqual(st["url_resolved"], "https://example.com/custom.aac")


class DedupeHelpersTests(unittest.TestCase):
    def test_already_queued_by_fm_match_key(self) -> None:
        session = MagicMock()
        session.now_playing = None
        session.queue = [
            {"title": "Hello", "artist": "Adele", "fm_match_key": "adele|hello"},
        ]
        with patch("src.playback.guild_session", return_value=session):
            self.assertTrue(_already_queued_or_playing(1, "adele|hello", "Hello", "Adele"))
            self.assertFalse(_already_queued_or_playing(1, "queen|bohemian", "Bohemian", "Queen"))

    def test_count_seed_tracks(self) -> None:
        session = MagicMock()
        session.now_playing = {"from_fm_seed": True, "title": "A"}
        session.queue = [
            {"from_fm_seed": True},
            {"from_fm_seed": False},
            {"title": "user"},
        ]
        with patch("src.playback.guild_session", return_value=session):
            self.assertEqual(_count_seed_tracks_in_queue(1), 2)


class FillRadioSkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_fill_skips_spotify_for_stream_mood(self) -> None:
        import src.radio as radio

        guild = MagicMock()
        guild.id = 99
        vc = MagicMock()
        channel = MagicMock()

        with patch.object(radio, "get_mood", return_value="rock-radio"), patch.object(
            radio, "is_radio_active", return_value=True
        ), patch.object(radio, "_filling", {}), patch(
            "src.fm_seed_radio.ensure_fm_seed_listener", return_value=True
        ) as ensure, patch(
            "src.fm_seed_radio.cold_fill_stream_seed",
            new_callable=AsyncMock,
            return_value=2,
        ) as cold, patch(
            "src.spotify._get_recommendations_hybrid", new_callable=AsyncMock
        ) as recs:
            await radio.fill_radio_queue(guild, vc, channel, auto_play=False)
            ensure.assert_called_once()
            cold.assert_awaited_once()
            recs.assert_not_called()


class StopListenerTests(unittest.TestCase):
    def test_stop_is_idempotent(self) -> None:
        with patch("src.fm_seed_radio.stop_fm_recognizer") as stop_rec, patch(
            "src.fm_history.close_session"
        ):
            stop_fm_seed_listener(123)
            stop_fm_seed_listener(123)
            self.assertGreaterEqual(stop_rec.call_count, 1)


class StopFmRecognitionRespectsSeedTests(unittest.TestCase):
    def test_stop_fm_recognition_skips_when_seed_active(self) -> None:
        from src.playback import stop_fm_recognition

        with patch("src.fm_seed_radio.is_seed_listener_running", return_value=True), patch(
            "src.fm_recognizer.stop_fm_recognizer"
        ) as stop_rec, patch("src.fm_history.close_session") as close:
            stop_fm_recognition(99)
            stop_rec.assert_not_called()
            close.assert_not_called()

    def test_stop_fm_recognition_force_kills(self) -> None:
        from src.playback import stop_fm_recognition

        with patch("src.fm_seed_radio.is_seed_listener_running", return_value=True), patch(
            "src.fm_recognizer.stop_fm_recognizer"
        ) as stop_rec, patch("src.fm_history.close_session") as close:
            stop_fm_recognition(99, force=True)
            stop_rec.assert_called_once_with(99)
            close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
