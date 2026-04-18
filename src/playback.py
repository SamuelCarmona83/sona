import asyncio
import collections
import logging
import random
import time

import discord

from src.config import FFMPEG_OPTIONS, DJ_ANNOUNCER_ENABLED, DJ_FUN_FACT_INTERVAL
from src.dj_announcer import get_buenos_aires_hour
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
_player_channels: dict[int, discord.abc.Messageable | None] = {}
_player_refresh_tasks: dict[int, asyncio.Task | None] = {}
_player_update_locks: dict[int, asyncio.Lock] = {}
_paused: dict[int, bool] = {}
_last_cluster: dict[int, str | None] = {}  # DJ announcer: last genre cluster per guild
_prefetch_dj: dict[int, str | None] = {}   # DJ announcer: pre-generated TTS file path
_welcome_active: dict[int, bool] = {}       # Guard: prevent duplicate welcome per guild
_songs_since_comment: dict[int, int] = {}   # DJ fun-fact counter per guild
_last_embed_fresh: dict[int, float] = {}    # Timestamp of last fresh embed delete/recreate per guild

PLAYER_REFRESH_INTERVAL = 4.0
PLAYER_EMBED_FRESH_INTERVAL = 60.0  # Recreate embed every 60 seconds to bump it down


# ---------------------------------------------------------------------------
# Persistent player embed + buttons
# ---------------------------------------------------------------------------

def _build_v2_payload(guild_id: int) -> dict:
    """Build a Components V2 (IS_COMPONENTS_V2) message payload for the player."""
    from discord.http import Route  # noqa: F401 — imported here to avoid circular at module level
    track = now_playing_info.get(guild_id)
    q = queues.get(guild_id, collections.deque())
    paused = _paused.get(guild_id, False)
    from src import radio as _radio
    radio_on = _radio.is_radio_active(guild_id)
    mood = _radio.get_mood(guild_id)
    queue_size = len(q)
    has_track = bool(track)
    accent = 0x808080 if paused else 0x1DB954

    if not track:
        return {
            "flags": 32768,
            "components": [{
                "type": 17,
                "accent_color": 0x2B2D31,
                "components": [{"type": 10, "content": "Nada reproduciéndose.\n-# Usa `!play <cancion>` para agregar canciones"}]
            }]
        }

    duration = track.get("duration", 0)
    duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "--:--"
    
    # Compact text layout (no status line, radio/mood shown via buttons)
    artist = track.get("artist", "Unknown")
    requester = track["requester"]
    # Avoid duplicating artist if already in title
    title_line = track['title']
    
    # If artist is "Unknown", try to extract from title (format: "Song - Artist" or "Song · Artist")
    if artist == "Unknown" and (" - " in title_line or " · " in title_line):
        parts = title_line.replace(" · ", " - ").split(" - ")
        if len(parts) >= 2:
            artist = parts[-1].strip()
    
    if artist.lower() not in title_line.lower():
        title_line = f"{title_line} · {artist}"
    lines = [
        f"## {title_line}",
        f"{duration_str} · por {requester}",
    ]
    if queue_size > 0:
        next_title = list(q)[0].get("title", "?")[:80]
        lines.append(f"Siguiente: {next_title}")
    content_text = "\n".join(lines)

    children: list[dict] = []
    if track.get("thumbnail"):
        children.append({
            "type": 9,
            "components": [{"type": 10, "content": content_text}],
            "accessory": {"type": 11, "media": {"url": track["thumbnail"]}}
        })
    else:
        children.append({"type": 10, "content": content_text})

    children.append({"type": 14, "divider": True, "spacing": 1})

    # Row 1: Playback + Queue controls (left-aligned)
    children.append({"type": 1, "components": [
        {"type": 2, "custom_id": "player_toggle", "label": "▶" if paused else "⏸", "style": 3 if paused else 2},
        {"type": 2, "custom_id": "player_skip",   "label": "⏭", "style": 1, "disabled": not has_track and queue_size == 0},
        {"type": 2, "custom_id": "player_stop",   "label": "⏹", "style": 4},
        {"type": 2, "custom_id": "player_shuffle", "label": "⇄", "style": 2, "disabled": queue_size < 2},
        {"type": 2, "custom_id": "player_queue",   "label": "≡", "style": 2},
    ]})
    
    # Row 2: Radio + Mood + Like
    from src import likes as _likes_mod
    guild_likes = _likes_mod._likes.get(guild_id, {})
    like_count = sum(
        1 for user_likes in guild_likes.values()
        if track and any(
            t["track_id"] == (_likes_mod._track_id(track)) for t in user_likes
        )
    )
    like_label = f"❤️ {like_count}" if like_count > 0 else "🤍"
    like_style = 4 if like_count > 0 else 2  # red if any likes, grey otherwise
    children.append({"type": 1, "components": [
        {"type": 2, "custom_id": "player_radio", "label": "📻" + ("✓" if radio_on else ""), "style": 3 if radio_on else 2},
        {"type": 2, "custom_id": "player_mood",  "label": "🎭 Mood", "style": 2},
        {"type": 2, "custom_id": "player_like",  "label": like_label, "style": like_style, "disabled": not has_track},
    ]})

    return {
        "flags": 32768,
        "components": [{"type": 17, "accent_color": accent, "components": children}]
    }


