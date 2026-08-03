"""Local music library — persistent audio cache, popularity tracking, offline radio."""
import asyncio
import hashlib
import json
import logging
import pathlib
import random
import re
import shutil
import subprocess
import time

from src.config import (
    LIBRARY_AUTO_DOWNLOAD,
    LIBRARY_AUTO_ENRICH,
    LIBRARY_EMBED_METADATA,
    LIBRARY_EMERGENCY_EVICT_PINS,
    LIBRARY_ENABLED,
    LIBRARY_FETCH_COVERS,
    LIBRARY_LOCAL_HIT_MIN_SCORE,
    LIBRARY_LOCAL_HIT_VALIDATION_ENABLED,
    LIBRARY_MAX_DURATION_SEC,
    LIBRARY_MAX_FILE_MB,
    LIBRARY_MAX_MB,
    LIBRARY_MAX_TRACKS,
    LIBRARY_MIN_FREE_MB,
    LIBRARY_MIN_PLAYS_TO_PIN,
    LIBRARY_ORPHAN_MIN_AGE_SEC,
    LIBRARY_PATH,
    LIBRARY_REJECT_LIVE,
    LIBRARY_TARGET_FREE_MB,
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


def _add_source(entry: dict, source: str) -> None:
    """Append a provenance tag to entry['sources'] (unique, ordered)."""
    if not source:
        return
    sources = entry.get("sources")
    if not isinstance(sources, list):
        sources = []
    if source not in sources:
        sources.append(source)
    entry["sources"] = sources
    # Primary origin for simple UIs
    if source == "request" or not entry.get("origin"):
        entry["origin"] = source


def _normalize_track_identity(track: dict, entry: dict | None = None) -> tuple[str, str]:
    """Parse Artist - Title when artist is unknown; returns (artist, title)."""
    from src.catalog import parse_artist_title

    base_artist = (track.get("artist") if track else None) or (entry or {}).get("artist")
    base_title = (track.get("title") if track else None) or (entry or {}).get("title")
    return parse_artist_title(base_artist, base_title)


def _apply_identity_to_entry(entry: dict, track: dict | None = None) -> None:
    artist, title = _normalize_track_identity(track or {}, entry)
    # Only fill Unknown / empty artist
    cur = (entry.get("artist") or "").strip()
    if not cur or cur.lower() in {"unknown", "—", "?", "desconocido"}:
        entry["artist"] = artist
    if title and (not entry.get("title") or entry.get("title") == "?"):
        entry["title"] = title
    entry["display_artist"] = entry.get("artist") or artist
    entry["display_title"] = entry.get("title") or title

_index: dict[str, dict] = {}
_index_mtime: float | None = None
_download_sem = asyncio.Semaphore(1)
_pending_downloads: set[str] = set()

# Allowed on-disk formats for library cache (never keep full video containers).
_LIBRARY_AUDIO_SUFFIXES = {".m4a", ".opus", ".webm", ".mp3", ".ogg", ".flac", ".wav", ".aac"}
_LIBRARY_SKIP_SUFFIXES = {".part", ".ytdl", ".jpg", ".jpeg", ".png", ".webp", ".json", ".vtt", ".srt"}

# 24/7 radio / livestream title patterns — not individual songs.
# Avoid bare "radio" (would block e.g. "Video Killed the Radio Star").
_LIVE_STREAM_TITLE_RE = re.compile(
    r"(?:"
    r"\b24\s*/\s*7\b"
    r"|\bnon[\s-]?stop\b"
    r"|\bnonstop\b"
    r"|\blive\s+stream\b"
    r"|\blivestream\b"
    r"|\blistening\s+party\b"
    r"|\bradio\s+(?:hits|mix|station|stream|live|24)"
    r"|\b(?:classic\s+)?(?:rock|jazz|pop|hits)\s+radio\b"
    r")",
    re.IGNORECASE,
)


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


def _index_file_mtime() -> float | None:
    try:
        return _INDEX_PATH.stat().st_mtime
    except OSError:
        return None


def _load_index() -> None:
    global _index, _index_mtime
    if not _INDEX_PATH.exists():
        _index = {}
        _index_mtime = None
        return
    try:
        data = json.loads(_INDEX_PATH.read_text())
        if isinstance(data, dict):
            _index = data
        _index_mtime = _index_file_mtime()
    except Exception as exc:
        logger.warning("library: failed to load index: %s", exc)


def _maybe_reload_index() -> None:
    """Pick up external edits (explorer delete/dedupe) without restarting the bot."""
    global _index_mtime
    current = _index_file_mtime()
    if current is None:
        if _index_mtime is not None:
            _load_index()
        return
    if _index_mtime is None or current != _index_mtime:
        logger.info("library: reloading index (mtime changed)")
        _load_index()


def _save_index() -> None:
    global _index_mtime
    try:
        tmp = _INDEX_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_index, indent=2))
        tmp.replace(_INDEX_PATH)
        _index_mtime = _index_file_mtime()
    except Exception as exc:
        logger.warning("library: failed to save index: %s", exc)


