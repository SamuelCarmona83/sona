"""Shazam catalog helpers (search, related, charts) via shazamio.

Used to diversify radio fill and to refine free-text song requests when Spotify
is weak or wrong. All network calls are optional: failures return empty lists.
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_PATH = pathlib.Path(".cache/shazam_catalog.json")
_CACHE_TTL_SEC = 6 * 3600
_SEM = asyncio.Semaphore(2)
_client = None
_cache: dict[str, Any] = {"v": 1, "entries": {}}
_cache_loaded = False
# After repeated JSON/network failures, skip Shazam for this process (Spotify/fallbacks take over).
_fail_streak = 0
_FAIL_STREAK_DISABLE = 3

# Map free-text / Spotify-ish genre tags → shazamio GenreMusic values.
_GENRE_ALIASES: dict[str, str] = {
    "pop": "pop",
    "dance-pop": "pop",
    "hiphop": "hip-hop-rap",
    "hip-hop": "hip-hop-rap",
    "hip hop": "hip-hop-rap",
    "rap": "hip-hop-rap",
    "trap": "hip-hop-rap",
    "dance": "dance",
    "electronic": "electronic",
    "techno": "electronic",
    "edm": "electronic",
    "house": "house",
    "dubstep": "electronic",
    "rnb": "randb-soul",
    "r&b": "randb-soul",
    "soul": "randb-soul",
    "alternative": "alternative",
    "indie": "alternative",
    "rock": "rock",
    "metal": "rock",
    "hard-rock": "rock",
    "punk": "rock",
    "latin": "latin",
    "reggaeton": "latin",
    "salsa": "latin",
    "country": "country",
    "folk": "country",
    "reggae": "reggae-dancehall",
    "dancehall": "reggae-dancehall",
    "k-pop": "k-pop",
    "kpop": "k-pop",
    "afrobeats": "afrobeats",
    "worldwide": "worldwide",
    "singer-songwriter": "singer-songwriter",
    "regional-mexicano": "regional-mexicano",
    "mexican": "regional-mexicano",
}

# Mood / free-text → ISO country for charts (optional).
_COUNTRY_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"venezol|venezuela|\bve\b", re.I), "VE"),
    (re.compile(r"argentin|\bar\b", re.I), "AR"),
    (re.compile(r"mexico|m[eé]xico|\bmx\b", re.I), "MX"),
    (re.compile(r"colombia|\bco\b", re.I), "CO"),
    (re.compile(r"chile|\bcl\b", re.I), "CL"),
    (re.compile(r"spain|espa[nñ]a|\bes\b", re.I), "ES"),
    (re.compile(r"brazil|brasil|\bbr\b", re.I), "BR"),
    (re.compile(r"united states|usa|\bus\b", re.I), "US"),
    (re.compile(r"uk|united kingdom|britain", re.I), "GB"),
]


def _load_cache() -> None:
    global _cache_loaded, _cache
    if _cache_loaded:
        return
    _cache_loaded = True
    if not _CACHE_PATH.exists():
        return
    try:
        data = json.loads(_CACHE_PATH.read_text())
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            _cache = data
    except Exception as exc:
        logger.debug("shazam_catalog: cache load failed: %s", exc)


def _save_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Cap entries
        entries = _cache.get("entries") or {}
        if len(entries) > 400:
            items = sorted(entries.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
            _cache["entries"] = dict(items[:300])
        _CACHE_PATH.write_text(json.dumps(_cache))
    except Exception as exc:
        logger.debug("shazam_catalog: cache save failed: %s", exc)


def _cache_get(key: str) -> Any | None:
    _load_cache()
    entry = (_cache.get("entries") or {}).get(key)
    if not entry:
        return None
    if time.time() - float(entry.get("ts") or 0) > _CACHE_TTL_SEC:
        return None
    return entry.get("data")


def _cache_set(key: str, data: Any) -> None:
    _load_cache()
    _cache.setdefault("entries", {})[key] = {"ts": time.time(), "data": data}
    _save_cache()


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        from shazamio import Shazam

        _client = Shazam()
        return _client
    except Exception as exc:
        logger.warning("shazam_catalog: cannot import/init shazamio: %s", exc)
        return None


def map_genre_tag(tag: str) -> str | None:
    raw = (tag or "").strip().lower().replace("_", "-")
    if not raw:
        return None
    if raw in _GENRE_ALIASES:
        return _GENRE_ALIASES[raw]
    # substring match
    for key, val in _GENRE_ALIASES.items():
        if key in raw or raw in key:
            return val
    return None


def detect_country(*texts: str) -> str | None:
    blob = " ".join(t for t in texts if t)
    if not blob:
        return None
    for pat, code in _COUNTRY_HINTS:
        if pat.search(blob):
            return code
    return None


def _as_int_id(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _images_cover(images: Any) -> str | None:
    if not isinstance(images, dict):
        return None
    for key in ("coverarthq", "coverart", "background", "default"):
        url = images.get(key)
        if url:
            return str(url)
    return None


def _normalize_hit(raw: dict) -> dict | None:
    """Normalize heterogeneous Shazam payloads into a stable track dict."""
    if not isinstance(raw, dict):
        return None

    # Nested track (search hits, recognize)
    track = raw.get("track") if isinstance(raw.get("track"), dict) else raw

    # Apple Music style playlist item
    attrs = track.get("attributes") if isinstance(track.get("attributes"), dict) else None
    if attrs and attrs.get("name"):
        title = (attrs.get("name") or "").strip()
        artist = (attrs.get("artistName") or attrs.get("artist_name") or "").strip()
        sid = _as_int_id(track.get("id") or raw.get("id"))
        cover = None
        art = attrs.get("artwork")
        if isinstance(art, dict):
            cover = art.get("url") or art.get("bgColor")
            if cover and "{w}" in str(cover):
                cover = str(cover).replace("{w}", "400").replace("{h}", "400")
        if not title:
            return None
        query = f"{artist} - {title}" if artist else title
        return {
            "title": title,
            "artist": artist or "Unknown",
            "query": query,
            "shazam_id": sid,
            "cover_url": cover,
            "spotify_id": None,
            "artist_id": None,
        }

    title = (track.get("title") or track.get("name") or "").strip()
    artist = (
        track.get("subtitle")
        or track.get("artist")
        or track.get("artistName")
        or ""
    )
    if isinstance(artist, dict):
        artist = artist.get("name") or ""
    artist = (artist or "").strip()
    if not title:
        return None

    sid = _as_int_id(track.get("key") or track.get("id") or raw.get("key") or raw.get("id"))
    if sid is None and isinstance(track.get("hub"), dict):
        actions = track["hub"].get("actions") or []
        if actions and isinstance(actions[0], dict):
            sid = _as_int_id(actions[0].get("id"))

    cover = _images_cover(track.get("images")) or _images_cover(raw.get("images"))
    query = f"{artist} - {title}" if artist else title
    return {
        "title": title,
        "artist": artist or "Unknown",
        "query": query,
        "shazam_id": sid,
        "cover_url": cover,
        "spotify_id": None,
        "artist_id": None,
    }


def _extract_track_list(payload: Any) -> list[dict]:
    """Pull track-like dicts from any known Shazam response shape."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if not isinstance(payload, dict):
        return []

    out: list[dict] = []

    # search_track: {"tracks": {"hits": [{"track": {...}}]}}
    tracks = payload.get("tracks")
    if isinstance(tracks, dict):
        hits = tracks.get("hits")
        if isinstance(hits, list):
            for hit in hits:
                if isinstance(hit, dict):
                    out.append(hit.get("track") if isinstance(hit.get("track"), dict) else hit)
        # related sometimes: {"tracks": [ {...}, ... ]}
    elif isinstance(tracks, list):
        out.extend(t for t in tracks if isinstance(t, dict))

    # charts / AM: {"data": [ playlist items ]}
    data = payload.get("data")
    if isinstance(data, list):
        out.extend(t for t in data if isinstance(t, dict))

    # some endpoints wrap again
    if not out and isinstance(payload.get("track"), dict):
        out.append(payload["track"])

    return out


