import asyncio
import json
import logging
import pathlib
import random
import re
import time

try:
    import anthropic as _anthropic
    _anthropic_available = True
except ImportError:
    _anthropic_available = False

import yt_dlp

from src.config import (
    YTDL_OPTIONS,
    YTDL_OPTIONS_NO_COOKIES,
    get_cookie_status,
    SEARCH_RESULT_COUNT,
    MIN_SEARCH_SCORE,
    LLM_SCORE_MARGIN,
    LLM_RANKING_TIMEOUT,
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    YTDL_SEARCH_CONCURRENCY,
    YTDL_SEARCH_DELAY_SEC,
    YTDL_SEARCH_DELAY_URGENT_SEC,
    YTDL_SEARCH_JITTER_SEC,
    YOUTUBE_URL_CACHE_TTL_SEC,
    LIBRARY_ENABLED,
)
from src.scoring import _normalize_text, _build_search_queries, _rank_candidates

logger = logging.getLogger(__name__)

# Max tracks to extract from a YouTube/YouTube Music playlist
_YT_PLAYLIST_MAX = 50


# ---------------------------------------------------------------------------
# YouTube / YouTube Music URL helpers
# ---------------------------------------------------------------------------

_YT_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|music\.)?(?:youtube\.com|youtu\.be)/",
    re.IGNORECASE,
)


def _is_youtube_url(query: str) -> str | None:
    """Return ``'track'``, ``'playlist'``, or ``None``."""
    if not _YT_URL_RE.search(query):
        return None
    if re.search(r"[?&]list=", query):
        # YouTube Radio/Mix playlists (list=RD...) paired with a v= are treated as
        # single tracks — the Mix list itself is auto-generated and unreliable to expand.
        if re.search(r"[?&]list=RD", query) and re.search(r"[?&]v=", query):
            return "track"
        return "playlist"
    if re.search(r"youtu\.be/|[?&]v=|/watch", query):
        return "track"
    return None


