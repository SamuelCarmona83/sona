"""Likes system – per-user track likes with radio priority integration.

Users can ❤️ toggle the currently playing song. Liked tracks are persisted
to .cache/likes.json. During radio fill, tracks liked by ≥2 connected users
that haven't been played recently are prioritized over Spotify recommendations.
"""
import json
import logging
import pathlib
import time

from src.library import track_id

logger = logging.getLogger(__name__)

_LIKES_PATH = pathlib.Path(".cache/likes.json")
_LIKES_PATH.parent.mkdir(parents=True, exist_ok=True)

# Structure: {guild_id_str: {user_id_str: [{track_id, title, artist, cluster, liked_at}]}}
_likes: dict[int, dict[int, list[dict]]] = {}

# How long (seconds) a liked track must be absent from radio history before being prioritized
LIKES_COOLDOWN = 3 * 3600  # 3 hours
# Min connected users that liked a track to qualify for priority
LIKES_MIN_USERS = 2


def _load() -> None:
    if _LIKES_PATH.exists():
        try:
            data = json.loads(_LIKES_PATH.read_text())
            for gid_str, users in data.items():
                gid = int(gid_str)
                _likes[gid] = {}
                for uid_str, tracks in users.items():
                    _likes[gid][int(uid_str)] = tracks
        except Exception as exc:
            logger.warning("likes: failed to load: %s", exc)


def _save() -> None:
    try:
        data = {
            str(gid): {str(uid): tracks for uid, tracks in users.items()}
            for gid, users in _likes.items()
        }
        _LIKES_PATH.write_text(json.dumps(data))
    except Exception as exc:
        logger.warning("likes: failed to save: %s", exc)


def _entry_track_id(entry: dict) -> str:
    return track_id({
        "spotify_id": entry.get("spotify_id"),
        "video_id": entry.get("video_id"),
        "webpage_url": entry.get("webpage_url"),
        "yt_query": entry.get("yt_query") or entry.get("title", ""),
        "title": entry.get("title", ""),
    })


def _normalize_loaded_likes() -> None:
    changed = False
    for users in _likes.values():
        for uid, tracks in users.items():
            seen: set[str] = set()
            deduped: list[dict] = []
            for entry in tracks:
                new_tid = _entry_track_id(entry)
                if new_tid != entry.get("track_id"):
                    entry["track_id"] = new_tid
                    changed = True
                if new_tid in seen:
                    changed = True
                    continue
                seen.add(new_tid)
                deduped.append(entry)
            users[uid] = deduped
    if changed:
        _save()


_load()
_normalize_loaded_likes()


def toggle_like(guild_id: int, user_id: int, track: dict) -> bool:
    """Toggle like for track. Returns True if liked, False if unliked."""
    tid = track_id(track)
    guild_likes = _likes.setdefault(guild_id, {})
    user_likes = guild_likes.setdefault(user_id, [])

    # Check if already liked
    existing = next((t for t in user_likes if t["track_id"] == tid), None)
    if existing:
        user_likes.remove(existing)
        logger.debug("likes: user=%s unliked '%s' guild=%s", user_id, track.get("title"), guild_id)
        _save()
        return False
    else:
        user_likes.append({
            "track_id": tid,
            "title":    track.get("title", "?"),
            "artist":   track.get("artist", "Unknown"),
            "cluster":  track.get("cluster"),
            "yt_query": track.get("yt_query", track.get("title", "")),
            "spotify_id": track.get("spotify_id"),
            "artist_id":  track.get("artist_id"),
            "video_id":   track.get("video_id"),
            "webpage_url": track.get("webpage_url"),
            "thumbnail":  track.get("thumbnail", ""),
            "liked_at": time.time(),
        })
        logger.debug("likes: user=%s liked '%s' guild=%s", user_id, track.get("title"), guild_id)
        _save()
        return True


def is_liked_by(guild_id: int, user_id: int, track: dict) -> bool:
    """Return True if the user has liked this track."""
    tid = track_id(track)
    user_likes = _likes.get(guild_id, {}).get(user_id, [])
    return any(t["track_id"] == tid for t in user_likes)


def get_user_likes(guild_id: int, user_id: int) -> list[dict]:
    """Return list of liked tracks for a user in a guild."""
    return list(_likes.get(guild_id, {}).get(user_id, []))


def get_prioritized_tracks(
    guild_id: int,
    connected_user_ids: list[int],
    recently_played_ids: set[str],
    limit: int = 5,
) -> list[dict]:
    """Return liked tracks to prioritize in radio fill.

    Criteria:
    - Liked by ≥ LIKES_MIN_USERS connected users
    - Not in recently_played_ids (respects cooldown)
    Returns up to `limit` tracks sorted by like_count desc, then liked_at asc.
    """
    guild_likes = _likes.get(guild_id, {})

    # Aggregate: track_id → {count, track_data, oldest_like}
    aggregated: dict[str, dict] = {}
    for uid in connected_user_ids:
        for entry in guild_likes.get(uid, []):
            tid = entry["track_id"]
            if tid in recently_played_ids:
                continue
            if tid not in aggregated:
                aggregated[tid] = {"count": 0, "entry": entry, "oldest_like": entry["liked_at"]}
            aggregated[tid]["count"] += 1
            aggregated[tid]["oldest_like"] = min(aggregated[tid]["oldest_like"], entry["liked_at"])

    # Filter by minimum users
    candidates = [
        (tid, info) for tid, info in aggregated.items()
        if info["count"] >= LIKES_MIN_USERS
    ]

    # Sort: most liked first, then oldest liked first (waited longest)
    candidates.sort(key=lambda x: (-x[1]["count"], x[1]["oldest_like"]))

    results = []
    for _tid, info in candidates[:limit]:
        entry = info["entry"]
        results.append({
            "query":      entry["yt_query"],
            "spotify_id": entry.get("spotify_id"),
            "artist_id":  entry.get("artist_id"),
            "title":      entry["title"],
            "artist":     entry["artist"],
            "_like_count": info["count"],
        })

    if results:
        logger.info(
            "likes: %d priority tracks for guild=%s (connected=%d)",
            len(results), guild_id, len(connected_user_ids),
        )
    return results