def _resolve_interaction_guild(interaction: discord.Interaction, fallback_gid: int = 0) -> tuple[discord.Guild | None, int]:
    gid = interaction.guild_id or (interaction.guild.id if interaction.guild else fallback_gid)
    guild = interaction.guild or (bot.get_guild(gid) if gid else None)
    return guild, gid


async def _player_refresh_loop(guild_id: int):
    try:
        while True:
            await asyncio.sleep(PLAYER_REFRESH_INTERVAL)
            guild = bot.get_guild(guild_id)
            channel = _player_channels.get(guild_id)
            vc = guild.voice_client if guild else None
            active = bool(now_playing_info.get(guild_id) or queues.get(guild_id))
            if not guild or channel is None or not active and not (vc and (vc.is_playing() or vc.is_paused())):
                break
            try:
                # Check if time for fresh recreate (every ~60 seconds to bump embed down)
                last_fresh = _last_embed_fresh.get(guild_id, 0)
                if time.time() - last_fresh >= PLAYER_EMBED_FRESH_INTERVAL:
                    await refresh_player_embed_fresh(guild, channel)
                else:
                    await update_player_embed(guild, channel)
            except Exception as exc:
                logger.debug("_player_refresh_loop: embed refresh failed for guild=%s: %s", guild_id, exc)
    finally:
        _player_refresh_tasks.pop(guild_id, None)


