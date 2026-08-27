"""Queue helpers used by the request agent (radio-aware enqueue)."""
from __future__ import annotations

import asyncio
import collections
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.playback import (
    clear_user_tracks_from_queue,
    count_user_tracks_in_queue,
    enqueue_user_tracks,
    guild_session,
    move_queue_track,
    remove_queue_track_at,
    remove_queue_track_match,
)
from src.request_agent import RequestPlan


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


class FrontOffsetEnqueueTests(unittest.TestCase):
    def test_sequential_front_preserves_order(self):
        gid = 55
        session = guild_session(gid)
        session.queue = collections.deque()
        with patch("src.radio.is_radio_active", return_value=False):
            enqueue_user_tracks(
                gid, [{"title": "A"}], playback_active=False, position="front", front_offset=0
            )
            enqueue_user_tracks(
                gid, [{"title": "B"}], playback_active=False, position="front", front_offset=1
            )
            enqueue_user_tracks(
                gid, [{"title": "C"}], playback_active=False, position="front", front_offset=2
            )
        self.assertEqual([t["title"] for t in session.queue], ["A", "B", "C"])


class OrganicMergeAndMaterializeTests(unittest.TestCase):
    def test_organic_merge_not_strict_halves(self):
        import src.request_channel as rc

        pools = [
            [f"A{i}" for i in range(5)],
            [f"B{i}" for i in range(5)],
        ]
        # Run several times — should not always be AAAAA then BBBBB
        sequential = 0
        for _ in range(20):
            out = rc._organic_merge_pools(pools, 10)
            self.assertEqual(len(out), 10)
            self.assertEqual(len(set(out)), 10)
            if out[:5] == [f"A{i}" for i in range(5)]:
                sequential += 1
        self.assertLess(sequential, 15)

    def test_curated_fallback_nu_metal_and_thps(self):
        import src.request_channel as rc

        nm = rc._curated_style_queries("nu-metal", "Limp Bizkit", 5)
        self.assertGreaterEqual(len(nm), 5)
        self.assertTrue(all(" - " in q for q in nm))
        th = rc._curated_style_queries(
            "rock", "Tony Hawk Pro Skater 4 soundtrack early 2000s", 12
        )
        self.assertGreaterEqual(len(th), 5)
        self.assertTrue(
            any(
                name in q
                for q in th
                for name in ("Goldfinger", "Sum 41", "Blink-182", "Millencolin")
            )
        )

    def test_curated_mf_doom_hiphop_lofi_not_rock(self):
        """Regression: MF DOOM / lofi must never fall through to early-2000s rock."""
        import src.request_channel as rc

        for genre, hints in (
            ("hip-hop lofi", "MF DOOM"),
            ("hip-hop", "MF Doom similares"),
            ("lofi", "mf doom"),
        ):
            qs = rc._curated_style_queries(genre, hints, 8)
            self.assertGreaterEqual(len(qs), 5, msg=f"{genre!r} {hints!r}")
            joined = " ".join(qs)
            self.assertFalse(
                any(
                    bad in joined
                    for bad in (
                        "Foo Fighters",
                        "Nickelback",
                        "Evanescence",
                        "White Stripes",
                    )
                ),
                msg=f"got rock pack for {genre!r} {hints!r}: {qs}",
            )
            self.assertTrue(
                any(
                    name in joined
                    for name in ("MF DOOM", "Madvillain", "Nujabes", "J Dilla", "Madlib")
                ),
                msg=f"missing doom/lofi artists in {qs}",
            )


class MaterializeGenreTests(unittest.IsolatedAsyncioTestCase):
    async def test_materialize_replaces_genre_with_enqueue_queries(self):
        import src.request_channel as rc
        from src.request_agent import RequestPlan, planned_track_labels

        plan = RequestPlan(
            reply="mix",
            actions=[
                {"type": "genre_playlist", "genre": "nu-metal", "count": 3, "hints": ""},
                {"type": "genre_playlist", "genre": "rock", "count": 3, "hints": ""},
            ],
            source="test",
        )

        async def fake_genre(genre, count, hints):
            # Concrete Artist - Title lines (what Spotify would return)
            return [f"{genre.title()} Band - Song {i}" for i in range(count)]

        with patch.object(rc, "_genre_queries", side_effect=fake_genre):
            out = await rc.materialize_genre_playlists(plan)

        self.assertTrue(all(a["type"] != "genre_playlist" for a in out.actions))
        enqueue = [a for a in out.actions if a["type"] == "enqueue"]
        self.assertEqual(len(enqueue), 1)
        queries = enqueue[0]["queries"]
        self.assertEqual(len(queries), 6)
        labels = planned_track_labels(out)
        self.assertFalse(any("#" in lab and lab.split("#")[-1].strip().isdigit() for lab in labels))
        self.assertTrue(any("Song" in lab for lab in labels))

    async def test_materialize_expands_style_blob_enqueues(self):
        import src.request_channel as rc
        from src.request_agent import RequestPlan, planned_track_labels

        plan = RequestPlan(
            reply="¡Dale! Te armo dos playlists",
            actions=[{
                "type": "enqueue",
                "queries": [
                    "Tony Hawk Pro Skater 4 soundtrack early 2000s rock",
                    "Limp Bizkit nu-metal",
                ],
                "position": "end",
            }],
            source="llm",
        )

        async def fake_genre(genre, count, hints):
            tag = "NM" if "metal" in genre.lower() else "RK"
            return [f"Artist {tag} - Track {i}" for i in range(count)]

        with patch.object(rc, "_genre_queries", side_effect=fake_genre):
            out = await rc.materialize_genre_playlists(plan)

        enqueue = [a for a in out.actions if a["type"] == "enqueue"]
        self.assertEqual(len(enqueue), 1)
        queries = enqueue[0]["queries"]
        self.assertGreaterEqual(len(queries), 6)
        labels = planned_track_labels(out)
        self.assertFalse(any("Limp Bizkit nu-metal" == lab for lab in labels))
        self.assertFalse(any("Tony Hawk" in lab and " - " not in lab for lab in labels))
        self.assertTrue(all(" - " in lab for lab in labels))


class AwaitUnlessAbortedTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_result_when_not_aborted(self):
        import src.request_channel as rc

        async def slow():
            await asyncio.sleep(0.05)
            return "ok"

        result = await rc._await_unless_aborted(slow(), lambda: False, poll_sec=0.02)
        self.assertEqual(result, "ok")

    async def test_cancels_when_abort_flips(self):
        import src.request_channel as rc

        aborted = False

        async def slow():
            await asyncio.sleep(2.0)
            return "late"

        async def flip():
            nonlocal aborted
            await asyncio.sleep(0.05)
            aborted = True

        flip_task = asyncio.create_task(flip())
        t0 = asyncio.get_event_loop().time()
        result = await rc._await_unless_aborted(
            slow(), lambda: aborted, poll_sec=0.02
        )
        elapsed = asyncio.get_event_loop().time() - t0
        await flip_task
        self.assertIsNone(result)
        self.assertLess(elapsed, 1.0)


class ExecutePlanPlayEarlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_play_next_after_first_resolve_not_after_all(self):
        """First match must start playback before later queries finish resolving."""
        import src.request_channel as rc

        gid = 777001
        author_id = 42
        session = guild_session(gid)
        session.queue = collections.deque()
        session.now_playing = None

        plan = RequestPlan(
            actions=[
                {
                    "type": "enqueue",
                    "queries": ["Song One", "Song Two", "Song Three"],
                    "position": "end",
                }
            ],
            source="test",
            model="test",
        )
        track_items = [
            {"label": "Song One", "status": "pending"},
            {"label": "Song Two", "status": "pending"},
            {"label": "Song Three", "status": "pending"},
        ]
        notes: list[str] = []
        resolve_order: list[str] = []
        play_at_resolve_count: list[int] = []

        async def fake_resolve(query: str, requester: str):
            resolve_order.append(query)
            # Simulate slow YT search so ordering of play_next is observable.
            await asyncio.sleep(0)
            return {
                "title": query,
                "yt_query": query,
                "url": f"https://example.test/{query}",
                "requester": requester,
                "artist": "Test",
                "duration": 120,
            }

        async def play_next_side_effect(guild, vc, text_channel):
            play_at_resolve_count.append(len(resolve_order))
            vc.is_playing = MagicMock(return_value=True)

        play_next = AsyncMock(side_effect=play_next_side_effect)

        guild = MagicMock()
        guild.id = gid
        member = MagicMock()
        member.display_name = "Tester"
        member.voice = None
        guild.get_member.return_value = member
        vc = MagicMock()
        vc.is_playing.return_value = False
        vc.is_paused.return_value = False
        guild.voice_client = vc

        with patch.object(rc, "bot") as bot_mock, patch.object(
            rc, "_resolve_one_query", side_effect=fake_resolve
        ), patch.object(rc, "play_next", play_next), patch.object(
            rc, "record_request"
        ), patch.object(
            rc, "update_player_embed", new_callable=AsyncMock
        ), patch(
            "src.playback.bot.get_guild", return_value=guild
        ):
            bot_mock.get_guild.return_value = guild
            bot_mock.get_channel.return_value = MagicMock()
            await rc.execute_plan(
                gid,
                author_id,
                plan,
                track_items=track_items,
                notes_out=notes,
            )

        self.assertEqual(resolve_order, ["Song One", "Song Two", "Song Three"])
        # Early start: first play_next while only the first track was resolved.
        self.assertGreaterEqual(play_next.await_count, 1)
        self.assertEqual(play_at_resolve_count[0], 1)
        self.assertEqual([t["status"] for t in track_items], ["ok", "ok", "ok"])
        # All three should have been enqueued (first may have been consumed by play_next
        # only if play_next mutates queue — our mock does not, so all three remain).
        self.assertEqual(len(session.queue), 3)


if __name__ == "__main__":
    unittest.main()
