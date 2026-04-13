import asyncio
import collections
import logging
import re

import discord
from discord.ext import commands
from discord.ui import Button, View

from src.config import ALLOWED_CHANNEL_ID, ADMIN_USER_ID, LLM_ENABLED_FOR_ALBUM_TRACKS, sp
from src.bot_instance import bot
import random

from src.playback import queues, now_playing_info, play_next, update_player_embed, _paused
from src.spotify import (
    _is_spotify_url,
    _get_tracks_from_spotify_url,
    _get_spotify_query,
    _get_spotify_track_info,
    _ensure_auth,
)
from src.youtube import search_youtube, get_search_candidates
from src.scoring import _split_query_parts

logger = logging.getLogger(__name__)

def error_embed(title: str, description: str = "", details: str = "") -> discord.Embed:
    """Create a formatted error embed."""
    embed = discord.Embed(title=f"❌ {title}", description=description, color=0xFF5555)
    if details:
        embed.add_field(name="Detalles", value=f"`{details[:1024]}`", inline=False)
    return embed


class SearchSelectionView(View):
    """Interactive view for selecting one of multiple search results."""
    def __init__(self, candidates: list[dict], query: str, ctx: commands.Context):
        super().__init__(timeout=30)
        self.candidates = candidates
        self.query = query
        self.ctx = ctx
        self.selected = None
        
        # Create buttons numbered 1-5
        for idx in range(min(5, len(candidates))):
            button = Button(
                label=str(idx + 1),
                style=discord.ButtonStyle.secondary,
                custom_id=f"search_select_{idx}"
            )
            button.callback = self._make_callback(idx)
            self.add_item(button)
    
    def _make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.send_message(
                    "Solo quien hizo la búsqueda puede seleccionar.",
                    ephemeral=True
                )
                return
            
            self.selected = self.candidates[idx]
            self.stop()
            await interaction.response.defer()
        
        return callback
    
    async def on_timeout(self):
        """Called when the view times out."""
        self.stop()


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


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """Stop radio and disconnect if bot is alone in the voice channel."""
    from src import radio as _radio
    
    # Only care if someone left a voice channel
    if before.channel is None or after.channel == before.channel:
        return
    
    # Check if the bot is in this channel
    vc = before.guild.voice_client
    if vc is None or vc.channel != before.channel:
        return
    
    # Count non-bot members in the channel
    human_members = [
        m for m in before.channel.members
        if not m.bot and m.id != member.id  # exclude the member who just left and bots
    ]
    
    # If no humans left, stop the radio and disconnect
    if not human_members:
        logger.info(
            "on_voice_state_update: bot is alone in channel %s (guild %s), stopping radio and disconnecting",
            before.channel.id,
            before.guild.id,
        )
        _radio.set_radio_active(before.guild.id, False)
        queues[before.guild.id] = collections.deque()
        now_playing_info[before.guild.id] = None
        _paused[before.guild.id] = False
        vc.stop()
        await vc.disconnect()


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
        track_infos = await _get_tracks_from_spotify_url(query)
        if not track_infos:
            await msg.edit(content="No se pudo procesar la URL de Spotify.")
            await asyncio.sleep(5)
            try:
                await msg.delete()
            except Exception:
                pass
            return
        # track_infos is now list[dict] with {query, spotify_id, artist_id}
        yt_queries = track_infos
    else:
        # Regular text search; refine with Spotify
        info = await _get_spotify_track_info(query)
        yt_queries = [info]

    vc = ctx.guild.voice_client
    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    if ctx.guild.id not in queues:
        queues[ctx.guild.id] = collections.deque()

    # Search YouTube for all queries in parallel and build track objects
    tracks_to_queue = []
    if len(yt_queries) == 1:
        # Single track: no gather overhead needed
        info = yt_queries[0]  # {query, spotify_id, artist_id}
        yt_info = await search_youtube(info["query"], enable_llm=True)
        if yt_info:
            artist, title = _split_query_parts(info["query"])
            tracks_to_queue.append({
                "title":      yt_info["title"],
                "yt_query":   info["query"],
                "url":        yt_info["url"],
                "requester":  ctx.author.display_name,
                "artist":     artist or "Unknown",
                "duration":   yt_info.get("duration") or 0,
                "thumbnail":  yt_info.get("thumbnail") or "",
                "spotify_id": info.get("spotify_id"),
                "artist_id":  info.get("artist_id"),
                "acodec":     yt_info.get("acodec", "?"),
                "abr":        yt_info.get("abr", 0),
            })
    else:
        # Album / playlist: search all tracks concurrently
        async def _fetch(idx: int, info: dict) -> dict | None:
            enable_llm = idx < LLM_ENABLED_FOR_ALBUM_TRACKS
            yt_info = await search_youtube(info["query"], enable_llm=enable_llm)
            if not yt_info:
                return None
            artist, title = _split_query_parts(info["query"])
            return {
                "title":      yt_info["title"],
                "yt_query":   info["query"],
                "url":        yt_info["url"],
                "requester":  ctx.author.display_name,
                "artist":     artist or "Unknown",
                "duration":   yt_info.get("duration") or 0,
                "thumbnail":  yt_info.get("thumbnail") or "",
                "spotify_id": info.get("spotify_id"),
                "artist_id":  info.get("artist_id"),
                "acodec":     yt_info.get("acodec", "?"),
                "abr":        yt_info.get("abr", 0),
                "_order":     idx,
            }

        results = await asyncio.gather(*(_fetch(i, q) for i, q in enumerate(yt_queries)))
        # Preserve original playlist order; filter out failed searches
        tracks_to_queue = [t for t in results if t is not None]
        for t in tracks_to_queue:
            t.pop("_order", None)

    if not tracks_to_queue:
        await msg.edit(content=f"No se encontro nada para: **{query}**")
        return

    try:
        await msg.delete()
    except Exception:
        pass

    # Add all tracks to queue
    # Radio mode: user requests go to the front (right after the current song)
    from src import radio as _radio
    radio_on = _radio.is_radio_active(ctx.guild.id)
    if radio_on and (vc.is_playing() or vc.is_paused()):
        # appendleft in reverse order so first requested track ends up at position 0
        for track in reversed(tracks_to_queue):
            queues[ctx.guild.id].appendleft(track)
    else:
        for track in tracks_to_queue:
            queues[ctx.guild.id].append(track)

    # Start playing if not already playing
    if vc.is_playing() or vc.is_paused():
        added_count = len(tracks_to_queue)
        label = tracks_to_queue[0]['title'] if added_count == 1 else f"{added_count} canciones"
        await ctx.send(f"\u2795 {label} anadida(s) a la cola.", delete_after=8)
    else:
        await play_next(ctx.guild, vc, ctx.channel)