def normalize_payload(payload: Any, *, limit: int = 20) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for raw in _extract_track_list(payload):
        item = _normalize_hit(raw)
        if not item:
            continue
        key = (item.get("query") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        results.append(item)
        if len(results) >= limit:
            break
    return results


def shazam_available() -> bool:
    """False when recent Shazam calls failed repeatedly (e.g. JSON decode errors)."""
    return _fail_streak < _FAIL_STREAK_DISABLE


async def _call(label: str, coro_factory, cache_key: str | None = None) -> Any:
    global _fail_streak
    if not shazam_available():
        return None
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    client = _get_client()
    if client is None:
        return None
    async with _SEM:
        try:
            result = await asyncio.wait_for(coro_factory(client), timeout=8.0)
        except Exception as exc:
            _fail_streak += 1
            logger.info(
                "shazam_catalog: %s failed: %s (fail_streak=%s)",
                label, exc, _fail_streak,
            )
            if _fail_streak >= _FAIL_STREAK_DISABLE:
                logger.warning(
                    "shazam_catalog: disabled for this process after %s failures "
                    "(using Spotify/curated fallbacks)",
                    _fail_streak,
                )
            return None
    _fail_streak = 0  # success resets
    if cache_key and result is not None:
        _cache_set(cache_key, result)
    return result


async def search_tracks(query: str, *, limit: int = 8) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    cache_key = f"search:{q.lower()}:{limit}"
    payload = await _call(
        f"search_track({q!r})",
        lambda c: c.search_track(query=q, limit=limit),
        cache_key=cache_key,
    )
    return normalize_payload(payload, limit=limit)


async def best_search_match(query: str) -> dict | None:
    """Best Shazam hit for a free-text query, or None."""
    hits = await search_tracks(query, limit=5)
    return hits[0] if hits else None


async def related_tracks(shazam_id: int | str, *, limit: int = 12) -> list[dict]:
    tid = _as_int_id(shazam_id)
    if tid is None:
        return []
    cache_key = f"related:{tid}:{limit}"
    payload = await _call(
        f"related({tid})",
        lambda c, t=tid: c.related_tracks(track_id=t, limit=limit),
        cache_key=cache_key,
    )
    return normalize_payload(payload, limit=limit)


async def top_genre_tracks(
    genre_tag: str,
    *,
    country: str | None = None,
    limit: int = 15,
    offset: int = 0,
) -> list[dict]:
    genre = map_genre_tag(genre_tag)
    if not genre:
        return []
    cache_key = f"genre:{country or 'world'}:{genre}:{limit}:{offset}"

    async def _run(client):
        if country:
            return await client.top_country_genre_tracks(
                country_code=country.upper(),
                genre=genre,
                limit=limit,
                offset=offset,
            )
        return await client.top_world_genre_tracks(
            genre=genre, limit=limit, offset=offset
        )

    payload = await _call(
        f"top_genre({genre}, country={country})",
        _run,
        cache_key=cache_key,
    )
    return normalize_payload(payload, limit=limit)


async def top_country_tracks(country: str, *, limit: int = 15, offset: int = 0) -> list[dict]:
    code = (country or "").strip().upper()
    if len(code) != 2:
        return []
    cache_key = f"country:{code}:{limit}:{offset}"
    payload = await _call(
        f"top_country({code})",
        lambda c: c.top_country_tracks(country_code=code, limit=limit, offset=offset),
        cache_key=cache_key,
    )
    return normalize_payload(payload, limit=limit)


def to_radio_info(item: dict) -> dict:
    """Shape compatible with radio fill / Spotify hybrid recs."""
    return {
        "query": item.get("query") or item.get("title") or "",
        "title": item.get("title"),
        "artist": item.get("artist") or "Unknown",
        "spotify_id": item.get("spotify_id"),
        "artist_id": item.get("artist_id"),
        "shazam_id": item.get("shazam_id"),
        "cover_url": item.get("cover_url"),
    }


async def radio_seed_recs(
    *,
    seed_genres: list[str] | None = None,
    mood_name: str | None = None,
    recent_shazam_ids: list[int] | None = None,
    recent_queries: list[str] | None = None,
    limit: int = 12,
) -> list[dict]:
    """Build a diversified recommendation list from Shazam charts + related.

    Returns radio-info dicts (query, artist, shazam_id, ...).
    """
    import random as _random

    seed_genres = list(seed_genres or [])
    recent_shazam_ids = list(recent_shazam_ids or [])
    recent_queries = list(recent_queries or [])
    country = detect_country(mood_name or "", " ".join(seed_genres))

    collected: list[dict] = []
    seen: set[str] = set()

    def _add_many(items: list[dict]) -> None:
        for it in items:
            info = to_radio_info(it)
            q = (info.get("query") or "").lower()
            if not q or q in seen:
                continue
            seen.add(q)
            collected.append(info)
            if len(collected) >= limit * 2:
                return

    # 1) Related to recently recognized / resolved tracks
    for sid in recent_shazam_ids[:3]:
        if len(collected) >= limit * 2:
            break
        _add_many(await related_tracks(sid, limit=min(10, limit)))

    # 2) If we only have text history, search once then related
    if not recent_shazam_ids and recent_queries:
        hit = await best_search_match(recent_queries[0])
        if hit and hit.get("shazam_id"):
            _add_many([hit])
            _add_many(await related_tracks(hit["shazam_id"], limit=min(10, limit)))

    # 3) Genre / country charts from mood seeds
    genres_to_try = []
    for g in seed_genres:
        mapped = map_genre_tag(g)
        if mapped and mapped not in genres_to_try:
            genres_to_try.append(mapped)
    if not genres_to_try and mood_name:
        mapped = map_genre_tag(mood_name)
        if mapped:
            genres_to_try.append(mapped)
    if not genres_to_try:
        # free-text mood like "rock venezolano"
        blob = f"{mood_name or ''} {' '.join(seed_genres)}".lower()
        if "rock" in blob:
            genres_to_try.append("rock")
        elif "pop" in blob:
            genres_to_try.append("pop")
        elif "latin" in blob or "reggaeton" in blob:
            genres_to_try.append("latin")

    offset = _random.randint(0, 30)
    for g in genres_to_try[:2]:
        if len(collected) >= limit * 2:
            break
        # Prefer country chart when we detected locale (e.g. VE)
        if country:
            _add_many(
                await top_genre_tracks(g, country=country, limit=limit, offset=offset)
            )
        _add_many(await top_genre_tracks(g, country=None, limit=limit, offset=offset))

    if country and len(collected) < limit:
        _add_many(await top_country_tracks(country, limit=limit, offset=offset))

    _random.shuffle(collected)
    logger.info(
        "shazam_catalog: radio_seed_recs n=%d country=%s genres=%s related_ids=%d",
        len(collected[:limit]),
        country,
        genres_to_try[:2],
        len(recent_shazam_ids),
    )
    return collected[:limit]
