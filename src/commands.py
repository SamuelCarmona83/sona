import asyncio
import collections
import logging
import re

import discord
from discord.ext import commands
from discord.ui import Button, View

from src.config import ALLOWED_CHANNEL_ID, ADMIN_USER_ID, LIBRARY_ENABLED, LLM_ENABLED_FOR_ALBUM_TRACKS, sp
from src.bot_instance import bot
import random

from src.playback import (
    queues, now_playing_info, play_next, update_player_embed,
    refresh_player_embed_fresh, _paused, maybe_notify_rate_limited,
)
from src.library import record_request, get_stats, search_index, entry_to_queue_track
from src.youtube import is_youtube_rate_limited
from src.spotify import (
    _is_spotify_url,
    _parse_spotify_url,
    _get_tracks_from_spotify_url,
    _get_spotify_query,
    _get_spotify_track_info,
    _ensure_auth,
)
from src.youtube import search_youtube, get_search_candidates, _is_youtube_url, extract_youtube_tracks
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


class LibrarySearchSelectionView(View):
    """Interactive view for selecting one library index match."""
    def __init__(self, candidates: list[tuple[str, dict]], query: str, ctx: commands.Context):
        super().__init__(timeout=30)
        self.candidates = candidates
        self.query = query
        self.ctx = ctx
        self.selected: tuple[str, dict] | None = None

        for idx in range(min(5, len(candidates))):
            button = Button(
                label=str(idx + 1),
                style=discord.ButtonStyle.secondary,
                custom_id=f"library_search_select_{idx}",
            )
            button.callback = self._make_callback(idx)
            self.add_item(button)

    def _make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.send_message(
                    "Solo quien hizo la búsqueda puede seleccionar.",
                    ephemeral=True,
                )
                return
            self.selected = self.candidates[idx]
            self.stop()
            await interaction.response.defer()

        return callback

    async def on_timeout(self):
        self.stop()


class LibraryActionView(View):
    """Play or seed radio from a selected library track."""
    def __init__(self, tid: str, entry: dict, ctx: commands.Context):
        super().__init__(timeout=30)
        self.tid = tid
        self.entry = entry
        self.ctx = ctx
        self.action: str | None = None

        play_btn = Button(
            label="Reproducir",
            emoji="▶️",
            style=discord.ButtonStyle.success,
            custom_id="library_action_play",
        )
        play_btn.callback = self._make_callback("play")
        self.add_item(play_btn)

        radio_btn = Button(
            label="Radio",
            emoji="📻",
            style=discord.ButtonStyle.primary,
            custom_id="library_action_radio",
        )
        radio_btn.callback = self._make_callback("radio")
        self.add_item(radio_btn)

    def _make_callback(self, action: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.send_message(
                    "Solo quien hizo la búsqueda puede seleccionar.",
                    ephemeral=True,
                )
                return
            self.action = action
            self.stop()
            await interaction.response.defer()

        return callback

    async def on_timeout(self):
        self.stop()


@bot.check
async def only_allowed_channel(ctx: commands.Context) -> bool:
    return ctx.channel.id == ALLOWED_CHANNEL_ID

@bot.event
async def on_ready():
    from src.playback import PlayerView
    from src.cookie_health import start_cookie_watchdog
    bot.add_view(PlayerView(0))
    start_cookie_watchdog()
    logger.info(f"Bot conectado como {bot.user} (id={bot.user.id})")
    logger.info(f"Guilds: {[g.name for g in bot.guilds]}")
    try:
        channel = await bot.fetch_channel(ALLOWED_CHANNEL_ID)
        embed = discord.Embed(
            title="🎵 Bot de música listo",
            description="Usa `!play <canción>` para reproducir.",
            color=0x1DB954
        )
        await channel.send(embed=embed)
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
    cause = error
    if isinstance(error, commands.CommandInvokeError) and error.original:
        cause = error.original
    try:
        from spotipy.oauth2 import SpotifyOauthError
        if isinstance(cause, SpotifyOauthError):
            from src.spotify import clear_spotify_token_cache
            if "invalid_client" in str(cause).lower() or "invalid_grant" in str(cause).lower():
                clear_spotify_token_cache()
            await ctx.send(
                "Spotify rechazo la autenticacion (token expirado o invalido).\n"
                "El admin debe ejecutar `!auth` y completar el enlace de autorizacion."
            )
            logger.error("[ERROR] %s: SpotifyOauthError: %s", ctx.command, cause)
            return
    except ImportError:
        pass
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
    vc = member.guild.voice_client
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
            member.guild.id,
        )
        _radio.set_radio_active(member.guild.id, False)
        queues[member.guild.id] = collections.deque()
        now_playing_info[member.guild.id] = None
        _paused[member.guild.id] = False
        vc.stop()
        await vc.disconnect()


