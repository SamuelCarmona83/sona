"""Local music library — persistent audio cache, popularity tracking, offline radio."""
import asyncio
import hashlib
import json
import logging
import pathlib
import random
import re
import time

import yt_dlp

from src.config import (
    LIBRARY_AUTO_DOWNLOAD,
    LIBRARY_ENABLED,
    LIBRARY_MAX_MB,
    LIBRARY_MAX_TRACKS,
    LIBRARY_MIN_PLAYS_TO_PIN,
    LIBRARY_PATH,
    YTDL_OPTIONS,
)

logger = logging.getLogger(__name__)

_INDEX_PATH = pathlib.Path(".cache/library_index.json")
_LIBRARY_DIR = pathlib.Path(LIBRARY_PATH)
_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)

_index: dict[str, dict] = {}
_download_sem = asyncio.Semaphore(1)
_pending_downloads: set[str] = set()


def _stable_query_hash(key: str) -> str:
    normalized = _normalize_search_text(key)
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:8]
    return f"yt_{digest}"


def track_video_id(track: dict, video_ref: str | None = None) -> str | None:
    return (
        _extract_video_id(track.get("video_id"))
        or _extract_video_id(track.get("webpage_url"))
        or _extract_video_id(video_ref)
    )


def track_id(track: dict) -> str:
    sid = track.get("spotify_id")
    if sid:
        return sid
    video_id = track_video_id(track)
    if video_id:
        return f"yt_{video_id}"
    key = track.get("yt_query") or track.get("title", "")
    return _stable_query_hash(key)


def _find_tid_by_video_id(video_id: str) -> str | None:
    canonical = f"yt_{video_id}"
    if canonical in _index and get_local_path(canonical):
        return canonical
    for tid, entry in _index.items():
        if entry.get("video_id") == video_id and get_local_path(tid):
            return tid
    return None


def _load_index() -> None:
    global _index
    if not _INDEX_PATH.exists():
        return
    try:
        data = json.loads(_INDEX_PATH.read_text())
        if isinstance(data, dict):
            _index = data
    except Exception as exc:
        logger.warning("library: failed to load index: %s", exc)


def _save_index() -> None:
    try:
        _INDEX_PATH.write_text(json.dumps(_index, indent=2))
    except Exception as exc:
        logger.warning("library: failed to save index: %s", exc)


_load_index()


def _file_size_mb() -> float:
    total = 0
    for entry in _index.values():
        path = pathlib.Path(entry.get("file_path", ""))
        if path.is_file():
            total += path.stat().st_size
    return total / (1024 * 1024)


def get_local_path(tid: str) -> pathlib.Path | None:
    if not LIBRARY_ENABLED:
        return None
    entry = _index.get(tid)
    if not entry:
        return None
    file_path = entry.get("file_path")
    if not file_path:
        return None
    path = pathlib.Path(file_path)
    if path.is_file():
        return path.resolve()
    return None


def get_entry(tid: str) -> dict | None:
    return _index.get(tid)


def track_from_entry(tid: str, entry: dict, *, requester: str = "📻 Radio") -> dict:
    file_path = entry.get("file_path")
    if not file_path:
        raise ValueError(f"library entry {tid} has no file_path")
    path = pathlib.Path(file_path).resolve()
    return {
        "title": entry.get("title", "?"),
        "yt_query": entry.get("yt_query", entry.get("title", "")),
        "url": str(path),
        "requester": requester,
        "artist": entry.get("artist", "Unknown"),
        "duration": entry.get("duration", 0),
        "thumbnail": entry.get("thumbnail", ""),
        "spotify_id": entry.get("spotify_id"),
        "artist_id": entry.get("artist_id"),
        "video_id": entry.get("video_id"),
        "local": True,
        "track_id": tid,
    }


def _normalize_search_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def search_index(query: str, limit: int = 5) -> list[tuple[str, dict]]:
    """Return top library index matches for a text query."""
    q = _normalize_search_text(query)
    if not q or not _index:
        return []

    words = q.split()
    scored: list[tuple[int, str, dict]] = []

    for tid, entry in _index.items():
        haystack = _normalize_search_text(
            f"{entry.get('title', '')} {entry.get('artist', '')} {entry.get('yt_query', '')}"
        )
        if not all(word in haystack for word in words):
            continue

        title = _normalize_search_text(entry.get("title", ""))
        artist = _normalize_search_text(entry.get("artist", ""))
        score = 0
        if q in title:
            score += 50
        elif title.startswith(q):
            score += 30
        if q in artist:
            score += 40
        if pathlib.Path(entry.get("file_path", "")).is_file():
            score += 5
        score += entry.get("play_count", 0) * 2 + entry.get("request_count", 0)
        scored.append((score, tid, entry))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [(tid, entry) for _score, tid, entry in scored[:limit]]


