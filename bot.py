import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import discord
from discord.ext import commands
import spotipy
from spotipy.oauth2 import SpotifyOAuth

from poc_setlistfm import load_dotenv_values, get_config_value

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


SCOPES = (
    "user-modify-playback-state "
    "user-read-playback-state "
    "user-read-currently-playing "
    "playlist-read-private"
)

# Text channel where the bot listens for commands
ALLOWED_CHANNEL_ID = 1163479541029810226

# Voice channel where Spotify playback is intended (informational — audio plays
# on the user's active Spotify device via Spotify Connect, not through Discord)
VOICE_CHANNEL_ID = 1397428777876721716

# OAuth token cache — mount this path as a Docker volume for persistence
CACHE_PATH = os.getenv("SPOTIFY_CACHE_PATH", ".cache/spotify.cache")

OAUTH_PORT = 8888

# ---------------------------------------------------------------------------
# OAuth callback server (runs in a background thread for a single request)
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
            self.wfile.write(
                b"<h1>Autorizado!</h1><p>Puedes cerrar esta p&aacute;gina.</p>"
            )
        else:
            self.wfile.write(b"<h1>Error: no se recibio el codigo de autorizacion.</h1>")

    def log_message(self, *args):
        pass


def _run_callback_server():
    server = HTTPServer(("0.0.0.0", OAUTH_PORT), _CallbackHandler)
    server.handle_request()  # serve exactly one request then stop
    server.server_close()


def build_spotify_client(dotenv_values: dict) -> spotipy.Spotify:
    client_id = get_config_value("SPOTIFY_CLIENT_ID", dotenv_values)
    client_secret = get_config_value("SPOTIFY_CLIENT_SECRET", dotenv_values)
    redirect_uri = get_config_value(
        "SPOTIFY_REDIRECT_URI", dotenv_values, "http://localhost:8888/callback"
    )

    if not client_id or not client_secret:
        raise ValueError(
            "Faltan credenciales Spotify (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET)."
        )

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


def active_device_id(sp: spotipy.Spotify) -> str | None:
    """Return the id of the currently active Spotify device, or None."""
    state = sp.current_playback()
    if state and state.get("device"):
        return state["device"]["id"]
    return None


def format_track(item: dict) -> str:
    artists = ", ".join(a["name"] for a in item["artists"])
    return f"**{item['name']}** — {artists}"


dotenv_values = load_dotenv_values()
bot_token = get_config_value("BOT_TOKEN", dotenv_values)

if not bot_token:
    raise ValueError(
        "Falta BOT_TOKEN en variables de entorno o en .env."
    )

sp = build_spotify_client(dotenv_values)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

async def _ensure_auth(ctx: commands.Context) -> bool:
    """Return True if a valid Spotify token is available.

    If not, post the auth URL to the channel, spin up the callback server in a
    background thread, wait up to 5 minutes for the user to authorise, then
    exchange the code for a token.  Returns False both when auth is newly
    completed (so the user re-runs the command) and when it times out.
    """
    global _oauth_code, _oauth_received

    auth_manager = sp.auth_manager
    cached = await asyncio.to_thread(auth_manager.cache_handler.get_cached_token)
    valid = await asyncio.to_thread(auth_manager.validate_token, cached)
    if valid:
        return True

    # Reset for a fresh attempt
    _oauth_code = None
    _oauth_received.clear()

    auth_url = auth_manager.get_authorize_url()
    await ctx.send(
        f"Autenticacion de Spotify requerida. Abre este enlace y autoriza:\n{auth_url}"
    )

    threading.Thread(target=_run_callback_server, daemon=True).start()

    received = await asyncio.to_thread(_oauth_received.wait, 300)
    if not received or not _oauth_code:
        await ctx.send("Tiempo agotado esperando autorizacion. Usa `!auth` para reintentar.")
        return False

    code = _oauth_code
    await asyncio.to_thread(
        lambda: auth_manager.get_access_token(code, as_dict=False, check_cache=False)
    )
    await ctx.send("Autenticado correctamente. Vuelve a ejecutar tu comando.")
    return False  # user must re-run their command


