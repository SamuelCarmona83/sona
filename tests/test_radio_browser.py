import unittest

from src.radio_browser import pick_best_station, rank_stations, station_to_track


class RadioBrowserTests(unittest.TestCase):
    def test_rank_prefers_name_match_then_quality(self) -> None:
        stations = [
            {
                "name": "Jazz FM Berlin",
                "url": "https://example.com/jazz-berlin.mp3",
                "url_resolved": "https://example.com/jazz-berlin.mp3",
                "homepage": "",
                "country": "Germany",
                "state": "",
                "language": "de",
                "tags": "jazz",
                "codec": "MP3",
                "bitrate": 128,
                "votes": 30,
                "clickcount": 500,
            },
            {
                "name": "Talk AM 980",
                "url": "https://example.com/talk.mp3",
                "url_resolved": "https://example.com/talk.mp3",
                "homepage": "",
                "country": "Argentina",
                "state": "",
                "language": "es",
                "tags": "news",
                "codec": "AAC",
                "bitrate": 64,
                "votes": 900,
                "clickcount": 12000,
            },
            {
                "name": "Jazz FM Premium",
                "url": "https://example.com/jazz-premium.mp3",
                "url_resolved": "https://example.com/jazz-premium.mp3",
                "homepage": "",
                "country": "UK",
                "state": "",
                "language": "en",
                "tags": "jazz,smooth",
                "codec": "MP3",
                "bitrate": 192,
                "votes": 400,
                "clickcount": 40000,
            },
        ]

        ranked = rank_stations(stations, "jazz fm")
        self.assertEqual(ranked[0]["name"], "Jazz FM Premium")
        self.assertEqual(pick_best_station(stations, "jazz fm")["name"], "Jazz FM Premium")

    def test_station_to_track_prefers_url_resolved(self) -> None:
        station = {
            "name": "Radio Mitre",
            "url": "http://legacy.example.com/stream",
            "url_resolved": "https://cdn.example.com/mitre.aac",
            "homepage": "https://mitre.example.com",
            "country": "Argentina",
            "language": "es",
            "tags": "news,talk",
            "codec": "AAC",
            "bitrate": 96,
        }

        track = station_to_track(station, requester="📻 FM")
        self.assertEqual(track["url"], "https://cdn.example.com/mitre.aac")
        self.assertEqual(track["title"], "Radio Mitre")
        self.assertTrue(track["is_radio_stream"])
        self.assertEqual(track["duration"], 0)


if __name__ == "__main__":
    unittest.main()