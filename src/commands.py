import asyncio
import collections
import logging
import random
import re

import discord
from discord.ext import commands
from discord.ui import Button, View

from src.bot_instance import bot
from src.config import (
    BOT_TEXT_CHANNEL_ID,
    LIBRARY_ENABLED,
    LLM_ALBUM_TRACK_RANKING_LIMIT,
    OAUTH_ADMIN_USER_ID,
    RADIO_REQUESTER_LABEL,
    sp,
)
from src.library import entry_to_queue_track, get_stats, record_request, search_index
from src.playback import (
    _paused,
    maybe_notify_rate_limited,
    now_playing_info,
    play_next,
    queues,
    refresh_player_embed_fresh,
    update_player_embed,
)
from src.scoring import _split_query_parts
from src.spotify import (
    _ensure_auth,
    _get_spotify_query,
    _get_spotify_track_info,
    _get_tracks_from_spotify_url,
    _is_spotify_url,
    _parse_spotify_url,
)
from src.youtube import (
    _is_youtube_url,
    extract_youtube_tracks,
    get_search_candidates,
    is_youtube_rate_limited,
    search_youtube,
)

logger = logging.getLogger(__name__)

YOUTUBE_PLAYLIST_TRACK_LIMIT = 50
SELECTION_VIEW_TIMEOUT_SEC = 30


def error_embed(title: str, description: str = "", details: str = "") -> discord.Embed:
    embed = discord.Embed(title=f"❌ {title}", description=description, color=0xFF5555)
    if details:
        embed.add_field(name="Detalles", value=f"`{details[:1024]}`", inline=False)
    return embed


class SearchSelectionView(View):
    def __init__(self, candidates: list[dict], query: str, ctx: commands.Context):
        super().__init__(timeout=SELECTION_VIEW_TIMEOUT_SEC)
        self.candidates = candidates
        self.query = query
        self.ctx = ctx
        self.selected = None

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
        self.stop()


class LibrarySearchSelectionView(View):
    def __init__(self, candidates: list[tuple[str, dict]], query: str, ctx: commands.Context):
        super().__init__(timeout=SELECTION_VIEW_TIMEOUT_SEC)
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
    def __init__(self, tid: str, entry: dict, ctx: commands.Context):
        super().__init__(timeout=SELECTION_VIEW_TIMEOUT_SEC)
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


def _member_left_voice_channel(before: discord.VoiceState, after: discord.VoiceState) -> bool:
    return before.channel is not None and after.channel != before.channel


def _humans_remaining_after_leave(
    channel: discord.VoiceChannel,
    left_member: discord.Member,
) -> list[discord.Member]:
    return [m for m in channel.members if not m.bot and m.id != left_member.id]


async def _connect_author_voice_channel(
    ctx: commands.Context,
    voice_channel: discord.VoiceChannel,
) -> discord.VoiceClient:
    vc = ctx.guild.voice_client
    if vc is None:
        return await voice_channel.connect()
    if vc.channel != voice_channel:
        await vc.move_to(voice_channel)
    return vc


def _ensure_guild_queue(guild_id: int) -> None:
    queues.setdefault(guild_id, collections.deque())


def _queue_track_from_ytdl(raw: dict, requester: str) -> dict:
    return {
        "title": raw["title"],
        "yt_query": raw["yt_query"],
        "url": raw.get("url"),
        "requester": requester,
        "artist": raw.get("uploader") or "Unknown",
        "duration": raw.get("duration") or 0,
        "thumbnail": raw.get("thumbnail") or "",
        "acodec": raw.get("acodec", "?"),
        "abr": raw.get("abr", 0),
    }


def _queue_track_from_youtube_search(
    yt_info: dict,
    spotify_track: dict,
    requester: str,
) -> dict:
    artist, _ = _split_query_parts(spotify_track["query"])
    return {
        "title": yt_info["title"],
        "yt_query": spotify_track["query"],
        "url": yt_info["url"],
        "requester": requester,
        "artist": artist or "Unknown",
        "duration": yt_info.get("duration") or 0,
        "thumbnail": yt_info.get("thumbnail") or "",
        "spotify_id": spotify_track.get("spotify_id"),
        "artist_id": spotify_track.get("artist_id"),
        "acodec": yt_info.get("acodec", "?"),
        "abr": yt_info.get("abr", 0),
    }


