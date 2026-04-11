import asyncio
import collections
import logging
import re

import discord
from discord.ext import commands

from src.config import ALLOWED_CHANNEL_ID, ADMIN_USER_ID, LLM_ENABLED_FOR_ALBUM_TRACKS, sp
from src.bot_instance import bot
from src.playback import queues, now_playing_info, play_next, update_player_embed, _paused
from src.spotify import (
    _is_spotify_url,
    _get_tracks_from_spotify_url,
    _get_spotify_query,
    _ensure_auth,
)
from src.youtube import search_youtube

logger = logging.getLogger(__name__)

@bot.check
async def only_allowed_channel(ctx: commands.Context) -> bool:
    return ctx.channel.id == ALLOWED_CHANNEL_ID

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
    msg = await ctx.send(f"\U0001f50d Buscando **{query}**...", delete_after=30)

    # Check if query is a Spotify URL (album/playlist/track)
    if _is_spotify_url(query):
        yt_queries = await _get_tracks_from_spotify_url(query)
        if not yt_queries:
            await msg.edit(content="No se pudo procesar la URL de Spotify.")
            await asyncio.sleep(5)
            try:
                await msg.delete()
            except Exception:
                pass
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

    try:
        await msg.delete()
    except Exception:
        pass

    # Add all tracks to queue
    for track in tracks_to_queue:
        queues[ctx.guild.id].append(track)

    # Start playing if not already playing
    if vc.is_playing() or vc.is_paused():
        added_count = len(tracks_to_queue)
        label = tracks_to_queue[0]['title'] if added_count == 1 else f"{added_count} canciones"
        await ctx.send(f"\u2795 {label} anadida(s) a la cola.", delete_after=8)
    else:
        await play_next(ctx.guild, vc, ctx.channel)


@bot.command(name="skip", help="Salta la cancion actual.")
async def skip(ctx: commands.Context):
    vc = ctx.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        await ctx.send("No hay nada reproduciendose.", delete_after=5)
        return
    vc.stop()
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="pause", help="Pausa la reproduccion.")
async def pause(ctx: commands.Context):
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        _paused[ctx.guild.id] = True
        await update_player_embed(ctx.guild, ctx.channel)
    else:
        await ctx.send("No hay nada reproduciendose.", delete_after=5)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="resume", help="Reanuda la reproduccion.")
async def resume(ctx: commands.Context):
    vc = ctx.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        _paused[ctx.guild.id] = False
        await update_player_embed(ctx.guild, ctx.channel)
    else:
        await ctx.send("No hay nada en pausa.", delete_after=5)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="stop", help="Detiene la reproduccion y limpia la cola.")
async def stop(ctx: commands.Context):
    queues[ctx.guild.id] = collections.deque()
    now_playing_info[ctx.guild.id] = None
    _paused[ctx.guild.id] = False
    vc = ctx.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
    await update_player_embed(ctx.guild, ctx.channel)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="leave", help="Desconecta el bot del canal de voz.")
async def leave(ctx: commands.Context):
    queues[ctx.guild.id] = collections.deque()
    now_playing_info[ctx.guild.id] = None
    _paused[ctx.guild.id] = False
    vc = ctx.guild.voice_client
    if vc:
        await vc.disconnect()
    try:
        await ctx.message.delete()
    except Exception:
        pass


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
    await ctx.send("\n".join(lines), delete_after=20)


@bot.command(name="np", help="Muestra la cancion que se esta reproduciendo ahora.")
async def now_playing_cmd(ctx: commands.Context):
    current = now_playing_info.get(ctx.guild.id)
    if not current:
        await ctx.send("No hay nada reproduciendose.")
        return
    await ctx.send(f"\U0001f3b5 **{current['title']}** — pedido por {current['requester']}", delete_after=15)


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
        # Fetch all tracks (Spotify paginates at 50 for playlist_items)
        def _fetch_all_tracks(pid):
            tracks = []
            offset = 0
            limit = 50
            while True:
                result = sp.playlist_items(pid, offset=offset, limit=limit, market="from_token")
                items = result.get("items", [])
                if not items:
                    break
                for item in items:
                    t = item.get("track")
                    if t and t.get("name"):
                        artists = ", ".join(a["name"] for a in t.get("artists", []))
                        tracks.append(f"{artists} - {t['name']}")
                offset += limit
            return tracks

        track_queries = await asyncio.to_thread(_fetch_all_tracks, playlist_id)
    except Exception as e:
        err = str(e)
        if "404" in err:
            await msg.edit(
                content=(
                    "\u274c No se pudo acceder a esta playlist.\n"
                    "Las playlists editoriales de Spotify (Daily Mix, Top 50, etc.) "
                    "no son accesibles por bots de terceros \u2014 solo funcionan playlists propias o compartidas.\n"
                    f"-# `{err}`"
                )
            )
        else:
            await msg.edit(content=f"\u274c Error cargando la playlist: `{err}`")
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
