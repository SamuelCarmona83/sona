import random as _random
import asyncio
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import discord
from discord.ext import commands

from src.config import (
    OAUTH_PORT,
    ADMIN_USER_ID,
    MIN_SPOTIFY_REFINEMENT_SCORE,
    sp,
)
from src.scoring import _format_spotify_track_query, _score_spotify_match

logger = logging.getLogger(__name__)

_oauth_code: str | None = None
_oauth_received = threading.Event()

# In-memory cache: artist_id -> list of genre strings (avoids repeated API calls per artist)
_artist_genres_cache: dict[str, list[str]] = {}


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


def _is_spotify_url(query: str) -> bool:
    """Check if query is a Spotify URL."""
    return bool(re.match(r"https?://(open\.)?spotify\.com/(track|album|playlist)", query))


def _parse_spotify_url(url: str) -> dict | None:
    """Parse Spotify URL and return {type, id}. Returns None if not a valid Spotify URL."""
    match = re.search(r"spotify\.com/(track|album|playlist)/([a-zA-Z0-9]+)", url)
    if match:
        return {"type": match.group(1), "id": match.group(2)}
    return None


def _track_to_info(track: dict) -> dict:
    """Extract {query, spotify_id, artist_id} from a Spotify track object."""
    return {
        "query":     _format_spotify_track_query(track),
        "spotify_id": track.get("id"),
        "artist_id":  (track.get("artists") or [{}])[0].get("id"),
    }


async def _get_tracks_from_spotify_url(url: str) -> list[dict] | None:
    """Extract track info from a Spotify URL.

    Returns list of {query, spotify_id, artist_id} dicts, or None on error.
    """
    parsed = _parse_spotify_url(url)
    if not parsed:
        return None

    try:
        resource_type = parsed["type"]
        resource_id = parsed["id"]

        if resource_type == "track":
            track = await asyncio.to_thread(lambda: sp.track(resource_id))
            info = _track_to_info(track)
            logger.info(f"spotify_url: extraida cancion '{info['query']}' de URL de track")
            return [info]

        elif resource_type == "album":
            album = await asyncio.to_thread(lambda: sp.album(resource_id))
            album_name = album.get("name", "Album")
            tracks = album.get("tracks", {}).get("items", [])
            infos = [_track_to_info(t) for t in tracks]
            logger.info(f"spotify_url: extraidas {len(infos)} canciones del album '{album_name}'")
            return infos

        elif resource_type == "playlist":
            all_tracks = []
            playlist_name = ""
            offset = 0
            limit = 50
            while True:
                result = await asyncio.to_thread(lambda: sp.playlist_items(resource_id, offset=offset, limit=limit, market="from_token"))
                playlist_name = result.get("name", "Playlist")
                items = result.get("items", [])
                if not items:
                    break
                for item in items:
                    if item.get("track"):
                        all_tracks.append(item["track"])
                offset += limit
            infos = [_track_to_info(t) for t in all_tracks]
            logger.info(f"spotify_url: extraidas {len(infos)} canciones de la playlist '{playlist_name}'")
            return infos

    except Exception as exc:
        logger.warning(f"spotify_url: fallo extrayendo canciones de URL: {exc}")

    return None


async def _get_spotify_track_info(query: str) -> dict:
    """Refine a text query using Spotify and return {query, spotify_id, artist_id}.

    Falls back to {query=query, spotify_id=None, artist_id=None} on any failure.
    """
    fallback = {"query": query, "spotify_id": None, "artist_id": None}
    try:
        auth_manager = sp.auth_manager
        cached = await asyncio.to_thread(auth_manager.cache_handler.get_cached_token)
        valid  = await asyncio.to_thread(auth_manager.validate_token, cached)
        if not valid:
            return fallback
        results = await asyncio.to_thread(lambda: sp.search(q=query, type="track", limit=5))
        items = results.get("tracks", {}).get("items", [])
        if items:
            scored_items = []
            for item in items:
                score = _score_spotify_match(query, item)
                scored_items.append((score, item))
            scored_items.sort(key=lambda x: x[0], reverse=True)
            preview = ", ".join(
                f"{score:.2f}:{_format_spotify_track_query(item)}"
                for score, item in scored_items[:3]
            )
            logger.info(f"spotify_refine: top candidatos para '{query}': {preview}")
            best_score, best_item = scored_items[0]
            if best_score >= MIN_SPOTIFY_REFINEMENT_SCORE:
                refined = _format_spotify_track_query(best_item)
                logger.info(
                    "spotify_refine: usando '%s' para '%s' (score=%.2f)",
                    refined, query, best_score,
                )
                return {
                    "query":      refined,
                    "spotify_id": best_item.get("id"),
                    "artist_id":  (best_item.get("artists") or [{}])[0].get("id"),
                }
            logger.warning(
                "spotify_refine: descartando refinamiento para '%s'; mejor candidato '%s' con score %.2f",
                query, _format_spotify_track_query(best_item), best_score,
            )
    except Exception as exc:
        logger.warning(f"spotify_refine: fallo refinando '{query}': {exc}")
    return fallback


