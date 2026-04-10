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
