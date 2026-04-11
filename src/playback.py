import asyncio
import collections
import logging
import random

import discord

from src.config import FFMPEG_OPTIONS
from src.youtube import search_youtube
from src.bot_instance import bot

logger = logging.getLogger(__name__)

# Per-guild playback state.
# Each item in the queue is {title, yt_query, url (may be None), requester}.
# url is resolved lazily just before playback so playlist enqueuing is instant.
queues: dict[int, collections.deque] = {}
now_playing_info: dict[int, dict | None] = {}
_prefetch_tasks: dict[int, asyncio.Task | None] = {}
_player_messages: dict[int, discord.Message | None] = {}
_paused: dict[int, bool] = {}


# ---------------------------------------------------------------------------
# Persistent player embed + buttons
# ---------------------------------------------------------------------------

def _build_embed(guild_id: int) -> discord.Embed:
    track = now_playing_info.get(guild_id)
    q = queues.get(guild_id, collections.deque())
    if not track:
        embed = discord.Embed(description="Nada reproduciendose.", color=0x2B2D31)
        embed.set_footer(text="Usa !play <cancion> para agregar canciones")
        return embed
    paused = _paused.get(guild_id, False)
    embed = discord.Embed(title=track["title"], color=0x1DB954)
    embed.add_field(name="Artista", value=track.get("artist", "Unknown"), inline=True)
    duration = track.get("duration", 0)
    duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "--:--"
    embed.add_field(name="Duracion", value=duration_str, inline=True)
    embed.add_field(name="Pedido por", value=track["requester"], inline=True)
    embed.add_field(name="En cola", value=str(len(q)), inline=True)
    if len(q) > 0:
        next_track = list(q)[0]
        embed.add_field(name="Siguiente", value=next_track.get("title", "?")[:100], inline=False)
    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])
    embed.set_footer(text="\u23f8 En pausa" if paused else "\u25b6 Reproduciendo")
    return embed


class PlayerView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        paused = _paused.get(guild_id, False)
        self.toggle_btn.label = "\u25b6 Reanudar" if paused else "\u23f8 Pausar"
        self.toggle_btn.style = (
            discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
        )

    @discord.ui.button(label="\u23ed Saltar", style=discord.ButtonStyle.primary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await interaction.response.defer()

    @discord.ui.button(label="\u23f8 Pausar", style=discord.ButtonStyle.secondary)
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        gid = interaction.guild.id
        if vc and vc.is_playing():
            vc.pause()
            _paused[gid] = True
        elif vc and vc.is_paused():
            vc.resume()
            _paused[gid] = False
        await interaction.response.defer()
        await update_player_embed(interaction.guild, interaction.channel)

    @discord.ui.button(label="\u23f9 Detener", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        queues[gid] = collections.deque()
        now_playing_info[gid] = None
        _paused[gid] = False
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        await interaction.response.defer()
        await update_player_embed(interaction.guild, interaction.channel)

    @discord.ui.button(label="\U0001f500 Shuffle", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        q = queues.get(gid)
        if q and len(q) > 1:
            items = list(q)
            random.shuffle(items)
            queues[gid] = collections.deque(items)
        await interaction.response.defer()
        await update_player_embed(interaction.guild, interaction.channel)

    @discord.ui.button(label="\U0001f4dc Cola", style=discord.ButtonStyle.secondary, row=1)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        q = queues.get(gid, collections.deque())
        if not q:
            await interaction.response.send_message("La cola esta vacia.", ephemeral=True)
            return
        lines = []
        for i, track in enumerate(list(q)[:15], 1):
            lines.append(f"`{i}.` {track.get('title', '?')}")
        if len(q) > 15:
            lines.append(f"... y {len(q) - 15} mas")
        embed = discord.Embed(
            title=f"Cola de reproduccion ({len(q)} canciones)",
            description="\n".join(lines),
            color=0x1DB954
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def update_player_embed(guild: discord.Guild, channel):
    """Delete the previous player embed and post a fresh one at the bottom of the channel."""
    gid = guild.id
    old = _player_messages.get(gid)
    if old:
        try:
            await old.delete()
        except Exception:
            pass
    track = now_playing_info.get(gid)
    view = PlayerView(gid) if track else discord.ui.View()
    msg = await channel.send(embed=_build_embed(gid), view=view)
    _player_messages[gid] = msg


# ---------------------------------------------------------------------------
# Playback helpers
# ---------------------------------------------------------------------------

async def _resolve_url(track: dict) -> dict | None:
    """Ensure track['url'] is populated. Returns None if YouTube search fails or video is unavailable."""
    if track.get("url"):
        return track
    try:
        yt_info = await search_youtube(track["yt_query"])
    except Exception as exc:
        logger.warning(f"_resolve_url: error buscando '{track['yt_query']}': {exc}")
        return None
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
        _paused[guild.id] = False
        await _update_status(guild, None)
        await update_player_embed(guild, text_channel)
        await asyncio.sleep(1)
        if guild.voice_client:
            await guild.voice_client.disconnect()
        return

    track = q.popleft()

    # Resolve YouTube URL if not yet fetched (lazy playlist items)
    track = await _resolve_url(track)
    if not track:
        await text_channel.send("No se encontro en YouTube, saltando...", delete_after=5)
        await play_next(guild, vc, text_channel)
        return

    now_playing_info[guild.id] = track
    _paused[guild.id] = False

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
        logger.warning(f"play_next: video no disponible '{track['title']}': {e}, saltando...")
        # Evict the stale URL from the search cache so re-search works next time
        from src.youtube import _search_cache
        from src.scoring import _normalize_text as _n
        _search_cache.pop(_n(track.get("yt_query", "")), None)
        await play_next(guild, vc, text_channel)
        return

    def after(error):
        if error:
            logger.error(f"Error en reproduccion: {error}")
        asyncio.run_coroutine_threadsafe(play_next(guild, vc, text_channel), bot.loop)

    vc.play(source, after=after)
    await update_player_embed(guild, text_channel)
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
        # 403 Forbidden is common if bot lacks permissions; log as warning instead of error
        if "403" in str(e):
            logger.warning(f"_update_status: permisos insuficientes para actualizar estado del canal")
        else:
            logger.error(f"_update_status: error al actualizar estado del canal de voz: {e}", exc_info=True)