async def extract_youtube_tracks(url: str) -> list[dict]:
    """Extract track(s) from a YouTube / YouTube Music URL via yt-dlp.

    * Single video → 1-item list with resolved streaming URL.
    * Playlist     → list of ``{title, url (None – lazy), yt_query, duration, thumbnail, uploader}``
      capped at ``_YT_PLAYLIST_MAX``.
    """
    url_type = _is_youtube_url(url)
    if not url_type:
        return []

    def _extract():
        opts = {
            **YTDL_OPTIONS,
            "logger": _YtDlpLogger(),
            "noplaylist": url_type == "track",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as e:
                err = str(e)
                maybe_detect_rate_limit(err)
                maybe_detect_auth_failure(err)
                logger.warning("extract_youtube_tracks: DownloadError: %s", e)
                return []
            if not info:
                return []

            entries = info.get("entries")  # None for single video
            if entries is None:
                entries = [info]

            tracks: list[dict] = []
            for entry in entries:
                if not entry:
                    continue
                if entry.get("availability") in (
                    "needs_auth", "subscriber_only", "premium_only", "unavailable",
                ):
                    continue
                title = entry.get("title") or "Unknown"
                tracks.append({
                    "title": title,
                    "url": entry.get("url") if url_type == "track" else None,
                    "yt_query": title,
                    "duration": entry.get("duration") or 0,
                    "thumbnail": entry.get("thumbnail") or "",
                    "uploader": entry.get("uploader") or "",
                    "acodec": entry.get("acodec") or "?",
                    "abr": entry.get("abr") or 0,
                })
                if len(tracks) >= _YT_PLAYLIST_MAX:
                    break
            return tracks

    return await _run_rate_limited_yt_request(_extract, f"extract {url_type}")


_anthropic_client = None
_metadata_index: dict[str, dict] = {}
_url_cache: dict[str, dict] = {}  # normalized query -> {url, expires_at, ...}
_METADATA_PATH = pathlib.Path(".cache/youtube_metadata.json")
_METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
_search_semaphore = asyncio.Semaphore(YTDL_SEARCH_CONCURRENCY)
_search_spacing_lock = asyncio.Lock()
_last_search_start = 0.0

# --- YouTube Rate Limit + Auth Failure Detection ---
_last_rate_limit_time: float = 0.0
_rate_limit_cooldown_sec = 3600  # 1 hour
_rate_limit_message = "This content isn't available, try again later"
_auth_failed: bool = False
_AUTH_PATTERNS = (
    "sign in to confirm you're not a bot",
    "confirm you're not a bot",
    "cookies are rotated",
    "http error 403",
    "unable to extract data",
)


def set_youtube_rate_limited():
    global _last_rate_limit_time
    _last_rate_limit_time = time.time()


def is_youtube_rate_limited() -> bool:
    if _last_rate_limit_time == 0.0:
        return False
    return (time.time() - _last_rate_limit_time) < _rate_limit_cooldown_sec


def set_youtube_auth_failed() -> None:
    global _auth_failed
    _auth_failed = True


def clear_youtube_auth_failed() -> None:
    global _auth_failed
    _auth_failed = False


def is_youtube_auth_failed() -> bool:
    return _auth_failed


def maybe_detect_rate_limit(msg: str) -> bool:
    if _rate_limit_message in msg:
        set_youtube_rate_limited()
        return True
    return False


def maybe_detect_auth_failure(msg: str) -> bool:
    if maybe_detect_rate_limit(msg):
        return False
    lower = msg.lower()
    if any(pat in lower for pat in _AUTH_PATTERNS):
        set_youtube_auth_failed()
        try:
            from src.cookie_health import record_auth_failure
            record_auth_failure()
        except ImportError:
            pass
        logger.warning("yt-dlp: [AUTH FAILURE DETECTED] %s", msg[:200])
        return True
    return False


def _should_try_cookieless() -> bool:
    if is_youtube_auth_failed():
        return True
    status = get_cookie_status()
    return not status.get("fresh", True)


def _load_metadata_index() -> None:
    global _metadata_index
    if not _METADATA_PATH.exists():
        return
    try:
        data = json.loads(_METADATA_PATH.read_text())
        if isinstance(data, dict):
            _metadata_index = data
    except Exception as exc:
        logger.warning("youtube.metadata: load failed: %s", exc)


def _save_metadata_index() -> None:
    try:
        _METADATA_PATH.write_text(json.dumps(_metadata_index, indent=2))
    except Exception as exc:
        logger.warning("youtube.metadata: save failed: %s", exc)


def _extract_video_id(url_or_id: str | None) -> str | None:
    if not url_or_id:
        return None
    if re.fullmatch(r"[\w-]{11}", url_or_id):
        return url_or_id
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})", url_or_id)
    return m.group(1) if m else None


def _store_metadata(cache_key: str, candidate: dict) -> None:
    video_id = _extract_video_id(candidate.get("webpage_url")) or _extract_video_id(candidate.get("url"))
    _metadata_index[cache_key] = {
        "video_id": video_id,
        "title": candidate.get("title", ""),
        "duration": candidate.get("duration"),
        "thumbnail": candidate.get("thumbnail", ""),
        "uploader": candidate.get("uploader", ""),
        "acodec": candidate.get("acodec", "?"),
        "abr": candidate.get("abr", 0),
        "webpage_url": candidate.get("webpage_url", ""),
        "cached_at": time.time(),
    }
    _save_metadata_index()


def _metadata_to_candidate(meta: dict, url: str | None = None) -> dict:
    return {
        "title": meta.get("title", ""),
        "url": url,
        "duration": meta.get("duration"),
        "thumbnail": meta.get("thumbnail", ""),
        "uploader": meta.get("uploader", ""),
        "channel": meta.get("uploader", ""),
        "webpage_url": meta.get("webpage_url", ""),
        "acodec": meta.get("acodec", "?"),
        "abr": meta.get("abr", 0),
        "video_id": meta.get("video_id"),
    }


def _get_cached_url(cache_key: str) -> dict | None:
    cached = _url_cache.get(cache_key)
    if not cached:
        return None
    if time.time() > cached.get("expires_at", 0):
        _url_cache.pop(cache_key, None)
        return None
    return cached


def _set_cached_url(cache_key: str, candidate: dict) -> dict:
    entry = {**candidate, "expires_at": time.time() + YOUTUBE_URL_CACHE_TTL_SEC}
    _url_cache[cache_key] = entry
    return entry


