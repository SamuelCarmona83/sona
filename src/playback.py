import asyncio
import collections
import logging
import random

import discord

from src.config import FFMPEG_OPTIONS, DJ_ANNOUNCER_ENABLED, DJ_FUN_FACT_INTERVAL
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
_last_cluster: dict[int, str | None] = {}  # DJ announcer: last genre cluster per guild
_prefetch_dj: dict[int, str | None] = {}   # DJ announcer: pre-generated TTS file path
_welcome_active: dict[int, bool] = {}       # Guard: prevent duplicate welcome per guild
_songs_since_comment: dict[int, int] = {}   # DJ fun-fact counter per guild


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
    from src import radio as _radio
    radio_on  = _radio.is_radio_active(guild_id)
    mood      = _radio.get_mood(guild_id)
    radio_txt = " | 📻 Radio ON" if radio_on else ""
    mood_txt  = f" | 🎭 {mood.capitalize()}" if radio_on and mood != "neutral" else ""
    embed.set_footer(text=("\u23f8 En pausa" if paused else "\u25b6 Reproduciendo") + radio_txt + mood_txt)
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
        from src import radio as _radio
        radio_on = _radio.is_radio_active(guild_id)
        self.radio_btn.style = discord.ButtonStyle.success if radio_on else discord.ButtonStyle.secondary
        self.radio_btn.label = "📻 Radio ✓" if radio_on else "📻 Radio"
        mood = _radio.get_mood(guild_id)
        self.mood_btn.label = f"🎭 {mood.capitalize()}"

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

    @discord.ui.button(label="\U0001f4fb Radio", style=discord.ButtonStyle.secondary, row=2)
    async def radio_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        from src import radio as _radio
        was_active = _radio.is_radio_active(gid)
        _radio.set_radio_active(gid, not was_active)
        await interaction.response.defer()
        if not was_active:
            vc = interaction.guild.voice_client
            asyncio.ensure_future(start_radio_with_welcome(interaction.guild, vc, interaction.channel))
        await update_player_embed(interaction.guild, interaction.channel)

    @discord.ui.button(label="\U0001f3ad Mood", style=discord.ButtonStyle.secondary, row=2)
    async def mood_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        from src import radio as _radio
        mood_names = list(_radio.MOODS.keys())
        current = _radio.get_mood(gid)
        idx = mood_names.index(current) if current in mood_names else 0
        next_mood = mood_names[(idx + 1) % len(mood_names)]
        _radio.set_mood(gid, next_mood)
        _radio.flush_radio_tracks(gid)
        await interaction.response.defer()
        if _radio.is_radio_active(gid):
            vc = interaction.guild.voice_client
            if vc:
                asyncio.ensure_future(_radio.fill_radio_queue(interaction.guild, vc, interaction.channel))
        await update_player_embed(interaction.guild, interaction.channel)


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

    if track and view:
        q = queues.get(gid, collections.deque())
        queue_size = len(q)
        
        # Disable/enable buttons based on queue state
        for item in view.children:
            if hasattr(item, 'label'):
                # Skip button: disable if queue is empty
                if "Saltar" in item.label:
                    item.disabled = queue_size == 0
                # Shuffle button: disable if queue has 0 or 1 items
                elif "Shuffle" in item.label:
                    item.disabled = queue_size < 2
                # Queue button: disable if queue is empty
                elif "Cola" in item.label:
                    item.disabled = queue_size == 0

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
    """Resolve the URL of the next queued track in the background.

    Also pre-generates DJ transition TTS if a genre cluster change is detected,
    so play_next has zero delay when the song ends.
    """
    q = queues.get(guild_id)
    if not q:
        return
    next_track = q[0]
    try:
        await _resolve_url(next_track)
    except Exception as e:
        logger.warning(f"_prefetch_next: error prefetching next track: {e}")

    # Pre-generate DJ TTS for transition (runs during current song)
    if not DJ_ANNOUNCER_ENABLED:
        return
    from src import radio as _radio
    try:
        from src.dj_announcer import (
            check_cooldown, generate_dj_comment, generate_fun_fact,
            synthesize_dj_audio,
        )
        songs = _songs_since_comment.get(guild_id, 0)

        # Priority 1: genre transition (radio only)
        if _radio.is_radio_active(guild_id):
            if not check_cooldown(guild_id):
                return
            prev_cluster = _last_cluster.get(guild_id)
            if prev_cluster:
                new_cluster = await _radio.get_track_cluster(next_track)
                if new_cluster and new_cluster != prev_cluster:
                    comment = await generate_dj_comment(
                        prev_cluster, new_cluster,
                        next_track.get("title", ""), next_track.get("artist", "Unknown"),
                    )
                    dj_file = await synthesize_dj_audio(comment, guild_id)
                    if dj_file:
                        _prefetch_dj[guild_id] = dj_file
                        logger.info("_prefetch_next: pre-generated DJ transition TTS for guild=%s", guild_id)
                    return

        # Priority 2: fun fact every N songs (all modes)
        if songs >= DJ_FUN_FACT_INTERVAL - 1:
            cluster = await _radio.get_track_cluster(next_track) if _radio.is_radio_active(guild_id) else None
            comment = await generate_fun_fact(
                next_track.get("title", ""),
                next_track.get("artist", "Unknown"),
                cluster,
            )
            dj_file = await synthesize_dj_audio(comment, guild_id)
            if dj_file:
                _prefetch_dj[guild_id] = dj_file
                logger.info("_prefetch_next: pre-generated DJ fun-fact TTS for guild=%s", guild_id)
    except Exception as exc:
        logger.debug("_prefetch_next: DJ pre-gen failed: %s", exc)


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
        # Radio mode: refill instead of disconnecting
        from src import radio as _radio
        if _radio.is_radio_active(guild.id):
            asyncio.ensure_future(_radio.fill_radio_queue(guild, vc, text_channel))
            return
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

    # Record in radio history (lazy import to avoid circular)
    from src import radio as _radio
    asyncio.ensure_future(_radio.record_played(guild.id, track))

    # If radio is active and queue is running low, trigger a background fill
    from src.config import RADIO_QUEUE_MIN
    if _radio.is_radio_active(guild.id) and len(q) < RADIO_QUEUE_MIN:
        asyncio.ensure_future(_radio.fill_radio_queue(guild, vc, text_channel))

    # Pre-fetch the next track's URL while this one starts playing
    if q:
        _prefetch_tasks[guild.id] = asyncio.create_task(_prefetch_next(guild.id))

    # --- DJ Announcer: use pre-generated TTS or generate on-demand ---
    dj_file: str | None = _prefetch_dj.pop(guild.id, None)
    _songs_since_comment[guild.id] = _songs_since_comment.get(guild.id, 0) + 1

    if DJ_ANNOUNCER_ENABLED:
        try:
            from src.dj_announcer import mark_announced, cleanup_dj_audio

            # --- Genre transition (radio mode only) ---
            if _radio.is_radio_active(guild.id):
                new_cluster = await _radio.get_track_cluster(track)
                if new_cluster:
                    prev_cluster = _last_cluster.get(guild.id)
                    _last_cluster[guild.id] = new_cluster
                    # On-demand transition TTS if not pre-generated
                    if not dj_file and prev_cluster and prev_cluster != new_cluster:
                        from src.dj_announcer import (
                            check_cooldown, generate_dj_comment, synthesize_dj_audio,
                        )
                        if check_cooldown(guild.id):
                            comment = await generate_dj_comment(
                                prev_cluster, new_cluster,
                                track.get("title", ""), track.get("artist", "Unknown"),
                            )
                            dj_file = await synthesize_dj_audio(comment, guild.id)

            # --- Fun fact every N songs (all playback modes) ---
            if not dj_file and _songs_since_comment.get(guild.id, 0) >= DJ_FUN_FACT_INTERVAL:
                from src.dj_announcer import generate_fun_fact, synthesize_dj_audio
                cluster = _last_cluster.get(guild.id)
                comment = await generate_fun_fact(
                    track.get("title", ""), track.get("artist", "Unknown"), cluster,
                )
                dj_file = await synthesize_dj_audio(comment, guild.id)

            if dj_file:
                mark_announced(guild.id)
                _songs_since_comment[guild.id] = 0
        except Exception as exc:
            logger.warning("play_next: DJ announcer error: %s", exc)
            if dj_file:
                cleanup_dj_audio(dj_file)
            dj_file = None

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

    if dj_file:
        # Play TTS announcement first, then chain to the actual song
        from src.dj_announcer import cleanup_dj_audio, get_dj_ffmpeg_options

        def after_dj(error):
            cleanup_dj_audio(dj_file)
            if error:
                logger.warning("play_next: DJ TTS playback error: %s", error)
            # Now play the actual song
            try:
                song_source = discord.FFmpegOpusAudio(track["url"], **FFMPEG_OPTIONS)
                vc.play(song_source, after=after)
            except Exception as e:
                logger.warning("play_next: song source failed after DJ: %s", e)
                asyncio.run_coroutine_threadsafe(play_next(guild, vc, text_channel), bot.loop)

        try:
            dj_source = discord.FFmpegOpusAudio(dj_file, **get_dj_ffmpeg_options())
            vc.play(dj_source, after=after_dj)
        except Exception as e:
            logger.warning("play_next: DJ TTS source failed: %s", e)
            cleanup_dj_audio(dj_file)
            vc.play(source, after=after)
    else:
        vc.play(source, after=after)
    await update_player_embed(guild, text_channel)
    await _update_status(guild, track["title"])


