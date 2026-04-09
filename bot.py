import asyncio
import collections
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

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
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "source_address": "0.0.0.0",
}

# -reconnect*        keeps the stream alive on transient network errors.
# -probesize         bytes FFmpeg reads to detect the stream format (~200 MB fills ~10 s buffer).
# -analyzeduration   microseconds FFmpeg spends analyzing before playback starts (10 s = 10_000_000).
# -thread_queue_size packet queue between demuxer and decoder threads — reduces starvation.
# -bufsize 128k      larger decode output buffer to absorb network jitter.
FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
        "-probesize 200M -analyzeduration 10000000 "
        "-thread_queue_size 4096"
    ),
    "options": "-vn -bufsize 128k",
}


async def search_youtube(query: str) -> dict | None:
    """Search YouTube and return {title, url} or None."""
    def _search():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if not info or not info.get("entries"):
                return None
            entry = info["entries"][0]
            return {"title": entry.get("title", query), "url": entry["url"]}
    return await asyncio.to_thread(_search)


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

async def _get_spotify_query(query: str) -> str:
    """Try to refine the search query using Spotify metadata; fall back to raw."""
    try:
        auth_manager = sp.auth_manager
        cached = await asyncio.to_thread(auth_manager.cache_handler.get_cached_token)
        valid  = await asyncio.to_thread(auth_manager.validate_token, cached)
        if not valid:
            return query
        results = await asyncio.to_thread(lambda: sp.search(q=query, type="track", limit=1))
        items = results.get("tracks", {}).get("items", [])
        if items:
            t = items[0]
            artists = ", ".join(a["name"] for a in t["artists"])
            return f"{artists} - {t['name']}"
    except Exception:
        pass
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
        source = await discord.FFmpegOpusAudio.from_probe(track["url"], **FFMPEG_OPTIONS)
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

    yt_query = await _get_spotify_query(query)
    yt_info = await search_youtube(yt_query)
    if not yt_info:
        await msg.edit(content=f"No se encontro nada para: **{yt_query}**")
        return

    track = {
        "title":    yt_info["title"],
        "yt_query": yt_query,
        "url":      yt_info["url"],
        "requester": ctx.author.display_name,
    }

    vc = ctx.guild.voice_client
    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    if ctx.guild.id not in queues:
        queues[ctx.guild.id] = collections.deque()

    if vc.is_playing() or vc.is_paused():
        queues[ctx.guild.id].append(track)
        await msg.edit(content=f"\u2795 Anadido a la cola: **{track['title']}**")
    else:
        queues[ctx.guild.id].append(track)
        await msg.edit(content=f"\u2705 Listo!")
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
