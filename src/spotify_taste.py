"""Build taste profiles from Spotify user data or playlists."""
import asyncio
import json
import logging
import pathlib
import random
import re
import time
from dataclasses import dataclass, field

import spotipy

from src.config import TASTE_CACHE_TTL_SEC
from src.scoring import _format_spotify_track_query
from src.spotify import _parse_spotify_url

logger = logging.getLogger(__name__)

_CACHE_DIR = pathlib.Path(".cache/taste_profiles")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_SOURCE_WEIGHTS = {
    "liked": 0.40,
    "top": 0.30,
    "recent": 0.20,
    "artist_top": 0.10,
    "playlist": 1.0,
}


@dataclass
class TasteProfile:
    direct_tracks: list[dict] = field(default_factory=list)
    seed_track_ids: list[str] = field(default_factory=list)
    seed_genres: list[str] = field(default_factory=list)
    source_label: str = ""


def _track_to_taste_info(track: dict, source: str) -> dict | None:
    if not track or not track.get("id"):
        return None
    artists = track.get("artists") or [{}]
    return {
        "query": _format_spotify_track_query(track),
        "spotify_id": track.get("id"),
        "artist_id": artists[0].get("id"),
        "title": track.get("name", "?"),
        "artist": artists[0].get("name", "Unknown"),
        "source": source,
    }


async def fetch_liked_tracks(client: spotipy.Spotify, limit: int = 50) -> list[dict]:
    try:
        first = await asyncio.to_thread(lambda: client.current_user_saved_tracks(limit=1, offset=0))
        total = first.get("total") or 0
        if total <= 0:
            return []
        offset = random.randint(0, max(0, total - limit))
        result = await asyncio.to_thread(
            lambda: client.current_user_saved_tracks(limit=limit, offset=offset)
        )
        tracks = []
        for item in result.get("items") or []:
            info = _track_to_taste_info(item.get("track"), "liked")
            if info:
                tracks.append(info)
        return tracks
    except Exception as exc:
        logger.warning("spotify_taste: liked tracks failed: %s", exc)
        return []


async def fetch_top_tracks(client: spotipy.Spotify, limit: int = 20) -> list[dict]:
    tracks = []
    for time_range in ("short_term", "medium_term"):
        try:
            result = await asyncio.to_thread(
                lambda tr=time_range: client.current_user_top_tracks(limit=min(limit, 10), time_range=tr)
            )
            for track in result.get("items") or []:
                info = _track_to_taste_info(track, "top")
                if info:
                    tracks.append(info)
        except Exception as exc:
            logger.warning("spotify_taste: top tracks (%s) failed: %s", time_range, exc)
    return tracks


async def fetch_recent_tracks(client: spotipy.Spotify, limit: int = 20) -> list[dict]:
    try:
        result = await asyncio.to_thread(lambda: client.current_user_recently_played(limit=limit))
        tracks = []
        for item in result.get("items") or []:
            info = _track_to_taste_info(item.get("track"), "recent")
            if info:
                tracks.append(info)
        return tracks
    except Exception as exc:
        logger.warning("spotify_taste: recent tracks failed: %s", exc)
        return []


async def fetch_top_artist_tracks(client: spotipy.Spotify, limit: int = 10) -> tuple[list[dict], list[str]]:
    tracks: list[dict] = []
    genres: list[str] = []
    try:
        artists_result = await asyncio.to_thread(
            lambda: client.current_user_top_artists(limit=5, time_range="medium_term")
        )
        for artist in artists_result.get("items") or []:
            for genre in artist.get("genres") or []:
                if genre not in genres:
                    genres.append(genre)
            artist_id = artist.get("id")
            if not artist_id:
                continue
            top = await asyncio.to_thread(lambda aid=artist_id: client.artist_top_tracks(aid, country="US"))
            for track in (top.get("tracks") or [])[:2]:
                info = _track_to_taste_info(track, "artist_top")
                if info:
                    tracks.append(info)
                if len(tracks) >= limit:
                    break
            if len(tracks) >= limit:
                break
    except Exception as exc:
        logger.warning("spotify_taste: top artist tracks failed: %s", exc)
    return tracks, genres[:5]


async def fetch_playlist_tracks(client: spotipy.Spotify, playlist_id: str, limit: int = 100) -> list[dict]:
    tracks: list[dict] = []
    offset = 0
    page_size = 50
    try:
        while len(tracks) < limit:
            result = await asyncio.to_thread(
                lambda off=offset: client.playlist_items(
                    playlist_id, offset=off, limit=page_size, market="from_token"
                )
            )
            items = result.get("items") or []
            if not items:
                break
            for item in items:
                info = _track_to_taste_info(item.get("track"), "playlist")
                if info:
                    tracks.append(info)
                if len(tracks) >= limit:
                    break
            if len(items) < page_size:
                break
            offset += page_size
    except Exception as exc:
        logger.warning("spotify_taste: playlist %s failed: %s", playlist_id, exc)
    return tracks