@bot.command(name="search", help="Busca canciones y te permite elegir una. Uso: !search <busqueda>")
async def search(ctx: commands.Context, *, query: str):
    if not ctx.author.voice:
        await ctx.send("Debes estar en un canal de voz para usar este comando.", delete_after=5)
        return

    voice_channel = ctx.author.voice.channel
    msg = await ctx.send(f"\U0001f50d Buscando **{query}**...", delete_after=60)

    # Get top 5 candidates
    candidates = await get_search_candidates(query)
    if not candidates:
        await msg.edit(content=f"No se encontro nada para: **{query}**")
        return

    # Connect to voice if needed (do this early)
    vc = ctx.guild.voice_client
    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    if ctx.guild.id not in queues:
        queues[ctx.guild.id] = collections.deque()

    # Keep track of tried candidates
    available_candidates = list(candidates)
    
    while available_candidates:
        # Prepare embed with remaining candidates
        embed = discord.Embed(
            title="🎵 Elige una canción",
            description=f"Buscaste: **{query}**\n\nSelecciona una opción (válido por 30 segundos)",
            color=0x1DB954  # Spotify green
        )

        # Add fields for each candidate
        for idx, candidate in enumerate(available_candidates):
            title = candidate.get("title", "Unknown")
            uploader = candidate.get("uploader", "Unknown")
            duration = candidate.get("duration") or 0
            dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else "?"
            
            field_value = f"{uploader}\n`[{dur_str}]`"
            embed.add_field(
                name=f"{idx + 1}️⃣ {title}",
                value=field_value,
                inline=False
            )

        # Create view with buttons
        view = SearchSelectionView(available_candidates, query, ctx)
        
        try:
            await msg.delete()
        except Exception:
            pass

        selection_msg = await ctx.send(embed=embed, view=view)

        # Wait for user selection
        await view.wait()

        if view.selected is None:
            await selection_msg.edit(content="⏱️ Tiempo agotado. Búsqueda cancelada.", embed=None, view=None)
            return

        selected = view.selected
        
        # Create track object
        artist, title = _split_query_parts(query)
        track = {
            "title":     selected["title"],
            "yt_query":  query,
            "url":       selected["url"],
            "requester":  ctx.author.display_name,
            "artist":     artist or selected.get("uploader", "Unknown"),
            "duration":   selected.get("duration") or 0,
            "thumbnail":  selected.get("thumbnail") or "",
        }

        # Add to queue
        queues[ctx.guild.id].append(track)

        # Try to play
        if not (vc.is_playing() or vc.is_paused()):
            try:
                await play_next(ctx.guild, vc, ctx.channel)
                # Success! Update message and exit
                embed_confirm = discord.Embed(
                    title="✅ Reproduciendo",
                    description=f"**{selected['title']}**\n{selected.get('uploader', '')}",
                    color=0x1DB954
                )
                await selection_msg.edit(embed=embed_confirm, view=None)
                return
            except Exception as e:
                logger.error(f"Error al reproducir canción: {e}")
                # Remove this candidate from available and retry
                available_candidates.remove(selected)
                queues[ctx.guild.id].pop()  # Remove from queue
                
                if available_candidates:
                    # Show message and retry
                    embed_error = discord.Embed(
                        title="⚠️ No disponible",
                        description=f"**{selected['title']}** no pudo reproducirse.\n\nIntenta con otra opción.",
                        color=0xFF9500
                    )
                    await selection_msg.edit(embed=embed_error, view=None)
                    await asyncio.sleep(2)
                    msg = selection_msg
                    continue
                else:
                    # No more candidates
                    embed_error = discord.Embed(
                        title="❌ Sin opciones disponibles",
                        description="Todas las canciones fallaron. Intenta una búsqueda diferente.",
                        color=0xFF5555
                    )
                    await selection_msg.edit(embed=embed_error, view=None)
                    return
        else:
            # Already playing, just add to queue
            embed_confirm = discord.Embed(
                title="✅ Canción agregada a la cola",
                description=f"**{selected['title']}**\n{selected.get('uploader', '')}",
                color=0x1DB954
            )
            await selection_msg.edit(embed=embed_confirm, view=None)
            return



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
        # Update voice channel status to show paused state
        from src.playback import _update_status
        await _update_status(ctx.guild, "⏸ Paused")
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
        # Update voice channel status with now playing track
        from src.playback import _update_status
        now_playing = now_playing_info.get(ctx.guild.id)
        if now_playing:
            await _update_status(ctx.guild, now_playing.get("title"))
        await update_player_embed(ctx.guild, ctx.channel)
    else:
        await ctx.send("No hay nada en pausa.", delete_after=5)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="stop", help="Detiene la reproduccion y limpia la cola.")
