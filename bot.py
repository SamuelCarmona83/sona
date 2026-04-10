import asyncio
import collections
import difflib
import logging
import os
import re
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import anthropic as _anthropic
    _anthropic_available = True
except ImportError:
    _anthropic_available = False

import discord
from discord.ext import commands
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import yt_dlp

from poc_setlistfm import load_dotenv_values, get_config_value

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


SCOPES = (
    "user-modify-playback-state "
    "user-read-playback-state "
    "user-read-currently-playing "
    "playlist-read-private"
)

ALLOWED_CHANNEL_ID = 1163479541029810226
VOICE_CHANNEL_ID   = 1397428777876721716
CACHE_PATH = os.getenv("SPOTIFY_CACHE_PATH", ".cache/spotify.cache")
OAUTH_PORT = 8888

# Only this user can run !auth; the OAuth URL is sent via DM (never visible in the channel)
ADMIN_USER_ID = 221081593790332929

# ---------------------------------------------------------------------------
# OAuth callback server
# ---------------------------------------------------------------------------

_oauth_code: str | None = None
_oauth_received = threading.Event()


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _oauth_code
        params = parse_qs(urlparse(self.path).query)
        code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if code:
            _oauth_code = code
            _oauth_received.set()
            self.wfile.write(b"<h1>Autorizado!</h1><p>Puedes cerrar esta pagina.</p>")
        else:
            self.wfile.write(b"<h1>Error: no se recibio el codigo.</h1>")

    def log_message(self, *args):
        pass


def _run_callback_server():
    server = HTTPServer(("0.0.0.0", OAUTH_PORT), _CallbackHandler)
    server.handle_request()
    server.server_close()


# ---------------------------------------------------------------------------
# Spotify client
# ---------------------------------------------------------------------------

def build_spotify_client(dotenv_values: dict) -> spotipy.Spotify:
    client_id     = get_config_value("SPOTIFY_CLIENT_ID",     dotenv_values)
    client_secret = get_config_value("SPOTIFY_CLIENT_SECRET", dotenv_values)
    redirect_uri  = get_config_value("SPOTIFY_REDIRECT_URI",  dotenv_values, "http://localhost:8888/callback")
    if not client_id or not client_secret:
        raise ValueError("Faltan credenciales Spotify (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET).")
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=SCOPES,
            cache_path=CACHE_PATH,
            open_browser=False,
        )
    )


dotenv_values = load_dotenv_values()
bot_token = get_config_value("BOT_TOKEN", dotenv_values)
if not bot_token:
    raise ValueError("Falta BOT_TOKEN en variables de entorno o en .env.")

sp = build_spotify_client(dotenv_values)

# ---------------------------------------------------------------------------
# YT-DLP
# ---------------------------------------------------------------------------

YTDL_OPTIONS = {
    # Prefer m4a/AAC: one transcode (AAC→Opus) is cleaner than opus→PCM→Opus.
    # Falls back to webm/opus if m4a is not available, then any best audio.
    "format": "bestaudio[ext=m4a]/bestaudio[acodec=opus]/bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "source_address": "0.0.0.0",
}

# -reconnect*        keeps the stream alive on transient network errors.
# -probesize         10M is enough for audio; 200M was stalling the pipeline.
# -analyzeduration   2s is sufficient for audio; 10s added unnecessary startup lag.
# -thread_queue_size large packet queue so the demuxer never starves the decoder.
# -bufsize 512k      output buffer large enough to absorb jitter (128k was causing artifacts).
# Note: do NOT add -ar or -ac here — FFmpegOpusAudio handles Opus encoding internally
# and forcing resample/channel conversion introduces stereo phase artifacts.
FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
        "-probesize 10M -analyzeduration 2000000 "
        "-thread_queue_size 4096"
    ),
    "options": "-vn -bufsize 512k",
}


