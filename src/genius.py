"""Genius API integration for song metadata, official artwork (song art), and lyrics links.

Complements Spotify (for verified official data) and Last.fm.
Uses search + /songs/:id for rich info: song_art_image_url is often high-quality/official artwork.
Caches results for 24h to minimize API calls.
Requires GENIUS_ACCESS_TOKEN (recommended) or falls back to client creds if implemented later.
"""
import asyncio
import json
import logging
import pathlib
import time
from datetime import datetime

from src.config import GENIUS_ACCESS_TOKEN, GENIUS_CLIENT_ID, GENIUS_CLIENT_SECRET

logger = logging.getLogger(__name__)

# Cache file for Genius results (24h TTL) - shares pattern with lastfm
_CACHE_DIR = pathlib.Path(".cache")
_CACHE_FILE = _CACHE_DIR / "genius_cache.json"
_CACHE_TTL = 86400  # 24 hours

_GENIUS_API_BASE = "https://api.genius.com"


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
        logger.debug(f"genius: cache load error: {e}")
        return {}


def _save_cache(cache: dict):
    """Persist cache to disk."""
    _ensure_cache_dir()
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except IOError as e:
        logger.warning(f"genius: cache save error: {e}")


def _get_cached(key: str) -> dict | None:
    """Get cached value if not expired. Return None if expired or missing."""
    cache = _load_cache()
    entry = cache.get(key)
    if not entry:
        return None
    if time.time() - entry.get("timestamp", 0) > _CACHE_TTL:
        logger.debug(f"genius: cache expired for key={key}")
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


async def _genius_request(path: str, params: dict | None = None) -> dict:
    """Async wrapper around Genius API GET (runs in executor)."""
    import requests

    token = GENIUS_ACCESS_TOKEN
    if not token:
        logger.info("genius: no GENIUS_ACCESS_TOKEN configured (or not loaded) - skipping request to api.genius.com. Set it in .env and restart the bot/explorer.")
        return {}

    try:
        url = f"{_GENIUS_API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "spoty-scanner/1.0",
        }
        resp = await asyncio.to_thread(
            requests.get,
            url,
            headers=headers,
            params=params or {},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"genius: request failed for {path}: {e}")
        return {}


def _normalize_name(name: str) -> str:
    """Simple normalize for matching."""
    if not name:
        return ""
    return name.lower().strip().replace("’", "'").replace("–", "-")


async def search_songs(q: str, limit: int = 5) -> list[dict]:
    """Search songs on Genius. Returns list of hit dicts (with 'result' key containing song)."""
    if not q:
        return []

    cache_key = f"search:{q}:{limit}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    res = await _genius_request("/search", {"q": q, "per_page": limit})
    hits = (res.get("response") or {}).get("hits") or []

    _set_cache(cache_key, hits)
    logger.debug(f"genius: search '{q}' returned {len(hits)} hits")
    return hits


async def get_song(song_id: int | str) -> dict | None:
    """Fetch full song details by Genius song ID."""
    if not song_id:
        return None

    cache_key = f"song:{song_id}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    res = await _genius_request(f"/songs/{song_id}")
    song = (res.get("response") or {}).get("song")
    if song:
        _set_cache(cache_key, song)
        logger.debug(f"genius: fetched song {song_id} title='{song.get('title')}'")
    return song


async def get_song_by_title_artist(title: str, artist: str, limit: int = 5) -> dict | None:
    """Best effort: search then pick best matching song by title/artist, then fetch full details."""
    if not title:
        return None

    query = f"{title} {artist}" if artist else title
    hits = await search_songs(query, limit=limit)

    norm_title = _normalize_name(title)
    norm_artist = _normalize_name(artist)

    best = None
    best_score = 0

    for hit in hits:
        result = hit.get("result") or {}
        hit_title = _normalize_name(result.get("title", ""))
        primary = result.get("primary_artist", {})
        hit_artist = _normalize_name(primary.get("name", ""))

        # Simple scoring
        score = 0
        if norm_title and norm_title in hit_title or hit_title in norm_title:
            score += 50
        if norm_artist and (norm_artist in hit_artist or hit_artist in norm_artist):
            score += 40
        if result.get("lyrics_state") == "complete":
            score += 10

        if score > best_score:
            best_score = score
            best = result

    if best and best.get("id"):
        # Fetch full for richer data (art, album, etc)
        full = await get_song(best["id"])
        return full or best

    return None


async def fetch_genius_cover_and_meta(title: str, artist: str) -> dict | None:
    """High-level helper for library enrichment.

    Returns normalized dict compatible with metadata.py expectations:
    {genius_id, title, artist, album, cover_url (song_art_image_url), genius_url, lyrics_state, ...}
    """
    if not (title or artist):
        return None
    if not GENIUS_ACCESS_TOKEN:
        return None

    try:
        song = await get_song_by_title_artist(title, artist)
        if not song:
            return None

        primary = song.get("primary_artist", {}) or {}
        album = song.get("album", {}) or {}

        # Genius has excellent song art (often official or high-res)
        cover = (
            song.get("song_art_image_url")
            or song.get("header_image_url")
            or (album.get("cover_art_url") if album else None)
            or ""
        )

        result = {
            "genius_id": song.get("id"),
            "title": song.get("title", title),
            "artist": primary.get("name", artist),
            "album": album.get("name", ""),
            "cover_url": cover,
            "genius_url": song.get("url", ""),
            "lyrics_state": song.get("lyrics_state", ""),
            "annotation_count": song.get("annotation_count", 0),
            "pageviews": (song.get("stats") or {}).get("pageviews"),
        }
        logger.debug(
            f"genius: enriched '{title}' by '{artist}' (id={result['genius_id']}, art={bool(cover)})"
        )
        return result
    except Exception as exc:
        logger.warning(f"genius: fetch failed for '{title}' / '{artist}': {exc}")
        return None
