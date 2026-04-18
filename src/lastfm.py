"""LastFM API integration for radio recommendations when Spotify is unavailable.

Provides fallback track discovery using LastFM's free API (no auth required).
Caches results for 24h to minimize API calls.
"""
import asyncio
import json
import logging
import pathlib
import time
from datetime import datetime

from poc_setlistfm import load_dotenv_values, get_config_value

logger = logging.getLogger(__name__)

# Cache file for LastFM results (24h TTL)
_CACHE_DIR = pathlib.Path(".cache")
_CACHE_FILE = _CACHE_DIR / "lastfm_cache.json"
_CACHE_TTL = 86400  # 24 hours

# LastFM API endpoint (free tier, no auth required)
_LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"

# Load LastFM API key from .env, with fallback to public key
_dotenv_values = load_dotenv_values()
_LASTFM_API_KEY = get_config_value(
    "LASTFM_API_KEY",
    _dotenv_values,
    default="04d915176ebf1c4fc3f3a42a2a65a5fa",  # Public fallback (rate limited)
)


def _ensure_cache_dir():
    """Ensure .cache directory exists."""
    _CACHE_DIR.mkdir(exist_ok=True)


def _load_cache() -> dict:
    """Load cache from disk. Return empty dict if missing or corrupted."""
    _ensure_cache_dir()
    if not _CACHE_FILE.exists():
        return {}
    try:
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.debug(f"lastfm: cache load error: {e}")
        return {}


def _save_cache(cache: dict):
    """Persist cache to disk."""
    _ensure_cache_dir()
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except IOError as e:
        logger.warning(f"lastfm: cache save error: {e}")


def _get_cached(key: str) -> dict | None:
    """Get cached value if not expired. Return None if expired or missing."""
    cache = _load_cache()
    entry = cache.get(key)
    if not entry:
        return None
    if time.time() - entry.get("timestamp", 0) > _CACHE_TTL:
        logger.debug(f"lastfm: cache expired for key={key}")
        return None
    return entry.get("data")


def _set_cache(key: str, data: dict):
    """Cache a value with current timestamp."""
    cache = _load_cache()
    cache[key] = {
        "data": data,
        "timestamp": time.time(),
    }
    _save_cache(cache)


async def _lastfm_request(
    method: str,
    params: dict,
) -> dict:
    """Async wrapper around LastFM API call (runs in executor to avoid blocking)."""
    import requests

    try:
        params_full = {
            "method": method,
            "api_key": _LASTFM_API_KEY,
            "format": "json",
            **params,
        }
        resp = await asyncio.to_thread(
            requests.get,
            _LASTFM_API_URL,
            params=params_full,
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"lastfm: request failed for {method}: {e}")
        return {}


async def get_similar_artists(artist_name: str, limit: int = 10) -> list[str]:
    """Get list of similar artists from LastFM. Return list of artist names."""
    if not artist_name:
        return []

    cache_key = f"similar_artists:{artist_name}:{limit}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    result = await _lastfm_request(
        "artist.getSimilar",
        {"artist": artist_name, "limit": limit},
    )

    artists = []
    for item in result.get("similarartists", {}).get("artist", [])[:limit]:
        name = item.get("name", "").strip()
        if name:
            artists.append(name)

    _set_cache(cache_key, artists)
    logger.debug(f"lastfm: got {len(artists)} similar artists for '{artist_name}'")
    return artists


async def get_top_tracks(artist_name: str, limit: int = 10) -> list[dict]:
    """Get top tracks for artist from LastFM.
    
    Return list of dicts with keys: artist, title, url, mbid.
    """
    if not artist_name:
        return []

    cache_key = f"top_tracks:{artist_name}:{limit}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    result = await _lastfm_request(
        "artist.getTopTracks",
        {"artist": artist_name, "limit": limit},
    )

    tracks = []
    for item in result.get("toptracks", {}).get("track", [])[:limit]:
        title = item.get("name", "").strip()
        if title:
            tracks.append({
                "artist": artist_name,
                "title": title,
                "url": item.get("url", ""),
                "mbid": item.get("mbid", ""),
            })

    _set_cache(cache_key, tracks)
    logger.debug(f"lastfm: got {len(tracks)} top tracks for '{artist_name}'")
    return tracks


async def search_artists_by_genre(genre: str, limit: int = 5) -> list[str]:
    """Search for popular artists in a genre.
    
    LastFM doesn't have direct genre search, so we search for a genre tag
    and get the top artists tagged with it.
    """
    if not genre:
        return []

    cache_key = f"genre_artists:{genre}:{limit}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    result = await _lastfm_request(
        "tag.getTopArtists",
        {"tag": genre, "limit": limit},
    )

    artists = []
    for item in result.get("topartists", {}).get("artist", [])[:limit]:
        name = item.get("name", "").strip()
        if name:
            artists.append(name)

    _set_cache(cache_key, artists)
    logger.debug(f"lastfm: got {len(artists)} artists for genre '{genre}'")
    return artists


async def get_artist_info(artist_name: str) -> dict | None:
    """Get artist info including genres (tags).
    
    Return dict with keys: name, tags, bio_url, image.
    """
    if not artist_name:
        return None

    cache_key = f"artist_info:{artist_name}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    result = await _lastfm_request(
        "artist.getInfo",
        {"artist": artist_name},
    )

    artist_obj = result.get("artist")
    if not artist_obj:
        return None

    tags = []
    for tag in artist_obj.get("tags", {}).get("tag", [])[:5]:
        tag_name = tag.get("name", "").strip()
        if tag_name:
            tags.append(tag_name)

    info = {
        "name": artist_obj.get("name", ""),
        "tags": tags,
        "bio_url": artist_obj.get("url", ""),
        "image": artist_obj.get("image", [{}])[-1].get("#text", "") if artist_obj.get("image") else "",
    }

    _set_cache(cache_key, info)
    logger.debug(f"lastfm: got info for artist '{artist_name}' with tags {tags}")
    return info