def _first_radio_track_index(tracks: list[dict]) -> int:
    return next(
        (i for i, track in enumerate(tracks) if track.get("requester") == RADIO_REQUESTER_LABEL),
        len(tracks),
    )


def _enqueue_user_tracks_before_radio(
    guild_id: int,
    tracks: list[dict],
    *,
    playback_active: bool,
) -> None:
    from src import radio as _radio

    if _radio.is_radio_active(guild_id) and playback_active:
        items = list(queues[guild_id])
        insert_at = _first_radio_track_index(items)
        for offset, track in enumerate(tracks):
            items.insert(insert_at + offset, track)
        queues[guild_id] = collections.deque(items)
        return
    for track in tracks:
        queues[guild_id].append(track)


async def _resolve_non_youtube_play_queries(query: str) -> tuple[list[dict] | None, str | None]:
    if _is_spotify_url(query):
        track_infos = await _get_tracks_from_spotify_url(query)
        if not track_infos:
            return None, "No se pudo procesar la URL de Spotify."
        return track_infos, None
    if re.search(r"https?://", query):
        return None, "No reconozco esa URL. Usa YouTube, Spotify o escribe el nombre de la canción."
    return [await _get_spotify_track_info(query)], None


async def _youtube_matches_for_spotify_tracks(
    spotify_tracks: list[dict],
    requester: str,
    *,
    trusted_spotify_source: bool,
) -> list[dict]:
    if len(spotify_tracks) == 1:
        info = spotify_tracks[0]
        yt_info = await search_youtube(
            info["query"],
            enable_llm=True,
            trusted=trusted_spotify_source,
            urgent=True,
        )
        if not yt_info:
            return []
        return [_queue_track_from_youtube_search(yt_info, info, requester)]

    async def _fetch(idx: int, info: dict) -> dict | None:
        enable_llm = idx < LLM_ALBUM_TRACK_RANKING_LIMIT
        yt_info = await search_youtube(info["query"], enable_llm=enable_llm, trusted=trusted_spotify_source)
        if not yt_info:
            return None
        track = _queue_track_from_youtube_search(yt_info, info, requester)
        track["_order"] = idx
        return track

    results = await asyncio.gather(*(_fetch(i, q) for i, q in enumerate(spotify_tracks)))
    matched = [track for track in results if track is not None]
    for track in matched:
        track.pop("_order", None)
    return matched


async def _publish_voice_channel_status(guild: discord.Guild, status: str) -> None:
    from src.playback import _update_status

    await _update_status(guild, status)


def _reset_guild_playback_state(guild_id: int) -> None:
    from src import radio as _radio

    _radio.set_radio_active(guild_id, False)
    queues[guild_id] = collections.deque()
    now_playing_info[guild_id] = None
    _paused[guild_id] = False


async def _ensure_radio_voice_session(
    ctx: commands.Context,
    guild_id: int,
) -> discord.VoiceClient | None:
    vc = ctx.guild.voice_client
    if vc is None and ctx.author.voice:
        vc = await ctx.author.voice.channel.connect()
        _ensure_guild_queue(guild_id)
    return vc


async def _start_radio_playback(ctx: commands.Context, guild_id: int) -> None:
    vc = await _ensure_radio_voice_session(ctx, guild_id)
    if vc:
        from src.playback import start_radio_with_welcome

        asyncio.ensure_future(start_radio_with_welcome(ctx.guild, vc, ctx.channel))


def _known_mood_names(guild_id: int) -> dict:
    from src import radio as _radio

    return {**_radio.MOODS, **_radio._custom_moods.get(guild_id, {})}


def _tokenize_mood_query(query: str) -> list[str]:
    if "," in query:
        return [term.strip() for term in query.split(",") if term.strip()]
    words = query.split()
    return [
        " ".join(words[i : i + 2])
        for i in range(0, len(words), 2)
        if words[i : i + 2]
    ]


async def _spotify_genres_for_query(query: str) -> list[str]:
    from src.spotify import _get_artist_genres

    info = await _get_spotify_track_info(query)
    artist_id = info.get("artist_id")
    if not artist_id:
        return []
    return await _get_artist_genres(artist_id)