def _extract_video_sync(video_id: str, base_opts: dict) -> dict | None:
    opts = {**base_opts, "logger": _YtDlpLogger()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=False,
            )
        except yt_dlp.utils.DownloadError as e:
            err = str(e)
            maybe_detect_rate_limit(err)
            maybe_detect_auth_failure(err)
            logger.warning("youtube.refresh_url: DownloadError for %s: %s", video_id, e)
            return None
    if not info or not info.get("url"):
        return None
    return {
        "title": info.get("title", ""),
        "url": info["url"],
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail", ""),
        "uploader": info.get("uploader") or "",
        "webpage_url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
        "acodec": info.get("acodec") or "?",
        "abr": info.get("abr") or 0,
        "video_id": video_id,
    }


async def _refresh_url_from_video_id(video_id: str) -> dict | None:
    if is_youtube_rate_limited():
        return None

    def _extract():
        result = _extract_video_sync(video_id, YTDL_OPTIONS)
        if result or is_youtube_rate_limited():
            return result
        if _should_try_cookieless():
            logger.info("youtube.refresh_url: cookieless retry for %s", video_id)
            return _extract_video_sync(video_id, YTDL_OPTIONS_NO_COOKIES)
        return result

    return await _run_rate_limited_yt_request(_extract, f"refresh {video_id}")


_load_metadata_index()


async def _throttle_youtube_request(reason: str, *, urgent: bool = False) -> None:
    """Space out yt-dlp requests so queue fills do not look like bot bursts."""
    global _last_search_start
    async with _search_spacing_lock:
        base_delay = YTDL_SEARCH_DELAY_URGENT_SEC if urgent else YTDL_SEARCH_DELAY_SEC
        jitter = random.uniform(0.0, YTDL_SEARCH_JITTER_SEC) if YTDL_SEARCH_JITTER_SEC > 0 else 0.0
        target_delay = base_delay + (0.0 if urgent else jitter)
        if target_delay > 0:
            now = time.monotonic()
            wait_for = target_delay - (now - _last_search_start)
            if wait_for > 0:
                logger.info(
                    "youtube.throttle: waiting %.2fs before %s (base=%.2fs, jitter=%.2fs)",
                    wait_for,
                    reason,
                    base_delay,
                    jitter,
                )
                await asyncio.sleep(wait_for)
        _last_search_start = time.monotonic()


async def _run_rate_limited_yt_request(func, reason: str, *, urgent: bool = False):
    async with _search_semaphore:
        await _throttle_youtube_request(reason, urgent=urgent)
        return await asyncio.to_thread(func)


class _YtDlpLogger:
    """Routes yt-dlp output through Python's logging instead of printing to stderr.

    Without this, yt-dlp's report_error() bypasses quiet=True and writes directly
    to stderr — e.g. age-restricted / unavailable videos during search scans.
    """
    _warned_once: set[str] = set()  # class-level dedup set for one-time warnings

    def debug(self, msg: str) -> None:
        if msg.startswith("[download]"):
            return  # suppress noisy download progress lines
        logger.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        # Deduplicate warnings that repeat on every yt-dlp instance (e.g. JS runtime missing)
        key = msg[:120]
        if key in _YtDlpLogger._warned_once:
            return
        _YtDlpLogger._warned_once.add(key)
        if maybe_detect_rate_limit(msg):
            logger.warning("yt-dlp: [RATE-LIMIT DETECTED] %s", msg)
        elif maybe_detect_auth_failure(msg):
            pass
        else:
            logger.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        if maybe_detect_rate_limit(msg):
            logger.warning("yt-dlp: [RATE-LIMIT DETECTED] %s", msg)
        elif maybe_detect_auth_failure(msg):
            pass
        else:
            logger.warning("yt-dlp: %s", msg)


