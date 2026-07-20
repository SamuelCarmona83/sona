"""Unit tests for FM detection history sessions."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import fm_history


class FmHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "fm_sessions.json"
        fm_history.reset_for_tests()
        self._path_patch = patch.object(fm_history, "_PATH", self.path)
        self._enabled = patch.object(fm_history, "FM_HISTORY_ENABLED", True)
        self._max_sess = patch.object(fm_history, "FM_HISTORY_MAX_SESSIONS", 5)
        self._max_tracks = patch.object(fm_history, "FM_HISTORY_MAX_TRACKS_PER_SESSION", 10)
        self._path_patch.start()
        self._enabled.start()
        self._max_sess.start()
        self._max_tracks.start()

    def tearDown(self) -> None:
        self._path_patch.stop()
        self._enabled.stop()
        self._max_sess.stop()
        self._max_tracks.stop()
        fm_history.reset_for_tests()
        self._tmp.cleanup()

    def _station(self, **overrides) -> dict:
        base = {
            "title": "Rock FM",
            "stationuuid": "uuid-rock",
            "countrycode": "AR",
            "tags": "rock,pop",
            "url": "https://example.com/stream.mp3",
            "is_radio_stream": True,
        }
        base.update(overrides)
        return base

    def test_open_append_close(self) -> None:
        sid = fm_history.open_session(1, self._station())
        self.assertIsNotNone(sid)
        self.assertTrue(
            fm_history.append_detection(
                1,
                {"artist": "Adele", "title": "Hello", "recognized_at": 100.0},
            )
        )
        self.assertTrue(
            fm_history.append_detection(
                1,
                {"artist": "Dua Lipa", "title": "Levitating", "recognized_at": 200.0},
            )
        )
        # Dedupe same track
        self.assertFalse(
            fm_history.append_detection(
                1,
                {"artist": "Dua Lipa", "title": "Levitating", "recognized_at": 210.0},
            )
        )
        self.assertTrue(fm_history.close_session(1))

        sessions = fm_history.list_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], sid)
        self.assertIsNotNone(s["ended_at"])
        self.assertEqual(s["track_count"], 2)
        self.assertEqual(s["tracks"][0]["title"], "Hello")
        self.assertIsNone(s["tracks"][0]["prev_match_key"])
        self.assertEqual(s["tracks"][1]["prev_match_key"], s["tracks"][0]["match_key"])

        on_disk = json.loads(self.path.read_text())
        self.assertEqual(len(on_disk["sessions"]), 1)

    def test_open_closes_orphan_for_same_guild(self) -> None:
        sid1 = fm_history.open_session(9, self._station())
        fm_history.append_detection(9, {"artist": "A", "title": "One"})
        sid2 = fm_history.open_session(
            9,
            self._station(stationuuid="other", url="https://example.com/other.mp3", title="Other"),
        )
        self.assertNotEqual(sid1, sid2)
        sessions = fm_history.list_sessions()
        first = next(s for s in sessions if s["id"] == sid1)
        second = next(s for s in sessions if s["id"] == sid2)
        self.assertIsNotNone(first["ended_at"])
        self.assertIsNone(second["ended_at"])

    def test_disabled_noop(self) -> None:
        with patch.object(fm_history, "FM_HISTORY_ENABLED", False):
            self.assertIsNone(fm_history.open_session(1, self._station()))
            self.assertFalse(fm_history.append_detection(1, {"artist": "A", "title": "T"}))
            self.assertFalse(fm_history.close_session(1))
        self.assertFalse(self.path.exists())

    def test_max_sessions_prunes_oldest(self) -> None:
        with patch.object(fm_history, "FM_HISTORY_MAX_SESSIONS", 3):
            for i in range(5):
                fm_history.open_session(
                    i,
                    self._station(title=f"S{i}", stationuuid=f"u{i}", url=f"http://x/{i}"),
                )
                fm_history.close_session(i)
            sessions = fm_history.list_sessions()
            self.assertEqual(len(sessions), 3)

    def test_reopen_same_station_reuses_session(self) -> None:
        sid1 = fm_history.open_session(3, self._station())
        sid2 = fm_history.open_session(3, self._station())
        self.assertEqual(sid1, sid2)
        open_sessions = [s for s in fm_history.list_sessions() if s.get("ended_at") is None]
        self.assertEqual(len(open_sessions), 1)


if __name__ == "__main__":
    unittest.main()
