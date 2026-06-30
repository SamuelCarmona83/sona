import unittest
from unittest.mock import Mock, patch

from src.radio_browser import (
    parse_search_query,
    pick_best_station,
    rank_stations,
    search_stations,
    station_to_track,
    top_stations,
)


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

    def test_top_stations_uses_popularity_params(self) -> None:
        response_payload = [
            {
                "name": "TechnoBase.FM",
                "url": "http://listen.technobase.fm/stream",
                "url_resolved": "http://listen.technobase.fm/stream",
                "country": "Germany",
                "language": "german",
                "codec": "AAC",
                "bitrate": 256,
                "votes": 2469,
                "clickcount": 13,
            }
        ]
        mock_response = Mock()
        mock_response.json.return_value = response_payload
        mock_response.raise_for_status.return_value = None

        with patch("src.radio_browser.requests.get", return_value=mock_response) as mock_get:
            stations = top_stations(limit=10)

        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0]["name"], "TechnoBase.FM")
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["order"], "clicktimestamp")
        self.assertEqual(kwargs["params"]["reverse"], "true")
        self.assertEqual(kwargs["params"]["hidebroken"], "true")

    def test_search_stations_relaxes_query_with_country_hint(self) -> None:
        empty_response = Mock()
        empty_response.json.return_value = []
        empty_response.raise_for_status.return_value = None

        hit_response = Mock()
        hit_response.json.return_value = [
            {
                "name": "CNN",
                "url": "https://tunein.cdnstream1.com/2868_96.mp3",
                "url_resolved": "https://tunein.cdnstream1.com/2868_96.mp3",
                "country": "The United States Of America",
                "countrycode": "US",
                "language": "english",
                "codec": "MP3",
                "bitrate": 96,
                "votes": 11506,
                "clickcount": 239,
            }
        ]
        hit_response.raise_for_status.return_value = None

        with patch(
            "src.radio_browser.requests.get",
            side_effect=[empty_response, hit_response, hit_response, hit_response, hit_response],
        ) as mock_get:
            stations = search_stations("CNN The United States Of America", limit=12)

        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0]["name"], "CNN")
        self.assertEqual(stations[0]["countrycode"], "US")
        self.assertGreaterEqual(mock_get.call_count, 2)

    def test_parse_search_query_extracts_supported_filters(self) -> None:
        query, filters = parse_search_query("cnn country:us language:english type:news codec:aac")
        self.assertEqual(query, "cnn")
        self.assertEqual(filters["countrycode"], "US")
        self.assertEqual(filters["language"], "english")
        self.assertEqual(filters["tag"], "news")
        self.assertEqual(filters["codec"], "AAC")

    def test_search_stations_applies_type_country_codec_filters(self) -> None:
        response = Mock()
        response.json.return_value = [
            {
                "name": "CNN US AAC",
                "url": "https://example.com/cnn-us.aac",
                "url_resolved": "https://example.com/cnn-us.aac",
                "country": "The United States Of America",
                "countrycode": "US",
                "language": "english",
                "tags": "news,talk",
                "codec": "AAC",
                "bitrate": 96,
                "votes": 100,
                "clickcount": 50,
            },
            {
                "name": "CNN Indonesia MP3",
                "url": "https://example.com/cnn-id.mp3",
                "url_resolved": "https://example.com/cnn-id.mp3",
                "country": "Indonesia",
                "countrycode": "ID",
                "language": "indonesian",
                "tags": "news",
                "codec": "MP3",
                "bitrate": 128,
                "votes": 100,
                "clickcount": 50,
            },
        ]
        response.raise_for_status.return_value = None

        with patch("src.radio_browser.requests.get", return_value=response):
            stations = search_stations(
                "cnn",
                limit=12,
                filters={"countrycode": "US", "tag": "news", "codec": "AAC"},
            )

        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0]["name"], "CNN US AAC")


if __name__ == "__main__":
    unittest.main()