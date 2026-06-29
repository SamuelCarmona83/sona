import random as _random
import asyncio
import logging
import pathlib
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import discord
from discord.ext import commands
from spotipy.oauth2 import SpotifyOauthError

from src.config import (
    OAUTH_ADMIN_USER_ID,
    SPOTIFY_OAUTH_PORT,
    SPOTIFY_TOKEN_CACHE_PATH,
    MIN_SPOTIFY_REFINEMENT_SCORE,
    sp,
)
from src.scoring import _format_spotify_track_query, _score_spotify_match

logger = logging.getLogger(__name__)

_oauth_code: str | None = None
_oauth_state: str | None = None
_oauth_received = threading.Event()

# In-memory cache: artist_id -> list of genre strings (avoids repeated API calls per artist)
_artist_genres_cache: dict[str, list[str]] = {}


def clear_spotify_token_cache() -> bool:
    """Delete the cached OAuth token file. Returns True if a file was removed."""
    path = pathlib.Path(SPOTIFY_TOKEN_CACHE_PATH)
    if path.is_file():
        path.unlink()
        logger.info("spotify: cleared stale token cache at %s", path)
        return True
    return False


def _is_stale_token_error(exc: SpotifyOauthError) -> bool:
    message = str(exc).lower()
    return "invalid_client" in message or "invalid_grant" in message


