"""Tests for free-space-aware library reclaim."""
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "test-token")

from src import library  # noqa: E402
from src import config as app_config  # noqa: E402


class LibraryDiskReclaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lib_dir = pathlib.Path(self._tmp.name) / "library"
        self.lib_dir.mkdir()
        self.covers_dir = self.lib_dir / "covers"
        self.covers_dir.mkdir()

        self._orig_index = library._index
        self._orig_lib = library._LIBRARY_DIR
        self._orig_covers = library._COVERS_DIR
        self._orig_save = library._save_index
        self._orig_max_mb = app_config.LIBRARY_MAX_MB
        self._orig_max_tracks = app_config.LIBRARY_MAX_TRACKS
        self._orig_min_free = app_config.LIBRARY_MIN_FREE_MB
        self._orig_target_free = app_config.LIBRARY_TARGET_FREE_MB
        self._orig_emergency = app_config.LIBRARY_EMERGENCY_EVICT_PINS
        self._orig_pin = app_config.LIBRARY_MIN_PLAYS_TO_PIN

        library._index = {}
        library._LIBRARY_DIR = self.lib_dir
        library._COVERS_DIR = self.covers_dir
        library._save_index = lambda: None

        # Small caps so tests stay light.
        app_config.LIBRARY_MAX_MB = 1  # 1 MiB
        app_config.LIBRARY_MAX_TRACKS = 100
        app_config.LIBRARY_MIN_FREE_MB = 100
        app_config.LIBRARY_TARGET_FREE_MB = 200
        app_config.LIBRARY_EMERGENCY_EVICT_PINS = True
        app_config.LIBRARY_MIN_PLAYS_TO_PIN = 3

        # library.py imported the constants by value — patch module attributes used in reclaim.
        library.LIBRARY_MAX_MB = app_config.LIBRARY_MAX_MB
        library.LIBRARY_MAX_TRACKS = app_config.LIBRARY_MAX_TRACKS
        library.LIBRARY_MIN_FREE_MB = app_config.LIBRARY_MIN_FREE_MB
        library.LIBRARY_TARGET_FREE_MB = app_config.LIBRARY_TARGET_FREE_MB
        library.LIBRARY_EMERGENCY_EVICT_PINS = app_config.LIBRARY_EMERGENCY_EVICT_PINS
        library.LIBRARY_MIN_PLAYS_TO_PIN = app_config.LIBRARY_MIN_PLAYS_TO_PIN

    def tearDown(self) -> None:
        library._index = self._orig_index
        library._LIBRARY_DIR = self._orig_lib
        library._COVERS_DIR = self._orig_covers
        library._save_index = self._orig_save
        app_config.LIBRARY_MAX_MB = self._orig_max_mb
        app_config.LIBRARY_MAX_TRACKS = self._orig_max_tracks
        app_config.LIBRARY_MIN_FREE_MB = self._orig_min_free
        app_config.LIBRARY_TARGET_FREE_MB = self._orig_target_free
        app_config.LIBRARY_EMERGENCY_EVICT_PINS = self._orig_emergency
        app_config.LIBRARY_MIN_PLAYS_TO_PIN = self._orig_pin
        library.LIBRARY_MAX_MB = self._orig_max_mb
        library.LIBRARY_MAX_TRACKS = self._orig_max_tracks
        library.LIBRARY_MIN_FREE_MB = self._orig_min_free
        library.LIBRARY_TARGET_FREE_MB = self._orig_target_free
        library.LIBRARY_EMERGENCY_EVICT_PINS = self._orig_emergency
        library.LIBRARY_MIN_PLAYS_TO_PIN = self._orig_pin
        self._tmp.cleanup()

    def _add_track(
        self,
        tid: str,
        *,
        size: int = 400_000,
        play_count: int = 0,
        last_played: float = 1.0,
        title: str | None = None,
    ) -> pathlib.Path:
        path = self.lib_dir / f"{tid}.m4a"
        path.write_bytes(b"x" * size)
        library._index[tid] = {
            "file_path": str(path),
            "title": title or tid,
            "play_count": play_count,
            "last_played": last_played,
            "cached_at": last_played,
        }
        return path

    def test_reclaim_evicts_lru_when_over_max_mb(self) -> None:
        old = self._add_track("yt_old", size=600_000, last_played=1.0, title="Old")
        new = self._add_track("yt_new", size=600_000, last_played=9.0, title="New")
        # ~1.2 MiB total > LIBRARY_MAX_MB=1; free space not the driver
        with patch.object(library, "disk_free_mb", return_value=10_000.0):
            stats = library.reclaim_disk(reason="test-caps")

        self.assertGreaterEqual(stats["evicted"], 1)
        self.assertFalse(old.is_file())
        self.assertNotIn("yt_old", library._index)
        self.assertTrue(new.is_file())
        self.assertIn("yt_new", library._index)

    def test_reclaim_under_free_pressure_evicts_until_target(self) -> None:
        a = self._add_track("yt_a", size=50_000, last_played=1.0)
        b = self._add_track("yt_b", size=50_000, last_played=2.0)
        # free climbs as we "evict" — simulate pressure then recovery
        free_values = [50.0, 50.0, 80.0, 250.0]  # after each call

        def free_side_effect(*_a, **_k):
            if free_values:
                return free_values.pop(0)
            return 250.0

        with patch.object(library, "disk_free_mb", side_effect=free_side_effect):
            # Raise max so size cap is not the reason
            library.LIBRARY_MAX_MB = 100
            stats = library.reclaim_disk(reason="test-free")

        self.assertGreaterEqual(stats["evicted"], 1)
        # At least the oldest should be gone
        self.assertFalse(a.is_file() and "yt_a" in library._index and b.is_file())

    def test_emergency_evicts_pins_when_enabled(self) -> None:
        pinned = self._add_track(
            "yt_pin", size=50_000, play_count=99, last_played=1.0, title="Pinned"
        )
        library.LIBRARY_MAX_MB = 100
        with patch.object(library, "disk_free_mb", return_value=10.0):
            library.LIBRARY_EMERGENCY_EVICT_PINS = True
            # free always low → needs victims; only pin available
            stats = library.reclaim_disk(reason="test-emergency")

        self.assertGreaterEqual(stats["emergency_pins"], 1)
        self.assertGreaterEqual(stats["evicted"], 1)
        self.assertFalse(pinned.is_file())
        self.assertNotIn("yt_pin", library._index)

    def test_emergency_off_keeps_pins(self) -> None:
        pinned = self._add_track(
            "yt_pin", size=50_000, play_count=99, last_played=1.0, title="Pinned"
        )
        library.LIBRARY_MAX_MB = 100
        library.LIBRARY_EMERGENCY_EVICT_PINS = False
        with patch.object(library, "disk_free_mb", return_value=10.0):
            stats = library.reclaim_disk(reason="test-no-emergency")

        self.assertEqual(stats["evicted"], 0)
        self.assertEqual(stats["emergency_pins"], 0)
        self.assertTrue(pinned.is_file())
        self.assertIn("yt_pin", library._index)

    def test_protect_tid_is_not_evicted(self) -> None:
        keep = self._add_track("yt_keep", size=600_000, last_played=1.0)
        other = self._add_track("yt_other", size=600_000, last_played=2.0)
        with patch.object(library, "disk_free_mb", return_value=10_000.0):
            library.reclaim_disk(reason="test-protect", protect_tid="yt_keep")

        self.assertTrue(keep.is_file())
        self.assertIn("yt_keep", library._index)
        self.assertFalse(other.is_file())
        self.assertNotIn("yt_other", library._index)

    def test_noop_when_under_caps_and_free_ok(self) -> None:
        path = self._add_track("yt_ok", size=10_000, last_played=1.0)
        with patch.object(library, "disk_free_mb", return_value=10_000.0):
            library.LIBRARY_MAX_MB = 100
            stats = library.reclaim_disk(reason="test-noop")
        self.assertEqual(stats["evicted"], 0)
        self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