async def _llm_pick_best(query: str, candidates: list[dict]) -> dict | None:
    """Ask Claude Haiku to pick the best YouTube candidate. Returns None on any failure."""
    global _anthropic_client
    if not _anthropic_available or not ANTHROPIC_API_KEY:
        return None
    if not candidates:
        return None
    try:
        if _anthropic_client is None:
            _anthropic_client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        lines = []
        for i, c in enumerate(candidates, 1):
            dur = c.get("duration") or 0
            dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
            lines.append(f"{i}. \"{c['title']}\" — {c.get('uploader', '')} [{dur_str}]")
        candidates_text = "\n".join(lines)

        prompt = (
            f"You are selecting the best YouTube video for a music bot.\n"
            f"The user wants to play: {query}\n"
            f"Candidates:\n{candidates_text}\n"
            f"Reply with ONLY the number (1-{len(candidates)}) of the best match. "
            f"Prefer official uploads. Avoid covers, remixes, or live versions unless requested."
        )

        def _call():
            return _anthropic_client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=5,
                messages=[{"role": "user", "content": prompt}],
            )

        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=LLM_RANKING_TIMEOUT)
        raw = response.content[0].text.strip()
        match = re.search(r"\d+", raw)
        if not match:
            logger.warning(f"llm_pick_best: respuesta inesperada del modelo: '{raw}'")
            return None
        idx = int(match.group()) - 1
        if 0 <= idx < len(candidates):
            logger.info(f"llm_pick_best: eligio candidato {idx + 1} '{candidates[idx]['title']}' para '{query}'")
            return candidates[idx]
        return None
    except Exception as exc:
        logger.warning(f"llm_pick_best: fallo, usando fallback heuristico: {exc}")
        return None


def _parse_search_entries(query: str, info: dict | None) -> list[dict]:
    if not info or not info.get("entries"):
        return []
    candidates = []
    for entry in info["entries"]:
        if not entry or not entry.get("url"):
            continue
        if entry.get("availability") in ("needs_auth", "subscriber_only", "premium_only", "unavailable"):
            logger.info(
                "_search_candidates: omitiendo video no disponible '%s' (%s)",
                entry.get("id"), entry.get("availability"),
            )
            continue
        candidates.append({
            "title": entry.get("title", query),
            "url": entry["url"],
            "duration": entry.get("duration"),
            "uploader": entry.get("uploader") or "",
            "channel": entry.get("channel") or "",
            "webpage_url": entry.get("webpage_url") or "",
            "thumbnail": entry.get("thumbnail") or "",
            "acodec": entry.get("acodec") or "?",
            "abr": entry.get("abr") or 0,
            "video_id": entry.get("id"),
        })
    return candidates