_load_index()
_migrate_index_duplicates()


def cache_reject_reason(meta: dict) -> str | None:
    """Return a short reason if *meta* must not be written into the library cache.

    Used before/after download so live radio streams and huge files never land
    on small disks. Does not gate playback selection by itself.
    """
    title = str(meta.get("title") or "")
    blob = f"{title} {meta.get('yt_query') or meta.get('artist') or ''}"

    if LIBRARY_REJECT_LIVE:
        if meta.get("is_live") is True:
            return "is_live"
        live_status = str(meta.get("live_status") or "").lower()
        if live_status in ("is_live", "is_upcoming", "was_live") and not meta.get("duration"):
            return f"live_status={live_status}"
        if _LIVE_STREAM_TITLE_RE.search(blob):
            return "live_or_radio_title"

    duration = meta.get("duration")
    if duration is not None:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = None
    if duration is not None:
        if duration <= 0:
            return "duration_zero_or_unknown"
        if duration > LIBRARY_MAX_DURATION_SEC:
            return f"duration>{LIBRARY_MAX_DURATION_SEC}s"

    max_bytes = LIBRARY_MAX_FILE_MB * 1024 * 1024
    for key in ("file_size_bytes", "filesize", "filesize_approx"):
        size = meta.get(key)
        if size is None:
            continue
        try:
            size = int(size)
        except (TypeError, ValueError):
            continue
        if size > max_bytes:
            return f"filesize>{LIBRARY_MAX_FILE_MB}MB"

    path = meta.get("file_path") or meta.get("filepath")
    if path:
        suffix = pathlib.Path(str(path)).suffix.lower()
        if suffix and suffix not in _LIBRARY_AUDIO_SUFFIXES and suffix not in _LIBRARY_SKIP_SUFFIXES:
            # .mp4 video containers are the main offender (multi-GB streams).
            if suffix in {".mp4", ".mkv", ".avi", ".mov"}:
                return f"non_audio_ext={suffix}"

    return None


def _file_size_mb() -> float:
    total = 0
    for entry in _index.values():
        path = pathlib.Path(entry.get("file_path", ""))
        if path.is_file():
            total += path.stat().st_size
    return total / (1024 * 1024)


def disk_free_mb(path: pathlib.Path | None = None) -> float:
    """Free space (MiB) on the filesystem that holds the library.

    Returns +inf if the path cannot be measured so callers fail open
    (do not wipe the library or block playback on a flaky stat).
    """
    root = path or _LIBRARY_DIR
    try:
        root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(root).free / (1024 * 1024)
    except OSError as exc:
        logger.warning("library: disk_usage failed for %s: %s", root, exc)
        return float("inf")


def _is_pinned(entry: dict) -> bool:
    return entry.get("play_count", 0) >= LIBRARY_MIN_PLAYS_TO_PIN