async def stop(ctx: commands.Context):
    from src import radio as _radio
    _radio.set_radio_active(ctx.guild.id, False)
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
    from src import radio as _radio
    _radio.set_radio_active(ctx.guild.id, False)
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


# @bot.command(name="search", help="Busca canciones en Spotify. Uso: !search <busqueda>")
# async def search(ctx: commands.Context, *, query: str):
#     if not await _ensure_auth(ctx):
#         return
#     try:
#         results = await asyncio.to_thread(lambda: sp.search(q=query, type="track", limit=5))
#     except Exception as e:
#         await ctx.send(f"Error buscando en Spotify: `{e}`")
#         return
#     items = results.get("tracks", {}).get("items", [])
#     if not items:
#         await ctx.send(f"No se encontraron resultados para: **{query}**")
#         return
#     embed = discord.Embed(title=f'Resultados para "{query}"', color=discord.Color.blurple())
#     for i, track in enumerate(items, 1):
#         artists = ", ".join(a["name"] for a in track["artists"])
#         album   = track.get("album", {}).get("name", "")
#         embed.add_field(name=f"{i}. {track['name']}", value=f"{artists} — {album}", inline=False)
#     await ctx.send(embed=embed)


@bot.command(name="move", help="Mueve una cancion en la cola. Uso: !move <pos_actual> <pos_nueva>")
async def move_cmd(ctx: commands.Context, current_pos: int, new_pos: int):
    q = queues.get(ctx.guild.id)
    if not q:
        await ctx.send("La cola esta vacia.", delete_after=5)
        return
    if current_pos < 1 or current_pos > len(q) or new_pos < 1 or new_pos > len(q):
        await ctx.send(f"Posiciones invalidas. La cola tiene {len(q)} canciones.", delete_after=5)
        return
    items = list(q)
    track = items.pop(current_pos - 1)
    items.insert(new_pos - 1, track)
    queues[ctx.guild.id] = collections.deque(items)
    await ctx.send(f"**{track.get('title', '?')}** movida a posicion {new_pos}.", delete_after=8)
    await update_player_embed(ctx.guild, ctx.channel)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="remove", help="Elimina una cancion de la cola. Uso: !remove <posicion>")