def entry_to_queue_track(tid: str, entry: dict, *, requester: str) -> dict:
    """Build a queue-ready track dict from a library index entry."""
    file_path = entry.get("file_path")
    if file_path and pathlib.Path(file_path).is_file():
        return track_from_entry(tid, entry, requester=requester)
    return {
        "title": entry.get("title", "?"),
        "yt_query": entry.get("yt_query", entry.get("title", "")),
        "url": None,
        "requester": requester,
        "artist": entry.get("artist", "Unknown"),
        "duration": entry.get("duration", 0),
        "thumbnail": entry.get("thumbnail", ""),
        "spotify_id": entry.get("spotify_id"),
        "artist_id": entry.get("artist_id"),
        "video_id": entry.get("video_id"),
        "track_id": tid,
    }


def resolve_local_track(track: dict) -> dict | None:
    """Return track with local file URL if cached on disk."""
    if not LIBRARY_ENABLED:
        return None
    tid = track_id(track)
    path = get_local_path(tid)
    if not path:
        video_id = track_video_id(track)
        if video_id:
            existing_tid = _find_tid_by_video_id(video_id)
            if existing_tid:
                tid = existing_tid
                path = get_local_path(tid)
    if not path:
        return None
    resolved = dict(track)
    resolved["url"] = str(path)
    resolved["local"] = True
    resolved["track_id"] = tid
    logger.info("library: hit local file for '%s' (%s)", track.get("title", tid), tid)
    return resolved


def record_play(track: dict) -> None:
    if not LIBRARY_ENABLED:
        return
    tid = track_id(track)
    entry = _index.setdefault(tid, {
        "title": track.get("title", "?"),
        "artist": track.get("artist", "Unknown"),
        "yt_query": track.get("yt_query", track.get("title", "")),
        "spotify_id": track.get("spotify_id"),
        "artist_id": track.get("artist_id"),
        "video_id": track.get("video_id"),
        "duration": track.get("duration", 0),
        "thumbnail": track.get("thumbnail", ""),
        "play_count": 0,
        "request_count": 0,
    })
    entry["play_count"] = entry.get("play_count", 0) + 1
    entry["last_played"] = time.time()
    if track.get("title"):
        entry["title"] = track["title"]
    _save_index()


def record_request(track: dict) -> None:
    if not LIBRARY_ENABLED:
        return
    tid = track_id(track)
    entry = _index.setdefault(tid, {
        "title": track.get("title", "?"),
        "artist": track.get("artist", "Unknown"),
        "yt_query": track.get("yt_query", track.get("title", "")),
        "spotify_id": track.get("spotify_id"),
        "artist_id": track.get("artist_id"),
        "video_id": track.get("video_id"),
        "duration": track.get("duration", 0),
        "thumbnail": track.get("thumbnail", ""),
        "play_count": 0,
        "request_count": 0,
    })
    entry["request_count"] = entry.get("request_count", 0) + 1
    entry["last_requested"] = time.time()
    _save_index()


def _extract_video_id(url_or_id: str | None) -> str | None:
    if not url_or_id:
        return None
    if re.fullmatch(r"[\w-]{11}", url_or_id):
        return url_or_id
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})", url_or_id)
    return m.group(1) if m else None


def _file_size_bytes(path: str) -> int | None:
    file = pathlib.Path(path)
    if file.is_file():
        return file.stat().st_size
    return None


def _upsert_entry_from_track(
    track: dict,
    file_path: str,
    video_id: str | None,
    *,
    tid: str | None = None,
) -> None:
    resolved_tid = tid or track_id(track)
    entry = _index.setdefault(resolved_tid, {})
    update = {
        "file_path": file_path,
        "title": track.get("title", entry.get("title", "?")),
        "artist": track.get("artist", entry.get("artist", "Unknown")),
        "yt_query": track.get("yt_query", entry.get("yt_query", "")),
        "spotify_id": track.get("spotify_id"),
        "artist_id": track.get("artist_id"),
        "video_id": video_id or entry.get("video_id"),
        "duration": track.get("duration", entry.get("duration", 0)),
        "thumbnail": track.get("thumbnail", entry.get("thumbnail", "")),
        "cached_at": time.time(),
        "play_count": entry.get("play_count", 0),
        "request_count": entry.get("request_count", 0),
    }
    file_size = _file_size_bytes(file_path)
    if file_size is not None:
        update["file_size_bytes"] = file_size
    entry.update(update)
    _save_index()