async def _resolve_custom_mood_genres(query: str) -> tuple[list[str], bool]:
    genres = await _spotify_genres_for_query(query)
    if genres:
        return genres, False

    seen: set[str] = set()
    merged: list[str] = []
    for term in _tokenize_mood_query(query):
        for genre in await _spotify_genres_for_query(term):
            if genre not in seen:
                seen.add(genre)
                merged.append(genre)
    if merged:
        return merged, False

    return _tokenize_mood_query(query), True


def _queue_lazy_spotify_playlist(
    guild_id: int,
    track_infos: list[dict],
    requester: str,
) -> None:
    _ensure_guild_queue(guild_id)
    for info in track_infos:
        query = info["query"]
        queues[guild_id].append({
            "title": query,
            "yt_query": query,
            "url": None,
            "requester": requester,
            "spotify_id": info.get("spotify_id"),
            "artist_id": info.get("artist_id"),
        })


async def _seed_radio_from_library_entry(
    ctx: commands.Context,
    vc: discord.VoiceClient,
    tid: str,
    entry: dict,
    selection_msg: discord.Message,
) -> None:
    from src import radio as _radio
    from src.playback import start_radio_with_welcome

    title = entry.get("title", "Unknown")
    artist = entry.get("artist", "Unknown")
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
        title=f"{RADIO_REQUESTER_LABEL} activado",
        description=f"Semilla: **{title}**\n{artist}\n\nLa cola se rellena con recomendaciones.",
        color=0x1DB954,
    )
    await selection_msg.edit(embed=confirm, view=None)


@bot.check
async def only_allowed_channel(ctx: commands.Context) -> bool:
    return ctx.channel.id == BOT_TEXT_CHANNEL_ID

