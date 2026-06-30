"""Local music library — persistent audio cache, popularity tracking, offline radio."""
import asyncio
import hashlib
import json
import logging
import pathlib
import random
import re
import subprocess
import time

from src.config import (
    LIBRARY_AUTO_DOWNLOAD,
    LIBRARY_AUTO_ENRICH,
    LIBRARY_EMBED_METADATA,
    LIBRARY_ENABLED,
    LIBRARY_FETCH_COVERS,
    LIBRARY_LOCAL_HIT_MIN_SCORE,
    LIBRARY_LOCAL_HIT_VALIDATION_ENABLED,
    LIBRARY_MAX_MB,
    LIBRARY_MAX_TRACKS,
    LIBRARY_MIN_PLAYS_TO_PIN,
    LIBRARY_PATH,
    YTDL_OPTIONS,
)
from src.scoring import _score_candidate

from src.metadata import (
    fetch_genius_cover_and_meta,
    fetch_lastfm_album_cover_and_meta,
    fetch_spotify_cover_and_meta,
    get_covers_dir,
    get_local_cover_path,
    pick_best_image,
    download_image as _metadata_download_image,
    try_attach_spotify_id as _metadata_try_attach_spotify,
)

logger = logging.getLogger(__name__)

_INDEX_PATH = pathlib.Path(".cache/library_index.json")
_LIBRARY_DIR = pathlib.Path(LIBRARY_PATH)
_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)

_COVERS_DIR = get_covers_dir()  # delegated to metadata for single source of truth

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
    """Stable index key — video_id wins over spotify_id to avoid duplicate entries."""
    video_id = track_video_id(track)
    if video_id:
        return f"yt_{video_id}"
    sid = track.get("spotify_id")
    if sid:
        return sid
    key = track.get("yt_query") or track.get("title", "")
    return _stable_query_hash(key)


def _find_tid_by_video_id(video_id: str) -> str | None:
    canonical = f"yt_{video_id}"
    if canonical in _index:
        return canonical
    for tid, entry in _index.items():
        if entry.get("video_id") == video_id:
            return tid
    return None


def _merge_entries_into(canonical_tid: str, legacy_tid: str) -> bool:
    if legacy_tid == canonical_tid or legacy_tid not in _index:
        return False
    legacy = _index.pop(legacy_tid)
    canonical = _index.setdefault(canonical_tid, {})
    canonical["play_count"] = canonical.get("play_count", 0) + legacy.get("play_count", 0)
    canonical["request_count"] = canonical.get("request_count", 0) + legacy.get("request_count", 0)
    canonical["last_played"] = max(canonical.get("last_played", 0), legacy.get("last_played", 0))
    canonical["last_requested"] = max(
        canonical.get("last_requested", 0), legacy.get("last_requested", 0),
    )
    canonical["cached_at"] = max(canonical.get("cached_at", 0), legacy.get("cached_at", 0))
    if not get_local_path(canonical_tid) and legacy.get("file_path"):
        canonical["file_path"] = legacy["file_path"]
        if legacy.get("file_size_bytes"):
            canonical["file_size_bytes"] = legacy["file_size_bytes"]
    for field in (
        "spotify_id", "artist_id", "title", "artist", "thumbnail",
        "duration", "yt_query", "video_id",
        "album", "release_date", "cover_url", "genres",
        "local_cover", "genius_id", "genius_url", "lyrics_state", "spotify_refined",
    ):
        if not canonical.get(field) and legacy.get(field):
            canonical[field] = legacy[field]
    canonical["video_id"] = canonical.get("video_id") or legacy.get("video_id")
    logger.info(
        "library: merged %s into %s ('%s')",
        legacy_tid, canonical_tid, canonical.get("title", "?"),
    )
    return True


