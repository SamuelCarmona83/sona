"""Stream-seeded radio moods: listen to a live FM station, enqueue clean YT tracks.

Test mood ``rock-radio`` samples Rock & Pop FM 95.9 (AR) in the background via
shazamio and queues ad-free YouTube/library versions for Discord voice.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import time

from src.config import (
    FM_SEED_COLD_FILL_COUNT,
    FM_SEED_CONTINGENCY_COOLDOWN_SEC,
    FM_SEED_CONTINGENCY_FILL_COUNT,
    FM_SEED_ROCK_STREAM_URL,
    RADIO_QUEUE_REFILL_THRESHOLD,
    RADIO_QUEUE_TARGET_SIZE,
    RADIO_REQUESTER_LABEL,
)
from src.fm_recognizer import match_key, start_fm_recognizer, stop_fm_recognizer

# Genre seeds used for cold-fill when mood has no Spotify genres (e.g. rock-radio)
_COLD_FILL_GENRE_MOOD: dict[str, str] = {
    "rock-radio": "rock",
}

logger = logging.getLogger(__name__)

# Hardcoded Rock & Pop 95.9 Buenos Aires — does not depend on per-guild FM favorites.
ROCK_RADIO_STATION: dict[str, Any] = {
    "stationuuid": "bcb3b03a-f0ec-4ceb-b22f-922138858800",
    "name": "Rock And Pop FM 95.9 (Rock & Pop)",
    "url": "https://playerservices.streamtheworld.com/api/livestream-redirect/ROCKANDPOPAAC.aac",
    "url_resolved": "https://playerservices.streamtheworld.com/api/livestream-redirect/ROCKANDPOPAAC.aac",
    "homepage": "https://fmrockandpop.com/",
    "favicon": "https://fmrockandpop.com/pwa/ios_192x192.png",
    "country": "Argentina",
    "countrycode": "AR",
    "language": "",
    "codec": "AAC+",
    "bitrate": 64,
    "tags": "rock,pop",
}

STREAM_SEEDED_MOODS: dict[str, dict[str, Any]] = {
    "rock-radio": {
        "station": ROCK_RADIO_STATION,
        "label": "Rock & Pop AR (limpia)",
    },
}

# guild_id -> mood key currently listening
_active_mood: dict[int, str] = {}
_last_enqueued_key: dict[int, str] = {}
# Cold fill only once per seed session (not every fill_radio_queue call)
_cold_fill_done: set[int] = set()
# guild_id -> last contingency fill timestamp
_last_contingency_at: dict[int, float] = {}


def is_stream_seeded_mood(mood: str) -> bool:
    return (mood or "").strip().lower() in STREAM_SEEDED_MOODS


def stream_seeded_mood_names() -> list[str]:
    return list(STREAM_SEEDED_MOODS.keys())


def get_stream_seed_station(mood: str) -> Optional[dict[str, Any]]:
    entry = STREAM_SEEDED_MOODS.get((mood or "").strip().lower())
    if not entry:
        return None
    station = dict(entry.get("station") or {})
    if not station:
        return None
    # Optional URL override for rock-radio
    if (mood or "").strip().lower() == "rock-radio" and FM_SEED_ROCK_STREAM_URL:
        station["url"] = FM_SEED_ROCK_STREAM_URL
        station["url_resolved"] = FM_SEED_ROCK_STREAM_URL
    return station


def stream_seed_label(mood: str) -> str:
    entry = STREAM_SEEDED_MOODS.get((mood or "").strip().lower()) or {}
    return str(entry.get("label") or mood)


def is_seed_listener_running(guild_id: int) -> bool:
    """True if seed mode is armed for this guild (even if recognizer task needs restart)."""
    return guild_id in _active_mood


def stop_fm_seed_listener(guild_id: int) -> None:
    """Stop background FM sampling for a guild."""
    had = _active_mood.pop(guild_id, None)
    _last_enqueued_key.pop(guild_id, None)
    _cold_fill_done.discard(guild_id)
    _last_contingency_at.pop(guild_id, None)
    try:
        stop_fm_recognizer(guild_id)
    except Exception as exc:
        logger.debug("fm_seed: stop recognizer: %s", exc)
    try:
        from src.fm_history import close_session

        close_session(guild_id)
    except Exception as exc:
        logger.debug("fm_seed: close history: %s", exc)
    if had:
        logger.info("fm_seed: stopped listener guild=%s mood=%s", guild_id, had)


def start_fm_seed_listener(
    guild: Any,
    text_channel: Any,
    *,
    mood_key: str | None = None,
) -> bool:
    """Start (or restart) FM seed listener for the guild's stream-seeded mood.

    Returns True if the listener was started.
    """
    from src import radio as _radio

    guild_id = int(guild.id)
    mood = (mood_key or _radio.get_mood(guild_id) or "").strip().lower()
    if not is_stream_seeded_mood(mood):
        stop_fm_seed_listener(guild_id)
        return False
    if not _radio.is_radio_active(guild_id):
        stop_fm_seed_listener(guild_id)
        return False

    station = get_stream_seed_station(mood)
    if not station:
        logger.warning("fm_seed: no station for mood=%s", mood)
        return False

    stream_url = (station.get("url_resolved") or station.get("url") or "").strip()
    if not stream_url:
        logger.warning("fm_seed: empty stream url mood=%s", mood)
        return False

    # Same mood restart (e.g. recognizer killed by mistake): only cancel the task.
    # Mood change / first start: full stop clears cold-fill + history.
    prev = _active_mood.get(guild_id)
    if prev is not None and prev != mood:
        stop_fm_seed_listener(guild_id)
    else:
        try:
            stop_fm_recognizer(guild_id)
        except Exception:
            pass
    _active_mood[guild_id] = mood

    # History session (station_to_track-like fields for open_session)
    try:
        from src.fm_history import open_session

        history_track = {
            "title": station.get("name") or "FM",
            "stationuuid": station.get("stationuuid") or "",
            "url": stream_url,
            "url_resolved": stream_url,
            "countrycode": station.get("countrycode") or "",
            "tags": station.get("tags") or "",
        }
        open_session(guild_id, history_track)
    except Exception as exc:
        logger.debug("fm_seed: history open failed: %s", exc)

    def _is_active() -> bool:
        from src import radio as radio_mod

        if not radio_mod.is_radio_active(guild_id):
            return False
        current = (radio_mod.get_mood(guild_id) or "").strip().lower()
        return current == mood and is_stream_seeded_mood(current)

    async def _on_match(gid: int, match: dict) -> None:
        await _handle_seed_match(guild, text_channel, mood, match)

    async def _on_stale(gid: int) -> None:
        logger.info("fm_seed: stale misses guild=%s — contingency check", gid)
        try:
            await maybe_contingency_fill(guild, guild.voice_client, text_channel, auto_play=True)
        except Exception as exc:
            logger.debug("fm_seed: contingency on_stale failed: %s", exc)

    start_fm_recognizer(
        guild_id,
        stream_url,
        on_match=_on_match,
        is_active=_is_active,
        text_channel=None,  # no chat spam from recognizer; seed mode is quiet
        on_stale=_on_stale,
    )
    logger.info(
        "fm_seed: started guild=%s mood=%s station=%s",
        guild_id,
        mood,
        station.get("name"),
    )
    return True


def ensure_fm_seed_listener(guild: Any, text_channel: Any) -> bool:
    """Idempotent: start listener if mood is stream-seeded and radio is on.

    Restarts the shazamio loop if it was killed (e.g. older play_next bug) while
    seed mode is still active.
    """
    from src import radio as _radio
    from src.fm_recognizer import is_running as recognizer_is_running

    gid = int(guild.id)
    mood = (_radio.get_mood(gid) or "").strip().lower()
    if not _radio.is_radio_active(gid) or not is_stream_seeded_mood(mood):
        if is_seed_listener_running(gid):
            stop_fm_seed_listener(gid)
        return False
    if _active_mood.get(gid) == mood and recognizer_is_running(gid):
        return True
    # Mood armed but recognizer dead, or first start / mood change
    return start_fm_seed_listener(guild, text_channel, mood_key=mood)


async def _enqueue_genre_fallback_tracks(
    guild: Any,
    vc: Any,
    text_channel: Any,
    *,
    mood: str,
    needed: int,
    reason: str,
    auto_play: bool,
) -> int:
    """Shared helper: library rock → YT queries. reason is cold_start|contingency."""
    from src.playback import guild_session, play_next, queues
    from src.library import get_radio_candidates
    from src.youtube import search_youtube, is_youtube_rate_limited

    if needed <= 0:
        return 0

    guild_id = int(guild.id)
    session = guild_session(guild_id)
    genre_mood = _COLD_FILL_GENRE_MOOD.get(mood, "rock")
    enqueued = 0
    is_contingency = reason == "contingency"

    try:
        local_tracks = await get_radio_candidates(guild_id, genre_mood, needed)
    except Exception as exc:
        logger.debug("fm_seed %s: library failed: %s", reason, exc)
        local_tracks = []

    for t in local_tracks:
        if enqueued >= needed:
            break
        track = dict(t)
        track["requester"] = RADIO_REQUESTER_LABEL
        track["from_cold_start"] = not is_contingency
        track["from_contingency"] = is_contingency
        track["from_fm_seed"] = False
        track["fm_seed_mood"] = mood
        # Avoid immediate re-queue of same local file
        if session.now_playing and session.now_playing.get("track_id") == track.get("track_id"):
            continue
        if any(q.get("track_id") and q.get("track_id") == track.get("track_id") for q in session.queue):
            continue
        session.queue.append(track)
        enqueued += 1

    if enqueued < needed and not is_youtube_rate_limited():
        queries = [
            "classic rock hits",
            "rock and pop argentina",
            "indie rock songs",
            "alternative rock radio",
        ]
        for q in queries:
            if enqueued >= needed:
                break
            try:
                yt = await search_youtube(q, enable_llm=False, trusted=True, urgent=True)
            except Exception as exc:
                logger.debug("fm_seed %s: YT failed '%s': %s", reason, q, exc)
                continue
            if not yt or not yt.get("url"):
                continue
            vid = yt.get("video_id")
            if vid and any(
                (session.now_playing or {}).get("video_id") == vid
                or t.get("video_id") == vid
                for t in session.queue
            ):
                continue
            track = {
                "title": yt.get("title") or q,
                "artist": "Unknown",
                "yt_query": q,
                "url": yt.get("url"),
                "requester": RADIO_REQUESTER_LABEL,
                "duration": yt.get("duration") or 0,
                "thumbnail": yt.get("thumbnail") or "",
                "video_id": yt.get("video_id"),
                "webpage_url": yt.get("webpage_url") or "",
                "acodec": yt.get("acodec", "?"),
                "abr": yt.get("abr", 0),
                "from_cold_start": not is_contingency,
                "from_contingency": is_contingency,
                "from_fm_seed": False,
                "fm_seed_mood": mood,
            }
            session.queue.append(track)
            enqueued += 1

    if enqueued:
        queues[guild_id] = session.queue
        logger.info(
            "fm_seed %s: enqueued %d tracks guild=%s mood=%s",
            reason,
            enqueued,
            guild_id,
            mood,
        )
        if auto_play and vc and not (vc.is_playing() or vc.is_paused()):
            try:
                await play_next(guild, vc, text_channel)
            except Exception as exc:
                logger.warning("fm_seed %s: play_next failed: %s", reason, exc)

    return enqueued


async def cold_fill_stream_seed(
    guild: Any,
    vc: Any,
    text_channel: Any,
    *,
    count: int | None = None,
    auto_play: bool = True,
) -> int:
    """Queue rock-ish tracks immediately so voice is not silent during first Shazam poll."""
    from src import radio as _radio
    from src.playback import guild_session

    guild_id = int(guild.id)
    mood = (_radio.get_mood(guild_id) or "").strip().lower()
    if not is_stream_seeded_mood(mood) or not _radio.is_radio_active(guild_id):
        return 0

    if guild_id in _cold_fill_done and count is None:
        return 0

    target = FM_SEED_COLD_FILL_COUNT if count is None else max(0, count)
    if target <= 0:
        _cold_fill_done.add(guild_id)
        return 0

    session = guild_session(guild_id)
    already = len(session.queue) + (1 if session.now_playing else 0)
    if already >= target:
        _cold_fill_done.add(guild_id)
        return 0
    needed = target - already

    enqueued = await _enqueue_genre_fallback_tracks(
        guild, vc, text_channel,
        mood=mood, needed=needed, reason="cold_start", auto_play=auto_play,
    )
    _cold_fill_done.add(guild_id)
    return enqueued


async def maybe_contingency_fill(
    guild: Any,
    vc: Any,
    text_channel: Any,
    *,
    auto_play: bool = True,
) -> int:
    """If queue is low and Shazam has been quiet, enqueue rock fallback tracks."""
    from src import radio as _radio
    from src.playback import guild_session

    guild_id = int(guild.id)
    mood = (_radio.get_mood(guild_id) or "").strip().lower()
    if not is_stream_seeded_mood(mood) or not _radio.is_radio_active(guild_id):
        return 0
    if FM_SEED_CONTINGENCY_FILL_COUNT <= 0:
        return 0

    session = guild_session(guild_id)
    depth = len(session.queue) + (1 if session.now_playing else 0)
    # Only when queue is thin (same idea as radio refill threshold)
    if depth > RADIO_QUEUE_REFILL_THRESHOLD:
        return 0

    now = time.time()
    last = _last_contingency_at.get(guild_id, 0.0)
    if now - last < FM_SEED_CONTINGENCY_COOLDOWN_SEC:
        return 0

    # Need at least cold fill attempted first (or session already running)
    if guild_id not in _cold_fill_done and not is_seed_listener_running(guild_id):
        return 0

    needed = max(1, FM_SEED_CONTINGENCY_FILL_COUNT - len(session.queue))
    enqueued = await _enqueue_genre_fallback_tracks(
        guild, vc, text_channel,
        mood=mood, needed=needed, reason="contingency", auto_play=auto_play,
    )
    if enqueued:
        _last_contingency_at[guild_id] = now
    return enqueued


def _count_seed_tracks_in_queue(guild_id: int) -> int:
    from src.playback import guild_session

    session = guild_session(guild_id)
    n = 0
    if session.now_playing and session.now_playing.get("from_fm_seed"):
        n += 1
    for t in session.queue:
        if t.get("from_fm_seed"):
            n += 1
    return n


def _already_queued_or_playing(guild_id: int, key: str, title: str, artist: str) -> bool:
    from src.playback import guild_session
    from src.fm_recognizer import match_key as mk

    session = guild_session(guild_id)
    candidates = []
    if session.now_playing:
        candidates.append(session.now_playing)
    candidates.extend(list(session.queue))
    for t in candidates:
        if t.get("fm_match_key") == key:
            return True
        t_title = (t.get("title") or "").strip()
        t_artist = (t.get("artist") or "").strip()
        if t_title and mk(t_artist, t_title) == key:
            return True
        # loose: yt_query contains both
        yq = (t.get("yt_query") or "").lower()
        if artist.lower() in yq and title.lower() in yq:
            return True
    return False


async def _handle_seed_match(guild: Any, text_channel: Any, mood: str, match: dict) -> None:
    from src import radio as _radio
    from src.playback import guild_session, play_next, queues, maybe_notify_rate_limited
    from src.youtube import search_youtube, is_youtube_rate_limited
    from src.library import resolve_local_track
    from src.fm_history import append_detection

    guild_id = int(guild.id)
    if not _radio.is_radio_active(guild_id):
        return
    if (_radio.get_mood(guild_id) or "").strip().lower() != mood:
        return

    artist = (match.get("artist") or "").strip()
    title = (match.get("title") or "").strip()
    if not title:
        return
    key = match_key(artist, title)

    try:
        append_detection(guild_id, match)
    except Exception as exc:
        logger.debug("fm_seed: history append: %s", exc)

    if _last_enqueued_key.get(guild_id) == key:
        return
    if _already_queued_or_playing(guild_id, key, title, artist):
        _last_enqueued_key[guild_id] = key
        return
    if _count_seed_tracks_in_queue(guild_id) >= RADIO_QUEUE_TARGET_SIZE:
        logger.debug("fm_seed: queue full of seed tracks guild=%s", guild_id)
        return

    query = f"{artist} - {title}" if artist else title
    shazam_cover = (match.get("cover_url") or "").strip() or None
    track: dict | None = {
        "title": title,
        "artist": artist or "Unknown",
        "yt_query": query,
        "url": None,
        "requester": RADIO_REQUESTER_LABEL,
        "duration": 0,
        "thumbnail": shazam_cover or "",
        "cover_url": shazam_cover or "",
        "recognized_cover_url": shazam_cover,
        "prefer_shazam_cover": bool(shazam_cover),
        "from_fm_seed": True,
        "fm_seed_mood": mood,
        "fm_match_key": key,
        "recognized_shazam_url": match.get("shazam_url"),
    }

    def _apply_shazam_cover(t: dict) -> dict:
        if shazam_cover:
            t["recognized_cover_url"] = shazam_cover
            t["cover_url"] = shazam_cover
            t["thumbnail"] = shazam_cover
            t["prefer_shazam_cover"] = True
        return t

    # Prefer local library
    try:
        local = resolve_local_track(track)
        if local:
            track = local
            track["from_fm_seed"] = True
            track["fm_seed_mood"] = mood
            track["fm_match_key"] = key
            track["requester"] = RADIO_REQUESTER_LABEL
            track["recognized_shazam_url"] = match.get("shazam_url")
            track = _apply_shazam_cover(track)
    except Exception as exc:
        logger.debug("fm_seed: local resolve: %s", exc)
        local = None

    if not track.get("url") and not track.get("local"):
        if is_youtube_rate_limited():
            if text_channel is not None:
                try:
                    await maybe_notify_rate_limited(guild_id, text_channel)
                except Exception:
                    pass
            return
        try:
            yt_info = await search_youtube(query, enable_llm=False, trusted=True)
        except Exception as exc:
            logger.warning("fm_seed: youtube search failed '%s': %s", query, exc)
            return
        if not yt_info:
            logger.info("fm_seed: no YT match for '%s'", query)
            return
        track.update(
            {
                "title": yt_info.get("title") or title,
                "url": yt_info.get("url"),
                "duration": yt_info.get("duration") or 0,
                "video_id": yt_info.get("video_id"),
                "webpage_url": yt_info.get("webpage_url") or "",
                "acodec": yt_info.get("acodec", "?"),
                "abr": yt_info.get("abr", 0),
            }
        )
        # Keep Shazam cover if present; only fall back to YT thumb
        if shazam_cover:
            track = _apply_shazam_cover(track)
        else:
            track["thumbnail"] = yt_info.get("thumbnail") or ""
            track["cover_url"] = yt_info.get("thumbnail") or ""

    if not track.get("url") and not track.get("local"):
        return

    # Re-check after async search
    if _already_queued_or_playing(guild_id, key, title, artist):
        _last_enqueued_key[guild_id] = key
        return

    session = guild_session(guild_id)
    session.queue.append(track)
    queues[guild_id] = session.queue
    _last_enqueued_key[guild_id] = key
    logger.info(
        "fm_seed: enqueued '%s' — '%s' guild=%s mood=%s",
        artist,
        title,
        guild_id,
        mood,
    )

    vc = guild.voice_client
    if vc and not (vc.is_playing() or vc.is_paused()):
        try:
            await play_next(guild, vc, text_channel)
        except Exception as exc:
            logger.warning("fm_seed: play_next failed: %s", exc)
    else:
        try:
            from src.playback import update_player_embed

            if text_channel is not None:
                await update_player_embed(guild, text_channel)
        except Exception:
            pass
