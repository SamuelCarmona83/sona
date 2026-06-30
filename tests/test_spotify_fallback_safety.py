import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("BOT_TOKEN", "test-token")

from src import library, spotify  # noqa: E402


class SpotifyFallbackSafetyTests(unittest.TestCase):
    def test_spotify_fallback_marks_query_as_untrusted(self) -> None:
        original_sp = spotify.sp
        try:
            spotify.sp = None
            info = asyncio.run(spotify._get_spotify_track_info("bajan espineta"))
            self.assertFalse(info.get("spotify_refined"))
            self.assertIsNone(info.get("spotify_id"))
            self.assertEqual(info.get("query"), "bajan espineta")
        finally:
            spotify.sp = original_sp

    def test_spotify_url_track_info_is_trusted(self) -> None:
        info = spotify._track_to_info(
            {
                "id": "track123",
                "name": "Bajan",
                "artists": [{"id": "artist123", "name": "Spinetta"}],
            }
        )
        self.assertTrue(info.get("spotify_refined"))
        self.assertEqual(info.get("spotify_id"), "track123")

    def test_resolve_local_track_rejects_conflicting_cached_metadata(self) -> None:
        original_index = library._index
        original_save = library._save_index
        original_validation = library.LIBRARY_LOCAL_HIT_VALIDATION_ENABLED
        original_min_score = library.LIBRARY_LOCAL_HIT_MIN_SCORE
        original_enabled = library.LIBRARY_ENABLED

        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as fh:
            fh.write(b"audio")
            file_path = fh.name

        try:
            library._index = {
                "yt_Egj4PSgMAfA": {
                    "title": "Guaco - Baja",
                    "artist": "Guaco",
                    "yt_query": "guaco baja",
                    "video_id": "Egj4PSgMAfA",
                    "file_path": file_path,
                    "spotify_id": "spotify:track:guaco",
                    "artist_id": "spotify:artist:guaco",
                    "cover_url": "https://img/guaco.jpg",
                    "album": "Guaco Album",
                    "release_date": "2000-01-01",
                    "spotify_refined": True,
                }
            }
            library._save_index = lambda: None
            library.LIBRARY_ENABLED = True
            library.LIBRARY_LOCAL_HIT_VALIDATION_ENABLED = True
            library.LIBRARY_LOCAL_HIT_MIN_SCORE = 6.5

            track = {
                "title": "SPINETTA - BAJAN",
                "yt_query": "spinetta bajan",
                "video_id": "Egj4PSgMAfA",
                "spotify_refined": False,
            }

            resolved = library.resolve_local_track(track)
            self.assertIsNone(resolved)

            entry = library._index["yt_Egj4PSgMAfA"]
            self.assertFalse(entry.get("spotify_refined"))
            self.assertIsNone(entry.get("spotify_id"))
            self.assertIsNone(entry.get("artist_id"))
            self.assertIsNone(entry.get("cover_url"))
            self.assertIsNone(entry.get("album"))
        finally:
            library._index = original_index
            library._save_index = original_save
            library.LIBRARY_ENABLED = original_enabled
            library.LIBRARY_LOCAL_HIT_VALIDATION_ENABLED = original_validation
            library.LIBRARY_LOCAL_HIT_MIN_SCORE = original_min_score
            try:
                os.remove(file_path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