async def remove_cmd(ctx: commands.Context, pos: int):
    q = queues.get(ctx.guild.id)
    if not q:
        await ctx.send("La cola esta vacia.", delete_after=5)
        return
    if pos < 1 or pos > len(q):
        await ctx.send(f"Posicion invalida. La cola tiene {len(q)} canciones.", delete_after=5)
        return
    items = list(q)
    track = items.pop(pos - 1)
    queues[ctx.guild.id] = collections.deque(items)
    await ctx.send(f"**{track.get('title', '?')}** eliminada de la cola.", delete_after=8)
    await update_player_embed(ctx.guild, ctx.channel)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="radio", help="Activa/desactiva el modo radio 24/7. Uso: !radio [on|off|<mood>]")
async def radio_cmd(ctx: commands.Context, action: str = ""):
    from src import radio as _radio
    gid = ctx.guild.id
    action = action.strip().lower()

    # Check if action is a known mood name → activate radio with that mood
    all_moods = {**_radio.MOODS, **_radio._custom_moods.get(gid, {})}
    if action and action not in ("on", "off") and action in all_moods:
        _radio.set_radio_active(gid, True)
        try:
            _radio.set_mood(gid, action)
        except ValueError:
            pass
        active = True
    elif action in ("on", "off"):
        active = action == "on"
        _radio.set_radio_active(gid, active)
    else:
        active = not _radio.is_radio_active(gid)  # toggle if no argument
        _radio.set_radio_active(gid, active)

    if active:
        embed = discord.Embed(
            title="📻 Radio activado",
            description=(
                f"Mood actual: **{_radio.get_mood(gid).capitalize()}**\n"
                "La cola se rellena automaticamente con recomendaciones.\n"
                "Usa `!mood <nombre>` para cambiar el estilo."
            ),
            color=0x1DB954,
        )
        await ctx.send(embed=embed, delete_after=15)
        vc = ctx.guild.voice_client
        if vc is None and ctx.author.voice:
            vc = await ctx.author.voice.channel.connect()
            if gid not in queues:
                queues[gid] = collections.deque()
        if vc:
            asyncio.ensure_future(_radio.fill_radio_queue(ctx.guild, vc, ctx.channel))
    else:
        embed = discord.Embed(
            title="📻 Radio desactivado",
            description="El bot reproducira la cola actual y se desconectara al terminar.",
            color=0x2B2D31,
        )
        await ctx.send(embed=embed, delete_after=10)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="mood", help="Cambia el mood del radio. Uso: !mood <nombre> | !mood create <nombre> <query> | !mood delete <nombre>")