SEARCH_RESULT_COUNT = 5
MIN_SEARCH_SCORE = 6.0
LLM_SCORE_MARGIN = 4.5  # Increased from 3.0 to reduce LLM calls; only use when candidates very close
LLM_RANKING_TIMEOUT = 8.0
LLM_ENABLED_FOR_ALBUM_TRACKS = 3  # Only use LLM for first N tracks in bulk operations
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-haiku-4-5"
_anthropic_client = None
_search_cache: dict[str, dict] = {}  # Cache YouTube search results to avoid redundant queries
NOISE_TERMS = {
    "official",
    "video",
    "audio",
    "lyrics",
    "lyric",
    "hd",
    "hq",
    "4k",
    "mv",
    "music",
    "visualizer",
    "visualiser",
    "clip",
    "version",
    "full",
}
VARIANT_TERMS = {"live", "remix", "cover", "karaoke", "acoustic", "instrumental"}
PREFERRED_CHANNEL_HINTS = ("topic", "vevo", "official", "records", "music")
MIN_SPOTIFY_REFINEMENT_SCORE = 7.5


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
                candidates.append({
                    "title": entry.get("title", query),
                    "url": entry["url"],
                    "duration": entry.get("duration"),
                    "uploader": entry.get("uploader") or "",
                    "channel": entry.get("channel") or "",
                    "webpage_url": entry.get("webpage_url") or "",
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


# ---------------------------------------------------------------------------
# Per-guild playback state
# ---------------------------------------------------------------------------

# Each item in the queue is {title, yt_query, url (may be None), requester}.
# url is resolved lazily just before playback so playlist enqueuing is instant.
queues: dict[int, collections.deque] = {}
now_playing_info: dict[int, dict | None] = {}
_prefetch_tasks: dict[int, asyncio.Task | None] = {}

# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------------------
# Spotify auth helper (optional — enhances !play search quality)
# ---------------------------------------------------------------------------

def _is_spotify_url(query: str) -> bool:
    """Check if query is a Spotify URL."""
    return bool(re.match(r"https?://(open\.)?spotify\.com/(track|album|playlist)", query))


def _parse_spotify_url(url: str) -> dict | None:
    """Parse Spotify URL and return {type, id}. Returns None if not a valid Spotify URL."""
    match = re.search(r"spotify\.com/(track|album|playlist)/([a-zA-Z0-9]+)", url)
    if match:
        return {"type": match.group(1), "id": match.group(2)}
    return None


async def _get_tracks_from_spotify_url(url: str) -> list[str] | None:
    """Extract track queries from a Spotify URL. Returns list of 'Artist - Title' queries or None on error."""
    parsed = _parse_spotify_url(url)
    if not parsed:
        return None
    
    try:
        resource_type = parsed["type"]
        resource_id = parsed["id"]
        
        if resource_type == "track":
            track = await asyncio.to_thread(lambda: sp.track(resource_id))
            query = _format_spotify_track_query(track)
            logger.info(f"spotify_url: extraida cancion '{query}' de URL de track")
            return [query]
        
        elif resource_type == "album":
            album = await asyncio.to_thread(lambda: sp.album(resource_id))
            album_name = album.get("name", "Album")
            tracks = album.get("tracks", {}).get("items", [])
            queries = [_format_spotify_track_query(t) for t in tracks]
            logger.info(f"spotify_url: extraidas {len(queries)} canciones del album '{album_name}'")
            return queries
        
        elif resource_type == "playlist":
            # Playlists are paginated; fetch all items
            all_tracks = []
            playlist_name = ""
            offset = 0
            limit = 50
            while True:
                result = await asyncio.to_thread(lambda: sp.playlist_items(resource_id, offset=offset, limit=limit))
                playlist_name = result.get("name", "Playlist")
                items = result.get("items", [])
                if not items:
                    break
                for item in items:
                    if item.get("track"):
                        all_tracks.append(item["track"])
                offset += limit
            queries = [_format_spotify_track_query(t) for t in all_tracks]
            logger.info(f"spotify_url: extraidas {len(queries)} canciones de la playlist '{playlist_name}'")
            return queries
    
    except Exception as exc:
        logger.warning(f"spotify_url: fallo extrayendo canciones de URL: {exc}")
    
    return None


async def _get_spotify_query(query: str) -> str:
    """Try to refine the search query using Spotify metadata; fall back to raw."""
    try:
        auth_manager = sp.auth_manager
        cached = await asyncio.to_thread(auth_manager.cache_handler.get_cached_token)
        valid  = await asyncio.to_thread(auth_manager.validate_token, cached)
        if not valid:
            return query
        results = await asyncio.to_thread(lambda: sp.search(q=query, type="track", limit=5))
        items = results.get("tracks", {}).get("items", [])
        if items:
            scored_items = []
            for item in items:
                score = _score_spotify_match(query, item)
                scored_items.append((score, item))

            scored_items.sort(key=lambda item: item[0], reverse=True)
            preview = ", ".join(
                f"{score:.2f}:{_format_spotify_track_query(item)}"
                for score, item in scored_items[:3]
            )
            logger.info(f"spotify_refine: top candidatos para '{query}': {preview}")

            best_score, best_item = scored_items[0]
            if best_score >= MIN_SPOTIFY_REFINEMENT_SCORE:
                refined_query = _format_spotify_track_query(best_item)
                logger.info(
                    "spotify_refine: usando '%s' para '%s' (score=%.2f)",
                    refined_query,
                    query,
                    best_score,
                )
                return refined_query

            logger.warning(
                "spotify_refine: descartando refinamiento para '%s'; mejor candidato '%s' con score %.2f",
                query,
                _format_spotify_track_query(best_item),
                best_score,
            )
    except Exception as exc:
        logger.warning(f"spotify_refine: fallo refinando '{query}': {exc}")
    return query


async def _ensure_auth(ctx: commands.Context) -> bool:
    global _oauth_code, _oauth_received
    auth_manager = sp.auth_manager
    cached = await asyncio.to_thread(auth_manager.cache_handler.get_cached_token)
    valid  = await asyncio.to_thread(auth_manager.validate_token, cached)
    if valid:
        return True

    # Only the admin can kick off the OAuth flow
    if ctx.author.id != ADMIN_USER_ID:
        await ctx.send("Spotify no esta autenticado. Pide al admin que ejecute `!auth`.")
        return False

    _oauth_code = None
    _oauth_received.clear()
    auth_url = auth_manager.get_authorize_url()

    # Send URL via DM so it is never visible in the channel
    try:
        await ctx.author.send(
            f"Autenticacion de Spotify requerida. Abre este enlace y autoriza:\n{auth_url}"
        )
        await ctx.send("Te envie el enlace de autenticacion por DM.")
    except discord.Forbidden:
        await ctx.send(
            "No pude enviarte un DM. Habilita los mensajes directos en Discord."
        )
        return False

    threading.Thread(target=_run_callback_server, daemon=True).start()
    received = await asyncio.to_thread(_oauth_received.wait, 300)
    if not received or not _oauth_code:
        await ctx.author.send("Tiempo agotado. Usa `!auth` para reintentar.")
        return False
    code = _oauth_code
    await asyncio.to_thread(
        lambda: auth_manager.get_access_token(code, as_dict=False, check_cache=False)
    )
    await ctx.send("Spotify autenticado correctamente.")
    return False


# ---------------------------------------------------------------------------
# Voice playback helper
# ---------------------------------------------------------------------------

async def _resolve_url(track: dict) -> dict | None:
    """Ensure track['url'] is populated. Returns None if YouTube search fails."""
    if track.get("url"):
        return track
    yt_info = await search_youtube(track["yt_query"])
    if not yt_info:
        return None
    track["url"]   = yt_info["url"]
    track["title"] = yt_info["title"]
    return track


async def _prefetch_next(guild_id: int):
    """Resolve the URL of the next queued track in the background."""
    q = queues.get(guild_id)
    if q:
        await _resolve_url(q[0])


async def play_next(guild: discord.Guild, vc: discord.VoiceClient, text_channel):
    # Cancel any pending prefetch for this guild
    task = _prefetch_tasks.pop(guild.id, None)
    if task and not task.done():
        task.cancel()

    q = queues.get(guild.id)
    if not q:
        now_playing_info[guild.id] = None
        await _update_status(guild, None)
        await asyncio.sleep(1)
        if guild.voice_client:
            await guild.voice_client.disconnect()
        return

    track = q.popleft()

    # Resolve YouTube URL if not yet fetched (lazy playlist items)
    track = await _resolve_url(track)
    if not track:
        await text_channel.send("No se encontro en YouTube, saltando...")
        await play_next(guild, vc, text_channel)
        return

    now_playing_info[guild.id] = track

    # Pre-fetch the next track's URL while this one starts playing
    if q:
        _prefetch_tasks[guild.id] = asyncio.create_task(_prefetch_next(guild.id))

    try:
        # Use FFmpegOpusAudio directly (no probe) to avoid an extra HTTP round-trip
        # on token-authenticated YouTube stream URLs.
        source = discord.FFmpegOpusAudio(track["url"], **FFMPEG_OPTIONS)
        logger.info(
            "play_next: reproduciendo '%s' (codec=%s, abr=%s)",
            track["title"],
            track.get("acodec", "?"),
            track.get("abr", "?"),
        )
    except Exception as e:
        logger.error(f"Error creando audio source: {e}")
        await text_channel.send(f"Error reproduciendo **{track['title']}**, saltando...")
        await play_next(guild, vc, text_channel)
        return

    def after(error):
        if error:
            logger.error(f"Error en reproduccion: {error}")
        asyncio.run_coroutine_threadsafe(play_next(guild, vc, text_channel), bot.loop)

    vc.play(source, after=after)
    await text_channel.send(
        f"\U0001f3b5 Reproduciendo: **{track['title']}** — pedido por {track['requester']}"
    )
    await _update_status(guild, track["title"])


async def _update_status(guild: discord.Guild, title: str | None):
    """Update bot presence activity and voice channel status."""
    # Bot activity — visible on the bot's profile as "Listening to ..."
    activity = (
        discord.Activity(type=discord.ActivityType.listening, name=title)
        if title else None
    )
    await bot.change_presence(activity=activity)

    # Voice channel status — uses Discord's dedicated endpoint (PUT /channels/{id}/voice-status)
    # discord.py 2.3 doesn't expose this via channel.edit(), so we call the raw HTTP route.
    vc = guild.voice_client
    if not vc:
        logger.warning("_update_status: no hay voice_client activo, no se puede actualizar estado")
        return
    status_text = f"\U0001f3b5 {title}" if title else ""
    logger.info(f"_update_status: actualizando canal {vc.channel.id} con estado: '{status_text}'")
    try:
        route = discord.http.Route(
            "PUT", "/channels/{channel_id}/voice-status",
            channel_id=vc.channel.id
        )
        await bot.http.request(route, json={"status": status_text})
        logger.info("_update_status: estado del canal actualizado correctamente")
    except Exception as e:
        logger.error(f"_update_status: error al actualizar estado del canal de voz: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Channel check
# ---------------------------------------------------------------------------

@bot.check
async def only_allowed_channel(ctx: commands.Context) -> bool:
    return ctx.channel.id == ALLOWED_CHANNEL_ID


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    logger.info(f"Bot conectado como {bot.user} (id={bot.user.id})")
    logger.info(f"Guilds: {[g.name for g in bot.guilds]}")
    try:
        channel = await bot.fetch_channel(ALLOWED_CHANNEL_ID)
        await channel.send(
            "\U0001f3b5 Bot de musica listo. "
            "Entra a un canal de voz y usa `!play <cancion>` para reproducir."
        )
        logger.info("Mensaje de startup enviado correctamente.")
    except Exception as e:
        logger.error(f"No se pudo enviar el mensaje de startup: {e}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CheckFailure):
        logger.info(f"Comando ignorado: canal incorrecto (id={ctx.channel.id})")
        return
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Faltan argumentos. Uso: `!{ctx.command.name} {ctx.command.signature}`")
        return
    await ctx.send(f"Error inesperado: `{error}`")
    logger.error(f"[ERROR] {ctx.command}: {error}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.command(name="ping", help="Comprueba que el bot esta vivo.")
async def ping(ctx: commands.Context):
    await ctx.send(f"Pong! Latencia: {round(bot.latency * 1000)}ms")


@bot.command(name="auth", help="Inicia o renueva la autenticacion de Spotify. Solo admin.")
async def auth_cmd(ctx: commands.Context):
    if ctx.author.id != ADMIN_USER_ID:
        await ctx.send("Solo el administrador puede usar este comando.")
        return
    if not await _ensure_auth(ctx):
        return
    await ctx.send("Spotify ya esta autenticado.")


@bot.command(name="play", help="Reproduce una cancion en tu canal de voz. Uso: !play <busqueda>")
async def play(ctx: commands.Context, *, query: str):
    if not ctx.author.voice:
        await ctx.send("Debes estar en un canal de voz para usar este comando.")
        return

    voice_channel = ctx.author.voice.channel
    msg = await ctx.send(f"\U0001f50d Buscando **{query}**...")

    # Check if query is a Spotify URL (album/playlist/track)
    if _is_spotify_url(query):
        yt_queries = await _get_tracks_from_spotify_url(query)
        if not yt_queries:
            await msg.edit(content=f"No se pudo procesar la URL de Spotify.")
            return
    else:
        # Regular text search; refine with Spotify
        refined = await _get_spotify_query(query)
        yt_queries = [refined]

    vc = ctx.guild.voice_client
    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    if ctx.guild.id not in queues:
        queues[ctx.guild.id] = collections.deque()

    # Search YouTube for all queries and build track objects
    tracks_to_queue = []
    for idx, yt_query in enumerate(yt_queries):
        # Only use LLM for first N tracks in album/playlist (reduces API calls by ~70%)
        enable_llm = (idx < LLM_ENABLED_FOR_ALBUM_TRACKS) if len(yt_queries) > 1 else True
        yt_info = await search_youtube(yt_query, enable_llm=enable_llm)
        if yt_info:
            track = {
                "title":    yt_info["title"],
                "yt_query": yt_query,
                "url":      yt_info["url"],
                "requester": ctx.author.display_name,
            }
            tracks_to_queue.append(track)

    if not tracks_to_queue:
        await msg.edit(content=f"No se encontro nada para: **{query}**")
        return

    # Add all tracks to queue
    for track in tracks_to_queue:
        queues[ctx.guild.id].append(track)

    # Start playing if not already playing
    if vc.is_playing() or vc.is_paused():
        added_count = len(tracks_to_queue)
        if added_count == 1:
            await msg.edit(content=f"\u2795 Anadido a la cola: **{tracks_to_queue[0]['title']}**")
        else:
            await msg.edit(content=f"\u2795 Anadidas {added_count} canciones a la cola.")
    else:
        if len(tracks_to_queue) == 1:
            await msg.edit(content=f"\u2705 Listo!")
        else:
            await msg.edit(content=f"\u2705 Anadidas {len(tracks_to_queue)} canciones a la cola!")
        await play_next(ctx.guild, vc, ctx.channel)


@bot.command(name="skip", help="Salta la cancion actual.")
async def skip(ctx: commands.Context):
    vc = ctx.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        await ctx.send("No hay nada reproduciendose.")
        return
    vc.stop()
    await ctx.send("\u23ed\ufe0f Saltando...")


@bot.command(name="pause", help="Pausa la reproduccion.")
async def pause(ctx: commands.Context):
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("\u23f8\ufe0f Pausa.")
    else:
        await ctx.send("No hay nada reproduciendose.")


@bot.command(name="resume", help="Reanuda la reproduccion.")
async def resume(ctx: commands.Context):
    vc = ctx.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("\u25b6\ufe0f Reanudando.")
    else:
        await ctx.send("No hay nada en pausa.")


@bot.command(name="stop", help="Detiene la reproduccion y limpia la cola.")
async def stop(ctx: commands.Context):
    queues[ctx.guild.id] = collections.deque()
    now_playing_info[ctx.guild.id] = None
    vc = ctx.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
    await ctx.send("\u23f9\ufe0f Reproduccion detenida.")


@bot.command(name="leave", help="Desconecta el bot del canal de voz.")
async def leave(ctx: commands.Context):
    queues[ctx.guild.id] = collections.deque()
    now_playing_info[ctx.guild.id] = None
    vc = ctx.guild.voice_client
    if vc:
        await vc.disconnect()
        await ctx.send("\U0001f44b Hasta luego!")
    else:
        await ctx.send("No estoy en ningun canal de voz.")


@bot.command(name="queue", help="Muestra la cola de reproduccion.")
async def queue_cmd(ctx: commands.Context):
    q       = queues.get(ctx.guild.id, collections.deque())
    current = now_playing_info.get(ctx.guild.id)
    if not current and not q:
        await ctx.send("La cola esta vacia.")
        return
    lines = []
    if current:
        lines.append(f"\U0001f3b5 **Ahora:** {current['title']}")
    for i, track in enumerate(list(q)[:10], 1):
        lines.append(f"{i}. {track['title']}")
    if len(q) > 10:
        lines.append(f"... y {len(q) - 10} mas")
    await ctx.send("\n".join(lines))


@bot.command(name="np", help="Muestra la cancion que se esta reproduciendo ahora.")
async def now_playing_cmd(ctx: commands.Context):
    current = now_playing_info.get(ctx.guild.id)
    if not current:
        await ctx.send("No hay nada reproduciendose.")
        return
    await ctx.send(f"\U0001f3b5 **{current['title']}** — pedido por {current['requester']}")


@bot.command(name="search", help="Busca canciones en Spotify. Uso: !search <busqueda>")
async def search(ctx: commands.Context, *, query: str):
    if not await _ensure_auth(ctx):
        return
    try:
        results = await asyncio.to_thread(lambda: sp.search(q=query, type="track", limit=5))
    except Exception as e:
        await ctx.send(f"Error buscando en Spotify: `{e}`")
        return
    items = results.get("tracks", {}).get("items", [])
    if not items:
        await ctx.send(f"No se encontraron resultados para: **{query}**")
        return
    embed = discord.Embed(title=f'Resultados para "{query}"', color=discord.Color.blurple())
    for i, track in enumerate(items, 1):
        artists = ", ".join(a["name"] for a in track["artists"])
        album   = track.get("album", {}).get("name", "")
        embed.add_field(name=f"{i}. {track['name']}", value=f"{artists} — {album}", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="playlist", help="Carga una playlist de Spotify en la cola. Uso: !playlist <url>")
async def playlist_cmd(ctx: commands.Context, *, url: str):
    if not ctx.author.voice:
        await ctx.send("Debes estar en un canal de voz para usar este comando.")
        return

    if not await _ensure_auth(ctx):
        return

    msg = await ctx.send("📋 Cargando playlist de Spotify...")

    # Extract playlist ID from URL or plain ID
    import re as _re
    match = _re.search(r"playlist[:/]([A-Za-z0-9]+)", url)
    playlist_id = match.group(1) if match else url.strip()

    try:
        # Fetch all tracks (Spotify paginates at 100)
        def _fetch_all_tracks(pid):
            tracks = []
            result = sp.playlist_tracks(pid, fields="items(track(name,artists)),next", limit=100)
            while result:
                for item in result.get("items", []):
                    t = item.get("track")
                    if t and t.get("name"):
                        artists = ", ".join(a["name"] for a in t.get("artists", []))
                        tracks.append(f"{artists} - {t['name']}")
                result = sp.next(result) if result.get("next") else None
            return tracks

        track_queries = await asyncio.to_thread(_fetch_all_tracks, playlist_id)
    except Exception as e:
        await msg.edit(content=f"Error cargando la playlist: `{e}`")
        return

    if not track_queries:
        await msg.edit(content="La playlist esta vacia o no se pudo leer.")
        return

    voice_channel = ctx.author.voice.channel
    vc = ctx.guild.voice_client
    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    if ctx.guild.id not in queues:
        queues[ctx.guild.id] = collections.deque()

    # Enqueue all tracks lazily (url=None, resolved just before playback)
    for q_str in track_queries:
        queues[ctx.guild.id].append({
            "title":     q_str,
            "yt_query":  q_str,
            "url":       None,
            "requester": ctx.author.display_name,
        })

    await msg.edit(content=f"✅ {len(track_queries)} canciones añadidas a la cola.")

    # Start playing if nothing is currently playing
    if not (vc.is_playing() or vc.is_paused()):
        await play_next(ctx.guild, vc, ctx.channel)


if __name__ == "__main__":
    bot.run(bot_token)
