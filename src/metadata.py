"""Metadata enrichment helpers for official artwork, album info, and covers.

Spotify-first (when spotify_id or matchable), Last.fm fallback, YouTube as last resort.
Provides cover download to local files and best-image selection.
Designed to be called from library enrichment paths; keeps heavy API logic in source modules.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import Any

import requests

from src.config import LIBRARY_PATH

logger = logging.getLogger(__name__)

# Covers stored next to library audio for self-contained offline library
_COVERS_DIR = pathlib.Path(LIBRARY_PATH) / "covers"
_COVERS_DIR.mkdir(parents=True, exist_ok=True)


def get_covers_dir() -> pathlib.Path:
    return _COVERS_DIR


def pick_best_image(images: list[dict] | None) -> str | None:
    """Return the best (largest/official) image URL from Spotify album.images or Last.fm image list.

    Spotify images: list of {"url": "...", "width": 640, "height": 640} (usually largest first).
    Last.fm: list of {"#text": url, "size": "extralarge"}.
    """
    if not images:
        return None
    # Spotify style (has width)
    candidates = []
    for img in images:
        url = img.get("url") or img.get("#text") or ""
        if not url:
            continue
        width = img.get("width") or 0
        if isinstance(width, str):
            try:
                width = int(width)
            except Exception:
                width = 0
        candidates.append((width, url))
    if candidates:
        candidates.sort(reverse=True)  # largest width first
        return candidates[0][1]
    # Fallback: just first non-empty
    for img in images:
        url = img.get("url") or img.get("#text") or ""
        if url:
            return url
    return None


async def download_image(url: str, dest: pathlib.Path, *, timeout: int = 15) -> bool:
    """Download image (sync via thread) and save to dest. Returns True on success."""
    if not url:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _do():
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "spoty-scanner/1.0"})
            resp.raise_for_status()
            if len(resp.content) < 100:  # too small, probably error page
                return False
            dest.write_bytes(resp.content)
            return True
        except Exception as exc:
            logger.warning("metadata: cover download failed for %s -> %s: %s", url, dest, exc)
            return False

    return await asyncio.to_thread(_do)


def get_local_cover_path(tid: str) -> pathlib.Path:
    return _COVERS_DIR / f"{tid}.jpg"


# Light in-memory cache for Spotify track/album metadata to reduce API pressure during a run
_spotify_meta_cache: dict[str, dict] = {}


def _get_cached_spotify_meta(key: str) -> dict | None:
    return _spotify_meta_cache.get(key)


def _set_cached_spotify_meta(key: str, data: dict) -> None:
    _spotify_meta_cache[key] = data


async def fetch_spotify_cover_and_meta(spotify_id: str) -> dict[str, Any] | None:
    """Fetch full track + album info via Spotify. Returns normalized dict or None.

    Uses src.spotify; caches lightly in-process.
    Expected keys in return: album, release_date, cover_url, genres (from artist), artists, ...
    """
    from src.spotify import sp, _spotify_api_ready, _get_artist_genres

    if not spotify_id:
        return None
    cached = _get_cached_spotify_meta(f"track:{spotify_id}")
    if cached:
        return cached

    if not await _spotify_api_ready():
        return None
    try:
        track = await asyncio.to_thread(lambda: sp.track(spotify_id))
        if not track:
            return None
        album = track.get("album") or {}
        images = album.get("images") or []
        cover_url = pick_best_image(images)
        artist_ids = [a.get("id") for a in track.get("artists", []) if a.get("id")]
        genres: list[str] = []
        for aid in artist_ids[:2]:  # limit calls
            try:
                g = await _get_artist_genres(aid)
                for gg in g:
                    if gg not in genres:
                        genres.append(gg)
            except Exception:
                pass

        result = {
            "spotify_id": track.get("id"),
            "title": track.get("name", ""),
            "artists": [a.get("name", "") for a in track.get("artists", [])],
            "artist": ", ".join(a.get("name", "") for a in track.get("artists", [])),
            "album": album.get("name", ""),
            "album_id": album.get("id"),
            "release_date": album.get("release_date", ""),
            "cover_url": cover_url,
            "genres": genres,
            "duration_ms": track.get("duration_ms"),
            "isrc": (track.get("external_ids") or {}).get("isrc"),
        }
        _set_cached_spotify_meta(f"track:{spotify_id}", result)
        logger.debug("metadata: fetched Spotify meta for %s (cover=%s)", spotify_id, bool(cover_url))
        return result
    except Exception as exc:
        logger.warning("metadata: Spotify fetch failed for track %s: %s", spotify_id, exc)
        return None


async def try_attach_spotify_id(title: str, artist: str) -> dict[str, Any] | None:
    """Attempt to find a Spotify track match for title/artist using existing refinement + scoring.

    Returns a dict with spotify_id + basic fields if confident match, else None.
    Reuses spotify.py scoring to stay consistent.
    """
    from src.spotify import _get_spotify_track_info, _spotify_api_ready, sp
    from src.scoring import _score_spotify_match, _format_spotify_track_query

    if not title:
        return None
    if not await _spotify_api_ready():
        return None

    query = f"{artist} - {title}" if artist else title
    try:
        info = await _get_spotify_track_info(query)
        sid = info.get("spotify_id")
        if not sid:
            return None
        # Verify confidence with direct lookup + score
        full = await asyncio.to_thread(lambda: sp.track(sid))
        if not full:
            return None
        score = _score_spotify_match(query, full)
        if score < 7.0:  # conservative reuse of refinement spirit
            logger.debug("metadata: attach candidate '%s' score %.2f too low", _format_spotify_track_query(full), score)
            return None
        # Return minimal attach info; caller can call full enrich
        album = full.get("album") or {}
        return {
            "spotify_id": sid,
            "title": full.get("name", title),
            "artist": ", ".join(a.get("name", "") for a in full.get("artists", [])),
            "album": album.get("name", ""),
            "cover_url": pick_best_image(album.get("images") or []),
            "release_date": album.get("release_date", ""),
        }
    except Exception as exc:
        logger.debug("metadata: attach spotify failed for '%s': %s", query, exc)
        return None


async def fetch_lastfm_album_cover_and_meta(artist: str, album: str) -> dict[str, Any] | None:
    """Last.fm fallback for album art + tags. Returns similar shape to spotify result."""
    from src.lastfm import get_album_info

    if not artist or not album:
        return None
    try:
        info = await get_album_info(artist, album)
        if not info:
            return None
        # Reuse pick_best_image shape if needed; here image is already resolved url
        cover_url = info.get("image") or None
        return {
            "title": info.get("name", album),
            "artist": info.get("artist", artist),
            "album": info.get("name", album),
            "cover_url": cover_url,
            "genres": info.get("tags", []),
            "url": info.get("url", ""),
        }
    except Exception as exc:
        logger.debug("metadata: lastfm album info failed for %s - %s: %s", artist, album, exc)
        return None


async def fetch_genius_cover_and_meta(title: str, artist: str) -> dict[str, Any] | None:
    """Genius tier for artwork (song_art_image_url often official/high-res) + metadata.

    Complements Spotify (primary) and Last.fm. Uses title+artist search.
    """
    from src.genius import fetch_genius_cover_and_meta as _fetch_genius

    if not title:
        return None
    try:
        g = await _fetch_genius(title, artist)
        if not g:
            # This can happen if no GENIUS_ACCESS_TOKEN, no good search match, or API returned nothing useful
            logger.info("metadata: Genius returned no data for '%s' by '%s'", title, artist)
            return None
        # Normalize to common shape used by enrichment
        return {
            "genius_id": g.get("genius_id"),
            "title": g.get("title", title),
            "artist": g.get("artist", artist),
            "album": g.get("album", ""),
            "cover_url": g.get("cover_url", ""),
            "genius_url": g.get("genius_url", ""),
            "lyrics_state": g.get("lyrics_state", ""),
            "pageviews": g.get("pageviews"),
        }
    except Exception as exc:
        logger.warning("metadata: genius fetch failed for '%s' / '%s': %s", title, artist, exc)
        return None
