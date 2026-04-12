"""Smart Radio Mode

Per-guild state for 24/7 radio. When active:
  - Queue auto-fills to RADIO_QUEUE_MIN tracks using Spotify recommendations.
  - Recommendations are seeded by a diversity-aware mix of play history + active mood.
  - User !play requests are front-queued (handled in commands.py).
  - Bot stays connected when the queue empties (fill triggers instead of disconnect).
"""
import asyncio
import collections
import json
import logging
import pathlib

import discord

from src.config import RADIO_QUEUE_MIN, RADIO_FILL_COUNT, LLM_ENABLED_FOR_ALBUM_TRACKS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mood → Spotify genre seeds
# ---------------------------------------------------------------------------

MOODS: dict[str, list[str]] = {
    "neutral":  [],
    "mixed":    [],
    "metal":    ["metal", "heavy-metal", "thrash-metal"],
    "hiphop":   ["hip-hop", "rap", "trap"],
    "techno":   ["techno", "minimal-techno", "electronic"],
    "chill":    ["chill", "lo-fi", "ambient"],
    "rock":     ["rock", "alternative", "indie"],
    "pop":      ["pop", "dance-pop"],
    "latin":    ["latin", "reggaeton"],
    "jazz":     ["jazz", "soul", "blues"],
    "classical":["classical", "piano"],
    "reggae":   ["reggae", "dub"],
    "country":  ["country", "folk"],
}

# Macro genre clusters: raw Spotify genre tags → cluster key.
# Used to measure diversity in play history and avoid locking onto one genre.
_GENRE_CLUSTER_MAP: dict[str, str] = {
    # Metal family
    "metal": "metal", "heavy-metal": "metal", "thrash-metal": "metal",
    "death-metal": "metal", "black-metal": "metal", "metalcore": "metal",
    "hard-rock": "metal", "nu-metal": "metal",
    # Hip-hop family
    "hip-hop": "hiphop", "rap": "hiphop", "trap": "hiphop",
    "r-n-b": "hiphop", "soul": "hiphop",
    # Electronic / techno family
    "techno": "techno", "minimal-techno": "techno", "electronic": "techno",
    "house": "techno", "trance": "techno", "edm": "techno",
    "ambient": "techno", "idm": "techno",
    # Rock family
    "rock": "rock", "alternative": "rock", "indie": "rock",
    "punk": "rock", "emo": "rock", "grunge": "rock", "post-rock": "rock",
    # Pop family
    "pop": "pop", "dance-pop": "pop", "electropop": "pop",
    "k-pop": "pop", "j-pop": "pop", "synth-pop": "pop",
    # Chill / lo-fi
    "chill": "chill", "lo-fi": "chill", "study": "chill",
    "new-age": "chill", "sleep": "chill",
    # Latin
    "latin": "latin", "reggaeton": "latin", "salsa": "latin",
    "cumbia": "latin", "bachata": "latin",
    # Jazz / classical / other
    "jazz": "jazz", "blues": "jazz",
    "classical": "classical", "piano": "classical", "opera": "classical",
    "reggae": "reggae", "dub": "reggae",
    "country": "country", "folk": "country", "bluegrass": "country",
}


def _map_cluster(genres: list[str]) -> str | None:
    for g in genres:
        cluster = _GENRE_CLUSTER_MAP.get(g)
        if cluster:
            return cluster
    return None


# ---------------------------------------------------------------------------
# Per-guild state
# ---------------------------------------------------------------------------

_radio_active: dict[int, bool] = {}
_radio_mood:   dict[int, str]  = {}   # mood name (key of MOODS), default "neutral"
# Play history entries: {spotify_id, artist_id, cluster}
_play_history: dict[int, collections.deque] = {}
# Concurrency guard: prevent overlapping fill tasks per guild
_filling:      dict[int, bool] = {}

# ---------------------------------------------------------------------------
# Persistent played IDs (avoid repeating radio recommendations across sessions)
# ---------------------------------------------------------------------------
_PLAYED_IDS_PATH = pathlib.Path(".cache/played_ids.json")
_PLAYED_IDS_MAX  = 1000
_played_ids: dict[int, list[str]] = {}

# Custom moods per guild (guild_id → {mood_name → [genre_tags]})
_CUSTOM_MOODS_PATH = pathlib.Path(".cache/custom_moods.json")
_custom_moods: dict[int, dict[str, list[str]]] = {}

