"""Sticky vibe from agent genre plans + mood-locked seed behaviour."""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from src import radio


class StickyVibeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gid = 991001
        radio._sticky_vibe.pop(self.gid, None)
        radio._radio_mood.pop(self.gid, None)
        with patch.object(radio, "_save_sticky_vibe"):
            pass

    def tearDown(self) -> None:
        radio._sticky_vibe.pop(self.gid, None)
        radio._radio_mood.pop(self.gid, None)

    def test_last_write_moves_old_to_secondary(self):
        with patch.object(radio, "_save_sticky_vibe"):
            radio.set_sticky_vibe(
                self.gid, genres=["nu-metal"], hints=["Limp Bizkit"], user_id=1
            )
            radio.set_sticky_vibe(
                self.gid, genres=["reggaeton"], hints=["Bad Bunny"], user_id=2
            )
        state = radio.get_sticky_vibe(self.gid)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertIn("reggaeton", state["primary"]["genres"])
        self.assertIsNotNone(state["secondary"])
        self.assertIn("nu-metal", state["secondary"]["genres"])

    def test_same_vibe_renews_without_secondary_flip(self):
        with patch.object(radio, "_save_sticky_vibe"):
            radio.set_sticky_vibe(self.gid, genres=["nu-metal"], hints=["Korn"])
            first_exp = radio._sticky_vibe[self.gid]["primary"]["expires_at"]
            time.sleep(0.01)
            radio.set_sticky_vibe(self.gid, genres=["nu-metal"], hints=["Korn"])
        state = radio.get_sticky_vibe(self.gid)
        assert state is not None
        self.assertGreaterEqual(state["primary"]["expires_at"], first_exp)
        # Secondary may be None if same key
        sec = state.get("secondary")
        if sec:
            self.assertNotEqual(sec.get("key"), state["primary"].get("key"))

    def test_expiry(self):
        with patch.object(radio, "_save_sticky_vibe"):
            radio.set_sticky_vibe(self.gid, genres=["rock"], hints=[])
            radio._sticky_vibe[self.gid]["primary"]["expires_at"] = time.time() - 10
        self.assertIsNone(radio.get_sticky_vibe(self.gid))

    def test_mood_locked_ignores_sticky_in_seeds(self):
        with patch.object(radio, "_save_sticky_vibe"):
            radio.set_sticky_vibe(
                self.gid, genres=["reggaeton"], hints=["latin"]
            )
        radio.set_mood(self.gid, "metal")
        self.assertTrue(radio.is_mood_locked(self.gid))
        _tracks, genres = radio._build_diversity_seeds(self.gid)
        # Should prefer metal tags, not sticky reggaeton as sole source
        joined = " ".join(genres).lower()
        self.assertTrue(
            any(t in joined for t in ("metal", "thrash", "heavy")) or not genres
            or "metal" in joined
        )
        self.assertNotIn("reggaeton", joined)

    def test_neutral_uses_sticky_not_global_fallback(self):
        with patch.object(radio, "_save_sticky_vibe"):
            radio.set_sticky_vibe(
                self.gid, genres=["nu-metal"], hints=["Limp Bizkit"]
            )
        radio._radio_mood[self.gid] = "neutral"
        radio._play_history.pop(self.gid, None)
        _tracks, genres = radio._build_diversity_seeds(self.gid)
        joined = " ".join(genres).lower()
        self.assertTrue("nu-metal" in joined or "metal" in joined or "limp" in joined)
        # Must not be pure global fallback set
        self.assertNotEqual(set(genres), set(radio._FALLBACK_GENRES[: len(genres)]))

class StickyVibeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_yt_fallback_no_global_pad_with_sticky(self):
        gid = 991003
        radio._sticky_vibe.pop(gid, None)
        with patch.object(radio, "_save_sticky_vibe"):
            radio.set_sticky_vibe(gid, genres=["nu-metal"], hints=["Korn"])
        radio._radio_mood[gid] = "neutral"
        radio._play_history.pop(gid, None)

        from unittest.mock import AsyncMock

        with patch("src.youtube.search_youtube", new=AsyncMock(return_value=None)):
            out = await radio._youtube_fallback_fill(gid, 3)
        self.assertEqual(out, [])
        radio._sticky_vibe.pop(gid, None)


class MoodLockHelpersTests(unittest.TestCase):
    def test_nu_metal_mood_cluster(self):
        gid = 991002
        radio.set_mood(gid, "nu-metal")
        self.assertTrue(radio.is_mood_locked(gid))
        self.assertEqual(radio.mood_target_cluster(gid), "metal")
        radio._radio_mood.pop(gid, None)


if __name__ == "__main__":
    unittest.main()