def _pick_eviction_victim(
    *,
    protect_tid: str | None = None,
    allow_pins: bool = False,
) -> str | None:
    """Return the LRU track id eligible for eviction, or None."""
    candidates: list[tuple[float, str]] = []
    for tid, entry in _index.items():
        if protect_tid and tid == protect_tid:
            continue
        if _is_pinned(entry) and not allow_pins:
            continue
        sort_key = float(entry.get("last_played") or entry.get("cached_at") or 0)
        candidates.append((sort_key, tid))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def reclaim_disk(
    *,
    reason: str = "",
    protect_tid: str | None = None,
) -> dict:
    """Evict library tracks until under caps and free-space headroom.

    Source of truth for disk pressure is ``disk_free_mb()`` on the library
    filesystem, plus ``LIBRARY_MAX_MB`` / ``LIBRARY_MAX_TRACKS``. When free
    space starts below ``LIBRARY_MIN_FREE_MB``, reclaim until
    ``LIBRARY_TARGET_FREE_MB`` (hysteresis). Never deletes *protect_tid*.
    """
    stats = {
        "evicted": 0,
        "bytes_freed": 0,
        "emergency_pins": 0,
        "reason": reason,
        "free_mb_before": round(disk_free_mb(), 1),
    }
    if not _index:
        stats["free_mb_after"] = stats["free_mb_before"]
        return stats

    free_pressure = disk_free_mb() < LIBRARY_MIN_FREE_MB

    def _needs_more() -> bool:
        if len(_index) > LIBRARY_MAX_TRACKS:
            return True
        if _file_size_mb() > LIBRARY_MAX_MB:
            return True
        free = disk_free_mb()
        if free == float("inf"):
            return False
        # Under free-space pressure, reclaim up to TARGET; otherwise only enforce MIN.
        target = LIBRARY_TARGET_FREE_MB if free_pressure else LIBRARY_MIN_FREE_MB
        return free < target

    while _needs_more():
        victim = _pick_eviction_victim(protect_tid=protect_tid, allow_pins=False)
        used_emergency = False
        if victim is None and LIBRARY_EMERGENCY_EVICT_PINS and disk_free_mb() < LIBRARY_MIN_FREE_MB:
            victim = _pick_eviction_victim(protect_tid=protect_tid, allow_pins=True)
            used_emergency = victim is not None
        if victim is None:
            logger.warning(
                "library: reclaim stopped (%s); no victims free=%.0fMB size=%.0fMB tracks=%s",
                reason or "?",
                disk_free_mb(),
                _file_size_mb(),
                len(_index),
            )
            break

        result = delete_track_entry(
            _index,
            victim,
            library_dir=_LIBRARY_DIR,
            covers_dir=_COVERS_DIR,
        )
        stats["evicted"] += 1
        stats["bytes_freed"] += int(result.get("bytes_freed") or 0)
        if used_emergency:
            stats["emergency_pins"] += 1
            logger.warning(
                "library: emergency pin eviction %s ('%s') reason=%s",
                victim,
                result.get("title") or "?",
                reason or "?",
            )
        else:
            logger.info(
                "library: reclaimed %s ('%s') reason=%s",
                victim,
                result.get("title") or "?",
                reason or "?",
            )

    if stats["evicted"]:
        _save_index()

    free_after = disk_free_mb()
    stats["free_mb_after"] = round(free_after, 1) if free_after != float("inf") else None
    if free_after < LIBRARY_MIN_FREE_MB:
        logger.warning(
            "library: disk pressure after reclaim free=%.0fMB min=%s (reason=%s, evicted=%s)",
            free_after,
            LIBRARY_MIN_FREE_MB,
            reason or "?",
            stats["evicted"],
        )
    return stats


def _is_temp_artifact_name(name: str) -> bool:
    lower = name.lower()
    if lower.endswith((".part", ".ytdl")):
        return True
    if ".temp." in lower or lower.endswith(".temp"):
        return True
    return False


def _library_stem_from_name(name: str) -> str:
    """Leading id for library files (``yt_xxx.temp.m4a`` → ``yt_xxx``)."""
    return name.split(".", 1)[0]


# Abandoned yt-dlp partials only; never touch in-flight downloads (see pending set).
_LIBRARY_TEMP_MIN_AGE_SEC = 300


