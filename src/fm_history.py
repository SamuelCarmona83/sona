"""Persistent FM listening sessions: detected tracks and sequential relationships.

Writes to .cache/fm_sessions.json for the data explorer. No Discord commands.
Never raises into the playback path — all I/O failures are logged and ignored.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
from typing import Any, Optional

from src.config import (
    FM_HISTORY_ENABLED,
    FM_HISTORY_MAX_SESSIONS,
    FM_HISTORY_MAX_TRACKS_PER_SESSION,
)
from src.fm_recognizer import match_key

logger = logging.getLogger(__name__)

_PATH = pathlib.Path(".cache/fm_sessions.json")
_LOCK = threading.Lock()
_ORPHAN_MAX_AGE_SEC = 24 * 3600

# guild_id -> active session id
_active_session_ids: dict[int, str] = {}
_loaded = False
_store: dict[str, Any] = {"version": 1, "sessions": []}


def _empty_store() -> dict[str, Any]:
    return {"version": 1, "sessions": []}


def _ensure_loaded() -> None:
    global _loaded, _store
    if _loaded:
        return
    _store = _load_from_disk()
    _heal_orphans(_store, max_age_sec=_ORPHAN_MAX_AGE_SEC)
    _rebuild_active_index(_store)
    _loaded = True


def _load_from_disk() -> dict[str, Any]:
    if not _PATH.exists():
        return _empty_store()
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_store()
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
        return {"version": 1, "sessions": sessions}
    except Exception as exc:
        logger.warning("fm_history: could not load %s: %s", _PATH, exc)
        return _empty_store()


def _atomic_save(store: dict[str, Any]) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(store, indent=2, ensure_ascii=False)
        tmp = _PATH.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, _PATH)
    except Exception as exc:
        logger.warning("fm_history: could not save: %s", exc)
        try:
            tmp = _PATH.with_suffix(".json.tmp")
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _heal_orphans(store: dict[str, Any], *, max_age_sec: float, now: float | None = None) -> bool:
    """Close sessions left open longer than max_age_sec. Returns True if mutated."""
    now = time.time() if now is None else now
    changed = False
    for session in store.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        if session.get("ended_at") is not None:
            continue
        started = float(session.get("started_at") or 0)
        if started and (now - started) > max_age_sec:
            session["ended_at"] = now
            changed = True
            logger.info(
                "fm_history: closed orphan session %s (age > %.0fh)",
                session.get("id"),
                max_age_sec / 3600,
            )
    return changed


def _rebuild_active_index(store: dict[str, Any]) -> None:
    _active_session_ids.clear()
    for session in store.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        if session.get("ended_at") is not None:
            continue
        try:
            gid = int(session.get("guild_id"))
        except (TypeError, ValueError):
            continue
        sid = session.get("id")
        if isinstance(sid, str) and sid:
            _active_session_ids[gid] = sid


def _find_session(store: dict[str, Any], session_id: str) -> Optional[dict]:
    for session in store.get("sessions") or []:
        if isinstance(session, dict) and session.get("id") == session_id:
            return session
    return None


def _find_open_session_for_guild(store: dict[str, Any], guild_id: int) -> Optional[dict]:
    for session in store.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        if session.get("ended_at") is not None:
            continue
        try:
            if int(session.get("guild_id")) == guild_id:
                return session
        except (TypeError, ValueError):
            continue
    return None


def _close_session_obj(session: dict, *, ended_at: float | None = None) -> None:
    session["ended_at"] = time.time() if ended_at is None else ended_at
    tracks = session.get("tracks") if isinstance(session.get("tracks"), list) else []
    session["track_count"] = len(tracks)


def _prune_sessions(store: dict[str, Any]) -> None:
    sessions = [s for s in (store.get("sessions") or []) if isinstance(s, dict)]
    if len(sessions) <= FM_HISTORY_MAX_SESSIONS:
        store["sessions"] = sessions
        return
    sessions.sort(key=lambda s: float(s.get("started_at") or 0), reverse=True)
    store["sessions"] = sessions[:FM_HISTORY_MAX_SESSIONS]


def _make_session_id(guild_id: int, started_at: float) -> str:
    return f"fm_{guild_id}_{int(started_at)}"


def open_session(guild_id: int, station_track: dict) -> Optional[str]:
    """Start a new FM history session for this guild. Closes any prior open session."""
    if not FM_HISTORY_ENABLED:
        return None
    if not isinstance(station_track, dict):
        return None

    try:
        with _LOCK:
            _ensure_loaded()
            now = time.time()
            existing = _find_open_session_for_guild(_store, guild_id)
            if existing is not None:
                same_station = (
                    (existing.get("stationuuid") or "") == (station_track.get("stationuuid") or "")
                    and (existing.get("stream_url") or "")
                    == (station_track.get("url") or station_track.get("url_resolved") or "")
                )
                if same_station and existing.get("ended_at") is None:
                    sid = existing.get("id")
                    if isinstance(sid, str):
                        _active_session_ids[guild_id] = sid
                        return sid
                _close_session_obj(existing, ended_at=now)
                old_id = existing.get("id")
                if old_id and _active_session_ids.get(guild_id) == old_id:
                    _active_session_ids.pop(guild_id, None)

            started_at = now
            session_id = _make_session_id(guild_id, started_at)
            # Avoid id collision if reopen in the same second
            if _find_session(_store, session_id):
                session_id = f"{session_id}_{int(now * 1000) % 1000}"

            stream_url = station_track.get("url") or station_track.get("url_resolved") or ""
            session = {
                "id": session_id,
                "guild_id": int(guild_id),
                "stationuuid": (station_track.get("stationuuid") or "").strip(),
                "station_name": (station_track.get("title") or "FM Station").strip(),
                "countrycode": (station_track.get("countrycode") or "").strip().upper(),
                "tags": (station_track.get("tags") or "").strip(),
                "stream_url": stream_url,
                "started_at": started_at,
                "ended_at": None,
                "track_count": 0,
                "tracks": [],
            }
            _store.setdefault("sessions", []).append(session)
            _prune_sessions(_store)
            _active_session_ids[guild_id] = session_id
            _atomic_save(_store)
            logger.info(
                "fm_history: opened session %s guild=%s station=%s",
                session_id,
                guild_id,
                session["station_name"][:60],
            )
            return session_id
    except Exception as exc:
        logger.warning("fm_history: open_session failed: %s", exc)
        return None


def append_detection(guild_id: int, match: dict) -> bool:
    """Append a new recognized track to the active session (deduped by match_key)."""
    if not FM_HISTORY_ENABLED:
        return False
    if not isinstance(match, dict):
        return False

    try:
        with _LOCK:
            _ensure_loaded()
            session_id = _active_session_ids.get(guild_id)
            session = _find_session(_store, session_id) if session_id else None
            if session is None or session.get("ended_at") is not None:
                session = _find_open_session_for_guild(_store, guild_id)
                if session is None:
                    logger.debug("fm_history: append with no open session guild=%s", guild_id)
                    return False
                sid = session.get("id")
                if isinstance(sid, str):
                    _active_session_ids[guild_id] = sid

            artist = (match.get("artist") or "").strip()
            title = (match.get("title") or "").strip()
            if not title:
                return False
            key = match_key(artist, title)

            tracks = session.setdefault("tracks", [])
            if not isinstance(tracks, list):
                tracks = []
                session["tracks"] = tracks

            if tracks:
                last = tracks[-1]
                if isinstance(last, dict) and last.get("match_key") == key:
                    return False

            if len(tracks) >= FM_HISTORY_MAX_TRACKS_PER_SESSION:
                logger.debug(
                    "fm_history: session %s at track cap (%s)",
                    session.get("id"),
                    FM_HISTORY_MAX_TRACKS_PER_SESSION,
                )
                return False

            prev_key = None
            if tracks and isinstance(tracks[-1], dict):
                prev_key = tracks[-1].get("match_key")

            entry = {
                "seq": len(tracks),
                "artist": artist or "Unknown",
                "title": title,
                "album": match.get("album"),
                "match_key": key,
                "shazam_url": match.get("shazam_url"),
                "cover_url": match.get("cover_url"),
                "detected_at": float(match.get("recognized_at") or time.time()),
                "prev_match_key": prev_key,
            }
            tracks.append(entry)
            session["track_count"] = len(tracks)
            _atomic_save(_store)
            logger.info(
                "fm_history: +track session=%s '%s' — '%s'",
                session.get("id"),
                artist,
                title,
            )
            return True
    except Exception as exc:
        logger.warning("fm_history: append_detection failed: %s", exc)
        return False


def close_session(guild_id: int) -> bool:
    """End the active FM history session for this guild."""
    if not FM_HISTORY_ENABLED:
        # Still clear in-memory pointer so a later enable is clean
        _active_session_ids.pop(guild_id, None)
        return False

    try:
        with _LOCK:
            _ensure_loaded()
            session = _find_open_session_for_guild(_store, guild_id)
            if session is None:
                _active_session_ids.pop(guild_id, None)
                return False
            _close_session_obj(session)
            _active_session_ids.pop(guild_id, None)
            _atomic_save(_store)
            logger.info(
                "fm_history: closed session %s tracks=%s",
                session.get("id"),
                session.get("track_count"),
            )
            return True
    except Exception as exc:
        logger.warning("fm_history: close_session failed: %s", exc)
        return False


def get_active_session_id(guild_id: int) -> Optional[str]:
    with _LOCK:
        _ensure_loaded()
        return _active_session_ids.get(guild_id)


def list_sessions() -> list[dict]:
    """Return a shallow copy of all sessions (for tests / tooling)."""
    with _LOCK:
        _ensure_loaded()
        return list(_store.get("sessions") or [])


def reset_for_tests() -> None:
    """Clear in-memory state (unit tests only)."""
    global _loaded, _store
    with _LOCK:
        _loaded = False
        _store = _empty_store()
        _active_session_ids.clear()