async def mood_cmd(ctx: commands.Context, *, args: str = ""):
    from src import radio as _radio
    gid = ctx.guild.id
    parts = args.strip().split(None, 2)  # up to 3 parts: [subcommand, name, query]
    subcommand = parts[0].lower() if parts else ""

    if subcommand == "create":
        if len(parts) < 3:
            await ctx.send("Uso: `!mood create <nombre> <cancion o artista>`", delete_after=10)
            return
        mood_name = parts[1].lower()
        query = parts[2]
        if mood_name in _radio.MOODS:
            await ctx.send(f"`{mood_name}` es un mood built-in y no puede ser sobreescrito.", delete_after=10)
            return
        msg = await ctx.send(f"\U0001f50d Buscando géneros para **{query}**...")
        from src.spotify import _get_artist_genres

        # --- Try full query first, then individual comma/space-split terms ---
        async def _collect_genres(q: str) -> list[str]:
            info = await _get_spotify_track_info(q)
            aid = info.get("artist_id")
            if not aid:
                return []
            return await _get_artist_genres(aid)

        genres: list[str] = await _collect_genres(query)

        if not genres:
            # Split by comma first; otherwise slide a 2-word window left→right
            raw_terms: list[str] = (
                [t.strip() for t in query.split(",") if t.strip()]
                if "," in query
                else [
                    " ".join(query.split()[i : i + 2])
                    for i in range(0, len(query.split()), 2)
                    if query.split()[i : i + 2]
                ]
            )
            seen: set[str] = set()
            for term in raw_terms:
                for g in await _collect_genres(term):
                    if g not in seen:
                        seen.add(g)
                        genres.append(g)

        using_raw_tokens = False
        if not genres:
            # Last resort: store the raw query tokens as YouTube-friendly genre hints
            if "," in query:
                genres = [t.strip() for t in query.split(",") if t.strip()]
            else:
                words = query.split()
                genres = [
                    " ".join(words[i : i + 2])
                    for i in range(0, len(words), 2)
                    if words[i : i + 2]
                ]
            using_raw_tokens = True

        _radio.create_custom_mood(gid, mood_name, genres)
        genre_display = ", ".join(f"`{g}`" for g in genres[:8])
        if using_raw_tokens:
            description = (
                f"Sin géneros Spotify detectados. El radio buscará en YouTube: {genre_display}"
            )
        else:
            description = f"Géneros: {genre_display}"
        embed = discord.Embed(
            title=f"🎭 Mood custom creado: {mood_name}",
            description=description,
            color=0x1DB954,
        )
        await msg.edit(content=None, embed=embed)

    elif subcommand == "delete":
        if len(parts) < 2:
            await ctx.send("Uso: `!mood delete <nombre>`", delete_after=10)
            return
        mood_name = parts[1].lower()
        try:
            _radio.delete_custom_mood(gid, mood_name)
            await ctx.send(f"🗑️ Mood `{mood_name}` eliminado.", delete_after=8)
        except ValueError as e:
            await ctx.send(str(e), delete_after=10)

    elif not subcommand:
        current = _radio.get_mood(gid)
        lines = []
        for m in _radio.MOODS:
            marker = " ← actual" if m == current else ""
            lines.append(f"`{m}`{marker}")
        custom = _radio._custom_moods.get(gid, {})
        if custom:
            lines.append("")
            for m in custom:
                marker = " ← actual" if m == current else ""
                lines.append(f"`{m}` [custom]{marker}")
        embed = discord.Embed(
            title="🎭 Moods disponibles",
            description="\n".join(lines),
            color=0x1DB954,
        )
        await ctx.send(embed=embed, delete_after=20)

    else:
        name = subcommand
        all_moods = {**_radio.MOODS, **_radio._custom_moods.get(gid, {})}
        if name not in all_moods:
            available = ", ".join(f"`{m}`" for m in all_moods)
            await ctx.send(f"Mood desconocido. Disponibles: {available}", delete_after=10)
            return
        try:
            _radio.set_mood(gid, name)
        except ValueError as e:
            await ctx.send(str(e), delete_after=10)
            return
        embed = discord.Embed(
            title=f"🎭 Mood cambiado a {name.capitalize()}",
            description="El siguiente batch de recomendaciones usara este estilo.",
            color=0x1DB954,
        )
        await ctx.send(embed=embed, delete_after=10)

    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="priority", help="Mueve una cancion al tope de la cola. Uso: !priority <posicion>")