def sweep_library_temps(*, min_age_sec: int | None = None) -> dict:
    """Remove yt-dlp partials and temp remux artifacts under the library dir.

    Skips stems in ``_pending_downloads`` so the reaper cannot delete a
    ``.temp.m4a`` while yt-dlp is still remuxing (that race renames into ENOENT
    and can stall the host). Age-gates the rest so a crashed download's temps
    are cleaned after a few minutes without racing live writes.
    """
    age_gate = (
        _LIBRARY_TEMP_MIN_AGE_SEC if min_age_sec is None else max(0, int(min_age_sec))
    )
    removed = 0
    bytes_freed = 0
    skipped_pending = 0
    skipped_young = 0
    if not _LIBRARY_DIR.is_dir():
        return {"removed": 0, "bytes_freed": 0, "skipped_pending": 0, "skipped_young": 0}
    now = time.time()
    for path in _LIBRARY_DIR.iterdir():
        if not path.is_file() or not _is_temp_artifact_name(path.name):
            continue
        stem = _library_stem_from_name(path.name)
        if stem in _pending_downloads:
            skipped_pending += 1
            continue
        try:
            st = path.stat()
            if age_gate > 0 and (now - st.st_mtime) < age_gate:
                skipped_young += 1
                continue
            size = st.st_size
            path.unlink()
            removed += 1
            bytes_freed += size
        except OSError as exc:
            logger.warning("library: could not remove temp %s: %s", path, exc)
    if removed or skipped_pending:
        logger.info(
            "library: swept %s temp artifacts (%s bytes) skipped_pending=%s skipped_young=%s",
            removed,
            bytes_freed,
            skipped_pending,
            skipped_young,
        )
    return {
        "removed": removed,
        "bytes_freed": bytes_freed,
        "skipped_pending": skipped_pending,
        "skipped_young": skipped_young,
    }


def sweep_orphan_index_entries() -> dict:
    """Drop index rows whose audio file is missing on disk."""
    removed = 0
    stale = [
        tid
        for tid, entry in list(_index.items())
        if tid not in _pending_downloads
        and not pathlib.Path(entry.get("file_path") or "").is_file()
    ]
    for tid in stale:
        del _index[tid]
        removed += 1
    if removed:
        _save_index()
        logger.info("library: removed %s orphan index entries (missing files)", removed)
    return {"removed": removed, "bytes_freed": 0}


def sweep_orphan_files(*, min_age_sec: int | None = None) -> dict:
    """Delete library audio files not referenced by the index (age-gated)."""
    age_gate = LIBRARY_ORPHAN_MIN_AGE_SEC if min_age_sec is None else max(0, min_age_sec)
    removed = 0
    bytes_freed = 0
    if not _LIBRARY_DIR.is_dir():
        return {"removed": 0, "bytes_freed": 0}

    referenced: set[pathlib.Path] = set()
    referenced_stems: set[str] = set()
    for tid, entry in _index.items():
        referenced_stems.add(tid)
        fp = entry.get("file_path")
        if not fp:
            continue
        try:
            referenced.add(pathlib.Path(fp).resolve())
        except OSError:
            pass

    now = time.time()
    for path in _LIBRARY_DIR.iterdir():
        if not path.is_file():
            continue
        if _is_temp_artifact_name(path.name):
            continue  # temps handled separately
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".json"}:
            continue
        stem = _library_stem_from_name(path.name)
        if stem in _pending_downloads or stem in referenced_stems:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in referenced:
            continue
        try:
            st = path.stat()
            if now - st.st_mtime < age_gate:
                continue
            size = st.st_size
            path.unlink()
            removed += 1
            bytes_freed += size
        except OSError as exc:
            logger.warning("library: could not remove orphan file %s: %s", path, exc)

    if removed:
        logger.info("library: swept %s orphan files (%s bytes)", removed, bytes_freed)
    return {"removed": removed, "bytes_freed": bytes_freed}