async def start_radio_with_welcome(
    guild: discord.Guild,
    vc: discord.VoiceClient,
    text_channel,
) -> None:
    """Generate welcome TTS in parallel with queue fill, play welcome first."""
    from src import radio as _radio
    gid = guild.id

    # Prevent duplicate concurrent calls per guild
    if _welcome_active.get(gid):
        logger.info("start_radio_with_welcome: already active for guild=%s, skipping", gid)
        return
    _welcome_active[gid] = True

    dj_file: str | None = None

    async def _gen_welcome() -> str | None:
        if not DJ_ANNOUNCER_ENABLED:
            return None
        from src.dj_announcer import generate_welcome_message, synthesize_dj_audio
        mood = _radio.get_mood(gid)
        text = await generate_welcome_message(mood)
        return await synthesize_dj_audio(text, gid)

    async def _fill():
        await _radio.fill_radio_queue(guild, vc, text_channel, auto_play=False)

    try:
        welcome_task = asyncio.create_task(_gen_welcome())
        fill_task = asyncio.create_task(_fill())

        # Wait for welcome with timeout (fill runs in parallel)
        try:
            dj_file = await asyncio.wait_for(asyncio.shield(welcome_task), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("start_radio_with_welcome: welcome gen timed out, skipping")
        except Exception as exc:
            logger.warning("start_radio_with_welcome: welcome gen error: %s", exc)

        if dj_file:
            from src.dj_announcer import cleanup_dj_audio, get_dj_ffmpeg_options

            # If vc is already playing (fill finished first), skip welcome
            if vc.is_playing() or vc.is_paused():
                cleanup_dj_audio(dj_file)
            else:
                done_event = asyncio.Event()

                def after_welcome(error):
                    cleanup_dj_audio(dj_file)
                    if error:
                        logger.warning("start_radio_with_welcome: TTS error: %s", error)
                    bot.loop.call_soon_threadsafe(done_event.set)

                dj_source = discord.FFmpegOpusAudio(dj_file, **get_dj_ffmpeg_options())
                vc.play(dj_source, after=after_welcome)
                logger.info("start_radio_with_welcome: playing welcome for guild=%s", gid)
                try:
                    await asyncio.wait_for(done_event.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    pass

        # Ensure fill completes
        await fill_task

        # Kick playback: start first song (or re-fill if queue still empty)
        if not (vc.is_playing() or vc.is_paused()):
            q = queues.get(gid)
            if q:
                await play_next(guild, vc, text_channel)
            elif _radio.is_radio_active(gid):
                # Fill returned 0 tracks (all deduped) — retry with auto_play
                logger.info("start_radio_with_welcome: queue empty after fill, retrying")
                await _radio.fill_radio_queue(guild, vc, text_channel, auto_play=True)
    except Exception as exc:
        logger.warning("start_radio_with_welcome: error: %s", exc)
    finally:
        _welcome_active.pop(gid, None)


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
    # \U0001f3b5 is the musical note emoji 🎵
    status_text = f"{title}" if title else ""
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