@bot.command(name="ping", help="Comprueba que el bot esta vivo.")
async def ping(ctx: commands.Context):
    await ctx.send(f"Pong! Latencia: {round(bot.latency * 1000)}ms")


@bot.command(name="cookies", help="Estado de cookies de YouTube. Solo admin.")
async def cookies_cmd(ctx: commands.Context):
    if ctx.author.id != ADMIN_USER_ID:
        await ctx.send("Solo el administrador puede usar este comando.", delete_after=8)
        return
    from src.cookie_health import get_health_summary
    s = get_health_summary()
    age = s.get("age_h")
    age_str = f"{age:.1f}h" if age is not None else "n/a"
    fresh = "si" if s.get("fresh") else "no"
    embed = discord.Embed(title="Estado de cookies YouTube", color=0xFFAA00 if s.get("fresh") else 0xFF5555)
    embed.add_field(name="Archivo", value=f"`{s.get('path')}`", inline=False)
    embed.add_field(name="Edad", value=age_str, inline=True)
    embed.add_field(name="Frescas", value=fresh, inline=True)
    embed.add_field(name="Cookies exportadas", value=str(s.get("count", 0)), inline=True)
    embed.add_field(name="Auth fallida", value="si" if s.get("auth_failed") else "no", inline=True)
    embed.add_field(name="Rate-limited", value="si" if s.get("rate_limited") else "no", inline=True)
    embed.add_field(
        name="Biblioteca local",
        value=f"{s.get('library_on_disk', 0)} canciones ({s.get('library_size_mb', 0)} MB)",
        inline=False,
    )
    embed.add_field(
        name="Si necesitas refrescar",
        value="En el Mac: `./refresh_cookies.sh chrome`\nEl bot detecta el cambio sin reiniciar Docker.",
        inline=False,
    )
    await ctx.send(embed=embed, delete_after=60)


@bot.command(name="auth", help="Inicia o renueva la autenticacion de Spotify. Solo admin.")
async def auth_cmd(ctx: commands.Context):
    from src.config import sp as _sp
    from src.spotify import _safe_validate_token

    if ctx.author.id != ADMIN_USER_ID:
        await ctx.send("Solo el administrador puede usar este comando.")
        return
    if _sp and await _safe_validate_token(_sp.auth_manager):
        await ctx.send("Spotify ya esta autenticado.")
        return
    await _ensure_auth(ctx)