def run_disk_maintenance(*, reason: str = "reaper") -> dict:
    """Full maintenance pass: temps, orphans, DJ cache, then reclaim.

    Safe to call from a background thread via ``asyncio.to_thread``.
    """
    _maybe_reload_index()
    free_before = disk_free_mb()
    summary: dict = {
        "reason": reason,
        "free_mb_before": None if free_before == float("inf") else round(free_before, 1),
        "temps": {"removed": 0, "bytes_freed": 0},
        "orphan_index": {"removed": 0, "bytes_freed": 0},
        "orphan_files": {"removed": 0, "bytes_freed": 0},
        "dj_audio": {"removed": 0, "bytes_freed": 0},
        "reclaim": {"evicted": 0, "bytes_freed": 0, "emergency_pins": 0},
    }

    summary["temps"] = sweep_library_temps()
    if LIBRARY_ENABLED:
        summary["orphan_index"] = sweep_orphan_index_entries()
        summary["orphan_files"] = sweep_orphan_files()

    try:
        from src.dj_announcer import sweep_dj_audio_cache

        summary["dj_audio"] = sweep_dj_audio_cache()
    except Exception as exc:
        logger.warning("library: dj_audio sweep failed: %s", exc)

    if LIBRARY_ENABLED:
        summary["reclaim"] = reclaim_disk(reason=reason)

    free_after = disk_free_mb()
    summary["free_mb_after"] = None if free_after == float("inf") else round(free_after, 1)
    summary["disk_pressure"] = (
        free_after != float("inf") and free_after < LIBRARY_MIN_FREE_MB
    )
    summary["bytes_freed_total"] = (
        int(summary["temps"].get("bytes_freed") or 0)
        + int(summary["orphan_files"].get("bytes_freed") or 0)
        + int(summary["dj_audio"].get("bytes_freed") or 0)
        + int(summary["reclaim"].get("bytes_freed") or 0)
    )
    logger.info(
        "library: maintenance done reason=%s free=%.0f→%sMB freed=%sB "
        "temps=%s orphans_idx=%s orphans_fs=%s dj=%s evicted=%s pressure=%s",
        reason,
        free_before if free_before != float("inf") else -1,
        summary["free_mb_after"],
        summary["bytes_freed_total"],
        summary["temps"].get("removed"),
        summary["orphan_index"].get("removed"),
        summary["orphan_files"].get("removed"),
        summary["dj_audio"].get("removed"),
        summary["reclaim"].get("evicted"),
        summary["disk_pressure"],
    )
    return summary


def get_local_path(tid: str) -> pathlib.Path | None:
    if not LIBRARY_ENABLED:
        return None
    _maybe_reload_index()
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


