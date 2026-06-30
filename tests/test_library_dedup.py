import os
import unittest

os.environ.setdefault("BOT_TOKEN", "test-token")

from src import library  # noqa: E402


class LibraryDedupTests(unittest.TestCase):
    def test_upsert_uses_video_id_canonical_key_when_track_lacks_video_id(self) -> None:
        original_index = library._index
        original_save = library._save_index
        original_get_local_path = library.get_local_path

        try:
            library._index = {
                "spotify:track:abc": {
                    "spotify_id": "spotify:track:abc",
                    "title": "Song",
                    "play_count": 1,
                    "request_count": 1,
                    "cached_at": 100.0,
                }
            }
            library._save_index = lambda: None
            library.get_local_path = lambda tid: None

            video_id = "abcdefghijk"
            library._upsert_entry_from_track(
                {
                    "spotify_id": "spotify:track:abc",
                    "title": "Song",
                    "artist": "Artist",
                    "yt_query": "artist song",
                },
                file_path="/tmp/song.m4a",
                video_id=video_id,
            )

            canonical_tid = f"yt_{video_id}"
            self.assertIn(canonical_tid, library._index)
            self.assertNotIn("spotify:track:abc", library._index)
            self.assertEqual(library._index[canonical_tid]["video_id"], video_id)
            self.assertEqual(library._index[canonical_tid]["file_path"], "/tmp/song.m4a")
        finally:
            library._index = original_index
            library._save_index = original_save
            library.get_local_path = original_get_local_path


if __name__ == "__main__":
    unittest.main()