def _evict_if_needed() -> None:
    if not _index:
        return

    def _is_pinned(entry: dict) -> bool:
        return entry.get("play_count", 0) >= LIBRARY_MIN_PLAYS_TO_PIN

    while len(_index) > LIBRARY_MAX_TRACKS or _file_size_mb() > LIBRARY_MAX_MB:
        evictable = [
            (tid, entry) for tid, entry in _index.items()
            if not _is_pinned(entry)
        ]
        if not evictable:
            logger.warning("library: at capacity but all tracks are pinned")
            break
        evictable.sort(key=lambda x: x[1].get("last_played", x[1].get("cached_at", 0)))
        tid, entry = evictable[0]
        path = pathlib.Path(entry.get("file_path", ""))
        if path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("library: could not delete %s: %s", path, exc)
        del _index[tid]
        logger.info("library: evicted %s ('%s')", tid, entry.get("title", "?"))
    _save_index()


def _download_sync(video_id: str, tid: str, track: dict) -> str | None:
    from src.youtube import _YtDlpLogger, is_youtube_rate_limited, maybe_detect_rate_limit

    if is_youtube_rate_limited():
        return None

    url = f"https://www.youtube.com/watch?v={video_id}"
    outtmpl = str(_LIBRARY_DIR / f"{tid}.%(ext)s")

    def _run():
        opts = {
            **YTDL_OPTIONS,
            "outtmpl": outtmpl,
            "logger": _YtDlpLogger(),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    try:
        _run()
    except yt_dlp.utils.DownloadError as exc:
        maybe_detect_rate_limit(str(exc))
        logger.warning("library: download failed for %s: %s", video_id, exc)
        return None
    except Exception as exc:
        logger.warning("library: download failed for %s: %s", video_id, exc)
        return None

    for path in sorted(_LIBRARY_DIR.glob(f"{tid}.*")):
        if path.is_file() and path.suffix not in (".part", ".ytdl"):
            return str(path.resolve())
    return None


async def enqueue_download(track: dict, video_ref: str | None = None) -> None:
    if not LIBRARY_ENABLED or not LIBRARY_AUTO_DOWNLOAD:
        return

    video_id = track_video_id(track, video_ref)
    if not video_id:
        return

    existing_tid = _find_tid_by_video_id(video_id)
    if existing_tid:
        local_path = get_local_path(existing_tid)
        if local_path:
            _upsert_entry_from_track(track, str(local_path), video_id, tid=existing_tid)
            return

    tid = track_id(track)
    if get_local_path(tid):
        return
    if tid in _pending_downloads:
        return

    _pending_downloads.add(tid)

    async def _task():
        try:
            async with _download_sem:
                file_path = await asyncio.to_thread(_download_sync, video_id, tid, track)
            if file_path:
                _upsert_entry_from_track(track, file_path, video_id)
                _evict_if_needed()
                logger.info("library: cached '%s' -> %s", track.get("title", tid), file_path)
        finally:
            _pending_downloads.discard(tid)

    asyncio.create_task(_task())


async def get_radio_candidates(
    guild_id: int,
    mood: str,
    limit: int,
) -> list[dict]:
    """Return playable tracks from the local library for offline radio."""
    if not LIBRARY_ENABLED or not _index:
        return []

    mood_cluster = None
    if mood not in ("neutral", "mixed"):
        from src.radio import MOODS, _custom_moods, _GENRE_CLUSTER_MAP
        raw = _custom_moods.get(guild_id, {}).get(mood) or MOODS.get(mood, [])
        for g in raw:
            mood_cluster = _GENRE_CLUSTER_MAP.get(g)
            if mood_cluster:
                break

    candidates: list[tuple[int, dict, str]] = []
    for tid, entry in _index.items():
        path = pathlib.Path(entry.get("file_path", ""))
        if not path.is_file():
            continue
        score = entry.get("play_count", 0) * 2 + entry.get("request_count", 0)
        if mood_cluster and entry.get("artist_id"):
            try:
                from src.radio import get_track_cluster
                cluster = await get_track_cluster({"artist_id": entry["artist_id"]})
                if cluster == mood_cluster:
                    score += 10
            except Exception:
                pass
        candidates.append((score, entry, tid))

    if not candidates:
        return []

    candidates.sort(key=lambda x: x[0], reverse=True)
    top_pool = candidates[: max(limit * 3, 15)]
    random.shuffle(top_pool)
    selected = top_pool[:limit]

    return [
        track_from_entry(tid, entry, requester="📻 Radio (local)")
        for _score, entry, tid in selected
    ]


def get_stats() -> dict:
    pinned = sum(1 for e in _index.values() if e.get("play_count", 0) >= LIBRARY_MIN_PLAYS_TO_PIN)
    on_disk = sum(
        1 for e in _index.values()
        if pathlib.Path(e.get("file_path", "")).is_file()
    )
    top = sorted(
        _index.items(),
        key=lambda x: x[1].get("play_count", 0),
        reverse=True,
    )[:10]
    return {
        "total_indexed": len(_index),
        "on_disk": on_disk,
        "size_mb": round(_file_size_mb(), 1),
        "pinned": pinned,
        "top_plays": [
            (tid, e.get("title", "?"), e.get("play_count", 0))
            for tid, e in top
        ],
    }