async def priority_cmd(ctx: commands.Context, pos: int = 2):
    q = queues.get(ctx.guild.id)
    if not q:
        await ctx.send("La cola esta vacia.", delete_after=5)
        return
    if pos < 1 or pos > len(q):
        await ctx.send(f"Posicion invalida. La cola tiene {len(q)} canciones.", delete_after=5)
        return
    items = list(q)
    track = items.pop(pos - 1)
    items.insert(0, track)
    queues[ctx.guild.id] = collections.deque(items)
    await ctx.send(f"**{track.get('title', '?')}** movida al tope de la cola.", delete_after=8)
    await update_player_embed(ctx.guild, ctx.channel)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="shuffle", help="Mezcla la cola de reproduccion aleatoriamente.")
async def shuffle_cmd(ctx: commands.Context):
    q = queues.get(ctx.guild.id)
    if not q or len(q) < 2:
        await ctx.send("No hay suficientes canciones en la cola para mezclar.", delete_after=5)
        return
    items = list(q)
    random.shuffle(items)
    queues[ctx.guild.id] = collections.deque(items)
    await ctx.send(f"Cola mezclada ({len(items)} canciones).", delete_after=8)
    await update_player_embed(ctx.guild, ctx.channel)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="help", help="Muestra todos los comandos disponibles.")
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="Comandos del Bot de Musica",
        description="Estos son todos los comandos disponibles:",
        color=0x1DB954
    )
    commands_info = [
        ("!play <cancion>", "Reproduce una cancion. Acepta URLs de Spotify."),
        ("!playlist <url>", "Carga una playlist de Spotify en la cola."),
        ("!skip", "Salta la cancion actual."),
        ("!pause", "Pausa la reproduccion."),
        ("!resume", "Reanuda la reproduccion."),
        ("!stop", "Detiene la reproduccion y limpia la cola."),
        ("!leave", "Desconecta el bot del canal de voz."),
        ("!queue", "Muestra la cola de reproduccion."),
        ("!np", "Muestra la cancion actual."),
        ("!shuffle", "Mezcla la cola aleatoriamente."),
        ("!move <de> <a>", "Mueve una cancion a otra posicion en la cola."),
        ("!remove <pos>", "Elimina una cancion de la cola."),
        ("!priority <pos>", "Mueve una cancion al tope de la cola."),
        ("!search <cancion>", "Busca canciones en Spotify."),
        ("!auth", "Autenticacion de Spotify (solo admin)."),
        ("!ping", "Verifica que el bot este vivo."),
    ]
    for cmd, desc in commands_info:
        embed.add_field(name=cmd, value=desc, inline=False)
    embed.set_footer(text="Tambien puedes usar los botones del embed")
    await ctx.send(embed=embed, delete_after=60)


@bot.command(name="playlist", help="Carga una playlist de Spotify en la cola. Uso: !playlist <url>")
async def playlist_cmd(ctx: commands.Context, *, url: str):
    if not ctx.author.voice:
        await ctx.send("Debes estar en un canal de voz para usar este comando.")
        return

    if not await _ensure_auth(ctx):
        return

    msg = await ctx.send("📋 Cargando playlist de Spotify...")

    try:
        track_infos = await _get_tracks_from_spotify_url(url)
    except Exception as e:
        embed = error_embed("Error cargando la playlist", details=str(e)[:200])
        await msg.edit(embed=embed)
        return

    if not track_infos:
        embed = error_embed("Error cargando la playlist", details="URL de Spotify no válida o sin canciones.")
        await msg.edit(embed=embed)
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
    for info in track_infos:
        q_str = info["query"]
        queues[ctx.guild.id].append({
            "title":      q_str,
            "yt_query":   q_str,
            "url":        None,
            "requester":  ctx.author.display_name,
            "spotify_id": info.get("spotify_id"),
            "artist_id":  info.get("artist_id"),
        })

    await msg.edit(content=f"✅ {len(track_infos)} canciones añadidas a la cola.")

    # Start playing if nothing is currently playing
    if not (vc.is_playing() or vc.is_paused()):
        await play_next(ctx.guild, vc, ctx.channel)
