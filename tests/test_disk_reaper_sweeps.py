"""Tests for library/dj_audio disk sweeps used by the background reaper."""
import os
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "test-token")

from src import dj_announcer  # noqa: E402
from src import library  # noqa: E402


class SweepTempsAndOrphansTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lib_dir = pathlib.Path(self._tmp.name) / "library"
        self.lib_dir.mkdir()
        self.covers = self.lib_dir / "covers"
        self.covers.mkdir()

        self._orig_index = library._index
        self._orig_lib = library._LIBRARY_DIR
        self._orig_covers = library._COVERS_DIR
        self._orig_save = library._save_index
        self._orig_pending = set(library._pending_downloads)

        library._index = {}
        library._LIBRARY_DIR = self.lib_dir
        library._COVERS_DIR = self.covers
        library._save_index = lambda: None
        library._pending_downloads.clear()

    def tearDown(self) -> None:
        library._index = self._orig_index
        library._LIBRARY_DIR = self._orig_lib
        library._COVERS_DIR = self._orig_covers
        library._save_index = self._orig_save
        library._pending_downloads.clear()
        library._pending_downloads.update(self._orig_pending)
        self._tmp.cleanup()

    def test_sweep_temps_removes_part_and_temp(self) -> None:
        part = self.lib_dir / "yt_abc.m4a.part"
        temp = self.lib_dir / "yt_abc.temp.m4a"
        keep = self.lib_dir / "yt_abc.m4a"
        part.write_bytes(b"partial")
        temp.write_bytes(b"tempdata")
        keep.write_bytes(b"good")
        # Age-gate: only abandoned temps (not in-flight / not brand-new).
        old = time.time() - 10_000
        os.utime(part, (old, old))
        os.utime(temp, (old, old))

        stats = library.sweep_library_temps()
        self.assertEqual(stats["removed"], 2)
        self.assertFalse(part.exists())
        self.assertFalse(temp.exists())
        self.assertTrue(keep.exists())

    def test_sweep_temps_skips_pending_and_young(self) -> None:
        pending_temp = self.lib_dir / "yt_live.temp.m4a"
        young_temp = self.lib_dir / "yt_fresh.temp.m4a"
        old_temp = self.lib_dir / "yt_old.temp.m4a"
        pending_temp.write_bytes(b"downloading")
        young_temp.write_bytes(b"just-started")
        old_temp.write_bytes(b"abandoned")
        old = time.time() - 10_000
        os.utime(old_temp, (old, old))
        # Even if mtime is old, pending must win.
        os.utime(pending_temp, (old, old))
        library._pending_downloads.add("yt_live")

        stats = library.sweep_library_temps(min_age_sec=300)
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(stats["skipped_pending"], 1)
        self.assertGreaterEqual(stats["skipped_young"], 1)
        self.assertTrue(pending_temp.exists())
        self.assertTrue(young_temp.exists())
        self.assertFalse(old_temp.exists())

    def test_sweep_orphan_index_drops_missing_files(self) -> None:
        missing = self.lib_dir / "yt_gone.m4a"
        library._index["yt_gone"] = {"file_path": str(missing), "title": "Gone"}
        alive = self.lib_dir / "yt_live.m4a"
        alive.write_bytes(b"x")
        library._index["yt_live"] = {"file_path": str(alive), "title": "Live"}

        stats = library.sweep_orphan_index_entries()
        self.assertEqual(stats["removed"], 1)
        self.assertNotIn("yt_gone", library._index)
        self.assertIn("yt_live", library._index)

    def test_sweep_orphan_files_respects_age_and_index(self) -> None:
        indexed = self.lib_dir / "yt_keep.m4a"
        indexed.write_bytes(b"keep")
        library._index["yt_keep"] = {"file_path": str(indexed), "title": "Keep"}

        orphan_old = self.lib_dir / "yt_orphan.m4a"
        orphan_old.write_bytes(b"orphan")
        old_mtime = time.time() - 10_000
        os.utime(orphan_old, (old_mtime, old_mtime))

        orphan_new = self.lib_dir / "yt_fresh.m4a"
        orphan_new.write_bytes(b"fresh")

        stats = library.sweep_orphan_files(min_age_sec=60)
        self.assertEqual(stats["removed"], 1)
        self.assertFalse(orphan_old.exists())
        self.assertTrue(orphan_new.exists())
        self.assertTrue(indexed.exists())

    def test_sweep_orphan_skips_pending_downloads(self) -> None:
        pending = self.lib_dir / "yt_pending.m4a"
        pending.write_bytes(b"downloading")
        old_mtime = time.time() - 10_000
        os.utime(pending, (old_mtime, old_mtime))
        library._pending_downloads.add("yt_pending")

        stats = library.sweep_orphan_files(min_age_sec=60)
        self.assertEqual(stats["removed"], 0)
        self.assertTrue(pending.exists())

    def test_sweep_orphan_index_skips_pending(self) -> None:
        missing = self.lib_dir / "yt_pending.m4a"
        library._index["yt_pending"] = {"file_path": str(missing), "title": "Pending"}
        library._pending_downloads.add("yt_pending")
        other_missing = self.lib_dir / "yt_gone.m4a"
        library._index["yt_gone"] = {"file_path": str(other_missing), "title": "Gone"}

        stats = library.sweep_orphan_index_entries()
        self.assertEqual(stats["removed"], 1)
        self.assertIn("yt_pending", library._index)
        self.assertNotIn("yt_gone", library._index)


class SweepDjAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dj_dir = pathlib.Path(self._tmp.name) / "dj_audio"
        self.dj_dir.mkdir()
        self._orig = dj_announcer._DJ_CACHE_DIR
        dj_announcer._DJ_CACHE_DIR = self.dj_dir

    def tearDown(self) -> None:
        dj_announcer._DJ_CACHE_DIR = self._orig
        self._tmp.cleanup()

    def test_sweep_removes_old_files(self) -> None:
        old = self.dj_dir / "dj_1_old.mp3"
        new = self.dj_dir / "dj_1_new.mp3"
        old.write_bytes(b"x" * 100)
        new.write_bytes(b"y" * 100)
        os.utime(old, (time.time() - 99999, time.time() - 99999))

        stats = dj_announcer.sweep_dj_audio_cache(max_age_sec=3600, max_mb=50)
        self.assertEqual(stats["removed"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_sweep_enforces_size_cap(self) -> None:
        # max_mb=0 is clamped to 1 in code — use tiny files and max_mb=1 with many files
        paths = []
        for i in range(5):
            p = self.dj_dir / f"dj_bulk_{i}.mp3"
            p.write_bytes(b"z" * (300 * 1024))  # 300 KiB each ≈ 1.5 MiB total
            os.utime(p, (time.time() - i, time.time() - i))
            paths.append(p)

        stats = dj_announcer.sweep_dj_audio_cache(max_age_sec=10**9, max_mb=1)
        self.assertGreaterEqual(stats["removed"], 1)
        remaining = sum(1 for p in paths if p.exists())
        self.assertLess(remaining, 5)


class RunDiskMaintenanceTests(unittest.TestCase):
    def test_run_disk_maintenance_aggregates(self) -> None:
        with patch.object(library, "sweep_library_temps", return_value={"removed": 1, "bytes_freed": 10}), \
             patch.object(library, "sweep_orphan_index_entries", return_value={"removed": 0, "bytes_freed": 0}), \
             patch.object(library, "sweep_orphan_files", return_value={"removed": 2, "bytes_freed": 20}), \
             patch.object(library, "reclaim_disk", return_value={"evicted": 1, "bytes_freed": 30, "emergency_pins": 0}), \
             patch.object(library, "disk_free_mb", return_value=5000.0), \
             patch.object(library, "_maybe_reload_index"), \
             patch("src.dj_announcer.sweep_dj_audio_cache", return_value={"removed": 1, "bytes_freed": 5}):
            library.LIBRARY_ENABLED = True
            summary = library.run_disk_maintenance(reason="test")

        self.assertEqual(summary["bytes_freed_total"], 10 + 20 + 5 + 30)
        self.assertFalse(summary["disk_pressure"])
        self.assertEqual(summary["temps"]["removed"], 1)
        self.assertEqual(summary["reclaim"]["evicted"], 1)


if __name__ == "__main__":
    unittest.main()