def _search_sync(query: str, base_opts: dict) -> list[dict]:
    opts = {**base_opts, "logger": _YtDlpLogger()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch{SEARCH_RESULT_COUNT}:{query}", download=False)
        except yt_dlp.utils.DownloadError as e:
            err = str(e)
            maybe_detect_rate_limit(err)
            maybe_detect_auth_failure(err)
            logger.warning("_search_candidates: DownloadError buscando '%s': %s", query, e)
            return []
    return _parse_search_entries(query, info)


async def _search_youtube_candidates(query: str, *, urgent: bool = False) -> list[dict]:
    def _search():
        candidates = _search_sync(query, YTDL_OPTIONS)
        if candidates or is_youtube_rate_limited():
            return candidates
        if _should_try_cookieless():
            logger.info("_search_candidates: retrying '%s' without cookies", query[:60])
            fallback = _search_sync(query, YTDL_OPTIONS_NO_COOKIES)
            if fallback:
                logger.info("_search_candidates: cookieless fallback found %d results", len(fallback))
            return fallback
        return candidates

    return await _run_rate_limited_yt_request(_search, f"search {query[:60]}", urgent=urgent)


async def get_search_candidates(query: str) -> list[dict]:
    """Get top 5 search candidates for user selection (no auto-selection)."""
    if is_youtube_rate_limited():
        logger.info("get_search_candidates: omitiendo busqueda (rate-limited) para '%s'", query)
        return []
    for candidate_query in _build_search_queries(query):
        candidates = await _search_youtube_candidates(candidate_query)
        if not candidates:
            continue

        scored = _rank_candidates(query, candidates)
        preview = ", ".join(
            f"{c['score']:.2f}:{c.get('title', '?')}"
            for c in scored[:5]
        )
        logger.info(f"get_search_candidates: top 5 para '{query}': {preview}")

        # Return top 5 if first candidate meets minimum score
        if scored[0]["score"] >= MIN_SEARCH_SCORE:
            return scored[:5]

    logger.warning(f"get_search_candidates: no hubo candidatos confiables para '{query}'")
    return []


async def search_youtube(
    query: str,
    enable_llm: bool = True,
    *,
    trusted: bool = False,
    urgent: bool = False,
) -> dict | None:
    """Search YouTube and return the best scored candidate, using the LLM as tie-breaker.

    When *trusted* is True (e.g. query comes from a Spotify URL where we already
    know the exact track), accept the best candidate even if its score is below
    MIN_SEARCH_SCORE — as long as it exceeds a lower safety floor.
    """
    TRUSTED_FLOOR = 3.0  # absolute minimum even for trusted queries
    cache_key = _normalize_text(query)

    cached_url = _get_cached_url(cache_key)
    if cached_url and cached_url.get("url"):
        logger.info("search_youtube: usando URL en cache para '%s'", query)
        return cached_url

    meta = _metadata_index.get(cache_key)
    if meta and meta.get("video_id"):
        refreshed = await _refresh_url_from_video_id(meta["video_id"])
        if refreshed and refreshed.get("url"):
            result = _set_cached_url(cache_key, refreshed)
            if LIBRARY_ENABLED:
                from src.library import enqueue_download
                await enqueue_download(
                    {"title": refreshed["title"], "yt_query": query, **refreshed},
                    refreshed["video_id"],
                )
            return result
        if is_youtube_rate_limited():
            logger.info("search_youtube: rate-limited, metadata sin URL para '%s'", query)
            return _metadata_to_candidate(meta, url=None)

    if is_youtube_rate_limited():
        logger.info("search_youtube: omitiendo busqueda (rate-limited) para '%s'", query)
        return None

    best_overall: dict | None = None  # track the best candidate across all queries

    used_urgent = False
    for candidate_query in _build_search_queries(query):
        use_urgent = urgent and not used_urgent
        candidates = await _search_youtube_candidates(candidate_query, urgent=use_urgent)
        if use_urgent:
            used_urgent = True
        if not candidates:
            continue

        scored = _rank_candidates(query, candidates)
        preview = ", ".join(
            f"{c['score']:.2f}:{c.get('title', '?')}"
            for c in scored[:3]
        )
        logger.info(f"search_youtube: top candidatos para '{query}': {preview}")

        # Keep track of the absolute best candidate across all query variants
        if best_overall is None or scored[0]["score"] > best_overall["score"]:
            best_overall = scored[0]

        if scored[0]["score"] < MIN_SEARCH_SCORE:
            continue

        # Use LLM as a tie-breaker only when candidates are very close AND LLM enabled
        needs_llm = (
            enable_llm
            and ANTHROPIC_API_KEY
            and len(scored) >= 2
            and (scored[0]["score"] - scored[1]["score"]) < LLM_SCORE_MARGIN
        )
        if needs_llm:
            logger.info(
                "search_youtube: margen de score bajo (%.2f vs %.2f), consultando LLM",
                scored[0]["score"],
                scored[1]["score"],
            )
            best = await _llm_pick_best(query, scored[:5]) or scored[0]
        else:
            best = scored[0]

        logger.info(
            "search_youtube: elegido '%s' para '%s' (score=%.2f, llm=%s, codec=%s, abr=%s)",
            best["title"],
            query,
            best.get("score", 0),
            needs_llm,
            best.get("acodec", "?"),
            best.get("abr", "?"),
        )

        _store_metadata(cache_key, best)
        result = _set_cached_url(cache_key, best)
        if LIBRARY_ENABLED:
            from src.library import enqueue_download
            await enqueue_download(
                {"title": best["title"], "yt_query": query, **best},
                best.get("webpage_url") or best.get("video_id"),
            )
        return result

    # Trusted fallback: accept best candidate above the safety floor
    if trusted and best_overall and best_overall["score"] >= TRUSTED_FLOOR:
        logger.info(
            "search_youtube: trusted fallback '%s' para '%s' (score=%.2f)",
            best_overall["title"],
            query,
            best_overall["score"],
        )
        _store_metadata(cache_key, best_overall)
        result = _set_cached_url(cache_key, best_overall)
        if LIBRARY_ENABLED:
            from src.library import enqueue_download
            await enqueue_download(
                {"title": best_overall["title"], "yt_query": query, **best_overall},
                best_overall.get("webpage_url") or best_overall.get("video_id"),
            )
        return result

    logger.warning(f"search_youtube: no hubo candidato confiable para '{query}'")
    return None
