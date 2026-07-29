import asyncio
import unittest
from unittest.mock import patch

os_environ_set = __import__("os").environ.setdefault
os_environ_set("BOT_TOKEN", "test-token")

from src import spotify  # noqa: E402


class SpotifyPlaylistEmbedTests(unittest.TestCase):
    def test_parse_playlist_url_with_si_param(self) -> None:
        url = "https://open.spotify.com/playlist/2khNU9BxyPLqUcMhfWMnxU?si=2e6ab5d2776542e3"
        parsed = spotify._parse_spotify_url(url)
        self.assertEqual(parsed, {"type": "playlist", "id": "2khNU9BxyPLqUcMhfWMnxU"})

    def test_embed_track_to_info_normalizes_artists(self) -> None:
        info = spotify._embed_track_to_info(
            {
                "uri": "spotify:track:abc123XYZ0123456789012",
                "title": "T.N.T.",
                "subtitle": "AC/DC,\xa0Someone",
            }
        )
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["spotify_id"], "abc123XYZ0123456789012")
        self.assertEqual(info["query"], "AC/DC, Someone - T.N.T.")
        self.assertTrue(info["spotify_refined"])
        self.assertIsNone(info["artist_id"])

    def test_embed_track_to_info_skips_non_tracks(self) -> None:
        self.assertIsNone(
            spotify._embed_track_to_info(
                {"uri": "spotify:episode:xyz", "title": "Ep", "subtitle": "Show"}
            )
        )
        self.assertIsNone(spotify._embed_track_to_info({"uri": "spotify:track:x", "title": ""}))

    def test_get_tracks_falls_back_to_embed_when_api_403(self) -> None:
        embed_infos = [
            {
                "query": "Agent Orange - Bloodstains",
                "spotify_id": "2Bi07aWU3Mhj47r6GmQQ7s",
                "artist_id": None,
                "spotify_refined": True,
            }
        ]

        async def boom(_playlist_id: str):
            raise Exception("http status: 403 Forbidden")

        with patch.object(spotify, "_fetch_playlist_tracks_via_api", side_effect=boom), patch.object(
            spotify,
            "_fetch_playlist_tracks_from_embed",
            return_value=embed_infos,
        ) as embed_mock:
            result = asyncio.run(
                spotify._get_tracks_from_spotify_url(
                    "https://open.spotify.com/playlist/2khNU9BxyPLqUcMhfWMnxU"
                )
            )

        self.assertEqual(result, embed_infos)
        embed_mock.assert_called_once_with("2khNU9BxyPLqUcMhfWMnxU")


if __name__ == "__main__":
    unittest.main()
