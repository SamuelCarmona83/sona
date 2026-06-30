import difflib
import logging
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

RADIO_BROWSER_SEARCH_URL = "https://de1.api.radio-browser.info/json/stations/search"
DEFAULT_TIMEOUT_SEC = 8


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_station(raw: dict) -> dict:
    return {
        "name": (raw.get("name") or "").strip(),
        "url": (raw.get("url") or "").strip(),
        "url_resolved": (raw.get("url_resolved") or "").strip(),
        "homepage": (raw.get("homepage") or "").strip(),
        "country": (raw.get("country") or "").strip(),
        "state": (raw.get("state") or "").strip(),
        "language": (raw.get("language") or "").strip(),
        "tags": (raw.get("tags") or "").strip(),
        "codec": (raw.get("codec") or "").strip(),
        "bitrate": _to_int(raw.get("bitrate")),
        "votes": _to_int(raw.get("votes")),
        "clickcount": _to_int(raw.get("clickcount")),
    }


def _has_stream_url(station: dict) -> bool:
    url = station.get("url_resolved") or station.get("url")
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def search_stations(query: str, *, limit: int = 12, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> list[dict]:
    params = {
        "name": query,
        "hidebroken": "true",
        "limit": max(1, min(limit, 30)),
    }
    response = requests.get(RADIO_BROWSER_SEARCH_URL, params=params, timeout=timeout_sec)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        return []

    normalized = [_normalize_station(item) for item in payload if isinstance(item, dict)]
    return [station for station in normalized if station.get("name") and _has_stream_url(station)]


def _name_match_score(station_name: str, query: str) -> float:
    station_name_l = station_name.lower().strip()
    query_l = query.lower().strip()
    if not query_l:
        return 0.0
    if query_l in station_name_l:
        return 1.0
    return difflib.SequenceMatcher(None, station_name_l, query_l).ratio()


def _station_score(station: dict, query: str) -> float:
    quality = min(station.get("votes", 0), 5000) / 5000.0
    popularity = min(station.get("clickcount", 0), 100000) / 100000.0
    bitrate = min(station.get("bitrate", 0), 320) / 320.0
    name_match = _name_match_score(station.get("name", ""), query)
    return (name_match * 0.6) + (quality * 0.2) + (popularity * 0.15) + (bitrate * 0.05)


def rank_stations(stations: list[dict], query: str) -> list[dict]:
    return sorted(stations, key=lambda station: _station_score(station, query), reverse=True)


def pick_best_station(stations: list[dict], query: str) -> Optional[dict]:
    ranked = rank_stations(stations, query)
    return ranked[0] if ranked else None


def station_to_track(station: dict, *, requester: str = "📻 FM") -> dict:
    stream_url = station.get("url_resolved") or station.get("url")
    return {
        "title": station.get("name") or "FM Station",
        "yt_query": station.get("name") or "",
        "url": stream_url,
        "requester": requester,
        "artist": station.get("country") or "Radio",
        "duration": 0,
        "thumbnail": "",
        "webpage_url": station.get("homepage") or "",
        "is_radio_stream": True,
        "codec": station.get("codec") or "",
        "bitrate": station.get("bitrate") or 0,
        "language": station.get("language") or "",
        "tags": station.get("tags") or "",
    }