@bot.event
async def on_ready():
    from src.playback import PlayerView
    from src.cookie_health import start_cookie_watchdog
    bot.add_view(PlayerView(0))
    start_cookie_watchdog()
    logger.info(f"Bot conectado como {bot.user} (id={bot.user.id})")
    logger.info(f"Guilds: {[g.name for g in bot.guilds]}")
    try:
        channel = await bot.fetch_channel(BOT_TEXT_CHANNEL_ID)
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
    from src import radio as _radio

    if not _member_left_voice_channel(before, after):
        return

    vc = member.guild.voice_client
    if vc is None or vc.channel != before.channel:
        return

    if _humans_remaining_after_leave(before.channel, member):
        return

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
    if ctx.author.id != OAUTH_ADMIN_USER_ID:
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

    if ctx.author.id != OAUTH_ADMIN_USER_ID:
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

    if _is_youtube_url(query):
        yt_tracks = await extract_youtube_tracks(query)
        if not yt_tracks:
            await msg.edit(content="No se pudo procesar la URL de YouTube.")
            await asyncio.sleep(5)
            try:
                await msg.delete()
            except Exception:
                pass
            return

        vc = await _connect_author_voice_channel(ctx, voice_channel)
        _ensure_guild_queue(ctx.guild.id)

        hit_playlist_limit = len(yt_tracks) >= YOUTUBE_PLAYLIST_TRACK_LIMIT
        for raw_track in yt_tracks:
            track = _queue_track_from_ytdl(raw_track, ctx.author.display_name)
            record_request(track)
            queues[ctx.guild.id].append(track)

        try:
            await msg.delete()
        except Exception:
            pass

        label = yt_tracks[0]["title"] if len(yt_tracks) == 1 else f"{len(yt_tracks)} canciones"
        suffix = f" (máximo {YOUTUBE_PLAYLIST_TRACK_LIMIT})" if hit_playlist_limit else ""
        if vc.is_playing() or vc.is_paused():
            await ctx.send(f"\u2795 {label}{suffix} añadida(s) a la cola.", delete_after=8)
        else:
            await play_next(ctx.guild, vc, ctx.channel)
        return

    spotify_tracks, resolve_error = await _resolve_non_youtube_play_queries(query)
    if resolve_error:
        await msg.edit(content=resolve_error)
        await asyncio.sleep(5)
        try:
            await msg.delete()
        except Exception:
            pass
        return

    vc = await _connect_author_voice_channel(ctx, voice_channel)
    _ensure_guild_queue(ctx.guild.id)

    tracks_to_queue = await _youtube_matches_for_spotify_tracks(
        spotify_tracks,
        ctx.author.display_name,
        trusted_spotify_source=_is_spotify_url(query),
    )

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

    playback_active = vc.is_playing() or vc.is_paused()
    _enqueue_user_tracks_before_radio(ctx.guild.id, tracks_to_queue, playback_active=playback_active)

    if playback_active:
        added_count = len(tracks_to_queue)
        label = tracks_to_queue[0]["title"] if added_count == 1 else f"{added_count} canciones"
        await ctx.send(f"\u2795 {label} anadida(s) a la cola.", delete_after=8)
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

    if _is_youtube_url(query):
        yt_tracks = await extract_youtube_tracks(query)
        if not yt_tracks:
            await msg.edit(content="No se pudo procesar la URL de YouTube.")
            return
        vc = await _connect_author_voice_channel(ctx, voice_channel)
        _ensure_guild_queue(ctx.guild.id)
        for raw_track in yt_tracks:
            queues[ctx.guild.id].append(_queue_track_from_ytdl(raw_track, ctx.author.display_name))
        await msg.delete()
        if not (vc.is_playing() or vc.is_paused()):
            await play_next(ctx.guild, vc, ctx.channel)
        return

    if _is_spotify_url(query):
        sp_parsed = _parse_spotify_url(query)
        if sp_parsed and sp_parsed["type"] in ("album", "playlist"):
            await msg.delete()
            await play(ctx, query=query)
            return
        spotify_infos = await _get_tracks_from_spotify_url(query)
        if spotify_infos:
            query = spotify_infos[0]["query"]
            await msg.edit(content=f"\U0001f50d Buscando **{query}**...")

    elif re.search(r"https?://", query):
        await msg.edit(content="No reconozco esa URL. Usa YouTube, Spotify o escribe el nombre de la canción.")
        return

    candidates = await get_search_candidates(query)
    if not candidates:
        await msg.edit(content=f"No se encontro nada para: **{query}**")
        return

    vc = await _connect_author_voice_channel(ctx, voice_channel)
    _ensure_guild_queue(ctx.guild.id)

    available_candidates = list(candidates)

    while available_candidates:
        embed = discord.Embed(
            title="🎵 Elige una canción",
            description=(
                f"Buscaste: **{query}**\n\n"
                f"Selecciona una opción (válido por {SELECTION_VIEW_TIMEOUT_SEC} segundos)"
            ),
            color=0x1DB954,
        )

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

        view = SearchSelectionView(available_candidates, query, ctx)

        try:
            await msg.delete()
        except Exception:
            pass

        selection_msg = await ctx.send(embed=embed, view=view)
        await view.wait()

        if view.selected is None:
            await selection_msg.edit(content="⏱️ Tiempo agotado. Búsqueda cancelada.", embed=None, view=None)
            return

        selected = view.selected
        artist, _ = _split_query_parts(query)
        track = {
            "title": selected["title"],
            "yt_query": query,
            "url": selected["url"],
            "requester": ctx.author.display_name,
            "artist": artist or selected.get("uploader", "Unknown"),
            "duration": selected.get("duration") or 0,
            "thumbnail": selected.get("thumbnail") or "",
        }

        playback_active = vc.is_playing() or vc.is_paused()
        _enqueue_user_tracks_before_radio(ctx.guild.id, [track], playback_active=playback_active)

        if not playback_active:
            try:
                await play_next(ctx.guild, vc, ctx.channel)
                embed_confirm = discord.Embed(
                    title="✅ Reproduciendo",
                    description=f"**{selected['title']}**\n{selected.get('uploader', '')}",
                    color=0x1DB954
                )
                await selection_msg.edit(embed=embed_confirm, view=None)
                return
            except Exception as e:
                logger.error(f"Error al reproducir canción: {e}")
                available_candidates.remove(selected)
                queues[ctx.guild.id].pop()

                if available_candidates:
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
                    embed_error = discord.Embed(
                        title="❌ Sin opciones disponibles",
                        description="Todas las canciones fallaron. Intenta una búsqueda diferente.",
                        color=0xFF5555
                    )
                    await selection_msg.edit(embed=embed_error, view=None)
                    return
        else:
            embed_confirm = discord.Embed(
                title="✅ Canción agregada a la cola",
                description=f"**{selected['title']}**\n{selected.get('uploader', '')}",
                color=0x1DB954,
            )
            await selection_msg.edit(embed=embed_confirm, view=None)
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
        await _publish_voice_channel_status(ctx.guild, "⏸ Paused")
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
        now_playing = now_playing_info.get(ctx.guild.id)
        if now_playing:
            await _publish_voice_channel_status(ctx.guild, now_playing.get("title"))
        await update_player_embed(ctx.guild, ctx.channel)
    else:
        await ctx.send("No hay nada en pausa.", delete_after=5)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="stop", help="Detiene la reproduccion y limpia la cola.")
