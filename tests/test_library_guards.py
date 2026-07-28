"""Tests for library cache reject guards and safe per-track delete."""
import os
import pathlib
import tempfile
import unittest

os.environ.setdefault("BOT_TOKEN", "test-token")

from src import library  # noqa: E402
from src.config import LIBRARY_MAX_DURATION_SEC, LIBRARY_MAX_FILE_MB  # noqa: E402


class CacheRejectReasonTests(unittest.TestCase):
    def test_rejects_classic_rock_radio_24_7_title(self) -> None:
        reason = library.cache_reject_reason({
            "title": (
                "Classic Rock Radio 🔴️ 24/7 Nonstop Classic Hits | "
                "Van Halen, Fleetwood Mac, Led Zeppelin and More"
            ),
            "duration": 0,
        })
        self.assertIsNotNone(reason)

    def test_rejects_duration_zero(self) -> None:
        reason = library.cache_reject_reason({
            "title": "Some Mystery Upload",
            "duration": 0,
        })
        self.assertEqual(reason, "duration_zero_or_unknown")

    def test_rejects_long_duration(self) -> None:
        reason = library.cache_reject_reason({
            "title": "Full Album Dump",
            "duration": LIBRARY_MAX_DURATION_SEC + 1,
        })
        self.assertIsNotNone(reason)
        self.assertIn("duration>", reason)

    def test_rejects_large_filesize_approx(self) -> None:
        reason = library.cache_reject_reason({
            "title": "Normal Song Title",
            "duration": 200,
            "filesize_approx": (LIBRARY_MAX_FILE_MB + 1) * 1024 * 1024,
        })
        self.assertIsNotNone(reason)
        self.assertIn("filesize>", reason)

    def test_rejects_mp4_path(self) -> None:
        reason = library.cache_reject_reason({
            "title": "Something",
            "duration": 200,
            "file_path": "/tmp/yt_abc.mp4",
        })
        self.assertEqual(reason, "non_audio_ext=.mp4")

    def test_rejects_is_live(self) -> None:
        reason = library.cache_reject_reason({
            "title": "Band - Song (Official Audio)",
            "duration": 210,
            "is_live": True,
        })
        self.assertEqual(reason, "is_live")

    def test_accepts_normal_single(self) -> None:
        reason = library.cache_reject_reason({
            "title": "Daft Punk - Giorgio by Moroder (Official Audio)",
            "duration": 554,
            "file_size_bytes": 9 * 1024 * 1024,
            "file_path": "/cache/library/yt_zhl.m4a",
        })
        self.assertIsNone(reason)

    def test_accepts_live_concert_title_when_duration_ok(self) -> None:
        # Concert "live" is not the same as 24/7 radio stream patterns.
        reason = library.cache_reject_reason({
            "title": "Artist - Song (Live at Wembley)",
            "duration": 240,
            "file_path": "/cache/library/yt_live.m4a",
        })
        self.assertIsNone(reason)

    def test_accepts_song_with_radio_in_title(self) -> None:
        reason = library.cache_reject_reason({
            "title": "The Buggles - Video Killed the Radio Star",
            "duration": 254,
            "file_path": "/cache/library/yt_radio_star.m4a",
        })
        self.assertIsNone(reason)


class DeleteTrackEntryTests(unittest.TestCase):
    def test_deletes_index_entry_and_file_under_library_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            lib = root / "library"
            lib.mkdir()
            tid = "yt_testdelete1"
            audio = lib / f"{tid}.m4a"
            audio.write_bytes(b"fake-audio-bytes-12345")
            size = audio.stat().st_size

            index = {
                tid: {
                    "title": "Delete Me",
                    "file_path": str(audio),
                    "play_count": 1,
                }
            }
            result = library.delete_track_entry(index, tid, library_dir=lib)

            self.assertTrue(result["deleted"])
            self.assertEqual(result["bytes_freed"], size)
            self.assertNotIn(tid, index)
            self.assertFalse(audio.exists())
            self.assertTrue(any(tid in p for p in result["files_removed"]))

    def test_refuses_path_outside_library_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            lib = root / "library"
            lib.mkdir()
            outside = root / "outside.m4a"
            outside.write_bytes(b"secret")
            tid = "yt_evil"
            index = {
                tid: {
                    "title": "Evil",
                    "file_path": str(outside),
                }
            }
            result = library.delete_track_entry(index, tid, library_dir=lib)
            self.assertTrue(result["deleted"])  # index entry removed
            self.assertTrue(outside.exists())  # file outside library untouched
            self.assertEqual(result["bytes_freed"], 0)

    def test_removes_orphan_globs_even_without_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = pathlib.Path(tmp) / "library"
            lib.mkdir()
            tid = "yt_orphan"
            orphan = lib / f"{tid}.mp4"
            orphan.write_bytes(b"x" * 100)
            index: dict = {}
            result = library.delete_track_entry(index, tid, library_dir=lib)
            self.assertTrue(result["deleted"])
            self.assertFalse(orphan.exists())
            self.assertEqual(result["bytes_freed"], 100)


if __name__ == "__main__":
    unittest.main()