def _resolve_index_tid(track: dict) -> str:
    tid = track_id(track)
    video_id = track_video_id(track)
    sid = track.get("spotify_id")
    if not video_id:
        # no video, but still try to merge any duplicate sids to this tid (if sid present)
        changed = False
        if sid:
            for legacy_tid in [t for t, e in list(_index.items()) if e.get("spotify_id") == sid and t != tid]:
                if _merge_entries_into(tid, legacy_tid):
                    changed = True
        if changed:
            _save_index()
        return tid
    canonical = f"yt_{video_id}"
    changed = False
    for legacy_tid in [
        t for t, entry in list(_index.items())
        if entry.get("video_id") == video_id and t != canonical
    ]:
        if _merge_entries_into(canonical, legacy_tid):
            changed = True
    if tid != canonical and tid in _index:
        if _merge_entries_into(canonical, tid):
            changed = True
    # also merge any other entries that share the spotify_id (even if they lack video_id in entry)
    if sid:
        for legacy_tid in [t for t, e in list(_index.items()) if e.get("spotify_id") == sid and t != canonical]:
            if _merge_entries_into(canonical, legacy_tid):
                changed = True
    if changed:
        _save_index()
    return canonical


def _migrate_index_duplicates() -> None:
    by_video: dict[str, list[str]] = {}
    for tid, entry in _index.items():
        video_id = entry.get("video_id")
        if video_id:
            by_video.setdefault(video_id, []).append(tid)
    changed = False
    for video_id, tids in by_video.items():
        if len(tids) <= 1:
            continue
        canonical = f"yt_{video_id}"
        for tid in tids:
            if tid != canonical and _merge_entries_into(canonical, tid):
                changed = True
    if changed:
        _save_index()
        logger.info("library: migrated duplicate index entries by video_id")

    # Also deduplicate entries that share the same spotify_id (can happen if one was keyed by sid
    # before video_id was known, e.g. after restarts or before download completed).
    # Prefer the canonical yt_{video} form if present.
    by_sid: dict[str, list[str]] = {}
    for tid, entry in list(_index.items()):
        sid = entry.get("spotify_id")
        if sid:
            by_sid.setdefault(sid, []).append(tid)
    for sid, tids in by_sid.items():
        if len(tids) <= 1:
            continue
        # Prefer yt_... or any that has video_id
        canonical = None
        for t in tids:
            e = _index.get(t, {})
            if e.get("video_id") or t.startswith("yt_"):
                if canonical is None or (t.startswith("yt_") and not canonical.startswith("yt_")):
                    canonical = t
        if canonical is None:
            canonical = tids[0]
        for t in tids:
            if t != canonical:
                if _merge_entries_into(canonical, t):
                    changed = True
    if changed:
        _save_index()
        logger.info("library: migrated duplicate index entries by spotify_id")


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
_migrate_index_duplicates()


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
        "spotify_refined": bool(entry.get("spotify_refined", False)),
        "video_id": entry.get("video_id"),
        "album": entry.get("album", ""),
        "release_date": entry.get("release_date", ""),
        "cover_url": entry.get("cover_url", ""),
        "local_cover": entry.get("local_cover"),
        "genres": entry.get("genres") or [],
        "genius_id": entry.get("genius_id"),
        "genius_url": entry.get("genius_url", ""),
        "lyrics_state": entry.get("lyrics_state", ""),
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
            f"{entry.get('title', '')} {entry.get('artist', '')} {entry.get('yt_query', '')} {entry.get('album', '')}"
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
        "spotify_refined": bool(entry.get("spotify_refined", False)),
        "video_id": entry.get("video_id"),
        "album": entry.get("album", ""),
        "release_date": entry.get("release_date", ""),
        "cover_url": entry.get("cover_url", ""),
        "local_cover": entry.get("local_cover"),
        "genres": entry.get("genres") or [],
        "genius_id": entry.get("genius_id"),
        "genius_url": entry.get("genius_url", ""),
        "lyrics_state": entry.get("lyrics_state", ""),
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

    entry = _index.get(tid) or {}
    if not _local_hit_consistent(track, entry):
        _clear_conflicting_spotify_metadata(tid)
        logger.warning(
            "library: rejected local hit for '%s' (%s) due to low query/title coherence",
            track.get("title", tid),
            tid,
        )
        return None

    resolved = dict(track)
    resolved["url"] = str(path)
    resolved["local"] = True
    resolved["track_id"] = tid
    logger.info("library: hit local file for '%s' (%s)", track.get("title", tid), tid)
    return resolved