def _path_is_under(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def delete_track_entry(
    index: dict[str, dict],
    tid: str,
    *,
    library_dir: pathlib.Path,
    covers_dir: pathlib.Path | None = None,
) -> dict:
    """Remove *tid* from *index* and delete files under *library_dir*.

    Safe for use by the bot and the explorer. Mutates *index*. Only unlinks
    paths that resolve inside *library_dir* (or *covers_dir* for local covers).
    """
    library_dir = library_dir.resolve()
    entry = index.get(tid)
    files_removed: list[str] = []
    bytes_freed = 0

    candidates: list[pathlib.Path] = []
    if entry and entry.get("file_path"):
        candidates.append(pathlib.Path(entry["file_path"]))
    if library_dir.is_dir():
        candidates.extend(library_dir.glob(f"{tid}.*"))

    seen: set[pathlib.Path] = set()
    for raw in candidates:
        try:
            path = raw.resolve()
        except OSError:
            continue
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file():
            continue
        if not _path_is_under(path, library_dir):
            logger.warning("library: refuse delete outside library dir: %s", path)
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            files_removed.append(str(path))
            bytes_freed += size
        except OSError as exc:
            logger.warning("library: could not delete %s: %s", path, exc)

    if entry and covers_dir is not None:
        cover = entry.get("local_cover")
        if cover:
            cover_path = pathlib.Path(cover)
            try:
                cover_resolved = cover_path.resolve()
                covers_root = covers_dir.resolve()
                if cover_resolved.is_file() and _path_is_under(cover_resolved, covers_root):
                    # Only remove cover if basename is tied to this tid (avoid shared art).
                    if tid in cover_resolved.stem or cover_resolved.stem.startswith(tid):
                        size = cover_resolved.stat().st_size
                        cover_resolved.unlink()
                        files_removed.append(str(cover_resolved))
                        bytes_freed += size
            except OSError as exc:
                logger.warning("library: could not delete cover %s: %s", cover, exc)

    existed = tid in index
    if existed:
        del index[tid]

    return {
        "deleted": existed or bool(files_removed),
        "track_id": tid,
        "bytes_freed": bytes_freed,
        "files_removed": files_removed,
        "had_index_entry": existed,
        "title": (entry or {}).get("title", ""),
    }


def delete_track(tid: str) -> dict:
    """Delete a library track from the bot's index + library directory."""
    _maybe_reload_index()
    result = delete_track_entry(
        _index,
        tid,
        library_dir=_LIBRARY_DIR,
        covers_dir=_COVERS_DIR,
    )
    if result["deleted"]:
        _save_index()
        logger.info(
            "library: deleted %s ('%s') freed %s bytes",
            tid, result.get("title") or "?", result.get("bytes_freed", 0),
        )
    return result


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
        "sources": [],
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
    _apply_identity_to_entry(entry, track)
    # Provenance: radio/seed fills vs generic play
    if track.get("from_fm_seed") or track.get("is_radio_stream") or (
        str(track.get("requester") or "").startswith("📻")
    ):
        _add_source(entry, "radio")
    else:
        _add_source(entry, "play")
    _save_index()
    try:
        from src.catalog import invalidate_catalog_cache
        invalidate_catalog_cache()
    except Exception:
        pass

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
        "sources": [],
    })
    entry["request_count"] = entry.get("request_count", 0) + 1
    entry["last_requested"] = time.time()
    if track.get("title"):
        entry["title"] = track["title"]
    if track.get("artist"):
        entry["artist"] = track["artist"]
    if track.get("album") and not entry.get("album"):
        entry["album"] = track["album"]
    if track.get("cover_url") and not entry.get("cover_url"):
        entry["cover_url"] = track["cover_url"]
    _apply_identity_to_entry(entry, track)
    _add_source(entry, "request")
    _save_index()
    try:
        from src.catalog import invalidate_catalog_cache
        invalidate_catalog_cache()
    except Exception:
        pass


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
    _apply_identity_to_entry(entry, track)
    if track.get("from_fm_seed") or str(track.get("requester") or "").startswith("📻"):
        _add_source(entry, "radio")
    file_size = _file_size_bytes(file_path)
    if file_size is not None:
        update["file_size_bytes"] = file_size
    entry.update(update)
    _save_index()


def _evict_if_needed(*, protect_tid: str | None = None) -> None:
    """Best-effort reclaim after caching a new file (caps + free-space headroom)."""
    reclaim_disk(reason="post-cache", protect_tid=protect_tid)


def _cleanup_tid_artifacts(tid: str) -> None:
    """Remove partial/rejected download artifacts for *tid* under the library dir."""
    if not _LIBRARY_DIR.is_dir():
        return
    for path in _LIBRARY_DIR.glob(f"{tid}.*"):
        if not path.is_file():
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("library: could not remove artifact %s: %s", path, exc)


def _validate_downloaded_file(path: str, track: dict) -> str | None:
    """Return resolved path if cacheable, else delete and return None."""
    p = pathlib.Path(path)
    meta = {
        **track,
        "file_path": str(p),
        "file_size_bytes": p.stat().st_size if p.is_file() else None,
        "duration": track.get("duration"),
        "title": track.get("title"),
    }
    reason = cache_reject_reason(meta)
    if reason:
        logger.warning(
            "library: rejecting cached file for '%s' (%s): %s",
            track.get("title", p.name), p, reason,
        )
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("library: could not remove rejected file %s: %s", p, exc)
        # Also drop sidecars / wrong-ext downloads for this tid stem.
        stem = p.stem
        if stem:
            _cleanup_tid_artifacts(stem)
        return None
    return str(p.resolve())


