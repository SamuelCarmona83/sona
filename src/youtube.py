import asyncio
import logging
import re

try:
    import anthropic as _anthropic
    _anthropic_available = True
except ImportError:
    _anthropic_available = False

import yt_dlp

from src.config import (
    YTDL_OPTIONS,
    SEARCH_RESULT_COUNT,
    MIN_SEARCH_SCORE,
    LLM_SCORE_MARGIN,
    LLM_RANKING_TIMEOUT,
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
)
from src.scoring import _normalize_text, _build_search_queries, _rank_candidates

logger = logging.getLogger(__name__)

_anthropic_client = None
_search_cache: dict[str, dict] = {}  # Cache YouTube search results to avoid redundant queries


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


async def _search_youtube_candidates(query: str) -> list[dict]:
    def _search():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(f"ytsearch{SEARCH_RESULT_COUNT}:{query}", download=False)
            if not info or not info.get("entries"):
                return []
            candidates = []
            for entry in info["entries"]:
                if not entry or not entry.get("url"):
                    continue
                # Skip entries marked as unavailable by yt-dlp
                if entry.get("availability") in ("needs_auth", "subscriber_only", "premium_only", "unavailable"):
                    logger.info(f"_search_candidates: omitiendo video no disponible '{entry.get('id')}' ({entry.get('availability')})")
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
                })
            return candidates

    return await asyncio.to_thread(_search)


async def search_youtube(query: str, enable_llm: bool = True) -> dict | None:
    """Search YouTube and return the best scored candidate, using the LLM as tie-breaker."""
    # Check cache first (reduces redundant YouTube searches)
    cache_key = _normalize_text(query)
    if cache_key in _search_cache:
        logger.info(f"search_youtube: usando resultado en cache para '{query}'")
        return _search_cache[cache_key]

    for candidate_query in _build_search_queries(query):
        candidates = await _search_youtube_candidates(candidate_query)
        if not candidates:
            continue

        scored = _rank_candidates(query, candidates)
        preview = ", ".join(
            f"{c['score']:.2f}:{c.get('title', '?')}"
            for c in scored[:3]
        )
        logger.info(f"search_youtube: top candidatos para '{query}': {preview}")

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

        # Cache the result for future queries
        _search_cache[cache_key] = best
        return best

    logger.warning(f"search_youtube: no hubo candidato confiable para '{query}'")
    return None
