"""Unit tests for Shazam catalog parsers (no live network)."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from src import shazam_catalog as sc


class NormalizePayloadTests(unittest.TestCase):
    def test_search_hits_shape(self):
        payload = {
            "tracks": {
                "hits": [
                    {
                        "track": {
                            "key": "12345",
                            "title": "Basket Case",
                            "subtitle": "Green Day",
                            "images": {"coverart": "http://img"},
                        }
                    },
                    {
                        "track": {
                            "key": "999",
                            "title": "Basket Case",
                            "subtitle": "Green Day",
                        }
                    },
                ]
            }
        }
        items = sc.normalize_payload(payload, limit=5)
        self.assertEqual(len(items), 1)  # dedupe by query
        self.assertEqual(items[0]["title"], "Basket Case")
        self.assertEqual(items[0]["artist"], "Green Day")
        self.assertEqual(items[0]["shazam_id"], 12345)
        self.assertEqual(items[0]["query"], "Green Day - Basket Case")

    def test_playlist_data_shape(self):
        payload = {
            "data": [
                {
                    "id": "555",
                    "attributes": {
                        "name": "Vampiro",
                        "artistName": "Zapato 3",
                        "artwork": {"url": "https://x/{w}x{h}.jpg"},
                    },
                }
            ]
        }
        items = sc.normalize_payload(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["query"], "Zapato 3 - Vampiro")
        self.assertEqual(items[0]["shazam_id"], 555)
        self.assertIn("400", items[0]["cover_url"] or "")

    def test_related_list_shape(self):
        payload = {
            "tracks": [
                {"key": 1, "title": "A", "subtitle": "Artist1"},
                {"key": 2, "title": "B", "subtitle": "Artist2"},
            ]
        }
        items = sc.normalize_payload(payload, limit=10)
        self.assertEqual(len(items), 2)


class MappingTests(unittest.TestCase):
    def test_genre_aliases(self):
        self.assertEqual(sc.map_genre_tag("rock"), "rock")
        self.assertEqual(sc.map_genre_tag("hiphop"), "hip-hop-rap")
        self.assertEqual(sc.map_genre_tag("reggaeton"), "latin")
        self.assertIsNone(sc.map_genre_tag(""))

    def test_country_detect(self):
        self.assertEqual(sc.detect_country("rock venezolano"), "VE")
        self.assertEqual(sc.detect_country("indie argentino"), "AR")
        self.assertIsNone(sc.detect_country("just rock"))


class RadioSeedRecsTests(unittest.IsolatedAsyncioTestCase):
    async def test_merges_related_and_charts(self):
        related = [
            {
                "title": "Rel",
                "artist": "A",
                "query": "A - Rel",
                "shazam_id": 1,
                "cover_url": None,
                "spotify_id": None,
                "artist_id": None,
            }
        ]
        charts = [
            {
                "title": "Chart",
                "artist": "B",
                "query": "B - Chart",
                "shazam_id": 2,
                "cover_url": None,
                "spotify_id": None,
                "artist_id": None,
            }
        ]
        with patch.object(sc, "related_tracks", new=AsyncMock(return_value=related)), patch.object(
            sc, "top_genre_tracks", new=AsyncMock(return_value=charts)
        ), patch.object(sc, "top_country_tracks", new=AsyncMock(return_value=[])):
            recs = await sc.radio_seed_recs(
                seed_genres=["rock"],
                mood_name="rock venezolano",
                recent_shazam_ids=[42],
                limit=10,
            )
        queries = {r["query"] for r in recs}
        self.assertIn("A - Rel", queries)
        self.assertIn("B - Chart", queries)


class BestSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_best_search_returns_first(self):
        hits = [
            {
                "title": "My Mind Is a Mountain",
                "artist": "Deftones",
                "query": "Deftones - My Mind Is a Mountain",
                "shazam_id": 7,
                "cover_url": None,
                "spotify_id": None,
                "artist_id": None,
            }
        ]
        with patch.object(sc, "search_tracks", new=AsyncMock(return_value=hits)):
            hit = await sc.best_search_match("deftones mi mind mountaind")
        self.assertEqual(hit["shazam_id"], 7)


if __name__ == "__main__":
    unittest.main()