def _local_hit_consistent(track: dict, entry: dict) -> bool:
    if not LIBRARY_LOCAL_HIT_VALIDATION_ENABLED:
        return True
    query = (track.get("yt_query") or track.get("title") or "").strip()
    candidate_title = (entry.get("title") or "").strip()
    if not query or not candidate_title:
        return True
    candidate = {
        "title": candidate_title,
        "uploader": entry.get("artist") or "",
        "duration": entry.get("duration") or 0,
    }
    score = _score_candidate(query, candidate)
    if score >= LIBRARY_LOCAL_HIT_MIN_SCORE:
        return True
    logger.warning(
        "library: local hit score %.2f below threshold %.2f for query '%s' vs cached '%s'",
        score,
        LIBRARY_LOCAL_HIT_MIN_SCORE,
        query,
        candidate_title,
    )
    return False


def _clear_conflicting_spotify_metadata(tid: str) -> None:
    entry = _index.get(tid)
    if not entry:
        return
    touched = False
    for key in ("spotify_id", "artist_id", "cover_url", "local_cover", "album", "release_date"):
        if entry.get(key):
            entry.pop(key, None)
            touched = True
    entry["spotify_refined"] = False
    if touched:
        _save_index()


def record_play(track: dict) -> None:
    if not LIBRARY_ENABLED:
        return
    tid = _resolve_index_tid(track)
    entry = _index.setdefault(tid, {
        "title": track.get("title", "?"),
        "artist": track.get("artist", "Unknown"),
        "yt_query": track.get("yt_query", track.get("title", "")),
        "spotify_id": track.get("spotify_id"),
        "artist_id": track.get("artist_id"),
        "spotify_refined": bool(track.get("spotify_refined", False)),
        "video_id": track.get("video_id"),
        "duration": track.get("duration", 0),
        "thumbnail": track.get("thumbnail", ""),
        "album": track.get("album", ""),
        "release_date": track.get("release_date", ""),
        "cover_url": track.get("cover_url", ""),
        "genres": track.get("genres") or [],
        "local_cover": track.get("local_cover"),
        "genius_id": track.get("genius_id"),
        "genius_url": track.get("genius_url", ""),
        "lyrics_state": track.get("lyrics_state", ""),
        "play_count": 0,
        "request_count": 0,
    })
    entry["play_count"] = entry.get("play_count", 0) + 1
    entry["last_played"] = time.time()
    if track.get("title"):
        entry["title"] = track["title"]
    if track.get("spotify_id") and not entry.get("spotify_id"):
        entry["spotify_id"] = track["spotify_id"]
    if track.get("video_id") and not entry.get("video_id"):
        entry["video_id"] = track["video_id"]
    if track.get("spotify_refined"):
        entry["spotify_refined"] = True
    if track.get("album") and not entry.get("album"):
        entry["album"] = track["album"]
    if track.get("cover_url") and not entry.get("cover_url"):
        entry["cover_url"] = track["cover_url"]
    _save_index()

    if LIBRARY_ENABLED:
        # First-time play (no enriched_at) always triggers enrichment for artwork + rich metadata
        # (autonomous on initial discovery/search -> play). Subsequent plays respect LIBRARY_AUTO_ENRICH
        # to avoid excessive API calls.
        first_time = not entry.get("enriched_at")
        if first_time or (LIBRARY_AUTO_ENRICH and not entry.get("cover_url")):
            asyncio.create_task(enrich_entry(tid))  # fire-and-forget


