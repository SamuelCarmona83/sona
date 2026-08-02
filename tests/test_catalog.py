import json
import tempfile
import unittest
from pathlib import Path

from src.catalog import (
    build_catalog,
    filter_tracks,
    handle_catalog_request,
    invalidate_catalog_cache,
    normalize_key,
    parse_artist_title,
    soft_match_key,
)


class CatalogTests(unittest.TestCase):
    def test_parse_artist_title_splits_dash(self) -> None:
        a, t = parse_artist_title("Unknown", "System Of A Down - Aerials (Official HD Video)")
        self.assertEqual(a, "System Of A Down")
        self.assertIn("Aerials", t)

    def test_parse_keeps_known_artist(self) -> None:
        a, t = parse_artist_title("Deftones", "Change (In the House of Flies)")
        self.assertEqual(a, "Deftones")
        self.assertEqual(t, "Change (In the House of Flies)")

    def test_soft_match_key_stable(self) -> None:
        self.assertEqual(
            soft_match_key("The Kid LAROI", "GIRLS"),
            soft_match_key("the kid laroi", "girls"),
        )

    def test_build_catalog_merges_fm_and_requests(self) -> None:
        invalidate_catalog_cache()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "library_index.json").write_text(
                json.dumps(
                    {
                        "yt_abc": {
                            "title": "Foo Band - Hello World",
                            "artist": "Unknown",
                            "request_count": 2,
                            "play_count": 3,
                            "last_requested": 100.0,
                            "duration": 120,
                            "cover_url": "https://example.com/c.jpg",
                        },
                        "yt_radio": {
                            "title": "Radio Only",
                            "artist": "DJ",
                            "request_count": 0,
                            "play_count": 5,
                            "sources": ["radio"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "fm_sessions.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sessions": [
                            {
                                "id": "fm_1",
                                "station_name": "La Mega",
                                "tracks": [
                                    {
                                        "artist": "PinkPantheress",
                                        "title": "Stateside",
                                        "match_key": "pp\0stateside",
                                        "detected_at": 200.0,
                                        "cover_url": "https://example.com/pp.jpg",
                                    },
                                    {
                                        # same as library soft key → should not duplicate
                                        "artist": "Foo Band",
                                        "title": "Hello World",
                                        "detected_at": 201.0,
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "likes.json").write_text("{}", encoding="utf-8")

            cat = build_catalog(root)
            self.assertGreaterEqual(cat["summary"]["tracks"], 3)
            self.assertGreaterEqual(cat["summary"]["artists"], 2)

            # Parsed artist from YT title
            hello = next(t for t in cat["tracks"] if "Hello" in t["title"])
            self.assertEqual(hello["artist"], "Foo Band")
            self.assertGreaterEqual(hello["request_count"], 2)

            reqs = filter_tracks(cat["tracks"], source="request")
            self.assertTrue(any(t["id"] == "yt_abc" for t in reqs))

            fm = filter_tracks(cat["tracks"], source="fm")
            self.assertTrue(any("Stateside" in t["title"] for t in fm))
            # library track not duplicated as fm
            self.assertFalse(any(t["id"].startswith("fm_") and "Hello" in t["title"] for t in fm))

            status, body = handle_catalog_request(root, "/api/catalog/artists", "")
            self.assertEqual(status, 200)
            self.assertTrue(body["artists"])

            key = normalize_key("Foo Band")
            status, body = handle_catalog_request(root, f"/api/catalog/artist/{key}", "")
            self.assertEqual(status, 200)
            self.assertGreaterEqual(len(body["tracks"]), 1)


if __name__ == "__main__":
    unittest.main()
