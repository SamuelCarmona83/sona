"""Real-time song recognition for FM (internet radio) streams via shazamio.

Samples the live stream URL in a sidecar FFmpeg process (does not tap Discord
voice PCM) and fingerprints the clip with Shazam's unofficial API through
shazamio. One background loop per guild while an FM station is playing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from typing import Any, Awaitable, Callable, Optional

from src.config import (
    FM_RECOGNIZER_ANNOUNCE,
    FM_RECOGNIZER_ENABLED,
    FM_RECOGNIZER_INTERVAL_SEC,
    FM_RECOGNIZER_SAMPLE_SEC,
)

logger = logging.getLogger(__name__)

MatchCallback = Callable[[int, dict], Awaitable[None]]
ActivePredicate = Callable[[], bool]

# Per-guild background tasks
_tasks: dict[int, asyncio.Task] = {}
_last_match_key: dict[int, str] = {}
_consecutive_misses: dict[int, int] = {}

_MISS_INTERVAL_SEC = 22.0
_HIT_INTERVAL_SEC = 45.0
_STALE_AFTER_MISSES = 3
_FFMPEG_TIMEOUT_PAD_SEC = 12.0


def match_key(artist: str, title: str) -> str:
    """Stable dedupe key for a recognized track."""
    return f"{_normalize_key(artist)}|{_normalize_key(title)}"


def _normalize_key(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def parse_shazam_response(payload: dict | None) -> Optional[dict]:
    """Map a raw shazamio/Shazam JSON payload to a stable match dict, or None."""
    if not isinstance(payload, dict):
        return None
    track = payload.get("track")
    if not isinstance(track, dict):
        return None
    title = (track.get("title") or "").strip()
    artist = (track.get("subtitle") or track.get("artist") or "").strip()
    if not title:
        return None

    images = track.get("images") if isinstance(track.get("images"), dict) else {}
    cover = (
        images.get("coverarthq")
        or images.get("coverart")
        or images.get("background")
        or ""
    )
    album = None
    sections = track.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            if (section.get("type") or "").upper() == "SONG":
                for meta in section.get("metadata") or []:
                    if not isinstance(meta, dict):
                        continue
                    if (meta.get("title") or "").lower() == "album":
                        album = (meta.get("text") or "").strip() or None
                        break
            if album:
                break

    return {
        "title": title,
        "artist": artist or "Unknown",
        "album": album,
        "cover_url": cover or None,
        "shazam_url": (track.get("url") or "").strip() or None,
        "recognized_at": time.time(),
    }


def _hls_before_options(url: str) -> str:
    base = (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
        "-nostdin -hide_banner -loglevel error"
    )
    url_l = (url or "").lower()
    if ".m3u8" in url_l or "playlist" in url_l or "/live/" in url_l:
        base += (
            " -protocol_whitelist file,http,https,tcp,tls,crypto,data,httpproxy"
            " -allowed_extensions ALL -live_start_index -1"
        )
    return base


async def sample_stream(url: str, *, seconds: float | None = None) -> Optional[str]:
    """Capture a short WAV clip from a live stream URL. Returns temp file path or None."""
    if not url:
        return None
    sample_sec = float(seconds if seconds is not None else FM_RECOGNIZER_SAMPLE_SEC)
    sample_sec = max(3.0, min(15.0, sample_sec))

    fd, path = tempfile.mkstemp(prefix="fm_shazam_", suffix=".wav")
    os.close(fd)

    before = _hls_before_options(url)
    cmd = [
        "ffmpeg",
        *before.split(),
        "-i",
        url,
        "-t",
        f"{sample_sec:.1f}",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-f",
        "wav",
        "-y",
        path,
    ]
    timeout = sample_sec + _FFMPEG_TIMEOUT_PAD_SEC
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.warning("fm_recognizer: ffmpeg sample timed out url=%s", url[:120])
            _safe_unlink(path)
            return None
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace")[:300]
            logger.debug("fm_recognizer: ffmpeg failed code=%s err=%s", proc.returncode, err)
            _safe_unlink(path)
            return None
        if not os.path.exists(path) or os.path.getsize(path) < 1000:
            logger.debug("fm_recognizer: sample too small or missing")
            _safe_unlink(path)
            return None
        return path
    except FileNotFoundError:
        logger.error("fm_recognizer: ffmpeg not found on PATH")
        _safe_unlink(path)
        return None
    except Exception as exc:
        logger.warning("fm_recognizer: sample_stream error: %s", exc)
        _safe_unlink(path)
        return None


async def recognize_clip(path_or_bytes: str | bytes) -> Optional[dict]:
    """Fingerprint a clip with shazamio and return a normalized match dict."""
    try:
        from shazamio import Shazam
    except ImportError:
        logger.error("fm_recognizer: shazamio is not installed")
        return None

    try:
        shazam = Shazam()
        payload = await shazam.recognize(path_or_bytes)
        return parse_shazam_response(payload)
    except Exception as exc:
        logger.warning("fm_recognizer: recognize failed: %s", exc)
        return None


def _safe_unlink(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def is_running(guild_id: int) -> bool:
    task = _tasks.get(guild_id)
    return bool(task and not task.done())


def stop_fm_recognizer(guild_id: int) -> None:
    """Cancel the recognition loop for a guild (idempotent)."""
    task = _tasks.pop(guild_id, None)
    _last_match_key.pop(guild_id, None)
    _consecutive_misses.pop(guild_id, None)
    if task and not task.done():
        task.cancel()
        logger.info("fm_recognizer: stopped guild=%s", guild_id)


def start_fm_recognizer(
    guild_id: int,
    stream_url: str,
    *,
    on_match: MatchCallback,
    is_active: ActivePredicate,
    text_channel: Any = None,
    on_stale: Optional[Callable[[int], Awaitable[None]]] = None,
) -> None:
    """Start (or restart) continuous recognition for an FM stream."""
    stop_fm_recognizer(guild_id)
    if not FM_RECOGNIZER_ENABLED:
        return
    if not stream_url:
        return

    task = asyncio.create_task(
        _recognition_loop(
            guild_id,
            stream_url,
            on_match=on_match,
            is_active=is_active,
            text_channel=text_channel,
            on_stale=on_stale,
        ),
        name=f"fm_recognizer_{guild_id}",
    )
    _tasks[guild_id] = task

    def _done(t: asyncio.Task) -> None:
        if _tasks.get(guild_id) is t:
            _tasks.pop(guild_id, None)
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if exc:
            logger.warning("fm_recognizer: loop crashed guild=%s: %s", guild_id, exc)

    task.add_done_callback(_done)
    logger.info("fm_recognizer: started guild=%s interval=%.0fs", guild_id, FM_RECOGNIZER_INTERVAL_SEC)


async def _recognition_loop(
    guild_id: int,
    stream_url: str,
    *,
    on_match: MatchCallback,
    is_active: ActivePredicate,
    text_channel: Any,
    on_stale: Optional[Callable[[int], Awaitable[None]]],
) -> None:
    # First sample after a short delay so the main FFmpeg playback can settle.
    await asyncio.sleep(5.0)

    while True:
        if not is_active():
            logger.debug("fm_recognizer: inactive guild=%s, exiting loop", guild_id)
            return

        clip_path: Optional[str] = None
        try:
            clip_path = await sample_stream(stream_url)
            match = await recognize_clip(clip_path) if clip_path else None
        finally:
            _safe_unlink(clip_path)

        if match:
            key = match_key(match["artist"], match["title"])
            prev = _last_match_key.get(guild_id)
            _consecutive_misses[guild_id] = 0
            if key != prev:
                _last_match_key[guild_id] = key
                logger.info(
                    "fm_recognizer: match guild=%s '%s' — '%s'",
                    guild_id,
                    match["artist"],
                    match["title"],
                )
                try:
                    await on_match(guild_id, match)
                except Exception as exc:
                    logger.warning("fm_recognizer: on_match error: %s", exc)
                if FM_RECOGNIZER_ANNOUNCE and text_channel is not None:
                    try:
                        await text_channel.send(
                            f"🎵 Ahora suena: **{match['artist']}** — **{match['title']}**",
                            delete_after=25,
                        )
                    except Exception as exc:
                        logger.debug("fm_recognizer: announce failed: %s", exc)
                sleep_for = max(FM_RECOGNIZER_INTERVAL_SEC, _HIT_INTERVAL_SEC)
            else:
                # Same track still playing — poll less aggressively
                sleep_for = max(FM_RECOGNIZER_INTERVAL_SEC, _HIT_INTERVAL_SEC)
        else:
            misses = _consecutive_misses.get(guild_id, 0) + 1
            _consecutive_misses[guild_id] = misses
            if misses == _STALE_AFTER_MISSES and on_stale is not None:
                try:
                    await on_stale(guild_id)
                except Exception as exc:
                    logger.debug("fm_recognizer: on_stale error: %s", exc)
            sleep_for = _MISS_INTERVAL_SEC

        try:
            await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            raise