# Ensure cache directory exists
_PLAYED_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load_played_ids() -> None:
    if _PLAYED_IDS_PATH.exists():
        try:
            data = json.loads(_PLAYED_IDS_PATH.read_text())
            for gid_str, ids in data.items():
                _played_ids[int(gid_str)] = list(ids)[-_PLAYED_IDS_MAX:]
        except Exception:
            pass


def _save_played_ids() -> None:
    try:
        _PLAYED_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {str(gid): ids for gid, ids in _played_ids.items()}
        _PLAYED_IDS_PATH.write_text(json.dumps(data))
    except Exception as exc:
        logger.warning("radio: could not save played_ids: %s", exc)


def _load_custom_moods() -> None:
    if _CUSTOM_MOODS_PATH.exists():
        try:
            data = json.loads(_CUSTOM_MOODS_PATH.read_text())
            for gid_str, moods in data.items():
                _custom_moods[int(gid_str)] = moods
        except Exception:
            pass


def _save_custom_moods() -> None:
    try:
        _CUSTOM_MOODS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {str(gid): moods for gid, moods in _custom_moods.items()}
        _CUSTOM_MOODS_PATH.write_text(json.dumps(data))
    except Exception as exc:
        logger.warning("radio: could not save custom_moods: %s", exc)


_load_played_ids()
_load_custom_moods()


def is_radio_active(guild_id: int) -> bool:
    return _radio_active.get(guild_id, False)


def set_radio_active(guild_id: int, active: bool) -> None:
    _radio_active[guild_id] = active
    if not active:
        _filling.pop(guild_id, None)


def get_mood(guild_id: int) -> str:
    return _radio_mood.get(guild_id, "neutral")


def set_mood(guild_id: int, mood: str) -> None:
    all_moods = {**MOODS, **_custom_moods.get(guild_id, {})}
    if mood not in all_moods:
        raise ValueError(f"Mood desconocido: '{mood}'. Disponibles: {', '.join(all_moods)}")
    _radio_mood[guild_id] = mood


def create_custom_mood(guild_id: int, name: str, genres: list[str]) -> None:
    if name in MOODS:
        raise ValueError(f"'{name}' es un mood built-in y no puede ser sobreescrito.")
    _custom_moods.setdefault(guild_id, {})[name] = genres
    _save_custom_moods()


def delete_custom_mood(guild_id: int, name: str) -> None:
    guild_moods = _custom_moods.get(guild_id, {})
    if name not in guild_moods:
        raise ValueError(f"No existe un mood custom llamado '{name}'.")
    del guild_moods[name]
    _save_custom_moods()


async def record_played(guild_id: int, track: dict) -> None:
    """Record a track in the guild's play history for diversity seeding."""
    if guild_id not in _play_history:
        _play_history[guild_id] = collections.deque(maxlen=20)

    artist_id  = track.get("artist_id")
    spotify_id = track.get("spotify_id")
    cluster    = None

    if artist_id:
        from src.spotify import _get_artist_genres
        genres = await _get_artist_genres(artist_id)
        cluster = _map_cluster(genres)

    _play_history[guild_id].append({
        "spotify_id": spotify_id,
        "artist_id":  artist_id,
        "cluster":    cluster,
    })
    if spotify_id:
        ids = _played_ids.setdefault(guild_id, [])
        ids.append(spotify_id)
        if len(ids) > _PLAYED_IDS_MAX:
            _played_ids[guild_id] = ids[-_PLAYED_IDS_MAX:]
        _save_played_ids()
    logger.debug(
        "radio.record_played: guild=%s track='%s' cluster=%s",
        guild_id, track.get("title", "?"), cluster,
    )


# ---------------------------------------------------------------------------
# Diversity seed builder
# ---------------------------------------------------------------------------

# Fallback genre seeds used when radio starts with no history and neutral/mixed mood.
# One representative tag per macro cluster so the first fill is always diverse.
_FALLBACK_GENRES = ["pop", "rock", "hip-hop", "electronic", "latin"]