def _ensure_player_refresh(guild: discord.Guild, channel) -> None:
    _player_channels[guild.id] = channel
    task = _player_refresh_tasks.get(guild.id)
    if task and not task.done():
        return
    _player_refresh_tasks[guild.id] = asyncio.create_task(_player_refresh_loop(guild.id))


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

    @discord.ui.button(label="\u23f8 Pausar", style=discord.ButtonStyle.secondary, row=0, custom_id="player_toggle")
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, gid = _resolve_interaction_guild(interaction, self.guild_id)
        if not guild:
            await interaction.response.send_message("No pude encontrar este servidor.", ephemeral=True)
            return
        self.guild_id = gid
        vc = guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            _paused[gid] = True
        elif vc and vc.is_paused():
            vc.resume()
            _paused[gid] = False
        await interaction.response.defer()
        await refresh_player_embed_fresh(guild, interaction.channel)

    @discord.ui.button(label="\u23ed Saltar", style=discord.ButtonStyle.primary, row=0, custom_id="player_skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, gid = _resolve_interaction_guild(interaction, self.guild_id)
        if not guild:
            await interaction.response.send_message("No pude encontrar este servidor.", ephemeral=True)
            return
        self.guild_id = gid
        vc = guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Nada para saltar ahora mismo.", ephemeral=True)

    @discord.ui.button(label="\u23f9 Detener", style=discord.ButtonStyle.danger, row=0, custom_id="player_stop")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, gid = _resolve_interaction_guild(interaction, self.guild_id)
        if not guild:
            await interaction.response.send_message("No pude encontrar este servidor.", ephemeral=True)
            return
        self.guild_id = gid
        queues[gid] = collections.deque()
        now_playing_info[gid] = None
        _paused[gid] = False
        vc = guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        await interaction.response.defer()
        await refresh_player_embed_fresh(guild, interaction.channel)

    @discord.ui.button(label="⇄", style=discord.ButtonStyle.secondary, row=0, custom_id="player_shuffle")
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, gid = _resolve_interaction_guild(interaction, self.guild_id)
        if not guild:
            await interaction.response.send_message("No pude encontrar este servidor.", ephemeral=True)
            return
        self.guild_id = gid
        q = queues.get(gid)
        if q and len(q) > 1:
            items = list(q)
            random.shuffle(items)
            queues[gid] = collections.deque(items)
        await interaction.response.defer()
        await refresh_player_embed_fresh(guild, interaction.channel)

    @discord.ui.button(label="≡", style=discord.ButtonStyle.secondary, row=0, custom_id="player_queue")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, gid = _resolve_interaction_guild(interaction, self.guild_id)
        if not gid:
            await interaction.response.send_message("No pude encontrar este servidor.", ephemeral=True)
            return
        if guild:
            self.guild_id = gid
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

    @discord.ui.button(label="🤍", style=discord.ButtonStyle.secondary, row=1, custom_id="player_like")
    async def like_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, gid = _resolve_interaction_guild(interaction, self.guild_id)
        if not guild:
            await interaction.response.send_message("No pude encontrar este servidor.", ephemeral=True)
            return
        self.guild_id = gid
        track = now_playing_info.get(gid)
        if not track:
            await interaction.response.send_message("Nada reproduciéndose ahora.", ephemeral=True)
            return
        from src import likes as _likes_mod
        liked = _likes_mod.toggle_like(gid, interaction.user.id, track)
        action = "❤️ Le diste like" if liked else "💔 Quitaste el like de"
        await interaction.response.send_message(
            f"{action} **{track.get('title', '?')}**", ephemeral=True
        )
        await update_player_embed(guild, interaction.channel)

    @discord.ui.button(label="\U0001f4fb Radio", style=discord.ButtonStyle.secondary, row=1, custom_id="player_radio")
    async def radio_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, gid = _resolve_interaction_guild(interaction, self.guild_id)
        if not guild:
            await interaction.response.send_message("No pude encontrar este servidor.", ephemeral=True)
            return
        self.guild_id = gid
        from src import radio as _radio
        was_active = _radio.is_radio_active(gid)
        _radio.set_radio_active(gid, not was_active)
        await interaction.response.defer()
        if not was_active:
            vc = guild.voice_client
            asyncio.ensure_future(start_radio_with_welcome(guild, vc, interaction.channel))
        await refresh_player_embed_fresh(guild, interaction.channel)

    @discord.ui.button(label="\U0001f3ad Mood", style=discord.ButtonStyle.secondary, row=1, custom_id="player_mood")
    async def mood_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, gid = _resolve_interaction_guild(interaction, self.guild_id)
        if not guild:
            await interaction.response.send_message("No pude encontrar este servidor.", ephemeral=True)
            return
        self.guild_id = gid
        from src import radio as _radio
        
        # Get available moods
        mood_names = list(_radio.MOODS.keys())
        current = _radio.get_mood(gid)
        
        # Build radio options for modal
        options = [
            discord.SelectOption(label=m.capitalize(), value=m, default=(m == current))
            for m in mood_names
        ]
        
        # State for tracking selected mood
        selected_mood = [current]  # Use list to allow mutation in nested class
        
        # Create select menu for mood
        class MoodSelect(discord.ui.Select):
            def __init__(self, parent_view):
                super().__init__(
                    placeholder="Elige un mood...",
                    min_values=1,
                    max_values=1,
                    options=options,
                    custom_id="mood_select"
                )
                self.parent_view = parent_view
            
            async def callback(self, select_interaction: discord.Interaction):
                selected_mood[0] = self.values[0]
                await select_interaction.response.defer()
        
        # Create modal view with select + buttons
        class MoodModalView(discord.ui.View):
            def __init__(self):
                super().__init__()
                self.add_item(MoodSelect(self))
            
            @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.success, row=1)
            async def confirm_btn(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
                _radio.set_mood(gid, selected_mood[0])
                _radio.flush_radio_tracks(gid)
                await btn_interaction.response.defer()
                if _radio.is_radio_active(gid):
                    vc = guild.voice_client
                    if vc:
                        asyncio.ensure_future(_radio.fill_radio_queue(guild, vc, interaction.channel))
                await update_player_embed(guild, interaction.channel)
                await btn_interaction.delete_original_response()
            
            @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, row=1)
            async def cancel_btn(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
                await btn_interaction.response.defer()
                await btn_interaction.delete_original_response()
        
        # Send as followup with select menu + buttons
        view = MoodModalView()
        await interaction.response.send_message(
            "🎭 **Selecciona un Mood:**",
            view=view,
            ephemeral=True
        )


async def update_player_embed(guild: discord.Guild, channel):
    """Upsert the player message and keep button state fresh."""
    from discord.http import Route
    gid = guild.id
    _player_channels[gid] = channel
    _ensure_player_refresh(guild, channel)
    lock = _player_update_locks.setdefault(gid, asyncio.Lock())

    async with lock:
        payload = _build_v2_payload(gid)
        old = _player_messages.get(gid)

        if old:
            try:
                route = Route(
                    "PATCH",
                    "/channels/{channel_id}/messages/{message_id}",
                    channel_id=old.channel.id,
                    message_id=old.id,
                )
                data = await bot.http.request(route, json=payload)
                msg = discord.Message(state=bot._connection, channel=old.channel, data=data)
                _player_messages[gid] = msg
                return
            except Exception:
                logger.debug("update_player_embed: patch failed for guild=%s; creating fresh message", gid)

        route = Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
        data = await bot.http.request(route, json=payload)
        msg = discord.Message(state=bot._connection, channel=channel, data=data)
        _player_messages[gid] = msg


async def refresh_player_embed_fresh(guild: discord.Guild, channel):
    """Delete old player message and create a fresh one (bumps it down in chat)."""
    from discord.http import Route
    gid = guild.id
    _player_channels[gid] = channel
    _ensure_player_refresh(guild, channel)
    lock = _player_update_locks.setdefault(gid, asyncio.Lock())

    async with lock:
        # Delete old message if exists
        old = _player_messages.get(gid)
        if old:
            try:
                await old.delete()
            except Exception as exc:
                logger.debug("refresh_player_embed_fresh: delete failed for guild=%s: %s", gid, exc)
            _player_messages[gid] = None

        # Create fresh message
        payload = _build_v2_payload(gid)
        try:
            route = Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
            data = await bot.http.request(route, json=payload)
            msg = discord.Message(state=bot._connection, channel=channel, data=data)
            _player_messages[gid] = msg
            _last_embed_fresh[gid] = time.time()
            logger.debug("refresh_player_embed_fresh: recreated embed for guild=%s", gid)
        except Exception as exc:
            logger.error("refresh_player_embed_fresh: failed to create new message for guild=%s: %s", gid, exc)


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
        hour = get_buenos_aires_hour()
        # Priority 1: if current playing track is a user pick, generate fun fact
        # about it — plays before the next track as a bridge back to radio
        current = now_playing_info.get(guild_id)
        if current and current.get("requester") != "\U0001f4fb Radio":
            comment = await generate_fun_fact(
                current.get("title", ""),
                current.get("artist", "Unknown"),
                _last_cluster.get(guild_id),
                hour,
                artist_id=current.get("artist_id"),
            )
            dj_file = await synthesize_dj_audio(comment, guild_id)
            if dj_file:
                _prefetch_dj[guild_id] = dj_file
                logger.info("_prefetch_next: pre-generated user-pick fun-fact TTS for guild=%s", guild_id)
            return

        songs = _songs_since_comment.get(guild_id, 0)

        # Priority 2: genre transition (radio only) — only if cooldown has passed
        if _radio.is_radio_active(guild_id) and check_cooldown(guild_id):
            prev_cluster = _last_cluster.get(guild_id)
            if prev_cluster:
                new_cluster = await _radio.get_track_cluster(next_track)
                if new_cluster and new_cluster != prev_cluster:
                    comment = await generate_dj_comment(
                        prev_cluster, new_cluster,
                        next_track.get("title", ""), next_track.get("artist", "Unknown"),
                        hour,
                    )
                    dj_file = await synthesize_dj_audio(comment, guild_id)
                    if dj_file:
                        _prefetch_dj[guild_id] = dj_file
                        logger.info("_prefetch_next: pre-generated DJ transition TTS for guild=%s", guild_id)
                    return  # transition wins; skip fun fact

        # Priority 2: fun fact every N songs (all modes) — never blocked by cooldown
        if songs >= DJ_FUN_FACT_INTERVAL - 1:
            cluster = await _radio.get_track_cluster(next_track) if _radio.is_radio_active(guild_id) else None
            comment = await generate_fun_fact(
                next_track.get("title", ""),
                next_track.get("artist", "Unknown"),
                cluster,
                hour,
                artist_id=next_track.get("artist_id"),
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
    is_user_pick = track.get("requester") != "\U0001f4fb Radio"

    if DJ_ANNOUNCER_ENABLED and not is_user_pick:
        # User picks: no TTS before their song — fun fact generated during playback instead
        try:
            from src.dj_announcer import mark_announced, cleanup_dj_audio

            # --- Genre transition (radio mode only) ---
            if _radio.is_radio_active(guild.id):
                new_cluster = await _radio.get_track_cluster(track)
                if new_cluster:
                    prev_cluster = _last_cluster.get(guild.id)
                    _last_cluster[guild.id] = new_cluster
                    if not dj_file and prev_cluster and prev_cluster != new_cluster:
                        from src.dj_announcer import (
                            check_cooldown, generate_dj_comment, synthesize_dj_audio,
                        )
                        if check_cooldown(guild.id):
                            hour = get_buenos_aires_hour()
                            comment = await generate_dj_comment(
                                prev_cluster, new_cluster,
                                track.get("title", ""), track.get("artist", "Unknown"),
                                hour,
                            )
                            dj_file = await synthesize_dj_audio(comment, guild.id)

            # --- Fun fact every N songs (radio mode) ---
            if not dj_file and _songs_since_comment.get(guild.id, 0) >= DJ_FUN_FACT_INTERVAL:
                from src.dj_announcer import generate_fun_fact, synthesize_dj_audio
                hour = get_buenos_aires_hour()
                cluster = _last_cluster.get(guild.id)
                comment = await generate_fun_fact(
                    track.get("title", ""), track.get("artist", "Unknown"), cluster, hour,
                    artist_id=track.get("artist_id"),
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
    elif dj_file:
        # Discard any stale pre-gen that was queued before a user pick
        from src.dj_announcer import cleanup_dj_audio
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
        hour = get_buenos_aires_hour()
        text = await generate_welcome_message(mood, hour)
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