def _dedupe_tracks(tracks: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for track in tracks:
        sid = track.get("spotify_id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        unique.append(track)
    return unique


def _weighted_sample(pools: dict[str, list[dict]], total: int = 80) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    active_sources = [src for src, items in pools.items() if items]
    if not active_sources:
        return []

    attempts = 0
    while len(selected) < total and attempts < total * 4:
        attempts += 1
        source = random.choices(
            active_sources,
            weights=[_SOURCE_WEIGHTS.get(src, 0.1) for src in active_sources],
            k=1,
        )[0]
        pool = pools[source]
        track = random.choice(pool)
        sid = track.get("spotify_id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        selected.append(track)
    return selected


def _build_seeds(tracks: list[dict], genres: list[str]) -> tuple[list[str], list[str]]:
    seed_ids = []
    pool = list(tracks)
    random.shuffle(pool)
    for track in pool:
        sid = track.get("spotify_id")
        if sid and sid not in seed_ids:
            seed_ids.append(sid)
        if len(seed_ids) >= 5:
            break
    seed_genres = []
    for genre in genres:
        if " " not in genre and genre not in seed_genres:
            seed_genres.append(genre)
        if len(seed_genres) >= 2:
            break
    return seed_ids[:5], seed_genres[:2]


async def build_user_taste_profile(client: spotipy.Spotify, label: str = "user") -> TasteProfile:
    liked, top, recent, (artist_top, genres) = await asyncio.gather(
        fetch_liked_tracks(client),
        fetch_top_tracks(client),
        fetch_recent_tracks(client),
        fetch_top_artist_tracks(client),
    )
    pools = {
        "liked": liked,
        "top": top,
        "recent": recent,
        "artist_top": artist_top,
    }
    direct = _dedupe_tracks(_weighted_sample(pools))
    seed_track_ids, seed_genres = _build_seeds(direct, genres)
    return TasteProfile(
        direct_tracks=direct,
        seed_track_ids=seed_track_ids,
        seed_genres=seed_genres,
        source_label=label,
    )


async def build_playlist_taste_profile(
    client: spotipy.Spotify,
    playlist_id: str,
    label: str = "playlist",
) -> TasteProfile:
    tracks = _dedupe_tracks(await fetch_playlist_tracks(client, playlist_id))
    random.shuffle(tracks)
    seed_track_ids, _seed_genres = _build_seeds(tracks, [])
    return TasteProfile(
        direct_tracks=tracks,
        seed_track_ids=seed_track_ids,
        seed_genres=[],
        source_label=label,
    )


def merge_taste_profiles(profiles: list[TasteProfile], label: str = "voice") -> TasteProfile:
    if not profiles:
        return TasteProfile(source_label=label)
    if len(profiles) == 1:
        profile = profiles[0]
        profile.source_label = label
        return profile

    direct: list[dict] = []
    seed_ids: list[str] = []
    seed_genres: list[str] = []
    seen_ids: set[str] = set()

    for profile in profiles:
        for track in profile.direct_tracks:
            sid = track.get("spotify_id")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                direct.append(track)
        for sid in profile.seed_track_ids:
            if sid not in seed_ids:
                seed_ids.append(sid)
        for genre in profile.seed_genres:
            if genre not in seed_genres:
                seed_genres.append(genre)

    random.shuffle(direct)
    return TasteProfile(
        direct_tracks=direct,
        seed_track_ids=seed_ids[:5],
        seed_genres=seed_genres[:2],
        source_label=label,
    )


def _cache_path(cache_key: str) -> pathlib.Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cache_key)
    return _CACHE_DIR / f"{safe}.json"


def _load_cached_profile(cache_key: str) -> TasteProfile | None:
    path = _cache_path(cache_key)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data.get("cached_at", 0) > TASTE_CACHE_TTL_SEC:
            return None
        return TasteProfile(
            direct_tracks=data.get("direct_tracks", []),
            seed_track_ids=data.get("seed_track_ids", []),
            seed_genres=data.get("seed_genres", []),
            source_label=data.get("source_label", ""),
        )
    except Exception as exc:
        logger.warning("spotify_taste: cache read failed for %s: %s", cache_key, exc)
        return None


def _save_cached_profile(cache_key: str, profile: TasteProfile) -> None:
    path = _cache_path(cache_key)
    try:
        path.write_text(json.dumps({
            "cached_at": time.time(),
            "direct_tracks": profile.direct_tracks,
            "seed_track_ids": profile.seed_track_ids,
            "seed_genres": profile.seed_genres,
            "source_label": profile.source_label,
        }, indent=2))
    except Exception as exc:
        logger.warning("spotify_taste: cache write failed for %s: %s", cache_key, exc)


async def get_cached_profile(cache_key: str, builder) -> TasteProfile:
    cached = _load_cached_profile(cache_key)
    if cached and cached.direct_tracks:
        return cached
    profile = await builder()
    if profile.direct_tracks or profile.seed_track_ids:
        _save_cached_profile(cache_key, profile)
    return profile


def parse_playlist_id(value: str) -> str | None:
    value = value.strip()
    parsed = _parse_spotify_url(value)
    if parsed and parsed["type"] == "playlist":
        return parsed["id"]
    if re_fullmatch_spotify_id(value):
        return value
    return None


def re_fullmatch_spotify_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{22}", value))