async def _get_spotify_query(query: str) -> str:
    """Thin wrapper around _get_spotify_track_info; returns only the refined query string."""
    return (await _get_spotify_track_info(query))["query"]


async def _get_artist_genres(artist_id: str) -> list[str]:
    """Return Spotify genre tags for an artist, with in-memory caching."""
    if artist_id in _artist_genres_cache:
        return _artist_genres_cache[artist_id]
    try:
        artist = await asyncio.to_thread(lambda: sp.artist(artist_id))
        genres = artist.get("genres") or []
    except Exception as exc:
        logger.warning(f"_get_artist_genres: error para artist_id={artist_id}: {exc}")
        genres = []
    _artist_genres_cache[artist_id] = genres
    return genres


async def _get_recommendations(
    seed_tracks: list[str],
    seed_genres: list[str],
    limit: int = 10,
) -> list[dict]:
    """Return track suggestions as list of {query, spotify_id, artist_id}.

    The Spotify /recommendations and /related-artists endpoints are restricted
    to apps created before Nov 2023, so we use three search-based strategies:

    1. Genre seeds  → sp.search(q='genre:"<g>"') with a randomised offset for variety.
    2. Track seeds  → fetch the seed track's artist top-tracks directly.
    3. Track seeds  → search by artist name to find more tracks (genre search variant).

    Results are deduplicated and shuffled before returning.
    """
    if not seed_tracks and not seed_genres:
        return []

    seen_ids: set[str] = set()
    results: list[dict] = []

    # --- Strategy 1: genre search ---
    per_genre = max(1, (limit // max(len(seed_genres), 1)) + 1) if seed_genres else 0
    for genre in seed_genres:
        if len(results) >= limit * 2:
            break
        try:
            # Random offset (0–40) gives variety across fills without repeating the same top hits
            offset = _random.randint(0, 40)
            res = await asyncio.to_thread(
                lambda g=genre, o=offset: sp.search(
                    q=f'genre:"{g}"', type="track", limit=per_genre + 2, offset=o
                )
            )
            for t in (res.get("tracks") or {}).get("items") or []:
                if t and t.get("id") and t["id"] not in seen_ids:
                    seen_ids.add(t["id"])
                    results.append(_track_to_info(t))
        except Exception as exc:
            logger.warning(f"_get_recommendations: genre search failed for '{genre}': {exc}")

    # --- Strategy 2: seed track's artist top tracks (no related-artists call) ---
    for track_id in seed_tracks:
        if len(results) >= limit * 2:
            break
        try:
            track = await asyncio.to_thread(lambda tid=track_id: sp.track(tid))
            artists = track.get("artists") or []
            if not artists:
                continue
            artist_id = artists[0]["id"]
            artist_name = artists[0].get("name", "")
            # Top tracks for the seed artist directly
            top = await asyncio.to_thread(
                lambda aid=artist_id: sp.artist_top_tracks(aid, country="US")
            )
            for t in (top.get("tracks") or [])[:5]:
                if t and t.get("id") and t["id"] not in seen_ids:
                    seen_ids.add(t["id"])
                    results.append(_track_to_info(t))
            # Also search by artist name with random offset for more variety
            if len(results) < limit * 2 and artist_name:
                offset = _random.randint(0, 20)
                res = await asyncio.to_thread(
                    lambda an=artist_name, o=offset: sp.search(
                        q=f'artist:"{an}"', type="track", limit=5, offset=o
                    )
                )
                for t in (res.get("tracks") or {}).get("items") or []:
                    if t and t.get("id") and t["id"] not in seen_ids:
                        seen_ids.add(t["id"])
                        results.append(_track_to_info(t))
        except Exception as exc:
            logger.warning(f"_get_recommendations: artist search failed for track {track_id}: {exc}")

    if not results:
        return []

    _random.shuffle(results)
    logger.info(
        "_get_recommendations: %d sugerencias generadas (genres=%s, seed_tracks=%d)",
        len(results), seed_genres, len(seed_tracks),
    )
    return results[:limit]


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