@bot.command(name="play", help="Reproduce una cancion en tu canal de voz. Uso: !play <busqueda>")
async def play(ctx: commands.Context, *, query: str):
    if not ctx.author.voice:
        await ctx.send("Debes estar en un canal de voz para usar este comando.")
        return

    voice_channel = ctx.author.voice.channel
    msg = await ctx.send(f"\U0001f50d Buscando **{query}**...", delete_after=30)

    # Check if query is a YouTube / YouTube Music URL
    yt_url_type = _is_youtube_url(query)
    if yt_url_type:
        yt_tracks = await extract_youtube_tracks(query)
        if not yt_tracks:
            await msg.edit(content="No se pudo procesar la URL de YouTube.")
            await asyncio.sleep(5)
            try:
                await msg.delete()
            except Exception:
                pass
            return

        vc = ctx.guild.voice_client
        if vc is None:
            vc = await voice_channel.connect()
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
        if ctx.guild.id not in queues:
            queues[ctx.guild.id] = collections.deque()

        truncated = len(yt_tracks) >= 50  # hit the cap
        for t in yt_tracks:
            track = {
                "title":     t["title"],
                "yt_query":  t["yt_query"],
                "url":       t.get("url"),
                "requester": ctx.author.display_name,
                "artist":    t.get("uploader") or "Unknown",
                "duration":  t.get("duration") or 0,
                "thumbnail": t.get("thumbnail") or "",
                "acodec":    t.get("acodec", "?"),
                "abr":       t.get("abr", 0),
            }
            record_request(track)
            queues[ctx.guild.id].append(track)

        try:
            await msg.delete()
        except Exception:
            pass

        label = yt_tracks[0]["title"] if len(yt_tracks) == 1 else f"{len(yt_tracks)} canciones"
        suffix = " (máximo 50)" if truncated else ""
        if vc.is_playing() or vc.is_paused():
            await ctx.send(f"\u2795 {label}{suffix} añadida(s) a la cola.", delete_after=8)
        else:
            await play_next(ctx.guild, vc, ctx.channel)
        return

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
    elif re.search(r"https?://", query):
        # Unknown URL (SoundCloud, Tidal, etc.) — reject early
        await msg.edit(content="No reconozco esa URL. Usa YouTube, Spotify o escribe el nombre de la canción.")
        await asyncio.sleep(5)
        try:
            await msg.delete()
        except Exception:
            pass
        return
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
    from_spotify = _is_spotify_url(query)
    tracks_to_queue = []
    if len(yt_queries) == 1:
        # Single track: no gather overhead needed
        info = yt_queries[0]  # {query, spotify_id, artist_id}
        yt_info = await search_youtube(info["query"], enable_llm=True, trusted=from_spotify)
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
            yt_info = await search_youtube(info["query"], enable_llm=enable_llm, trusted=from_spotify)
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
        if is_youtube_rate_limited():
            await maybe_notify_rate_limited(ctx.guild.id, ctx.channel)
            await msg.edit(content=f"YouTube bloqueado y sin copia local para: **{query}**")
        else:
            await msg.edit(content=f"No se encontro nada para: **{query}**")
        return

    try:
        await msg.delete()
    except Exception:
        pass

    for track in tracks_to_queue:
        record_request(track)

    # Add all tracks to queue
    # Radio mode: user requests go to the front (right after the current song)
    from src import radio as _radio
    radio_on = _radio.is_radio_active(ctx.guild.id)
    if radio_on and (vc.is_playing() or vc.is_paused()):
        # Insert user tracks before the first radio track in queue
        items = list(queues[ctx.guild.id])
        insert_idx = next(
            (i for i, t in enumerate(items) if t.get("requester") == "📻 Radio"),
            len(items),
        )
        for i, track in enumerate(tracks_to_queue):
            items.insert(insert_idx + i, track)
        queues[ctx.guild.id] = collections.deque(items)
    else:
        for track in tracks_to_queue:
            queues[ctx.guild.id].append(track)

    # Start playing if not already playing
    if vc.is_playing() or vc.is_paused():
        added_count = len(tracks_to_queue)
        label = tracks_to_queue[0]['title'] if added_count == 1 else f"{added_count} canciones"
        await ctx.send(f"\u2795 {label} anadida(s) a la cola.", delete_after=8)
        # Bump player embed down when user adds to queue
        asyncio.ensure_future(refresh_player_embed_fresh(ctx.guild, ctx.channel))
    else:
        await play_next(ctx.guild, vc, ctx.channel)