async def stop(ctx: commands.Context):
    _reset_guild_playback_state(ctx.guild.id)
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
    _reset_guild_playback_state(ctx.guild.id)
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
    await _start_radio_playback(ctx, gid)


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

    all_moods = _known_mood_names(gid)
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
            title=f"{RADIO_REQUESTER_LABEL} activado",
            description=(
                f"Mood actual: **{_radio.get_mood(gid).capitalize()}**\n"
                f"{profile_line}"
                "La cola se rellena automaticamente con recomendaciones.\n"
                "Usa `!mood <nombre>` o `!radio profile` para personalizar."
            ),
            color=0x1DB954,
        )
        await ctx.send(embed=embed, delete_after=15)
        await _start_radio_playback(ctx, gid)
    else:
        embed = discord.Embed(
            title=f"{RADIO_REQUESTER_LABEL} desactivado",
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
        genres, using_raw_tokens = await _resolve_custom_mood_genres(query)
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
        all_moods = _known_mood_names(gid)
        if name not in all_moods:
            available = ", ".join(f"`{m}`" for m in all_moods)
            await ctx.send(f"Mood desconocido. Disponibles: {available}", delete_after=10)
            return
        try:
            _radio.set_mood(gid, name)
        except ValueError as e:
            await ctx.send(str(e), delete_after=10)
            return
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

    if _is_youtube_url(url) == "playlist":
        msg = await ctx.send("📋 Cargando playlist de YouTube...")
        yt_tracks = await extract_youtube_tracks(url)
        if not yt_tracks:
            await msg.edit(content="No se pudo procesar la playlist de YouTube.")
            return

        voice_channel = ctx.author.voice.channel
        vc = await _connect_author_voice_channel(ctx, voice_channel)
        _ensure_guild_queue(ctx.guild.id)

        hit_playlist_limit = len(yt_tracks) >= YOUTUBE_PLAYLIST_TRACK_LIMIT
        for raw_track in yt_tracks:
            queues[ctx.guild.id].append(_queue_track_from_ytdl(raw_track, ctx.author.display_name))

        suffix = f" (máximo {YOUTUBE_PLAYLIST_TRACK_LIMIT})" if hit_playlist_limit else ""
        await msg.edit(content=f"✅ {len(yt_tracks)} canciones{suffix} añadidas a la cola.")
        if not (vc.is_playing() or vc.is_paused()):
            await play_next(ctx.guild, vc, ctx.channel)
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
    vc = await _connect_author_voice_channel(ctx, voice_channel)
    _queue_lazy_spotify_playlist(ctx.guild.id, track_infos, ctx.author.display_name)

    await msg.edit(content=f"✅ {len(track_infos)} canciones añadidas a la cola.")

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
    vc = await _connect_author_voice_channel(ctx, voice_channel)
    _ensure_guild_queue(ctx.guild.id)

    while candidates:
        embed = discord.Embed(
            title="📚 Elige una canción de la biblioteca",
            description=(
                f"Buscaste: **{query}**\n\n"
                f"Selecciona una opción (válido por {SELECTION_VIEW_TIMEOUT_SEC} segundos)"
            ),
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

            playback_active = vc.is_playing() or vc.is_paused()
            _enqueue_user_tracks_before_radio(ctx.guild.id, [track], playback_active=playback_active)

            if not playback_active:
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

        await _seed_radio_from_library_entry(ctx, vc, tid, entry, selection_msg)
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