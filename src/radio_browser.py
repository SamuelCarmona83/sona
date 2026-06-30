import difflib
import logging
import re
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

RADIO_BROWSER_SEARCH_URL = "https://de1.api.radio-browser.info/json/stations/search"
DEFAULT_TIMEOUT_SEC = 8

_COUNTRY_HINTS = {
    "us": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states": "US",
    "united states of america": "US",
    "estados unidos": "US",
    "argentina": "AR",
    "germany": "DE",
    "deutschland": "DE",
    "italy": "IT",
    "spain": "ES",
    "españa": "ES",
    "france": "FR",
    "brazil": "BR",
    "brasil": "BR",
    "chile": "CL",
    "mexico": "MX",
    "colombia": "CO",
    "uk": "GB",
    "united kingdom": "GB",
    "england": "GB",
}

_FILTER_ALIASES = {
    "country": "countrycode",
    "pais": "countrycode",
    "countrycode": "countrycode",
    "cc": "countrycode",
    "language": "language",
    "lang": "language",
    "idioma": "language",
    "type": "tag",
    "tipo": "tag",
    "tag": "tag",
    "genre": "tag",
    "genero": "tag",
    "codec": "codec",
    "format": "codec",
}


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
        "countrycode": (raw.get("countrycode") or "").strip().upper(),
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


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _extract_country_code(query: str) -> Optional[str]:
    q = _clean_text(query)
    if not q:
        return None
    for hint, code in _COUNTRY_HINTS.items():
        if hint in q:
            return code
    return None


def _country_value_to_code(value: str) -> Optional[str]:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned.upper()
    direct = _COUNTRY_HINTS.get(cleaned)
    if direct:
        return direct
    return _extract_country_code(cleaned)


def parse_search_query(query: str) -> tuple[str, dict]:
    tokens = query.split()
    filters: dict[str, str] = {}
    free_tokens: list[str] = []

    for token in tokens:
        if ":" not in token:
            free_tokens.append(token)
            continue
        key, value = token.split(":", 1)
        canonical = _FILTER_ALIASES.get(key.lower().strip())
        value = value.strip()
        if not canonical or not value:
            free_tokens.append(token)
            continue

        if canonical == "countrycode":
            code = _country_value_to_code(value)
            if code:
                filters["countrycode"] = code
                continue
            free_tokens.append(token)
            continue

        if canonical == "codec":
            filters["codec"] = value.upper()
            continue

        filters[canonical] = _clean_text(value)

    parsed_query = " ".join(free_tokens).strip()
    return parsed_query, filters


def _strip_country_hints(query: str) -> str:
    q = _clean_text(query)
    if not q:
        return ""
    for hint in sorted(_COUNTRY_HINTS.keys(), key=len, reverse=True):
        q = q.replace(hint, " ")
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _search_once(
    *,
    name_query: str,
    limit: int,
    timeout_sec: int,
    country_code: Optional[str] = None,
    filters: Optional[dict] = None,
) -> list[dict]:
    applied_filters = filters or {}
    params = {
        "name": name_query,
        "hidebroken": "true",
        "limit": max(1, min(limit, 100)),
    }
    final_country_code = country_code or applied_filters.get("countrycode")
    if final_country_code:
        params["countrycode"] = final_country_code
    if applied_filters.get("language"):
        params["language"] = applied_filters["language"]
    if applied_filters.get("tag"):
        params["tag"] = applied_filters["tag"]
    response = requests.get(RADIO_BROWSER_SEARCH_URL, params=params, timeout=timeout_sec)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        return []
    normalized = [_normalize_station(item) for item in payload if isinstance(item, dict)]
    candidates = [station for station in normalized if station.get("name") and _has_stream_url(station)]
    if not applied_filters:
        return candidates

    filtered: list[dict] = []
    for station in candidates:
        if applied_filters.get("countrycode") and station.get("countrycode") != applied_filters["countrycode"]:
            continue
        if applied_filters.get("language"):
            station_lang = _clean_text(station.get("language", ""))
            if applied_filters["language"] not in station_lang:
                continue
        if applied_filters.get("tag"):
            station_tags = _clean_text(station.get("tags", ""))
            if applied_filters["tag"] not in station_tags:
                continue
        if applied_filters.get("codec"):
            station_codec = (station.get("codec") or "").upper()
            if applied_filters["codec"] not in station_codec:
                continue
        filtered.append(station)
    return filtered


def search_stations(query: str, *, limit: int = 12, timeout_sec: int = DEFAULT_TIMEOUT_SEC, filters: Optional[dict] = None) -> list[dict]:
    cleaned_query = _clean_text(query)
    applied_filters = dict(filters or {})
    if not cleaned_query:
        cleaned_query = applied_filters.get("tag") or applied_filters.get("language") or "radio"

    country_code = applied_filters.get("countrycode") or _extract_country_code(cleaned_query)
    stripped_query = _strip_country_hints(cleaned_query)

    attempts = [
        (cleaned_query, country_code),
        (stripped_query, country_code),
        (stripped_query or cleaned_query, None),
    ]

    tokens = [tok for tok in stripped_query.split() if len(tok) >= 2]
    if tokens:
        attempts.append((tokens[0], country_code))
        if len(tokens) > 1:
            attempts.append((" ".join(tokens[:2]), country_code))

    seen_pairs: set[tuple[str, Optional[str]]] = set()
    merged: list[dict] = []
    seen_station_keys: set[tuple[str, str]] = set()

    for name_attempt, country_attempt in attempts:
        if not name_attempt:
            continue
        key_attempt = (name_attempt, country_attempt)
        if key_attempt in seen_pairs:
            continue
        seen_pairs.add(key_attempt)
        try:
            batch = _search_once(
                name_query=name_attempt,
                limit=max(limit, 30),
                timeout_sec=timeout_sec,
                country_code=country_attempt,
                filters=applied_filters,
            )
        except Exception as exc:
            logger.warning("radio_browser: search attempt failed name=%s country=%s err=%s", name_attempt, country_attempt, exc)
            continue

        for station in batch:
            station_key = (
                station.get("url_resolved") or station.get("url") or "",
                station.get("name") or "",
            )
            if station_key in seen_station_keys:
                continue
            seen_station_keys.add(station_key)
            merged.append(station)

        if len(merged) >= max(limit * 2, 30):
            break

    return merged


def top_stations(*, limit: int = 10, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> list[dict]:
    params = {
        "limit": max(1, min(limit, 30)),
        "hidebroken": "true",
        "order": "clicktimestamp",
        "reverse": "true",
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
    country_boost = 0.0
    query_country_code = _extract_country_code(query)
    if query_country_code and (station.get("countrycode") or "").upper() == query_country_code:
        country_boost = 0.1
    return (name_match * 0.55) + (quality * 0.2) + (popularity * 0.15) + (bitrate * 0.05) + country_boost


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