@bot.command(name="search", help="Busca canciones y te permite elegir una. Uso: !search <busqueda>")
async def search(ctx: commands.Context, *, query: str):
    if not ctx.author.voice:
        await ctx.send("Debes estar en un canal de voz para usar este comando.", delete_after=5)
        return

    voice_channel = ctx.author.voice.channel
    msg = await ctx.send(f"\U0001f50d Buscando **{query}**...", delete_after=60)

    # If a direct YouTube URL is passed to !search, delegate to !play logic
    if _is_youtube_url(query):
        yt_tracks = await extract_youtube_tracks(query)
        if not yt_tracks:
            await msg.edit(content="No se pudo procesar la URL de YouTube.")
            return
        vc = ctx.guild.voice_client
        if vc is None:
            vc = await voice_channel.connect()
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
        if ctx.guild.id not in queues:
            queues[ctx.guild.id] = collections.deque()
        for t in yt_tracks:
            queues[ctx.guild.id].append({
                "title":     t["title"],
                "yt_query":  t["yt_query"],
                "url":       t.get("url"),
                "requester": ctx.author.display_name,
                "artist":    t.get("uploader") or "Unknown",
                "duration":  t.get("duration") or 0,
                "thumbnail": t.get("thumbnail") or "",
                "acodec":    t.get("acodec", "?"),
                "abr":       t.get("abr", 0),
            })
        await msg.delete()
        if not (vc.is_playing() or vc.is_paused()):
            await play_next(ctx.guild, vc, ctx.channel)
        return

    # Resolve Spotify URLs to track name before searching YouTube
    if _is_spotify_url(query):
        sp_parsed = _parse_spotify_url(query)
        if sp_parsed and sp_parsed["type"] in ("album", "playlist"):
            # Album/playlist → silently delegate to !play (enqueues all tracks)
            await msg.delete()
            await play(ctx, query=query)
            return
        spotify_infos = await _get_tracks_from_spotify_url(query)
        if spotify_infos:
            query = spotify_infos[0]["query"]
            await msg.edit(content=f"\U0001f50d Buscando **{query}**...")

    # Unknown URL → reject early with a helpful message
    elif re.search(r"https?://", query):
        await msg.edit(content="No reconozco esa URL. Usa YouTube, Spotify o escribe el nombre de la canción.")
        return

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

        # Add to queue (user tracks go before radio tracks)
        from src import radio as _radio
        if _radio.is_radio_active(ctx.guild.id) and (vc.is_playing() or vc.is_paused()):
            items = list(queues[ctx.guild.id])
            insert_idx = next(
                (i for i, t in enumerate(items) if t.get("requester") == "📻 Radio"),
                len(items),
            )
            items.insert(insert_idx, track)
            queues[ctx.guild.id] = collections.deque(items)
        else:
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
            # Bump player embed down when user adds to queue
            asyncio.ensure_future(refresh_player_embed_fresh(ctx.guild, ctx.channel))
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


async def _radio_profile_cmd(ctx: commands.Context, args: str) -> None:
    from src import radio as _radio
    from src.spotify_taste import parse_playlist_id

    gid = ctx.guild.id
    parts = args.strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if not sub:
        embed = discord.Embed(
            title="Perfil de radio Spotify",
            description=(
                f"Modo actual: **{_radio.describe_profile_mode(gid)}**\n\n"
                "**Comandos:**\n"
                "`!radio profile admin` — gustos de la cuenta del admin (`!auth`)\n"
                "`!radio profile voice` — gustos de usuarios en el VC (`!spotify link`)\n"
                "`!radio profile playlist <url>` — canciones de una playlist\n"
                "`!radio profile off` — volver al historial del servidor"
            ),
            color=0x1DB954,
        )
        await ctx.send(embed=embed, delete_after=45)
        return

    if sub == "off":
        _radio.set_profile_mode(gid, "off")
        await ctx.send("Perfil de Spotify desactivado. La radio usa el historial del servidor.", delete_after=12)
        return

    if sub == "admin":
        _radio.set_profile_mode(gid, "admin")
        mode_desc = "perfil del admin"
    elif sub == "voice":
        _radio.set_profile_mode(gid, "voice")
        mode_desc = "usuarios en el canal de voz"
    elif sub == "playlist":
        if not rest:
            await ctx.send("Uso: `!radio profile playlist <url de Spotify>`", delete_after=8)
            return
        playlist_id = parse_playlist_id(rest)
        if not playlist_id:
            await ctx.send("URL o ID de playlist de Spotify invalido.", delete_after=8)
            return
        _radio.set_profile_mode(gid, "playlist", playlist_id=playlist_id)
        mode_desc = f"playlist `{playlist_id}`"
    else:
        await ctx.send(
            "Subcomando desconocido. Usa `!radio profile` para ver opciones.",
            delete_after=10,
        )
        return

    _radio.set_radio_active(gid, True)
    embed = discord.Embed(
        title="Perfil de radio activado",
        description=(
            f"Fuente: **{mode_desc}**\n"
            "La radio mezclara canciones del perfil y recomendaciones derivadas.\n"
            "Ejecuta `!auth` (admin) o `!spotify link` (usuarios) si aun no vinculaste Spotify."
        ),
        color=0x1DB954,
    )
    await ctx.send(embed=embed, delete_after=20)
    vc = ctx.guild.voice_client
    if vc is None and ctx.author.voice:
        vc = await ctx.author.voice.channel.connect()
        if gid not in queues:
            queues[gid] = collections.deque()
    if vc:
        from src.playback import start_radio_with_welcome
        asyncio.ensure_future(start_radio_with_welcome(ctx.guild, vc, ctx.channel))