async def _safe_validate_token(auth_manager) -> bool:
    """Validate or refresh the cached token, clearing stale cache on auth errors."""
    cached = await asyncio.to_thread(auth_manager.cache_handler.get_cached_token)
    if not cached:
        return False
    try:
        return bool(await asyncio.to_thread(auth_manager.validate_token, cached))
    except SpotifyOauthError as exc:
        if _is_stale_token_error(exc):
            clear_spotify_token_cache()
            logger.warning("spotify: token refresh failed (%s), cache cleared", exc)
            return False
        raise


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _oauth_code, _oauth_state
        params = parse_qs(urlparse(self.path).query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if code:
            _oauth_code = code
            _oauth_state = state
            _oauth_received.set()
            self.wfile.write(b"<h1>Autorizado!</h1><p>Puedes cerrar esta pagina.</p>")
        else:
            self.wfile.write(b"<h1>Error: no se recibio el codigo.</h1>")

    def log_message(self, *args):
        pass


def _run_callback_server():
    server = HTTPServer(("0.0.0.0", SPOTIFY_OAUTH_PORT), _CallbackHandler)
    server.handle_request()
    server.server_close()


async def _complete_oauth_from_callback(expected_state: str) -> bool:
    """Exchange the OAuth code captured by the callback server."""
    global _oauth_code, _oauth_state
    if not _oauth_code:
        return False
    if _oauth_state and _oauth_state != expected_state:
        logger.warning(
            "spotify: OAuth state mismatch (expected=%s got=%s)",
            expected_state,
            _oauth_state,
        )
        return False

    code = _oauth_code
    if expected_state == "admin":
        if sp is None:
            return False
        await asyncio.to_thread(
            lambda: sp.auth_manager.get_access_token(code, as_dict=False, check_cache=False)
        )
        return True

    if expected_state.startswith("user:"):
        from src.spotify_users import complete_user_oauth
        discord_user_id = int(expected_state.split(":", 1)[1])
        await complete_user_oauth(discord_user_id, code)
        return True

    return False


async def run_oauth_flow(
    ctx: commands.Context,
    *,
    expected_state: str,
    authorize_url: str,
    success_message: str,
    timeout_message: str = "Tiempo agotado. Intenta de nuevo.",
) -> bool:
    """Shared OAuth waiter used by admin auth and per-user Spotify linking."""
    global _oauth_code, _oauth_state, _oauth_received

    _oauth_code = None
    _oauth_state = None
    _oauth_received.clear()

    try:
        await ctx.author.send(
            "Autorizacion de Spotify requerida. Abre este enlace:\n"
            f"{authorize_url}"
        )
        await ctx.send("Te envie el enlace de autenticacion por DM.", delete_after=10)
    except discord.Forbidden:
        await ctx.send("No pude enviarte un DM. Habilita los mensajes directos en Discord.")
        return False

    threading.Thread(target=_run_callback_server, daemon=True).start()
    received = await asyncio.to_thread(_oauth_received.wait, 300)
    if not received or not _oauth_code:
        await ctx.author.send(timeout_message)
        return False

    try:
        if not await _complete_oauth_from_callback(expected_state):
            await ctx.author.send("Fallo la autenticacion de Spotify. Intenta de nuevo.")
            return False
    except SpotifyOauthError as exc:
        logger.error("spotify: OAuth token exchange failed: %s", exc)
        await ctx.author.send(
            "Fallo la autenticacion de Spotify.\n"
            "Verifica `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` y el Redirect URI "
            "`http://127.0.0.1:8888/callback` en el dashboard de Spotify."
        )
        return False

    await ctx.send(success_message)
    return True


def _is_spotify_url(query: str) -> bool:
    """Check if query is a Spotify URL."""
    return bool(re.search(r"spotify\.com/(?:[a-z]{2}(?:-[a-zA-Z]{2,4})?/)?(track|album|playlist)/", query))


def _parse_spotify_url(url: str) -> dict | None:
    """Parse Spotify URL and return {type, id}. Returns None if not a valid Spotify URL."""
    match = re.search(r"spotify\.com/(?:[a-z]{2}(?:-[a-zA-Z]{2,4})?/)?(track|album|playlist)/([a-zA-Z0-9]+)", url)
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
        if sp is None:
            return fallback
        auth_manager = sp.auth_manager
        if not await _safe_validate_token(auth_manager):
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
    if not await _spotify_api_ready():
        return []
    try:
        artist = await asyncio.to_thread(lambda: sp.artist(artist_id))
        genres = artist.get("genres") or []
    except Exception as exc:
        logger.warning(f"_get_artist_genres: error para artist_id={artist_id}: {exc}")
        genres = []
    _artist_genres_cache[artist_id] = genres
    return genres


async def _spotify_api_ready() -> bool:
    if sp is None:
        return False
    return await _safe_validate_token(sp.auth_manager)


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
    if not await _spotify_api_ready():
        logger.warning("_get_recommendations: Spotify no autorizado, omitiendo")
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


async def _get_recommendations_hybrid(
    seed_tracks: list[str] = None,
    seed_genres: list[str] = None,
    limit: int = 10,
) -> list[dict]:
    """Get recommendations with fallback chain: Spotify → LastFM → YouTube generic.
    
    Tries Spotify first (if available), falls back to LastFM similar artists,
    then YouTube generic search as last resort.
    
    Returns list of dicts with keys: query, spotify_id (or None), artist_id (or None).
    """
    from src.config import SPOTIFY_AVAILABLE
    
    if seed_tracks is None:
        seed_tracks = []
    if seed_genres is None:
        seed_genres = []
    
    # Tier 1: Try Spotify if available and authorized
    if SPOTIFY_AVAILABLE and (seed_tracks or seed_genres) and await _spotify_api_ready():
        try:
            recs = await _get_recommendations(seed_tracks, seed_genres, limit=limit)
            if recs:
                logger.info(
                    "_get_recommendations_hybrid: Spotify tier worked, %d recs",
                    len(recs),
                )
                return recs
        except Exception as exc:
            logger.warning(
                "_get_recommendations_hybrid: Spotify tier failed: %s",
                exc,
            )
    
    # Tier 2: LastFM fallback using similar artists + top tracks
    from src import lastfm
    recommendations: list[dict] = []
    seen_queries: set[str] = set()
    
    # If we have seed_genres, search for artists in those genres via LastFM
    if seed_genres:
        for genre in seed_genres[:3]:  # Limit to 3 genres
            try:
                artists = await lastfm.search_artists_by_genre(genre, limit=3)
                for artist_name in artists:
                    if len(recommendations) >= limit * 2:
                        break
                    try:
                        tracks = await lastfm.get_top_tracks(artist_name, limit=3)
                        for track in tracks:
                            if len(recommendations) >= limit * 2:
                                break
                            query = f"{track['artist']} {track['title']}"
                            if query not in seen_queries:
                                seen_queries.add(query)
                                recommendations.append({
                                    "query": query,
                                    "spotify_id": None,
                                    "artist_id": None,
                                })
                    except Exception as e:
                        logger.debug(f"_get_recommendations_hybrid: LastFM tracks for {artist_name} failed: {e}")
            except Exception as e:
                logger.debug(f"_get_recommendations_hybrid: LastFM genre search for {genre} failed: {e}")
    
    if recommendations:
        _random.shuffle(recommendations)
        logger.info(
            "_get_recommendations_hybrid: LastFM tier worked, %d recs from genres",
            len(recommendations),
        )
        return recommendations[:limit]
    
    # If we have seed_tracks (artist info), get similar artists via LastFM
    if seed_tracks and await _spotify_api_ready():
        for track_id in seed_tracks[:3]:
            try:
                track = await asyncio.to_thread(lambda tid=track_id: sp.track(tid))
                artists = track.get("artists") or []
                if not artists:
                    continue
                artist_name = artists[0].get("name", "")
                if not artist_name:
                    continue
                
                # Get similar artists and their top tracks
                similar = await lastfm.get_similar_artists(artist_name, limit=5)
                for similar_artist in similar:
                    if len(recommendations) >= limit * 2:
                        break
                    try:
                        tracks = await lastfm.get_top_tracks(similar_artist, limit=2)
                        for track in tracks:
                            if len(recommendations) >= limit * 2:
                                break
                            query = f"{track['artist']} {track['title']}"
                            if query not in seen_queries:
                                seen_queries.add(query)
                                recommendations.append({
                                    "query": query,
                                    "spotify_id": None,
                                    "artist_id": None,
                                })
                    except Exception as e:
                        logger.debug(f"_get_recommendations_hybrid: LastFM tracks for {similar_artist} failed: {e}")
            except Exception as e:
                logger.debug(f"_get_recommendations_hybrid: LastFM similar artists failed: {e}")
    
    if recommendations:
        _random.shuffle(recommendations)
        logger.info(
            "_get_recommendations_hybrid: LastFM tier worked, %d recs from similar",
            len(recommendations),
        )
        return recommendations[:limit]
    
    # Tier 3: Generic YouTube fallback (if all else fails)
    logger.warning(
        "_get_recommendations_hybrid: all tiers exhausted, returning empty list for fallback YouTube"
    )
    return []


async def _ensure_auth(ctx: commands.Context) -> bool:
    if sp is None:
        await ctx.send(
            "Spotify no esta configurado. Revisa `SPOTIFY_CLIENT_ID` y `SPOTIFY_CLIENT_SECRET` en `.env`."
        )
        return False

    auth_manager = sp.auth_manager
    if await _safe_validate_token(auth_manager):
        return True

    # Only the admin can kick off the OAuth flow
    if ctx.author.id != OAUTH_ADMIN_USER_ID:
        await ctx.send(
            "Spotify no esta autenticado o el token expiro. Pide al admin que ejecute `!auth`."
        )
        return False

    auth_url = auth_manager.get_authorize_url(state="admin")
    return await run_oauth_flow(
        ctx,
        expected_state="admin",
        authorize_url=auth_url,
        success_message="Spotify autenticado correctamente.",
        timeout_message="Tiempo agotado. Usa `!auth` para reintentar.",
    )