def record_request(track: dict) -> None:
    if not LIBRARY_ENABLED:
        return
    tid = _resolve_index_tid(track)
    entry = _index.setdefault(tid, {
        "title": track.get("title", "?"),
        "artist": track.get("artist", "Unknown"),
        "yt_query": track.get("yt_query", track.get("title", "")),
        "spotify_id": track.get("spotify_id"),
        "artist_id": track.get("artist_id"),
        "spotify_refined": bool(track.get("spotify_refined", False)),
        "video_id": track.get("video_id"),
        "duration": track.get("duration", 0),
        "thumbnail": track.get("thumbnail", ""),
        "album": track.get("album", ""),
        "release_date": track.get("release_date", ""),
        "cover_url": track.get("cover_url", ""),
        "genres": track.get("genres") or [],
        "local_cover": track.get("local_cover"),
        "genius_id": track.get("genius_id"),
        "genius_url": track.get("genius_url", ""),
        "lyrics_state": track.get("lyrics_state", ""),
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


def _sanitize_audio_file(path: str) -> str:
    """Remux local audio file with FFmpeg to fix common container issues
    (e.g. 'timescale not set' in m4a/mp4 files from certain YT uploads).
    Uses -c copy for speed, no re-encode.

    SECURITY: subprocess.run is called with a list of arguments (no shell=True),
    so it is NOT vulnerable to command injection even if 'path' contained
    shell metacharacters. We also explicitly validate the path is inside
    the library directory.
    """
    try:
        lib_dir = _LIBRARY_DIR.resolve()
        p = pathlib.Path(path).resolve()
        if not str(p).startswith(str(lib_dir)):
            logger.warning("library: sanitize refused path outside library dir: %s", path)
            return path
        if not p.is_file():
            return path
        fixed = p.with_suffix(p.suffix + ".fix")
        cmd = [
            "ffmpeg", "-y", "-i", str(p),
            "-c", "copy",
            "-fflags", "+genpts",
            "-movflags", "+faststart",
            str(fixed),
        ]
        res = subprocess.run(
            cmd, capture_output=True, timeout=60, text=True, shell=False
        )
        if res.returncode == 0 and fixed.exists() and fixed.stat().st_size > 1000:
            p.unlink(missing_ok=True)
            fixed.rename(p)
            logger.info("library: sanitized audio %s (fixed timescale/container)", path)
            return str(p)
        else:
            fixed.unlink(missing_ok=True)
            if res.returncode != 0:
                logger.debug("library: sanitize ffmpeg failed for %s: %s", path, res.stderr[:200])
    except FileNotFoundError:
        # ffmpeg not in PATH, skip silently
        pass
    except Exception as exc:
        logger.debug("library: sanitize failed for %s: %s", path, exc)
    return path


def _upsert_entry_from_track(
    track: dict,
    file_path: str,
    video_id: str | None,
    *,
    tid: str | None = None,
) -> None:
    if tid is not None:
        resolved_tid = tid
    else:
        track_for_resolution = track
        if video_id and not track.get("video_id"):
            track_for_resolution = {**track, "video_id": video_id}
        resolved_tid = _resolve_index_tid(track_for_resolution)
    entry = _index.setdefault(resolved_tid, {})
    update = {
        "file_path": file_path,
        "title": track.get("title", entry.get("title", "?")),
        "artist": track.get("artist", entry.get("artist", "Unknown")),
        "yt_query": track.get("yt_query", entry.get("yt_query", "")),
        "spotify_id": track.get("spotify_id") if track.get("spotify_refined") else entry.get("spotify_id"),
        "artist_id": track.get("artist_id") if track.get("spotify_refined") else entry.get("artist_id"),
        "spotify_refined": bool(track.get("spotify_refined", entry.get("spotify_refined", False))),
        "video_id": video_id or entry.get("video_id"),
        "duration": track.get("duration", entry.get("duration", 0)),
        "thumbnail": track.get("thumbnail", entry.get("thumbnail", "")),
        "album": track.get("album", entry.get("album", "")),
        "release_date": track.get("release_date", entry.get("release_date", "")),
        "cover_url": track.get("cover_url", entry.get("cover_url", "")),
        "genres": track.get("genres") or entry.get("genres") or [],
        "local_cover": track.get("local_cover") or entry.get("local_cover"),
        "genius_id": track.get("genius_id") or entry.get("genius_id"),
        "genius_url": track.get("genius_url", entry.get("genius_url", "")),
        "lyrics_state": track.get("lyrics_state", entry.get("lyrics_state", "")),
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
        import yt_dlp  # lazy import so modules that only use library for stats/enrich (e.g. explorer) don't require yt-dlp
        opts = {
            **YTDL_OPTIONS,
            "outtmpl": outtmpl,
            "logger": _YtDlpLogger(),
            "writethumbnail": True,
        }
        if LIBRARY_EMBED_METADATA:
            # FFmpegMetadata writes tags from yt info; EmbedThumbnail attaches downloaded thumb
            opts.setdefault("postprocessors", [])
            opts["postprocessors"] = list(opts.get("postprocessors", [])) + [
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail", "already_have_thumbnail": False},
            ]
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
            resolved = str(path.resolve())
            resolved = _sanitize_audio_file(resolved)
            return resolved
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

    tid = _resolve_index_tid(track)
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
                _upsert_entry_from_track(track, file_path, video_id, tid=tid)
                _evict_if_needed()
                logger.info("library: cached '%s' -> %s", track.get("title", tid), file_path)
                # Always enrich on first addition to the local library (when a song is searched/played for the first time).
                # This ensures artwork and rich metadata (Spotify/Genius/Last.fm) are fetched autonomously at discovery time.
                try:
                    asyncio.create_task(enrich_entry(tid))
                except Exception:
                    pass  # non-fatal
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
    with_cover = sum(1 for e in _index.values() if e.get("cover_url") or e.get("local_cover"))
    enriched = sum(1 for e in _index.values() if e.get("enriched_at"))
    with_genius = sum(1 for e in _index.values() if e.get("genius_id") or e.get("genius_url"))
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
        "with_cover": with_cover,
        "enriched": enriched,
        "with_genius": with_genius,
        "top_plays": [
            (tid, e.get("title", "?"), e.get("play_count", 0))
            for tid, e in top
        ],
    }


# ---------------------------------------------------------------------------
# Enrichment: official metadata + artwork (Spotify-first) + autonomous scan
# ---------------------------------------------------------------------------

def _best_artwork_url(entry: dict) -> str:
    """Prefer official cover_url, then local_cover (as file url if needed), else thumbnail."""
    if entry.get("cover_url"):
        return entry["cover_url"]
    lc = entry.get("local_cover")
    if lc:
        p = pathlib.Path(lc)
        if p.is_file():
            return str(p.resolve())
    return entry.get("thumbnail", "")


async def ensure_local_cover(tid: str, entry: dict | None = None) -> str | None:
    """Ensure a local jpg cover for tid if we have a remote cover_url and LIBRARY_FETCH_COVERS.
    Returns local path str or None.
    """
    if not LIBRARY_FETCH_COVERS:
        return None
    entry = entry or _index.get(tid, {})
    if not entry:
        return None
    existing = entry.get("local_cover")
    if existing:
        p = pathlib.Path(existing)
        if p.is_file():
            return str(p.resolve())
    url = entry.get("cover_url")
    if not url or not url.startswith(("http://", "https://")):
        return None  # yt thumb or already local; skip heavy download unless we want to cache yt too
    dest = get_local_cover_path(tid)
    ok = await _metadata_download_image(url, dest)
    if ok:
        entry["local_cover"] = str(dest.resolve())
        _save_index()
        logger.info("library: cached local cover for %s -> %s", tid, dest)
        return str(dest.resolve())
    return None


async def enrich_entry(tid: str) -> bool:
    """Enrich a single library entry with Spotify/Last.fm/Genius metadata + cover. Idempotent-ish.

    Genius adds high-quality song art (often official), genius_url, lyrics_state.
    Returns True if entry was updated with new useful data.
    """
    entry = _index.get(tid)
    if not entry:
        return False
    updated = False
    now = time.time()

    # Skip heavy re-enrichment (Spotify/Genius/LastFM fetches) if recently enriched AND we already have artwork.
    # This allows retries for tracks that didn't get a cover on previous attempts (e.g. no Genius token at the time, poor match, etc.).
    recently = entry.get("enriched_at") and (now - entry["enriched_at"] < 86400 * 7)
    has_artwork = bool(entry.get("cover_url"))
    if recently and has_artwork:
        # Only skip full re-fetch when we successfully got artwork before
        if LIBRARY_FETCH_COVERS and not entry.get("local_cover") and entry.get("cover_url"):
            await ensure_local_cover(tid, entry)
        return False

    spotify_trusted = bool(entry.get("spotify_refined", False))
    sid = entry.get("spotify_id") if spotify_trusted else None
    meta = None
    if sid:
        meta = await fetch_spotify_cover_and_meta(sid)
    elif spotify_trusted:
        # Always try to attach Spotify ID using title/artist during enrich (for manual !library enrich / script / explorer button).
        # This enables full Spotify metadata + makes future auto-enrich more powerful.
        # (The LIBRARY_AUTO_ENRICH flag mainly controls whether to kick off enrich on every play for tracks without sid.)
        title = entry.get("title", "")
        artist = entry.get("artist", "")
        if title:
            attach = await _metadata_try_attach_spotify(title, artist)  # from metadata (uses scoring)
            if attach and attach.get("spotify_id"):
                entry["spotify_id"] = attach["spotify_id"]
                entry["artist_id"] = entry.get("artist_id") or None  # may fill later
                if attach.get("album"):
                    entry["album"] = attach["album"]
                if attach.get("cover_url"):
                    entry["cover_url"] = attach["cover_url"]
                if attach.get("release_date"):
                    entry["release_date"] = attach["release_date"]
                updated = True
                sid = attach["spotify_id"]
                entry["spotify_refined"] = True
                meta = await fetch_spotify_cover_and_meta(sid)

    if meta:
        if meta.get("album") and not entry.get("album"):
            entry["album"] = meta["album"]
            updated = True
        if meta.get("release_date") and not entry.get("release_date"):
            entry["release_date"] = meta["release_date"]
            updated = True
        if meta.get("cover_url") and not entry.get("cover_url"):
            entry["cover_url"] = meta["cover_url"]
            updated = True
        if meta.get("genres"):
            entry["genres"] = list(dict.fromkeys((entry.get("genres") or []) + meta["genres"]))
            updated = True
        # backfill artist if better
        if meta.get("artist") and entry.get("artist", "Unknown").lower() in ("unknown", "?", ""):
            entry["artist"] = meta["artist"]
            updated = True

    # Last.fm fallback for cover/album if still missing key pieces
    if not entry.get("cover_url") or not entry.get("album"):
        artist = entry.get("artist", "")
        album = entry.get("album", "")
        if artist and album:
            lm = await fetch_lastfm_album_cover_and_meta(artist, album)
            if lm:
                if lm.get("cover_url") and not entry.get("cover_url"):
                    entry["cover_url"] = lm["cover_url"]
                    updated = True
                if lm.get("album") and not entry.get("album"):
                    entry["album"] = lm["album"]
                    updated = True
                if lm.get("genres"):
                    entry["genres"] = list(dict.fromkeys((entry.get("genres") or []) + lm["genres"]))
                    updated = True

    # Genius API tier: excellent for song_art_image_url (official artwork complement) + lyrics/genius url
    if not entry.get("cover_url") or not entry.get("genius_url"):
        title = entry.get("title", "")
        artist = entry.get("artist", "")
        if title:
            logger.info("library: attempting Genius enrichment for '%s' by '%s' (tid=%s)", title, artist, tid)
            gm = await fetch_genius_cover_and_meta(title, artist)
            if gm:
                if gm.get("cover_url") and not entry.get("cover_url"):
                    entry["cover_url"] = gm["cover_url"]
                    updated = True
                if gm.get("album") and not entry.get("album"):
                    entry["album"] = gm["album"]
                    updated = True
                if gm.get("genius_id"):
                    entry["genius_id"] = gm["genius_id"]
                    updated = True
                if gm.get("genius_url") and not entry.get("genius_url"):
                    entry["genius_url"] = gm["genius_url"]
                    updated = True
                if gm.get("lyrics_state"):
                    entry["lyrics_state"] = gm["lyrics_state"]
                    updated = True
                logger.info("library: Genius contributed data for %s (cover=%s, url=%s)", tid, bool(gm.get("cover_url")), bool(gm.get("genius_url")))
            else:
                logger.info("library: no useful Genius data for '%s' (check GENIUS_ACCESS_TOKEN and logs)", title)

    # Always try to materialize local cover for autonomy
    if LIBRARY_FETCH_COVERS and entry.get("cover_url") and not entry.get("local_cover"):
        await ensure_local_cover(tid, entry)
        if entry.get("local_cover"):
            updated = True

    entry["enriched_at"] = now
    if updated:
        _save_index()
        logger.info("library: enriched %s ('%s' album=%s cover=%s)", tid, entry.get("title", "?"), bool(entry.get("album")), bool(entry.get("cover_url")))
        # Best-effort: if this track is currently playing, refresh its player embed so the new 1:1 artwork appears
        # instead of stale YT thumbnail. Overlay in _build_v2_payload also helps on next build.
        try:
            from src.playback import guild_sessions, update_player_embed, bot as _bot
            for gid, sess in list(guild_sessions.items()):
                np = sess.now_playing
                if np and (np.get("track_id") == tid or (tid.startswith("yt_") and np.get("video_id") == tid[3:])):
                    if sess.player_channel and _bot:
                        g = _bot.get_guild(gid)
                        if g:
                            asyncio.create_task(update_player_embed(g, sess.player_channel))
        except Exception:
            pass  # no hard failure
    return updated


async def scan_and_enrich_library(*, max_items: int | None = None, force: bool = False) -> dict:
    """Autonomous library organization/enrichment pass (Spotify + Last.fm + Genius).

    Scans index, enriches entries missing rich meta or old enriched_at.
    Returns summary dict.
    """
    if not LIBRARY_ENABLED or not _index:
        return {"processed": 0, "updated": 0, "skipped": 0, "errors": 0}

    processed = updated = skipped = errors = 0
    items = list(_index.items())
    if max_items:
        items = items[:max_items]

    for tid, entry in items:
        processed += 1
        try:
            if not force and entry.get("enriched_at") and entry.get("cover_url"):
                skipped += 1
                continue
            did = await enrich_entry(tid)
            if did:
                updated += 1
            else:
                skipped += 1
        except Exception as exc:
            errors += 1
            logger.warning("library: enrich error for %s: %s", tid, exc)

    _save_index()
    summary = {
        "processed": processed,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }
    logger.info("library: scan_and_enrich complete %s", summary)
    return summary


def get_best_artwork(tid: str | None = None, entry: dict | None = None) -> str:
    """Convenience: best displayable artwork url for a tid or pre-fetched entry."""
    if entry is None:
        entry = _index.get(tid or "", {})
    return _best_artwork_url(entry) if entry else ""