def _download_sync(video_id: str, tid: str, track: dict) -> str | None:
    from src.youtube import _YtDlpLogger, is_youtube_rate_limited, maybe_detect_rate_limit

    if is_youtube_rate_limited():
        return None

    reason = cache_reject_reason(track)
    if reason:
        logger.info(
            "library: skip download for %s ('%s'): %s",
            video_id, track.get("title", "?"), reason,
        )
        return None

    # Free-space first: reclaim LRU (and emergency pins if needed), then skip cache
    # if the host is still under MIN_FREE. Playback can continue without caching.
    reclaim_disk(reason="pre-download", protect_tid=tid)
    free_now = disk_free_mb()
    if free_now < LIBRARY_MIN_FREE_MB:
        logger.warning(
            "library: skip download %s ('%s') — free disk %.0fMB < min %sMB",
            video_id,
            track.get("title", "?"),
            free_now,
            LIBRARY_MIN_FREE_MB,
        )
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
        from src.config import _CookieFallbackYDL
        with _CookieFallbackYDL(opts) as ydl:
            # Pre-check metadata when possible (live flag, duration, approx size).
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as exc:
                logger.debug("library: extract_info precheck failed for %s: %s", video_id, exc)
                info = None
            if info:
                pre = {
                    **track,
                    "title": info.get("title") or track.get("title"),
                    "duration": info.get("duration") if info.get("duration") is not None else track.get("duration"),
                    "is_live": info.get("is_live"),
                    "live_status": info.get("live_status"),
                    "filesize": info.get("filesize"),
                    "filesize_approx": info.get("filesize_approx"),
                }
                pre_reason = cache_reject_reason(pre)
                if pre_reason:
                    logger.info(
                        "library: skip download after extract for %s ('%s'): %s",
                        video_id, pre.get("title", "?"), pre_reason,
                    )
                    return
                # Keep richer metadata for post-validation.
                track.update({
                    k: pre[k] for k in ("title", "duration", "is_live", "live_status")
                    if pre.get(k) is not None
                })
            ydl.download([url])

    try:
        _run()
    except Exception as exc:
        # yt_dlp may not be imported if failure was earlier; treat generically.
        try:
            import yt_dlp
            if isinstance(exc, yt_dlp.utils.DownloadError):
                maybe_detect_rate_limit(str(exc))
        except Exception:
            pass
        try:
            from src.config import is_cookie_load_error, mark_cookies_unusable
            if is_cookie_load_error(exc):
                mark_cookies_unusable(str(exc))
        except Exception:
            pass
        logger.warning("library: download failed for %s: %s", video_id, exc)
        _cleanup_tid_artifacts(tid)
        return None

    for path in sorted(_LIBRARY_DIR.glob(f"{tid}.*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in _LIBRARY_SKIP_SUFFIXES or path.suffix in (".part", ".ytdl"):
            continue
        if path.suffix.lower() not in _LIBRARY_AUDIO_SUFFIXES:
            logger.warning("library: removing non-audio download %s", path)
            try:
                path.unlink()
            except OSError:
                pass
            continue
        resolved = str(path.resolve())
        resolved = _sanitize_audio_file(resolved)
        return _validate_downloaded_file(resolved, track)
    return None


async def enqueue_download(track: dict, video_ref: str | None = None) -> None:
    if not LIBRARY_ENABLED or not LIBRARY_AUTO_DOWNLOAD:
        return

    _maybe_reload_index()

    reason = cache_reject_reason(track)
    if reason:
        logger.info(
            "library: skip enqueue for '%s': %s",
            track.get("title", "?"), reason,
        )
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
                _evict_if_needed(protect_tid=tid)
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
    _maybe_reload_index()
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
    free = disk_free_mb()
    return {
        "total_indexed": len(_index),
        "on_disk": on_disk,
        "size_mb": round(_file_size_mb(), 1),
        "max_mb": LIBRARY_MAX_MB,
        "max_tracks": LIBRARY_MAX_TRACKS,
        "free_mb": None if free == float("inf") else round(free, 1),
        "min_free_mb": LIBRARY_MIN_FREE_MB,
        "target_free_mb": LIBRARY_TARGET_FREE_MB,
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