@bot.command(
    name="radio",
    help="Radio 24/7. Uso: !radio [on|off|<mood>|profile ...]",
)
async def radio_cmd(ctx: commands.Context, *, args: str = ""):
    from src import radio as _radio
    gid = ctx.guild.id
    parts = args.strip().split(None, 1)
    action = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if action == "profile":
        await _radio_profile_cmd(ctx, rest)
        try:
            await ctx.message.delete()
        except Exception:
            pass
        return

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
        profile_line = ""
        if _radio.get_profile_mode(gid) != "off":
            profile_line = f"Perfil: **{_radio.describe_profile_mode(gid)}**\n"
        embed = discord.Embed(
            title="📻 Radio activado",
            description=(
                f"Mood actual: **{_radio.get_mood(gid).capitalize()}**\n"
                f"{profile_line}"
                "La cola se rellena automaticamente con recomendaciones.\n"
                "Usa `!mood <nombre>` o `!radio profile` para personalizar."
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
            from src.playback import start_radio_with_welcome
            asyncio.ensure_future(start_radio_with_welcome(ctx.guild, vc, ctx.channel))
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


@bot.command(name="spotify", help="Vincula tu cuenta Spotify. Uso: !spotify link | unlink | status")
async def spotify_cmd(ctx: commands.Context, *, args: str = ""):
    from src.spotify import run_oauth_flow
    from src.spotify_users import (
        get_authorize_url,
        is_user_linked,
        unlink_user,
        validate_user_token,
    )

    parts = args.strip().split(None, 1)
    sub = parts[0].lower() if parts else "status"

    if sub == "link":
        if await validate_user_token(ctx.author.id):
            await ctx.send("Tu Spotify ya esta vinculado. Usa `!spotify unlink` para desvincular.", delete_after=12)
            return
        auth_url = get_authorize_url(ctx.author.id)
        ok = await run_oauth_flow(
            ctx,
            expected_state=f"user:{ctx.author.id}",
            authorize_url=auth_url,
            success_message="Spotify vinculado correctamente. La radio en modo `voice` usara tus gustos.",
            timeout_message="Tiempo agotado. Usa `!spotify link` para reintentar.",
        )
        if not ok:
            return
        return

    if sub == "unlink":
        if unlink_user(ctx.author.id):
            await ctx.send("Spotify desvinculado.", delete_after=10)
        else:
            await ctx.send("No tenias Spotify vinculado.", delete_after=10)
        return

    if sub == "status":
        if await validate_user_token(ctx.author.id):
            await ctx.send("Tu cuenta de Spotify esta vinculada.", delete_after=10)
        elif is_user_linked(ctx.author.id):
            await ctx.send(
                "Tu token de Spotify expiro. Ejecuta `!spotify link` para renovar.",
                delete_after=12,
            )
        else:
            await ctx.send(
                "No tienes Spotify vinculado. Usa `!spotify link` para conectar tu cuenta.",
                delete_after=12,
            )
        return

    await ctx.send("Uso: `!spotify link` | `!spotify unlink` | `!spotify status`", delete_after=10)


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
        # Flush radio tracks and refill with new mood
        removed = _radio.flush_radio_tracks(gid)
        if _radio.is_radio_active(gid):
            vc = ctx.guild.voice_client
            if vc:
                asyncio.ensure_future(_radio.fill_radio_queue(ctx.guild, vc, ctx.channel))
        embed = discord.Embed(
            title=f"🎭 Mood cambiado a {name.capitalize()}",
            description=f"Se removieron {removed} canciones del mood anterior." if removed else "El siguiente batch usara este estilo.",
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
        ("🎵 Reproduccion", None),
        ("!play <cancion>", "Reproduce una cancion. Acepta URLs de Spotify, YouTube y YouTube Music."),
        ("!search <cancion>", "Busca canciones y te permite elegir entre resultados."),
        ("!playlist <url>", "Carga una playlist de Spotify o YouTube en la cola."),
        ("!skip", "Salta la cancion actual."),
        ("!pause", "Pausa la reproduccion."),
        ("!resume", "Reanuda la reproduccion."),
        ("!stop", "Detiene la reproduccion y limpia la cola."),
        ("!leave", "Desconecta el bot del canal de voz."),
        ("📋 Cola", None),
        ("!queue", "Muestra la cola de reproduccion."),
        ("!np", "Muestra la cancion actual."),
        ("!shuffle", "Mezcla la cola aleatoriamente."),
        ("!move <de> <a>", "Mueve una cancion a otra posicion en la cola."),
        ("!remove <pos>", "Elimina una cancion de la cola."),
        ("!priority <pos>", "Mueve una cancion al tope de la cola."),
        ("📻 Radio", None),
        ("!radio [on|off]", "Activa/desactiva el modo radio 24/7 con recomendaciones."),
        ("!radio profile [admin|voice|playlist <url>|off]", "Radio basada en perfil Spotify."),
        ("!spotify link", "Vincula tu cuenta Spotify para el modo radio `voice`."),
        ("!mood [nombre]", "Lista moods disponibles o cambia el mood del radio."),
        ("!mood create <nombre> <query>", "Crea un mood custom basado en un artista/cancion."),
        ("!mood delete <nombre>", "Elimina un mood custom."),
        ("⚙️ Otros", None),
        ("!library", "Muestra estadisticas de la biblioteca local cacheada."),
        ("!library search <busqueda>", "Busca en la biblioteca local y elige reproducir o iniciar radio."),
        ("!cookies", "Estado de cookies de YouTube (solo admin)."),
        ("!auth", "Autenticacion de Spotify (solo admin)."),
        ("!ping", "Verifica que el bot este vivo."),
    ]
    for cmd, desc in commands_info:
        if desc is None:
            embed.add_field(name=f"\u200b\n**{cmd}**", value="\u200b", inline=False)
        else:
            embed.add_field(name=cmd, value=desc, inline=False)
    embed.set_footer(text="Tambien puedes usar los botones del embed del reproductor")
    await ctx.send(embed=embed, delete_after=60)


@bot.command(name="playlist", help="Carga una playlist de Spotify o YouTube en la cola. Uso: !playlist <url>")
async def playlist_cmd(ctx: commands.Context, *, url: str):
    if not ctx.author.voice:
        await ctx.send("Debes estar en un canal de voz para usar este comando.")
        return

    # YouTube / YouTube Music playlist
    if _is_youtube_url(url) == "playlist":
        msg = await ctx.send("📋 Cargando playlist de YouTube...")
        yt_tracks = await extract_youtube_tracks(url)
        if not yt_tracks:
            await msg.edit(content="No se pudo procesar la playlist de YouTube.")
            return

        voice_channel = ctx.author.voice.channel
        vc = ctx.guild.voice_client
        if vc is None:
            vc = await voice_channel.connect()
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
        if ctx.guild.id not in queues:
            queues[ctx.guild.id] = collections.deque()

        truncated = len(yt_tracks) >= 50
        for t in yt_tracks:
            queues[ctx.guild.id].append({
                "title":     t["title"],
                "yt_query":  t["yt_query"],
                "url":       t.get("url"),
                "requester": ctx.author.display_name,
                "artist":    t.get("uploader") or "Unknown",
                "duration":  t.get("duration") or 0,
                "thumbnail": t.get("thumbnail") or "",
                "acodec":    t.get("acodec", "?"),
                "abr":       t.get("abr", 0),
            })

        suffix = " (máximo 50)" if truncated else ""
        await msg.edit(content=f"✅ {len(yt_tracks)} canciones{suffix} añadidas a la cola.")
        if not (vc.is_playing() or vc.is_paused()):
            await play_next(ctx.guild, vc, ctx.channel)
        return

    # Spotify playlist
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


def _library_entry_field_value(entry: dict) -> str:
    import pathlib

    artist = entry.get("artist", "Unknown")
    duration = entry.get("duration") or 0
    dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else "?"
    plays = entry.get("play_count", 0)
    cached = "💾 " if pathlib.Path(entry.get("file_path", "")).is_file() else ""
    return f"{cached}{artist}\n`[{dur_str}]` · `{plays}` reproducciones"


async def _library_search(ctx: commands.Context, query: str) -> None:
    if not ctx.author.voice:
        await ctx.send("Debes estar en un canal de voz para usar este comando.", delete_after=5)
        return
    if not LIBRARY_ENABLED:
        await ctx.send("La biblioteca local no está habilitada.", delete_after=8)
        return

    stats = get_stats()
    if stats["total_indexed"] == 0:
        await ctx.send(
            "La biblioteca está vacía. Reproduce canciones con `!play` para empezar a indexarlas.",
            delete_after=10,
        )
        return

    msg = await ctx.send(f"📚 Buscando en la biblioteca: **{query}**...", delete_after=60)
    candidates = search_index(query)
    if not candidates:
        await msg.edit(content=f"No se encontró nada en la biblioteca para: **{query}**")
        return

    voice_channel = ctx.author.voice.channel
    vc = ctx.guild.voice_client
    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)
    if ctx.guild.id not in queues:
        queues[ctx.guild.id] = collections.deque()

    while candidates:
        embed = discord.Embed(
            title="📚 Elige una canción de la biblioteca",
            description=f"Buscaste: **{query}**\n\nSelecciona una opción (válido por 30 segundos)",
            color=0x1DB954,
        )
        for idx, (_tid, entry) in enumerate(candidates):
            title = entry.get("title", "Unknown")
            embed.add_field(
                name=f"{idx + 1}️⃣ {title}",
                value=_library_entry_field_value(entry),
                inline=False,
            )

        select_view = LibrarySearchSelectionView(candidates, query, ctx)
        try:
            await msg.delete()
        except Exception:
            pass
        selection_msg = await ctx.send(embed=embed, view=select_view)
        await select_view.wait()

        if select_view.selected is None:
            await selection_msg.edit(content="⏱️ Tiempo agotado. Búsqueda cancelada.", embed=None, view=None)
            return

        tid, entry = select_view.selected
        title = entry.get("title", "Unknown")
        artist = entry.get("artist", "Unknown")

        action_embed = discord.Embed(
            title="¿Qué quieres hacer?",
            description=f"**{title}**\n{artist}",
            color=0x1DB954,
        )
        action_view = LibraryActionView(tid, entry, ctx)
        await selection_msg.edit(embed=action_embed, view=action_view)
        await action_view.wait()

        if action_view.action is None:
            await selection_msg.edit(content="⏱️ Tiempo agotado. Búsqueda cancelada.", embed=None, view=None)
            return

        if action_view.action == "play":
            track = entry_to_queue_track(tid, entry, requester=ctx.author.display_name)
            record_request(track)

            from src import radio as _radio
            if _radio.is_radio_active(ctx.guild.id) and (vc.is_playing() or vc.is_paused()):
                items = list(queues[ctx.guild.id])
                insert_idx = next(
                    (i for i, t in enumerate(items) if t.get("requester") == "📻 Radio"),
                    len(items),
                )
                items.insert(insert_idx, track)
                queues[ctx.guild.id] = collections.deque(items)
            else:
                queues[ctx.guild.id].append(track)

            if not (vc.is_playing() or vc.is_paused()):
                try:
                    await play_next(ctx.guild, vc, ctx.channel)
                    confirm = discord.Embed(
                        title="✅ Reproduciendo",
                        description=f"**{title}**\n{artist}",
                        color=0x1DB954,
                    )
                    await selection_msg.edit(embed=confirm, view=None)
                    return
                except Exception as exc:
                    logger.error("library search play error: %s", exc)
                    items = [t for t in queues[ctx.guild.id] if t.get("track_id") != tid]
                    queues[ctx.guild.id] = collections.deque(items)
                    candidates = [(c_tid, c_entry) for c_tid, c_entry in candidates if c_tid != tid]
                    if candidates:
                        retry_embed = discord.Embed(
                            title="⚠️ No disponible",
                            description=f"**{title}** no pudo reproducirse.\n\nElige otra opción.",
                            color=0xFF9500,
                        )
                        await selection_msg.edit(embed=retry_embed, view=None)
                        await asyncio.sleep(2)
                        msg = selection_msg
                        continue
                    await selection_msg.edit(
                        content="❌ No quedan opciones disponibles.",
                        embed=None,
                        view=None,
                    )
                    return
            else:
                confirm = discord.Embed(
                    title="✅ Canción agregada a la cola",
                    description=f"**{title}**\n{artist}",
                    color=0x1DB954,
                )
                await selection_msg.edit(embed=confirm, view=None)
                asyncio.ensure_future(refresh_player_embed_fresh(ctx.guild, ctx.channel))
                return

        # Radio: seed recommendations without playing the selected track
        from src import radio as _radio
        from src.playback import start_radio_with_welcome

        track = entry_to_queue_track(tid, entry, requester=ctx.author.display_name)
        was_active = _radio.is_radio_active(ctx.guild.id)
        _radio.set_radio_active(ctx.guild.id, True)
        await _radio.record_played(ctx.guild.id, track)
        if was_active:
            _radio.flush_radio_tracks(ctx.guild.id)

        if not (vc.is_playing() or vc.is_paused()):
            asyncio.ensure_future(start_radio_with_welcome(ctx.guild, vc, ctx.channel))
        else:
            await _radio.fill_radio_queue(ctx.guild, vc, ctx.channel, auto_play=False)

        confirm = discord.Embed(
            title="📻 Radio activado",
            description=f"Semilla: **{title}**\n{artist}\n\nLa cola se rellena con recomendaciones.",
            color=0x1DB954,
        )
        await selection_msg.edit(embed=confirm, view=None)
        return


@bot.command(
    name="library",
    help="Estadisticas de la biblioteca local. Uso: !library | !library search <busqueda>",
)
async def library_cmd(ctx: commands.Context, *, args: str = ""):
    parts = args.strip().split(None, 1)
    subcommand = parts[0].lower() if parts else ""

    if subcommand == "search":
        query = parts[1].strip() if len(parts) > 1 else ""
        if not query:
            await ctx.send("Uso: `!library search <busqueda>`", delete_after=8)
            return
        await _library_search(ctx, query)
        return

    stats = get_stats()
    embed = discord.Embed(
        title="Biblioteca local",
        description=(
            f"**{stats['on_disk']}** canciones en disco "
            f"({stats['size_mb']} MB) · **{stats['pinned']}** fijadas por popularidad"
        ),
        color=0x1DB954,
    )
    if stats["top_plays"]:
        lines = [
            f"`{plays:>3}` {title[:60]}"
            for _tid, title, plays in stats["top_plays"]
        ]
        embed.add_field(name="Mas reproducidas", value="\n".join(lines), inline=False)
    else:
        embed.add_field(
            name="Mas reproducidas",
            value="Aun vacia — se llena al reproducir canciones.",
            inline=False,
        )
    embed.set_footer(text="Usa `!library search <busqueda>` para buscar y reproducir o iniciar radio")
    await ctx.send(embed=embed, delete_after=45)


@bot.command(name="likes", help="Muestra tus canciones con ❤️ en este servidor.")
async def likes_cmd(ctx: commands.Context):
    from src import likes as _likes_mod
    gid = ctx.guild.id
    uid = ctx.author.id
    user_likes = _likes_mod.get_user_likes(gid, uid)
    if not user_likes:
        await ctx.send("No tienes canciones con ❤️ todavía. Usa el botón del reproductor para dar like.", delete_after=15)
        return
    lines = []
    for i, entry in enumerate(user_likes[:20], 1):
        lines.append(f"`{i}.` {entry['title']} — {entry['artist']}")
    if len(user_likes) > 20:
        lines.append(f"... y {len(user_likes) - 20} más")
    embed = discord.Embed(
        title=f"❤️ Tus likes ({len(user_likes)})",
        description="\n".join(lines),
        color=0xe74c3c,
    )
    await ctx.send(embed=embed, delete_after=60)