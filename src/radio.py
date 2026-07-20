import asyncio
import collections
import json
import logging
import pathlib

import discord

from src.config import (
    LLM_ALBUM_TRACK_RANKING_LIMIT,
    RADIO_QUEUE_REFILL_THRESHOLD,
    RADIO_QUEUE_TARGET_SIZE,
)

logger = logging.getLogger(__name__)

MOODS: dict[str, list[str]] = {
    "neutral":  [],
    "mixed":    [],
    "metal":    ["metal", "heavy-metal", "thrash-metal"],
    "hiphop":   ["hip-hop", "rap", "trap"],
    "techno":   ["techno", "minimal-techno", "electronic"],
    "dubstep":  ["dubstep", "drum-and-bass", "breakbeat"],
    "hardcore": ["hardcore", "hardstyle", "speedcore"],
    "gabber":   ["gabber", "hardcore", "industrial"],
    "chill":    ["chill", "lo-fi", "ambient"],
    "rock":     ["rock", "alternative", "indie"],
    "pop":      ["pop", "dance-pop"],
    "latin":    ["latin", "reggaeton"],
    "jazz":     ["jazz", "soul", "blues"],
    "classical":["classical", "piano"],
    "reggae":   ["reggae", "dub"],
    "country":  ["country", "folk"],
    # Stream-seeded test mood: listens to Rock & Pop AR, enqueues clean YT tracks
    "rock-radio": [],
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
    "gabber": "techno", "hardstyle": "techno", "speedcore": "techno",
    "hardcore": "techno", "industrial": "techno", "drum-and-bass": "techno",
    "dubstep": "techno", "breakbeat": "techno", "noise": "techno",
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


async def get_track_cluster(track: dict) -> str | None:
    """Return the macro genre cluster for a track, or None if unknown."""
    artist_id = track.get("artist_id")
    if not artist_id:
        return None
    from src.spotify import _get_artist_genres
    genres = await _get_artist_genres(artist_id)
    return _map_cluster(genres)


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

# Radio profile source per guild: off | admin | voice | playlist
_PROFILE_PATH = pathlib.Path(".cache/radio_profiles.json")
_radio_profiles: dict[int, dict] = {}

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


def _load_radio_profiles() -> None:
    if _PROFILE_PATH.exists():
        try:
            data = json.loads(_PROFILE_PATH.read_text())
            for gid_str, profile in data.items():
                _radio_profiles[int(gid_str)] = profile
        except Exception:
            pass


def _save_radio_profiles() -> None:
    try:
        data = {str(gid): profile for gid, profile in _radio_profiles.items()}
        _PROFILE_PATH.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.warning("radio: could not save radio_profiles: %s", exc)


_load_played_ids()
_load_custom_moods()
_load_radio_profiles()


def get_profile_mode(guild_id: int) -> str:
    return _radio_profiles.get(guild_id, {}).get("mode", "off")


def get_profile_playlist_id(guild_id: int) -> str | None:
    return _radio_profiles.get(guild_id, {}).get("playlist_id")


def set_profile_mode(guild_id: int, mode: str, *, playlist_id: str | None = None) -> None:
    if mode not in ("off", "admin", "voice", "playlist"):
        raise ValueError(f"Modo de perfil desconocido: {mode}")
    if mode == "playlist" and not playlist_id:
        raise ValueError("El modo playlist requiere una playlist de Spotify.")
    if mode == "off":
        _radio_profiles.pop(guild_id, None)
    else:
        profile = {"mode": mode}
        if mode == "playlist":
            profile["playlist_id"] = playlist_id
        _radio_profiles[guild_id] = profile
    _save_radio_profiles()


def describe_profile_mode(guild_id: int) -> str:
    mode = get_profile_mode(guild_id)
    if mode == "off":
        return "desactivado (historial del servidor)"
    if mode == "admin":
        return "perfil del admin (`!auth`)"
    if mode == "voice":
        return "perfiles Spotify de usuarios en el canal de voz (`!spotify link`)"
    if mode == "playlist":
        playlist_id = get_profile_playlist_id(guild_id) or "?"
        return f"playlist Spotify (`{playlist_id}`)"
    return mode


async def _resolve_taste_profile(guild: discord.Guild) -> "TasteProfile | None":
    from src.config import sp, SPOTIFY_AVAILABLE
    from src.spotify_taste import (
        TasteProfile,
        build_playlist_taste_profile,
        build_user_taste_profile,
        get_cached_profile,
        merge_taste_profiles,
        parse_playlist_id,
    )
    from src.spotify_users import get_valid_user_client, linked_users_in

    gid = guild.id
    mode = get_profile_mode(gid)
    if mode == "off" or not SPOTIFY_AVAILABLE:
        return None

    if mode == "admin":
        if sp is None:
            return None
        from src.spotify import _safe_validate_token
        if not await _safe_validate_token(sp.auth_manager):
            return None
        return await get_cached_profile(
            f"guild_{gid}_admin",
            lambda: build_user_taste_profile(sp, label="admin"),
        )

    if mode == "playlist":
        playlist_id = get_profile_playlist_id(gid)
        if not playlist_id or sp is None:
            return None
        from src.spotify import _safe_validate_token
        if not await _safe_validate_token(sp.auth_manager):
            return None
        pid = playlist_id
        return await get_cached_profile(
            f"guild_{gid}_playlist_{pid}",
            lambda: build_playlist_taste_profile(sp, pid, label="playlist"),
        )

    if mode == "voice":
        vc = guild.voice_client
        channel = getattr(vc, "channel", None)
        if channel is None:
            return None
        connected_ids = [m.id for m in channel.members if not m.bot]
        linked = await linked_users_in(connected_ids)
        if not linked:
            if sp is not None:
                from src.spotify import _safe_validate_token
                if await _safe_validate_token(sp.auth_manager):
                    logger.info("radio.profile: voice mode sin usuarios vinculados, fallback admin guild=%s", gid)
                    return await get_cached_profile(
                        f"guild_{gid}_admin_fallback",
                        lambda: build_user_taste_profile(sp, label="admin"),
                    )
            return None

        profiles = []
        for uid in linked:
            client = await get_valid_user_client(uid)
            if client is None:
                continue
            profile = await get_cached_profile(
                f"user_{uid}",
                lambda c=client: build_user_taste_profile(c, label=f"user:{uid}"),
            )
            if profile.direct_tracks or profile.seed_track_ids:
                profiles.append(profile)
        if not profiles:
            return None
        return merge_taste_profiles(profiles, label="voice")

    return None


def is_radio_active(guild_id: int) -> bool:
    return _radio_active.get(guild_id, False)


def set_radio_active(guild_id: int, active: bool) -> None:
    _radio_active[guild_id] = active
    if not active:
        _filling.pop(guild_id, None)
        try:
            from src.fm_seed_radio import stop_fm_seed_listener

            stop_fm_seed_listener(guild_id)
        except Exception:
            pass


def get_mood(guild_id: int) -> str:
    return _radio_mood.get(guild_id, "neutral")


def set_mood(guild_id: int, mood: str) -> None:
    all_moods = {**MOODS, **_custom_moods.get(guild_id, {})}
    if mood not in all_moods:
        raise ValueError(f"Mood desconocido: '{mood}'. Disponibles: {', '.join(all_moods)}")
    prev = _radio_mood.get(guild_id, "neutral")
    _radio_mood[guild_id] = mood
    # Leaving a stream-seeded mood stops the FM listener; entering one is started by fill/ensure.
    try:
        from src.fm_seed_radio import is_stream_seeded_mood, stop_fm_seed_listener

        if is_stream_seeded_mood(prev) and not is_stream_seeded_mood(mood):
            stop_fm_seed_listener(guild_id)
    except Exception:
        pass


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


def flush_radio_tracks(guild_id: int) -> int:
    """Remove radio-auto-queued tracks from queue, keep user tracks. Returns removed count."""
    from src.playback import queues, _prefetch_dj, _last_cluster
    from src.dj_announcer import cleanup_dj_audio
    q = queues.get(guild_id)
    if not q:
        return 0
    items = list(q)
    kept = [t for t in items if t.get("requester") != "\U0001f4fb Radio"]
    removed = len(items) - len(kept)
    queues[guild_id] = collections.deque(kept)
    # Discard stale pre-generated TTS — it was for a track that no longer fits the new mood
    stale = _prefetch_dj.pop(guild_id, None)
    if stale:
        cleanup_dj_audio(stale)
    # Reset cluster so first new-mood song doesn't trigger a false genre transition
    _last_cluster.pop(guild_id, None)
    logger.info("flush_radio_tracks: guild=%s removed=%d kept=%d", guild_id, removed, len(kept))
    return removed


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
        "spotify_id":  spotify_id,
        "artist_id":   artist_id,
        "cluster":     cluster,
        "artist_name": track.get("artist"),
    })
    
    from src.library import track_id as library_track_id

    track_title = track.get("title", "")
    tid = library_track_id(track)
    ids = _played_ids.setdefault(guild_id, [])
    if tid not in ids:
        ids.append(tid)
        if len(ids) > _PLAYED_IDS_MAX:
            _played_ids[guild_id] = ids[-_PLAYED_IDS_MAX:]
        _save_played_ids()
        logger.debug(
            "radio.record_played: saved track id=%s title='%s' (total: %d)",
            tid, track_title, len(ids),
        )
    else:
        logger.debug(
            "radio.record_played: track id=%s already in history",
            tid,
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
    raw_mood_genres = (
        _custom_moods.get(guild_id, {}).get(mood)
        or MOODS.get(mood, [])
    )
    # Only pass Spotify-valid genre seeds (single-word or hyphenated, known in cluster map).
    # Free-text tokens from raw-token moods are kept for YouTube fallback only.
    mood_seed_genres = [
        g for g in raw_mood_genres
        if g in _GENRE_CLUSTER_MAP or " " not in g
    ][:2]

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

    # When a specific mood is active (not neutral/mixed), only consider seed
    # tracks whose cluster matches the mood.  This prevents leftover hiphop
    # history from seeding metal recommendations (and vice-versa).
    mood_cluster: str | None = None
    if mood not in ("neutral", "mixed"):
        for g in raw_mood_genres:
            mood_cluster = _GENRE_CLUSTER_MAP.get(g)
            if mood_cluster:
                break

    if mood_cluster:
        matching = cluster_tracks.get(mood_cluster, [])
        filtered_clusters = {mood_cluster: matching} if matching else {}
    else:
        filtered_clusters = cluster_tracks

    # Sort clusters by count ascending → underrepresented first
    sorted_clusters = sorted(filtered_clusters.items(), key=lambda x: len(x[1]))

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
# YouTube-only fallback fill (used when Spotify is unavailable)
# ---------------------------------------------------------------------------

async def _youtube_fallback_fill(guild_id: int, needed: int) -> list[dict]:
    """Build track dicts using YouTube search when Spotify recommendations fail.

    Strategy:
    1. Use artist names from recent play history (most diverse, most recent).
    2. Fill remaining slots with mood-genre plain-text queries.
    3. Return list of {query, spotify_id=None, artist_id=None}.
    """
    import random as _random
    from src.scoring import _split_query_parts

    queries: list[str] = []

    # --- From play history: recent unique artists ---
    history = list(_play_history.get(guild_id, []))
    seen_artists: set[str] = set()
    for entry in reversed(history):
        artist = entry.get("artist_name")
        if artist and artist not in seen_artists:
            seen_artists.add(artist)
            queries.append(f"{artist} best songs")
        if len(queries) >= needed:
            break

    # --- From mood genres: genre playlist queries ---
    mood = get_mood(guild_id)
    mood_genres = (
        _custom_moods.get(guild_id, {}).get(mood)
        or MOODS.get(mood, [])
        or _FALLBACK_GENRES
    )
    _random.shuffle(mood_genres)
    for genre in mood_genres:
        if len(queries) >= needed:
            break
        queries.append(f"{genre} playlist mix")

    # Pad with fallback if still short
    for genre in _FALLBACK_GENRES:
        if len(queries) >= needed:
            break
        queries.append(f"{genre} popular songs")

    _random.shuffle(queries)

    from src.youtube import search_youtube
    results: list[dict] = []
    for q in queries[:needed]:
        yt_info = await search_youtube(q, enable_llm=False, trusted=True)
        if not yt_info:
            continue
        artist, _title = _split_query_parts(q)
        results.append({
            "query":      q,
            "spotify_id": None,
            "artist_id":  None,
            "yt_info":    yt_info,
            "artist":     artist or "Unknown",
        })

    logger.info(
        "radio.yt_fallback: guild=%s generated %d candidates from YouTube",
        guild_id, len(results),
    )
    return results


# ---------------------------------------------------------------------------
# Fill engine
# ---------------------------------------------------------------------------

async def _try_early_play(
    gid: int,
    guild: discord.Guild,
    vc: discord.VoiceClient,
    text_channel,
    *,
    early_play: bool,
    triggered: dict[int, bool],
) -> None:
    if not early_play or triggered.get(gid):
        return
    from src.playback import play_next, queues

    if vc and (vc.is_playing() or vc.is_paused()):
        triggered[gid] = True
        return
    if not queues.get(gid):
        return
    triggered[gid] = True
    logger.info("radio.fill: early_play iniciando reproduccion guild=%s", gid)
    await play_next(guild, vc, text_channel)


async def fill_radio_queue(
    guild: discord.Guild,
    vc: discord.VoiceClient,
    text_channel,
    *,
    auto_play: bool = True,
    early_play: bool = False,
) -> None:
    """Fetch Spotify recommendations and add them to the queue.

    Called when queue length drops below target (or on explicit radio start), or when radio is
    first activated. Guarded by _filling to prevent concurrent fills.

    If *auto_play* is False, tracks are queued but playback is NOT started
    automatically (caller is responsible for starting playback).
    """
    from src.playback import queues, now_playing_info, play_next, maybe_notify_rate_limited
    from src.spotify import _get_recommendations_hybrid
    from src.youtube import search_youtube, is_youtube_rate_limited
    from src.scoring import _split_query_parts
    from src.library import get_radio_candidates, resolve_local_track, track_id, get_entry, track_from_entry

    gid = guild.id
    if _filling.get(gid):
        return
    _filling[gid] = True
    early_play_triggered: dict[int, bool] = {}

    try:
        # Stream-seeded moods (e.g. rock-radio): cold-fill rock tracks + FM listener.
        try:
            from src.fm_seed_radio import (
                is_stream_seeded_mood,
                ensure_fm_seed_listener,
                cold_fill_stream_seed,
            )

            if is_stream_seeded_mood(get_mood(gid)):
                from src.fm_seed_radio import maybe_contingency_fill

                ensure_fm_seed_listener(guild, text_channel)
                # Never force auto_play via early_play — welcome must play first on cold start
                filled = await cold_fill_stream_seed(
                    guild, vc, text_channel, auto_play=auto_play,
                )
                cont = await maybe_contingency_fill(
                    guild, vc, text_channel, auto_play=auto_play,
                )
                logger.info(
                    "radio.fill: stream-seeded mood=%s guild=%s cold_fill=%d contingency=%d — skipping Spotify",
                    get_mood(gid),
                    gid,
                    filled,
                    cont,
                )
                return
        except Exception as exc:
            logger.warning("radio.fill: stream-seed ensure failed: %s", exc)

        q = queues.get(gid, collections.deque())
        needed = RADIO_QUEUE_TARGET_SIZE - len(q)
        if needed <= 0:
            return

        queue_was_empty = len(q) == 0
        yt_search_needs_urgent = early_play and queue_was_empty

        taste_profile = await _resolve_taste_profile(guild)
        if taste_profile and (taste_profile.seed_track_ids or taste_profile.seed_genres):
            seed_tracks = taste_profile.seed_track_ids[:5]
            seed_genres = taste_profile.seed_genres[:2]
        else:
            seed_tracks, seed_genres = _build_diversity_seeds(gid)
        logger.info(
            "radio.fill: guild=%s mood=%s profile=%s seed_tracks=%s seed_genres=%s needed=%s",
            gid, get_mood(gid), get_profile_mode(gid), seed_tracks, seed_genres, needed,
        )

        if is_youtube_rate_limited():
            local_tracks = await get_radio_candidates(gid, get_mood(gid), needed)
            if local_tracks:
                queues.setdefault(gid, collections.deque())
                for track in local_tracks:
                    queues[gid].append(track)
                logger.info(
                    "radio.fill: %d canciones locales (YouTube bloqueado) para guild=%s",
                    len(local_tracks), gid,
                )
                await maybe_notify_rate_limited(gid, text_channel)
                if auto_play and not (vc and (vc.is_playing() or vc.is_paused())):
                    await play_next(guild, vc, text_channel)
                return
            logger.warning("radio.fill: YouTube bloqueado y biblioteca local vacia para guild=%s", gid)
            await maybe_notify_rate_limited(gid, text_channel)
            return

        # --- Spotify profile direct mix ---
        if taste_profile and taste_profile.direct_tracks:
            import random as _random
            from src.youtube import search_youtube as _syt_profile

            profile_label = taste_profile.source_label or get_profile_mode(gid)
            requester = f"📻 Radio 🎧 {profile_label}"
            profile_pool = list(taste_profile.direct_tracks)
            _random.shuffle(profile_pool)
            profile_needed = max(1, min(needed, len(profile_pool), needed // 2 + 1))
            profile_candidates = profile_pool[: profile_needed * 2]

            async def _fetch_profile(info: dict) -> dict | None:
                nonlocal yt_search_needs_urgent
                stub = {
                    "title": info.get("title", info["query"]),
                    "yt_query": info["query"],
                    "spotify_id": info.get("spotify_id"),
                    "artist_id": info.get("artist_id"),
                    "artist": info.get("artist", "Unknown"),
                }
                local = resolve_local_track(stub)
                if local:
                    local["requester"] = requester
                    return local
                tid = track_id(stub)
                entry = get_entry(tid)
                if entry:
                    return track_from_entry(tid, entry, requester=requester)
                urgent = yt_search_needs_urgent
                if urgent:
                    yt_search_needs_urgent = False
                yt_info = await _syt_profile(
                    info["query"], enable_llm=False, trusted=True, urgent=urgent,
                )
                if not yt_info or not yt_info.get("url"):
                    local = resolve_local_track(stub)
                    if local:
                        local["requester"] = requester
                        return local
                    return None
                return {
                    "title": yt_info["title"],
                    "yt_query": info["query"],
                    "url": yt_info["url"],
                    "requester": requester,
                    "artist": info.get("artist", "Unknown"),
                    "duration": yt_info.get("duration") or 0,
                    "thumbnail": yt_info.get("thumbnail") or "",
                    "cover_url": yt_info.get("cover_url") or "",
                    "spotify_id": info.get("spotify_id"),
                    "artist_id": info.get("artist_id"),
                    "video_id": yt_info.get("video_id"),
                    "webpage_url": yt_info.get("webpage_url"),
                }

            played_set_profile = set(_played_ids.get(gid, []))
            seen_urls_profile: set[str] = set()
            profile_tracks = []
            for info in profile_candidates:
                track = await _fetch_profile(info)
                if track is None:
                    continue
                sid = track.get("spotify_id")
                url = track.get("url", "")
                dedup_key = sid if sid else f"url_{url}"
                if dedup_key in played_set_profile or url in seen_urls_profile:
                    continue
                seen_urls_profile.add(url)
                profile_tracks.append(track)
                queues.setdefault(gid, collections.deque())
                queues[gid].append(track)
                await _try_early_play(
                    gid, guild, vc, text_channel,
                    early_play=early_play, triggered=early_play_triggered,
                )
                if len(profile_tracks) >= profile_needed:
                    break
            if profile_tracks:
                needed -= len(profile_tracks)
                logger.info(
                    "radio.fill: %d canciones de perfil Spotify (%s) para guild=%s",
                    len(profile_tracks), profile_label, gid,
                )
                if needed <= 0:
                    if auto_play and not (vc and (vc.is_playing() or vc.is_paused())):
                        await play_next(guild, vc, text_channel)
                    return

        # --- Liked-tracks priority ---
        from src import likes as _likes_mod
        liked_tracks = []
        vc_channel = getattr(guild.voice_client, "channel", None)
        if vc_channel is not None:
            connected_ids = [m.id for m in vc_channel.members if not m.bot]
            if connected_ids:
                played_set_for_likes = set(_played_ids.get(gid, []))
                liked_tracks = _likes_mod.get_prioritized_tracks(
                    gid, connected_ids, played_set_for_likes, limit=needed
                )
        # Resolve liked tracks through YouTube
        if liked_tracks:
            from src.youtube import search_youtube as _syt
            async def _fetch_liked(info: dict) -> dict | None:
                nonlocal yt_search_needs_urgent
                stub = {
                    "title": info.get("title", info["query"]),
                    "yt_query": info["query"],
                    "spotify_id": info.get("spotify_id"),
                    "artist_id": info.get("artist_id"),
                    "artist": info.get("artist", "Unknown"),
                }
                local = resolve_local_track(stub)
                if local:
                    local["requester"] = f"📻 Radio ❤️×{info['_like_count']}"
                    return local
                tid = track_id(stub)
                entry = get_entry(tid)
                if entry:
                    t = track_from_entry(tid, entry, requester=f"📻 Radio ❤️×{info['_like_count']}")
                    return t
                urgent = yt_search_needs_urgent
                if urgent:
                    yt_search_needs_urgent = False
                yt_info = await _syt(
                    info["query"], enable_llm=False, trusted=True, urgent=urgent,
                )
                if not yt_info or not yt_info.get("url"):
                    local = resolve_local_track(stub)
                    if local:
                        local["requester"] = f"📻 Radio ❤️×{info['_like_count']}"
                        return local
                    return None
                return {
                    "title":      yt_info["title"],
                    "yt_query":   info["query"],
                    "url":        yt_info["url"],
                    "requester":  f"📻 Radio ❤️×{info['_like_count']}",
                    "artist":     info.get("artist", "Unknown"),
                    "duration":   yt_info.get("duration") or 0,
                    "thumbnail":  yt_info.get("thumbnail") or "",
                    "spotify_id": info.get("spotify_id"),
                    "artist_id":  info.get("artist_id"),
                    "video_id":   yt_info.get("video_id"),
                    "webpage_url": yt_info.get("webpage_url"),
                }

            played_set_lk = set(_played_ids.get(gid, []))
            seen_urls_lk: set[str] = set()
            priority_tracks = []
            for info in liked_tracks:
                t = await _fetch_liked(info)
                if t is None:
                    continue
                sid = t.get("spotify_id")
                url = t.get("url", "")
                dedup_key = sid if sid else f"url_{url}"
                if dedup_key in played_set_lk or url in seen_urls_lk:
                    continue
                seen_urls_lk.add(url)
                priority_tracks.append(t)
                queues.setdefault(gid, collections.deque())
                queues[gid].append(t)
                await _try_early_play(
                    gid, guild, vc, text_channel,
                    early_play=early_play, triggered=early_play_triggered,
                )
            if priority_tracks:
                needed -= len(priority_tracks)
                logger.info(
                    "radio.fill: %d canciones liked priorizadas para guild=%s",
                    len(priority_tracks), gid,
                )
                if needed <= 0:
                    if auto_play and not (vc and (vc.is_playing() or vc.is_paused())):
                        await play_next(guild, vc, text_channel)
                    return

        recs = await _get_recommendations_hybrid(seed_tracks, seed_genres, limit=needed + 3)
        using_fallback = False
        if not recs:
            logger.warning(
                "radio.fill: Hybrid recommendations exhausted for guild=%s, usando fallback YouTube",
                gid,
            )
            recs = await _youtube_fallback_fill(gid, needed + 3)
            using_fallback = True

        if not recs:
            logger.warning("radio.fill: fallback YouTube tambien vacio para guild=%s", gid)
            return

        # Search YouTube (or use local) for Spotify-sourced recs.
        # We try local library first (fast path, no YT hit if already cached from prior plays).
        # Then launch remaining searches concurrently (internally rate-limited + throttled).
        async def _fetch(idx: int, info: dict) -> dict | None:
            nonlocal yt_search_needs_urgent
            # Local cache fast-path (avoid YT search for tracks already in library)
            stub = {
                "title": info.get("title", info.get("query", "")),
                "yt_query": info["query"],
                "spotify_id": info.get("spotify_id"),
                "artist_id": info.get("artist_id"),
                "artist": info.get("artist", "Unknown"),
            }
            local = resolve_local_track(stub)
            if local:
                local["requester"] = "📻 Radio"
                return local
            tid = track_id(stub)
            entry = get_entry(tid)
            if entry:
                return track_from_entry(tid, entry, requester="📻 Radio")

            if using_fallback:
                yt_info = info.get("yt_info")
            else:
                enable_llm = idx < LLM_ALBUM_TRACK_RANKING_LIMIT
                urgent = yt_search_needs_urgent
                if urgent:
                    yt_search_needs_urgent = False
                yt_info = await search_youtube(
                    info["query"], enable_llm=enable_llm, trusted=True, urgent=urgent,
                )
            if not yt_info:
                return None
            artist, _title = _split_query_parts(info["query"])
            return {
                "title":      yt_info["title"],
                "yt_query":   info["query"],
                "url":        yt_info["url"],
                "requester":  "📻 Radio",
                "artist":     info.get("artist") or artist or "Unknown",
                "duration":   yt_info.get("duration") or 0,
                "thumbnail":  yt_info.get("thumbnail") or "",
                "spotify_id": info.get("spotify_id"),
                "artist_id":  info.get("artist_id"),
                "video_id":   yt_info.get("video_id"),
                "webpage_url": yt_info.get("webpage_url"),
            }

        played_set = set(_played_ids.get(gid, []))
        seen_urls: set[str] = set()
        new_tracks = []
        # Concurrent fetch (governed by yt search semaphore + per-search throttle)
        fetch_coros = [_fetch(i, info) for i, info in enumerate(recs)]
        fetched_results = await asyncio.gather(*fetch_coros, return_exceptions=True)
        for t in fetched_results:
            if isinstance(t, Exception):
                logger.debug("radio.fill: fetch error (ignored): %s", t)
                continue
            if t is None:
                continue
            sid = t.get("spotify_id")
            url = t.get("url", "")
            dedup_key = sid if sid else f"url_{url}"
            if dedup_key in played_set or url in seen_urls:
                continue
            seen_urls.add(url)
            new_tracks.append(t)
            queues.setdefault(gid, collections.deque())
            queues[gid].append(t)
            await _try_early_play(
                gid, guild, vc, text_channel,
                early_play=early_play, triggered=early_play_triggered,
            )
            if len(new_tracks) >= needed:
                break

        if not new_tracks:
            ids = _played_ids.get(gid, [])
            if ids:
                keep = ids[-200:]
                _played_ids[gid] = keep
                _save_played_ids()
                logger.warning(
                    "radio.fill: played_ids saturado para guild=%s, trimming a 200 y reintentando",
                    gid,
                )
                retry_played_set = set(keep)
                for i, info in enumerate(recs):
                    t = await _fetch(i, info)
                    if t is None:
                        continue
                    sid = t.get("spotify_id")
                    url = t.get("url", "")
                    dedup_key = sid if sid else f"url_{url}"
                    if dedup_key in retry_played_set or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_tracks.append(t)
                    queues.setdefault(gid, collections.deque())
                    queues[gid].append(t)
                    await _try_early_play(
                        gid, guild, vc, text_channel,
                        early_play=early_play, triggered=early_play_triggered,
                    )
                    if len(new_tracks) >= needed:
                        break

        if not new_tracks:
            logger.warning("radio.fill: ninguna recomendacion encontrada en YouTube para guild=%s", gid)
            return

        logger.info("radio.fill: %d canciones añadidas a la cola de guild=%s", len(new_tracks), gid)

        # If bot was idle, start playing (unless caller handles playback)
        if auto_play and not (vc and (vc.is_playing() or vc.is_paused())):
            await play_next(guild, vc, text_channel)

    except Exception as exc:
        logger.error("radio.fill: error inesperado para guild=%s: %s", gid, exc, exc_info=True)
    finally:
        _filling[gid] = False
