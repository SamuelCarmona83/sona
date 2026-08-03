"""Radio played-history keys must match fill dedup keys (no re-loop)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from src import radio


class PlayedKeyDedupTests(unittest.TestCase):
    def test_keys_include_yt_spotify_and_artist_title(self):
        track = {
            "title": "Deftones - Change (In the House of Flies)",
            "artist": "Deftones",
            "spotify_id": "abc123",
            "video_id": "KvknOXGPzCQ",
        }
        keys = radio.played_keys_for_track(track)
        self.assertIn("yt_KvknOXGPzCQ", keys)
        self.assertIn("sp:abc123", keys)
        self.assertIn("abc123", keys)
        self.assertTrue(any(k.startswith("at:") for k in keys))

    def test_record_played_blocks_spotify_id_dedup(self):
        """After play, fill must treat same spotify_id as already played."""
        gid = 424242
        radio._played_ids.pop(gid, None)
        track = {
            "title": "Basket Case",
            "artist": "Green Day",
            "spotify_id": "gd_basket",
            "video_id": "AJDiYxKSAqQ",
            "url": "https://example/stream1",
        }
        with patch.object(radio, "_save_played_ids"):
            # simulate record_played core without async genres
            radio._remember_played_keys(gid, track, persist=False)

        blocked = set(radio._played_ids.get(gid, []))
        # Same song, different YT url (common when re-searching)
        again = {
            "title": "Green Day - Basket Case Official",
            "artist": "Green Day",
            "spotify_id": "gd_basket",
            "video_id": "OTHERVIDEOID",
            "url": "https://example/stream2",
        }
        self.assertTrue(radio.track_is_recently_played(again, blocked))

        different = {
            "title": "American Idiot",
            "artist": "Green Day",
            "spotify_id": "gd_idiot",
            "video_id": "zzzz",
        }
        self.assertFalse(radio.track_is_recently_played(different, blocked))
        radio._played_ids.pop(gid, None)

    def test_artist_title_key_catches_same_song_without_spotify(self):
        a = {
            "title": "Zapato 3 - Vampiro",
            "artist": "Zapato 3",
            "video_id": "vid1",
        }
        b = {
            "title": "Zapato 3 - Vampiro (Official Video)",
            "artist": "Zapato 3",
            "video_id": "vid2",
        }
        keys_a = set(radio.played_keys_for_track(a))
        self.assertTrue(radio.track_is_recently_played(b, keys_a))


if __name__ == "__main__":
    unittest.main()