def _build_diversity_seeds(guild_id: int) -> tuple[list[str], list[str]]:
    """Return (seed_track_ids, seed_genre_tags) for Spotify recommendations.

    Strategy:
    1. Count recent history by cluster. Prefer seed_tracks from clusters with
       fewer plays (so the radio doesn't lock onto the most-requested genre).
    2. Blend up to 2 genre seeds from the active mood.
    3. Total seeds ≤ 5 (Spotify limit).
    4. If no seeds at all (no history, neutral mood) → fall back to diverse genre set.
    """
    mood = get_mood(guild_id)
    mood_seed_genres = (
        _custom_moods.get(guild_id, {}).get(mood)
        or MOODS.get(mood, [])
    )[:2]  # up to 2 mood genre seeds

    history = list(_play_history.get(guild_id, []))
    if not history:
        # No play history yet — use mood genres if available, else fallback diverse set
        genres = mood_seed_genres if mood_seed_genres else _FALLBACK_GENRES
        return [], genres[:5]

    # Count tracks per cluster
    cluster_tracks: dict[str | None, list[str]] = {}
    for entry in history:
        c = entry.get("cluster")
        sid = entry.get("spotify_id")
        if sid:
            cluster_tracks.setdefault(c, []).append(sid)

    # Sort clusters by count ascending → underrepresented first
    sorted_clusters = sorted(cluster_tracks.items(), key=lambda x: len(x[1]))

    # Pick 1 track ID from each cluster (underrepresented first) until we have
    # enough to fill remaining slots after mood genres
    track_slots = 5 - len(mood_seed_genres)
    seed_tracks: list[str] = []
    seen_ids: set[str] = set()
    for _cluster, track_ids in sorted_clusters:
        for tid in reversed(track_ids):  # most recent within cluster
            if tid not in seen_ids and len(seed_tracks) < track_slots:
                seed_tracks.append(tid)
                seen_ids.add(tid)
                break

    # If history exists but all tracks lacked spotify_id, fall back to genre seeds
    if not seed_tracks and not mood_seed_genres:
        return [], _FALLBACK_GENRES

    return seed_tracks, mood_seed_genres


# ---------------------------------------------------------------------------
# Fill engine
# ---------------------------------------------------------------------------

async def fill_radio_queue(
    guild: discord.Guild,
    vc: discord.VoiceClient,
    text_channel,
) -> None:
    """Fetch Spotify recommendations and add them to the queue.

    Called when queue length drops below RADIO_QUEUE_MIN, or when radio is
    first activated. Guarded by _filling to prevent concurrent fills.
    """
    from src.playback import queues, now_playing_info, play_next
    from src.spotify import _get_recommendations
    from src.youtube import search_youtube
    from src.scoring import _split_query_parts

    gid = guild.id
    if _filling.get(gid):
        return
    _filling[gid] = True

    try:
        q = queues.get(gid, collections.deque())
        needed = RADIO_FILL_COUNT - len(q)
        if needed <= 0:
            return

        seed_tracks, seed_genres = _build_diversity_seeds(gid)
        logger.info(
            "radio.fill: guild=%s mood=%s seed_tracks=%s seed_genres=%s needed=%s",
            gid, get_mood(gid), seed_tracks, seed_genres, needed,
        )

        recs = await _get_recommendations(seed_tracks, seed_genres, limit=needed + 3)
        if not recs:
            logger.warning("radio.fill: sin recomendaciones de Spotify para guild=%s", gid)
            return

        # Search YouTube for all recommendations in parallel
        async def _fetch(idx: int, info: dict) -> dict | None:
            enable_llm = idx < LLM_ENABLED_FOR_ALBUM_TRACKS
            yt_info = await search_youtube(info["query"], enable_llm=enable_llm)
            if not yt_info:
                return None
            artist, title = _split_query_parts(info["query"])
            return {
                "title":      yt_info["title"],
                "yt_query":   info["query"],
                "url":        yt_info["url"],
                "requester":  "📻 Radio",
                "artist":     artist or "Unknown",
                "duration":   yt_info.get("duration") or 0,
                "thumbnail":  yt_info.get("thumbnail") or "",
                "spotify_id": info.get("spotify_id"),
                "artist_id":  info.get("artist_id"),
            }

        results = await asyncio.gather(*(_fetch(i, r) for i, r in enumerate(recs)))
        played_set = set(_played_ids.get(gid, []))
        new_tracks = [
            t for t in results
            if t is not None and t.get("spotify_id") not in played_set
        ][:needed]

        if not new_tracks:
            logger.warning("radio.fill: ninguna recomendacion encontrada en YouTube para guild=%s", gid)
            return

        if gid not in queues:
            queues[gid] = collections.deque()
        for track in new_tracks:
            queues[gid].append(track)

        logger.info("radio.fill: %d canciones añadidas a la cola de guild=%s", len(new_tracks), gid)

        # If bot was idle, start playing
        if not (vc and (vc.is_playing() or vc.is_paused())):
            await play_next(guild, vc, text_channel)

    except Exception as exc:
        logger.error("radio.fill: error inesperado para guild=%s: %s", gid, exc, exc_info=True)
    finally:
        _filling[gid] = False
