"""Queue helpers used by the request agent (radio-aware enqueue)."""
from __future__ import annotations

import collections
import unittest
from unittest.mock import patch

from src.playback import (
    clear_user_tracks_from_queue,
    count_user_tracks_in_queue,
    enqueue_user_tracks,
    guild_session,
    move_queue_track,
    remove_queue_track_at,
    remove_queue_track_match,
)


class RequestQueueHelpersTests(unittest.TestCase):
    def _reset(self, gid: int = 1) -> None:
        session = guild_session(gid)
        session.queue = collections.deque()
        session.now_playing = None
        session.paused = False

    def test_enqueue_before_radio_when_active(self):
        gid = 42
        self._reset(gid)
        session = guild_session(gid)
        session.queue = collections.deque([
            {"title": "R1", "requester": "📻 Radio"},
            {"title": "R2", "requester": "📻 Radio"},
        ])
        with patch("src.radio.is_radio_active", return_value=True):
            enqueue_user_tracks(
                gid,
                [{"title": "U1", "requester": "Alice"}],
                playback_active=True,
                position="end",
            )
        titles = [t["title"] for t in session.queue]
        self.assertEqual(titles[0], "U1")
        self.assertEqual(titles[1], "R1")

    def test_clear_user_keeps_radio(self):
        gid = 43
        self._reset(gid)
        session = guild_session(gid)
        session.queue = collections.deque([
            {"title": "U1", "requester": "Alice"},
            {"title": "R1", "requester": "📻 Radio"},
            {"title": "U2", "requester": "Bob"},
        ])
        n = clear_user_tracks_from_queue(gid)
        self.assertEqual(n, 2)
        self.assertEqual([t["title"] for t in session.queue], ["R1"])
        self.assertEqual(count_user_tracks_in_queue(gid), 0)

    def test_move_remove_match(self):
        gid = 44
        self._reset(gid)
        session = guild_session(gid)
        session.queue = collections.deque([
            {"title": "Alpha Song", "requester": "A"},
            {"title": "Beta Song", "requester": "B"},
            {"title": "Gamma", "requester": "C"},
        ])
        move_queue_track(gid, 3, 1)
        self.assertEqual(session.queue[0]["title"], "Gamma")
        remove_queue_track_at(gid, 2)
        self.assertEqual([t["title"] for t in session.queue], ["Gamma", "Beta Song"])
        removed = remove_queue_track_match(gid, "beta")
        self.assertEqual(removed["title"], "Beta Song")
        self.assertEqual(len(session.queue), 1)


class RequestSettingsTests(unittest.TestCase):
    def test_auto_flag_roundtrip(self):
        import src.request_channel as rc

        gid = 999001
        rc._guild_auto.pop(gid, None)
        with patch.object(rc, "_save_settings", lambda: None):
            rc.set_auto_apply(gid, True)
            self.assertTrue(rc.get_auto_apply(gid))
            rc.set_auto_apply(gid, False)
            self.assertFalse(rc.get_auto_apply(gid))
        rc._guild_auto.pop(gid, None)


if __name__ == "__main__":
    unittest.main()