# ---------------------------------------------------------------------------
# Global channel check
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
    logger.info(f"Intentando enviar mensaje al canal id={ALLOWED_CHANNEL_ID}")
    try:
        channel = await bot.fetch_channel(ALLOWED_CHANNEL_ID)
        await channel.send("🎵 Bot de Spotify conectado. Usa `!auth` para autenticarte con Spotify.")
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
    print(f"[ERROR] {ctx.command}: {error}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@bot.command(name="ping", help="Comprueba que el bot esta vivo.")
async def ping(ctx: commands.Context):
    await ctx.send(f"Pong! Latencia: {round(bot.latency * 1000)}ms")


@bot.command(name="auth", help="Inicia o renueva la autenticacion de Spotify.")
async def auth_cmd(ctx: commands.Context):
    if not await _ensure_auth(ctx):
        return
    await ctx.send("Spotify ya esta autenticado.")


@bot.command(name="play", help="Reproduce una cancion. Uso: !play <busqueda>")
async def play(ctx: commands.Context, *, query: str):
    if not await _ensure_auth(ctx):
        return
    try:
        results = await asyncio.to_thread(lambda: sp.search(q=query, type="track", limit=1))
        items = results.get("tracks", {}).get("items", [])
    except Exception as e:
        await ctx.send(f"Error buscando en Spotify: `{e}`")
        return

    if not items:
        await ctx.send(f"No se encontro ninguna cancion para: **{query}**")
        return

    track = items[0]
    device_id = await asyncio.to_thread(active_device_id, sp)

    try:
        await asyncio.to_thread(
            lambda: sp.start_playback(device_id=device_id, uris=[track["uri"]])
        )
        await ctx.send(f"Reproduciendo: {format_track(track)}")
    except spotipy.SpotifyException as e:
        if e.http_status == 403:
            await ctx.send(
                "Spotify Premium requerido, o no hay dispositivo activo. "
                "Abre Spotify en tu dispositivo y vuelve a intentarlo."
            )
        elif e.http_status == 404:
            try:
                devs = (await asyncio.to_thread(sp.devices)).get("devices", [])
            except Exception:
                devs = []
            if not devs:
                await ctx.send(
                    "No se encontro un dispositivo activo. "
                    "Abre Spotify en tu computadora o celular primero."
                )
            else:
                names = ", ".join(d["name"] for d in devs)
                await ctx.send(
                    f"Sin dispositivo activo. Dispositivos disponibles: {names}\n"
                    "Reproduce algo manualmente en uno de ellos y vuelve a intentarlo."
                )
        else:
            await ctx.send(f"Error de Spotify: {e}")
    except Exception as e:
        await ctx.send(f"Error inesperado: `{e}`")


@bot.command(name="pause", help="Pausa la reproduccion actual.")
async def pause(ctx: commands.Context):
    if not await _ensure_auth(ctx):
        return
    try:
        await asyncio.to_thread(sp.pause_playback)
        await ctx.send("Pausa.")
    except Exception as e:
        await ctx.send(f"Error: {e}")


@bot.command(name="resume", help="Reanuda la reproduccion.")
async def resume(ctx: commands.Context):
    if not await _ensure_auth(ctx):
        return
    try:
        await asyncio.to_thread(sp.start_playback)
        await ctx.send("Reproduccion reanudada.")
    except Exception as e:
        await ctx.send(f"Error: {e}")


@bot.command(name="skip", help="Salta a la siguiente cancion.")
async def skip(ctx: commands.Context):
    if not await _ensure_auth(ctx):
        return
    try:
        await asyncio.to_thread(sp.next_track)
        await ctx.send("Siguiente cancion.")
    except Exception as e:
        await ctx.send(f"Error: {e}")


@bot.command(name="np", help="Muestra la cancion que se esta reproduciendo ahora.")
async def now_playing(ctx: commands.Context):
    if not await _ensure_auth(ctx):
        return
    try:
        current = await asyncio.to_thread(sp.currently_playing)
    except Exception as e:
        await ctx.send(f"Error consultando Spotify: `{e}`")
        return
    if not current or not current.get("item"):
        await ctx.send("No hay nada reproduciendose en este momento.")
        return

    item = current["item"]
    progress_ms = current.get("progress_ms", 0)
    duration_ms = item.get("duration_ms", 0)

    def fmt_ms(ms: int) -> str:
        s = ms // 1000
        return f"{s // 60}:{s % 60:02d}"

    album = item.get("album", {})
    images = album.get("images", [])
    thumbnail = images[0]["url"] if images else None

    embed = discord.Embed(
        title="Reproduciendo ahora",
        description=format_track(item),
        color=discord.Color.green(),
    )
    embed.add_field(
        name="Progreso",
        value=f"{fmt_ms(progress_ms)} / {fmt_ms(duration_ms)}",
        inline=True,
    )
    embed.add_field(name="Album", value=album.get("name", "—"), inline=True)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    await ctx.send(embed=embed)


@bot.command(name="search", help="Busca una cancion. Uso: !search <busqueda>")
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

    embed = discord.Embed(
        title=f'Resultados para "{query}"',
        color=discord.Color.blurple(),
    )
    for i, track in enumerate(items, 1):
        artists = ", ".join(a["name"] for a in track["artists"])
        album = track.get("album", {}).get("name", "")
        embed.add_field(
            name=f"{i}. {track['name']}",
            value=f"{artists} — {album}",
            inline=False,
        )

    await ctx.send(embed=embed)


@bot.command(name="devices", help="Lista los dispositivos Spotify disponibles.")
async def devices(ctx: commands.Context):
    if not await _ensure_auth(ctx):
        return
    try:
        device_list = (await asyncio.to_thread(sp.devices)).get("devices", [])
    except Exception as e:
        await ctx.send(f"Error consultando dispositivos: `{e}`")
        return
    if not device_list:
        await ctx.send("No se encontraron dispositivos Spotify activos.")
        return

    lines = []
    for d in device_list:
        active_mark = "✅" if d["is_active"] else "  "
        lines.append(f"{active_mark} **{d['name']}** ({d['type']})")

    embed = discord.Embed(
        title="Dispositivos Spotify",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed)


if __name__ == "__main__":
    bot.run(bot_token)
