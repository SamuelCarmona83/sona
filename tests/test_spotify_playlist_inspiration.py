"""Playlist search/sample used by genre expansion (mocked Spotify)."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src import spotify as sp_mod


class PlaylistInspirationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sample_tracks_from_playlist_search(self):
        fake_playlists = {
            "playlists": {
                "items": [
                    {
                        "id": "pl1",
                        "name": "Nu Metal Essentials",
                        "owner": {"display_name": "Spotify"},
                        "tracks": {"total": 50},
                    }
                ]
            }
        }
        track_infos = [
            {
                "query": "Korn - Freak on a Leash",
                "spotify_id": "t1",
                "artist_id": "a1",
                "title": "Freak on a Leash",
                "artist": "Korn",
                "spotify_refined": True,
            },
            {
                "query": "Limp Bizkit - Rollin",
                "spotify_id": "t2",
                "artist_id": "a2",
                "title": "Rollin",
                "artist": "Limp Bizkit",
                "spotify_refined": True,
            },
        ]

        mock_sp = MagicMock()
        mock_sp.search.return_value = fake_playlists

        with patch.object(sp_mod, "sp", mock_sp), patch.object(
            sp_mod, "_spotify_api_ready", new=AsyncMock(return_value=True)
        ), patch.object(
            sp_mod,
            "fetch_playlist_track_infos",
            new=AsyncMock(return_value=list(track_infos)),
        ):
            # Clear cache between tests
            sp_mod._playlist_sample_cache.clear()
            out = await sp_mod.sample_tracks_from_playlist_search(
                "nu-metal limp bizkit", limit=5
            )

        self.assertGreaterEqual(len(out), 1)
        self.assertTrue(any(t.get("source_playlist") for t in out))
        queries = {t["query"] for t in out}
        self.assertTrue(queries & {t["query"] for t in track_infos})

    async def test_search_playlists_empty_without_client(self):
        with patch.object(sp_mod, "sp", None):
            out = await sp_mod.search_spotify_playlists("rock")
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
