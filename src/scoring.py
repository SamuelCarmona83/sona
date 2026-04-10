import difflib
import logging
import re
import unicodedata

from src.config import (
    NOISE_TERMS,
    VARIANT_TERMS,
    PREFERRED_CHANNEL_HINTS,
    MIN_SEARCH_SCORE,
)

logger = logging.getLogger(__name__)


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[\[\](){}|]", " ", text)
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _canonical_token(token: str) -> str:
    token = token.strip("- ")
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("ches", "shes", "xes", "zes", "sses")):
        return token[:-2]
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _extract_variant_preferences(query: str) -> set[str]:
    normalized = _normalize_text(query)
    return {term for term in VARIANT_TERMS if term in normalized.split()}


def _tokenize(value: str, *, keep_variants: bool = False) -> list[str]:
    tokens = []
    for raw_token in _normalize_text(value).split():
        token = _canonical_token(raw_token)
        if not token:
            continue
        if token in NOISE_TERMS:
            continue
        if not keep_variants and token in VARIANT_TERMS:
            continue
        tokens.append(token)
    return tokens


def _clean_title_for_match(title: str, requested_variants: set[str]) -> str:
    cleaned = _normalize_text(title)
    if requested_variants:
        for term in VARIANT_TERMS - requested_variants:
            cleaned = re.sub(rf"\b{re.escape(term)}\b", " ", cleaned)
    else:
        for term in VARIANT_TERMS:
            cleaned = re.sub(rf"\b{re.escape(term)}\b", " ", cleaned)
    for term in NOISE_TERMS:
        cleaned = re.sub(rf"\b{re.escape(term)}\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _split_query_parts(query: str) -> tuple[str, str]:
    normalized = _normalize_text(query)
    if " - " in normalized:
        artist, title = normalized.split(" - ", 1)
        return artist.strip(), title.strip()
    if " by " in normalized:
        title, artist = normalized.rsplit(" by ", 1)
        return artist.strip(), title.strip()
    return "", normalized


def _build_search_queries(query: str) -> list[str]:
    queries = [query]
    artist, title = _split_query_parts(query)
    if artist and title:
        queries.append(f"{artist} - {title} official audio")
        queries.append(f"{artist} {title} topic")
    else:
        queries.append(f"{query} official audio")
    seen = set()
    unique_queries = []
    for item in queries:
        normalized = _normalize_text(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique_queries.append(item)
    return unique_queries


def _format_spotify_track_query(track: dict) -> str:
    artists = ", ".join(a["name"] for a in track.get("artists", []))
    return f"{artists} - {track['name']}"


def _score_spotify_match(user_query: str, track: dict) -> float:
    formatted = _format_spotify_track_query(track)
    requested_variants = _extract_variant_preferences(user_query)
    query_artist, query_title = _split_query_parts(user_query)
    track_artist, track_title = _split_query_parts(formatted)

    query_title_clean = _clean_title_for_match(query_title or user_query, requested_variants)
    track_title_clean = _clean_title_for_match(track_title or formatted, requested_variants)

    score = 0.0
    score += _similarity(
        query_title_clean.replace(" ", ""),
        track_title_clean.replace(" ", ""),
    ) * 8.0

    query_tokens = set(_tokenize(query_title or user_query))
    track_tokens = set(_tokenize(track_title or formatted))
    if query_tokens:
        score += (len(query_tokens & track_tokens) / len(query_tokens)) * 4.0

    if query_artist:
        score += _similarity(
            query_artist.replace(" ", ""),
            track_artist.replace(" ", ""),
        ) * 5.0

        artist_tokens = set(_tokenize(query_artist))
        track_artist_tokens = set(_tokenize(track_artist))
        if artist_tokens:
            score += (len(artist_tokens & track_artist_tokens) / len(artist_tokens)) * 4.0
    else:
        whole_query = _clean_title_for_match(user_query, requested_variants)
        whole_track = _clean_title_for_match(formatted, requested_variants)
        score += _similarity(whole_query.replace(" ", ""), whole_track.replace(" ", "")) * 4.0

    return score


def _score_candidate(query: str, candidate: dict) -> float:
    requested_variants = _extract_variant_preferences(query)
    artist_query, title_query = _split_query_parts(query)
    title_query_clean = _clean_title_for_match(title_query or query, requested_variants)
    candidate_title = candidate.get("title") or ""
    candidate_uploader = candidate.get("uploader") or candidate.get("channel") or ""
    candidate_title_clean = _clean_title_for_match(candidate_title, requested_variants)
    candidate_blob = f"{candidate_title} {candidate_uploader}"

    score = 0.0

    title_similarity = _similarity(
        title_query_clean.replace(" ", ""),
        candidate_title_clean.replace(" ", ""),
    )
    score += title_similarity * 8.0

    query_tokens = set(_tokenize(title_query or query))
    candidate_tokens = set(_tokenize(candidate_blob))
    overlap = query_tokens & candidate_tokens
    if query_tokens:
        score += (len(overlap) / len(query_tokens)) * 4.0

    if artist_query:
        artist_similarity = max(
            _similarity(artist_query.replace(" ", ""), _clean_title_for_match(candidate_uploader, requested_variants).replace(" ", "")),
            _similarity(artist_query.replace(" ", ""), candidate_title_clean.replace(" ", "")),
        )
        score += artist_similarity * 5.0

        artist_tokens = set(_tokenize(artist_query))
        if artist_tokens:
            artist_overlap = artist_tokens & candidate_tokens
            score += (len(artist_overlap) / len(artist_tokens)) * 3.0

    normalized_blob = _normalize_text(candidate_blob)
    for term in requested_variants:
        if re.search(rf"\b{re.escape(term)}\b", normalized_blob):
            score += 1.5

    if not requested_variants:
        for term in VARIANT_TERMS:
            if re.search(rf"\b{re.escape(term)}\b", normalized_blob):
                score -= 5.0  # More aggressively filter unwanted variants

    duration = candidate.get("duration") or 0
    if duration:
        if duration < 90:
            score -= 4.0
        elif duration < 150:
            score -= 1.0
        elif duration <= 600:
            score += 1.5
        elif duration > 900:
            score -= 2.0

    uploader_normalized = _normalize_text(candidate_uploader)
    if any(hint in uploader_normalized for hint in PREFERRED_CHANNEL_HINTS):
        score += 1.5

    return score


def _rank_candidates(query: str, candidates: list[dict]) -> list[dict]:
    """Return candidates sorted by heuristic score descending."""
    scored = []
    for candidate in candidates:
        score = _score_candidate(query, candidate)
        item = dict(candidate)
        item["score"] = score
        scored.append(item)
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored


def _select_best_candidate(query: str, candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    scored_candidates = _rank_candidates(query, candidates)
    preview = ", ".join(
        f"{item['score']:.2f}:{item.get('title', 'sin titulo')}"
        for item in scored_candidates[:3]
    )
    logger.info(f"search_youtube: top candidatos para '{query}': {preview}")

    best = scored_candidates[0]
    if best["score"] < MIN_SEARCH_SCORE:
        return None
    return best
