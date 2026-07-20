import asyncio
import collections
import logging
import random
import time
from dataclasses import dataclass, field


import discord

from src.radio_browser import top_stations
from src.config import (
    DJ_ANNOUNCER_ENABLED,
    DJ_FUN_FACT_INTERVAL_TRACKS,
    FFMPEG_LOCAL_OPTIONS,
    FFMPEG_OPTIONS,
    RADIO_QUEUE_REFILL_THRESHOLD,
    RADIO_QUEUE_TARGET_SIZE,
    RADIO_REQUESTER_LABEL,
)
from src.dj_announcer import get_buenos_aires_hour
from src.youtube import search_youtube, is_youtube_rate_limited
from src.library import resolve_local_track, record_play, enqueue_download
from src.bot_instance import bot

logger = logging.getLogger(__name__)

PLAYER_REFRESH_INTERVAL = 4.0
PLAYER_EMBED_RECREATE_INTERVAL_SEC = 60.0


@dataclass
class GuildPlaybackSession:
    queue: collections.deque = field(default_factory=collections.deque)
    now_playing: dict | None = None
    paused: bool = False
    prefetch_task: asyncio.Task | None = None
    player_message: discord.Message | None = None
    player_channel: discord.abc.Messageable | None = None
    player_refresh_task: asyncio.Task | None = None
    player_update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dj_last_genre_cluster: str | None = None
    prefetched_dj_audio: str | None = None
    radio_welcome_in_progress: bool = False
    tracks_since_dj_comment: int = 0
    player_embed_recreated_at: float = 0.0
    last_rate_limit_notify: float = 0.0
    playback_busy: bool = False

    def embed_recreate_due(self) -> bool:
        return time.time() - self.player_embed_recreated_at >= PLAYER_EMBED_RECREATE_INTERVAL_SEC

    def cancel_prefetch(self) -> None:
        task = self.prefetch_task
        self.prefetch_task = None
        if task and not task.done():
            task.cancel()

    def reset_playback(self) -> None:
        self.queue = collections.deque()
        self.now_playing = None
        self.paused = False


_sessions: dict[int, GuildPlaybackSession] = {}


def guild_session(guild_id: int) -> GuildPlaybackSession:
    return _sessions.setdefault(guild_id, GuildPlaybackSession())


class _SessionField:
    def __init__(self, attr: str):
        self._attr = attr

    def get(self, guild_id: int, default=None):
        session = _sessions.get(guild_id)
        if session is None:
            return default
        value = getattr(session, self._attr)
        return default if value is None and default is not None else value

    def __getitem__(self, guild_id: int):
        return getattr(guild_session(guild_id), self._attr)

    def __contains__(self, guild_id: int) -> bool:
        return guild_id in _sessions

    def __setitem__(self, guild_id: int, value) -> None:
        setattr(guild_session(guild_id), self._attr, value)

    def setdefault(self, guild_id: int, default=None):
        session = guild_session(guild_id)
        value = getattr(session, self._attr)
        if value is None and default is not None:
            setattr(session, self._attr, default)
            value = default
        return value

    def pop(self, guild_id: int, *default):
        session = _sessions.get(guild_id)
        if session is None:
            if default:
                return default[0]
            raise KeyError(guild_id)
        value = getattr(session, self._attr)
        reset = _SESSION_FIELD_RESET.get(self._attr)
        if reset is not None:
            setattr(session, self._attr, reset() if callable(reset) else reset)
        if value is None and default:
            return default[0]
        return value


_SESSION_FIELD_RESET = {
    "prefetch_task": None,
    "prefetched_dj_audio": None,
    "dj_last_genre_cluster": None,
    "player_message": None,
    "player_refresh_task": None,
    "radio_welcome_in_progress": False,
    "tracks_since_dj_comment": 0,
    "player_embed_recreated_at": 0.0,
    "last_rate_limit_notify": 0.0,
}


queues = _SessionField("queue")
now_playing_info = _SessionField("now_playing")
_prefetch_tasks = _SessionField("prefetch_task")
_player_messages = _SessionField("player_message")
_player_channels = _SessionField("player_channel")
_player_refresh_tasks = _SessionField("player_refresh_task")
_player_update_locks = _SessionField("player_update_lock")
_paused = _SessionField("paused")
_dj_last_genre_cluster = _SessionField("dj_last_genre_cluster")
_prefetched_dj_audio = _SessionField("prefetched_dj_audio")
_prefetch_dj = _prefetched_dj_audio
_last_cluster = _dj_last_genre_cluster
_radio_welcome_in_progress = _SessionField("radio_welcome_in_progress")
_tracks_since_dj_comment = _SessionField("tracks_since_dj_comment")
_player_embed_recreated_at = _SessionField("player_embed_recreated_at")
_last_rate_limit_notify = _SessionField("last_rate_limit_notify")


def _is_automated_radio_requester(requester: str) -> bool:
    return requester == RADIO_REQUESTER_LABEL


def _is_user_requested_track(track: dict) -> bool:
    return not _is_automated_radio_requester(track.get("requester", ""))


def _resolve_display_artist(title: str, artist: str) -> str:
    if artist != "Unknown":
        return artist
    if " - " in title or " · " in title:
        parts = title.replace(" · ", " - ").split(" - ")
        if len(parts) >= 2:
            return parts[0].strip()  # artist is first part in "Artist - Title"
    return artist


def _format_player_title_line(track: dict) -> str:
    title = track["title"]
    artist = _resolve_display_artist(title, track.get("artist", "Unknown"))
    if artist.lower() not in title.lower():
        return f"{title} · {artist}"
    return title


def _build_v2_payload(guild_id: int) -> dict:
    from discord.http import Route  # noqa: F401
    session = guild_session(guild_id)
    track = session.now_playing
    q = session.queue
    paused = session.paused
    from src import radio as _radio
    radio_on = _radio.is_radio_active(guild_id)
    mood = _radio.get_mood(guild_id)
    queue_size = len(q)
    has_track = bool(track)
    accent = 0x808080 if paused else 0x1DB954

    if track:
        # Always overlay the latest artwork from the library index.
        # This ensures that if enrichment (Genius/Spotify cover) happened after
        # play started (background), the embed will use the good 1:1 artwork
        # instead of the original YT thumbnail.
        try:
            from src.library import get_entry, get_best_artwork, track_id as _lib_track_id
            # Shazam covers win over Genius/library enrichment
            if not track.get("recognized_cover_url") and not track.get("prefer_shazam_cover"):
                ttid = track.get("track_id") or track.get("video_id")
                if not ttid and (track.get("spotify_id") or track.get("video_id")):
                    ttid = _lib_track_id(track)
                if ttid:
                    entry = get_entry(ttid)
                    if entry:
                        best = get_best_artwork(ttid, entry)
                        if best:
                            track = dict(track)
                            track["cover_url"] = best
                            session.now_playing = track
        except Exception:
            pass  # non fatal, use whatever was in track

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
    title_line = _format_player_title_line(track)
    lines = [
        f"## {title_line}",
        f"{duration_str} · por {track['requester']}",
    ]
    if track.get("is_radio_stream"):
        rec_title = (track.get("recognized_title") or "").strip()
        rec_artist = (track.get("recognized_artist") or "").strip()
        if rec_title:
            if rec_artist:
                lines.insert(1, f"♪ {rec_artist} — {rec_title}")
            else:
                lines.insert(1, f"♪ {rec_title}")
    if queue_size > 0:
        next_title = list(q)[0].get("title", "?")[:80]
        lines.append(f"Siguiente: {next_title}")
    content_text = "\n".join(lines)

    children: list[dict] = []
    art = (
        track.get("recognized_cover_url")
        or track.get("cover_url")
        or track.get("thumbnail")
        or ""
    )
    if art:
        children.append({
            "type": 9,
            "components": [{"type": 10, "content": content_text}],
            "accessory": {"type": 11, "media": {"url": art}}
        })
    else:
        children.append({"type": 10, "content": content_text})

    children.append({"type": 14, "divider": True, "spacing": 1})

    children.append({"type": 1, "components": [
        {"type": 2, "custom_id": "player_toggle", "label": "▶" if paused else "⏸", "style": 3 if paused else 2},
        {"type": 2, "custom_id": "player_skip",   "label": "⏭", "style": 1, "disabled": not has_track and queue_size == 0},
        {"type": 2, "custom_id": "player_stop",   "label": "⏹", "style": 4},
        {"type": 2, "custom_id": "player_shuffle", "label": "⇄", "style": 2, "disabled": queue_size < 2},
        {"type": 2, "custom_id": "player_queue",   "label": "≡", "style": 2},
    ]})

    from src import likes as _likes_mod
    from src.library import track_id as library_track_id

    guild_likes = _likes_mod._likes.get(guild_id, {})
    current_tid = library_track_id(track) if track else None
    like_count = sum(
        1 for user_likes in guild_likes.values()
        if track and any(t["track_id"] == current_tid for t in user_likes)
    )
    like_label = f"❤️ {like_count}" if like_count > 0 else "🤍"
    like_style = 4 if like_count > 0 else 2  # red if any likes, grey otherwise
    if track and track.get("is_radio_stream"):
        from src.fm_favorites import favorite_id_for_track

        favorite_id = favorite_id_for_track(guild_id, track)
        like_label = f"❤️ {favorite_id}" if favorite_id is not None else "🤍"
        like_style = 4 if favorite_id is not None else 2
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
    session = guild_session(guild_id)
    try:
        while True:
            await asyncio.sleep(PLAYER_REFRESH_INTERVAL)
            guild = bot.get_guild(guild_id)
            channel = session.player_channel
            vc = guild.voice_client if guild else None
            active = bool(session.now_playing or session.queue)
            if not guild or channel is None or not active and not (vc and (vc.is_playing() or vc.is_paused())):
                break
            try:
                if session.embed_recreate_due():
                    await refresh_player_embed_fresh(guild, channel)
                else:
                    await update_player_embed(guild, channel)
            except Exception as exc:
                logger.debug("_player_refresh_loop: embed refresh failed for guild=%s: %s", guild_id, exc)
    finally:
        session.player_refresh_task = None


def _ensure_player_refresh(guild: discord.Guild, channel) -> None:
    session = guild_session(guild.id)
    session.player_channel = channel
    task = session.player_refresh_task
    if task and not task.done():
        return
    session.player_refresh_task = asyncio.create_task(_player_refresh_loop(guild.id))


class PlayerView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        paused = guild_session(guild_id).paused
        self.toggle_btn.label = "\u25b6 Reanudar" if paused else "\u23f8 Pausar"
        self.toggle_btn.style = (
            discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
        )
        from src import radio as _radio
        radio_on = _radio.is_radio_active(guild_id)
        self.radio_btn.style = discord.ButtonStyle.success if radio_on else discord.ButtonStyle.secondary
        self.radio_btn.label = f"{RADIO_REQUESTER_LABEL} ✓" if radio_on else RADIO_REQUESTER_LABEL
        mood = _radio.get_mood(guild_id)
        self.mood_btn.label = f"🎭 {mood.capitalize()}"

    @discord.ui.button(label="\u23f8 Pausar", style=discord.ButtonStyle.secondary, row=0, custom_id="player_toggle")
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, gid = _resolve_interaction_guild(interaction, self.guild_id)
        if not guild:
            await interaction.response.send_message("No pude encontrar este servidor.", ephemeral=True)
            return
        self.guild_id = gid
        session = guild_session(gid)
        vc = guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            session.paused = True
        elif vc and vc.is_paused():
            vc.resume()
            session.paused = False
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
        stop_fm_recognition(gid)
        guild_session(gid).reset_playback()
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
        session = guild_session(gid)
        if len(session.queue) > 1:
            items = list(session.queue)
            random.shuffle(items)
            session.queue = collections.deque(items)
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
        session = guild_session(gid)
        now_playing = session.now_playing
        q = guild_session(gid).queue
        if now_playing and now_playing.get("is_radio_stream"):
            from src.fm_favorites import list_favorites

            favorites = list_favorites(gid)
            if not favorites:
                await interaction.response.send_message(
                    "No hay favoritos FM guardados. Usa ❤️ mientras suena una estación.",
                    ephemeral=True,
                )
                return

            lines = []
            for fav_id, station in favorites[:30]:
                name = (station.get("name") or "FM Station")[:72]
                lines.append(f"`{fav_id}` — {name}")

            embed = discord.Embed(
                title="📻 Favoritos FM",
                description=(
                    "Usa `!fm id:<ID>` para cambiar con precisión.\n\n"
                    + "\n".join(lines)
                ),
                color=0x1DB954,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

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
        track = guild_session(gid).now_playing
        if not track:
            await interaction.response.send_message("Nada reproduciéndose ahora.", ephemeral=True)
            return

        if track.get("is_radio_stream"):
            from src.fm_favorites import toggle_favorite

            is_favorite, favorite_id = toggle_favorite(gid, track)
            if is_favorite:
                text = f"❤️ Guardada en favoritos FM con ID `{favorite_id}`"
            else:
                text = f"🗑️ Quitada de favoritos FM (ID `{favorite_id}`)"
            await interaction.response.send_message(text, ephemeral=True)
            await update_player_embed(guild, interaction.channel)
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

        mood_names = list(_radio.MOODS.keys())
        current = _radio.get_mood(gid)
        # Discord select max 25 options; labels ≤100 chars
        options = []
        for m in mood_names[:25]:
            kwargs = {
                "label": (m if len(m) <= 25 else m[:22] + "…"),
                "value": m,
                "default": (m == current),
            }
            if m == "rock-radio":
                kwargs["description"] = "FM→YouTube limpio"
            options.append(discord.SelectOption(**kwargs))
        selected_mood = [current]

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

        view = MoodModalView()
        await interaction.response.send_message(
            "🎭 **Selecciona un Mood:**",
            view=view,
            ephemeral=True
        )


async def update_player_embed(guild: discord.Guild, channel):
    from discord.http import Route
    session = guild_session(guild.id)
    session.player_channel = channel
    _ensure_player_refresh(guild, channel)

    async with session.player_update_lock:
        payload = _build_v2_payload(guild.id)
        old = session.player_message

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
                session.player_message = msg
                return
            except Exception:
                logger.debug("update_player_embed: patch failed for guild=%s; creating fresh message", guild.id)

        route = Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
        data = await bot.http.request(route, json=payload)
        msg = discord.Message(state=bot._connection, channel=channel, data=data)
        session.player_message = msg


async def refresh_player_embed_fresh(guild: discord.Guild, channel):
    from discord.http import Route
    session = guild_session(guild.id)
    session.player_channel = channel
    _ensure_player_refresh(guild, channel)

    async with session.player_update_lock:
        old = session.player_message
        if old:
            try:
                await old.delete()
            except Exception as exc:
                logger.debug("refresh_player_embed_fresh: delete failed for guild=%s: %s", guild.id, exc)
            session.player_message = None

        payload = _build_v2_payload(guild.id)
        try:
            route = Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
            data = await bot.http.request(route, json=payload)
            msg = discord.Message(state=bot._connection, channel=channel, data=data)
            session.player_message = msg
            session.player_embed_recreated_at = time.time()
            logger.debug("refresh_player_embed_fresh: recreated embed for guild=%s", guild.id)
        except Exception as exc:
            logger.error("refresh_player_embed_fresh: failed to create new message for guild=%s: %s", guild.id, exc)


def _cancel_guild_prefetch(guild_id: int) -> None:
    guild_session(guild_id).cancel_prefetch()


async def _handle_empty_playback_queue(
    guild: discord.Guild,
    vc: discord.VoiceClient,
    text_channel,
) -> None:
    from src import radio as _radio

    stop_fm_recognition(guild.id)
    if _radio.is_radio_active(guild.id):
        asyncio.ensure_future(_radio.fill_radio_queue(guild, vc, text_channel))
        return
    await asyncio.sleep(1)
    if guild.voice_client:
        await guild.voice_client.disconnect()


async def _ensure_playback_continues(guild: discord.Guild, vc: discord.VoiceClient, text_channel) -> None:
    """Deferred helper used when playback_busy to let current transition settle then retry play_next."""
    await asyncio.sleep(0.05)
    try:
        await play_next(guild, vc, text_channel)
    except Exception:
        pass


async def _resolve_url(track: dict) -> dict | None:
    if track.get("is_radio_stream"):
        if track.get("url"):
            logger.info("playback: usando stream en vivo para '%s'", track.get("title", "?"))
            return track
        return None

    # Always prefer local cached file if available (for library use, offline, etc.)
    # This must be first so that even if the incoming track has a YT stream URL (from search),
    # we override with local file path if the song is already in the library.
    local = resolve_local_track(track)
    if local:
        return local

    if track.get("url") and not is_youtube_rate_limited():
        logger.info(
            "playback: usando stream remoto de YT para '%s' (no había copia local en librería para este tid/video_id)",
            track.get("title", "?"),
        )
        return track

    if is_youtube_rate_limited():
        return None

    try:
        yt_info = await search_youtube(track["yt_query"])
    except Exception as exc:
        logger.warning(f"_resolve_url: error buscando '{track['yt_query']}': {exc}")
        return None
    if not yt_info:
        return None
    if not yt_info.get("url"):
        local = resolve_local_track(track)
        if local:
            return local
        return None
    track["url"] = yt_info["url"]
    track["title"] = yt_info["title"]
    if yt_info.get("video_id"):
        track["video_id"] = yt_info["video_id"]
    if yt_info.get("webpage_url"):
        track["webpage_url"] = yt_info["webpage_url"]
    return track


async def _prefetch_dj_audio_for_up_next(guild_id: int, next_track: dict) -> None:
    if not DJ_ANNOUNCER_ENABLED:
        return
    session = guild_session(guild_id)
    from src import radio as _radio
    try:
        from src.dj_announcer import (
            check_cooldown,
            generate_dj_comment,
            generate_fun_fact,
            synthesize_dj_audio,
        )

        hour = get_buenos_aires_hour()
        current = session.now_playing
        if current and _is_user_requested_track(current):
            comment = await generate_fun_fact(
                current.get("title", ""),
                current.get("artist", "Unknown"),
                session.dj_last_genre_cluster,
                hour,
                artist_id=current.get("artist_id"),
            )
            dj_file = await synthesize_dj_audio(comment, guild_id)
            if dj_file:
                session.prefetched_dj_audio = dj_file
                logger.info("_prefetch_next: pre-generated user-pick fun-fact TTS for guild=%s", guild_id)
            return

        tracks_since_comment = session.tracks_since_dj_comment
        if _radio.is_radio_active(guild_id) and check_cooldown(guild_id):
            prev_cluster = session.dj_last_genre_cluster
            if prev_cluster:
                new_cluster = await _radio.get_track_cluster(next_track)
                if new_cluster and new_cluster != prev_cluster:
                    comment = await generate_dj_comment(
                        prev_cluster,
                        new_cluster,
                        next_track.get("title", ""),
                        next_track.get("artist", "Unknown"),
                        hour,
                    )
                    dj_file = await synthesize_dj_audio(comment, guild_id)
                    if dj_file:
                        session.prefetched_dj_audio = dj_file
                        logger.info("_prefetch_next: pre-generated DJ transition TTS for guild=%s", guild_id)
                    return

        if tracks_since_comment >= DJ_FUN_FACT_INTERVAL_TRACKS - 1:
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
                session.prefetched_dj_audio = dj_file
                logger.info("_prefetch_next: pre-generated DJ fun-fact TTS for guild=%s", guild_id)
    except Exception as exc:
        logger.debug("_prefetch_next: DJ pre-gen failed: %s", exc)


async def _prefetch_next(guild_id: int):
    session = guild_session(guild_id)
    if not session.queue:
        return
    next_track = session.queue[0]
    try:
        await _resolve_url(next_track)
    except Exception as e:
        logger.warning(f"_prefetch_next: error prefetching next track: {e}")

    await _prefetch_dj_audio_for_up_next(guild_id, next_track)


_last_rate_limit_notify: dict[int, float] = {}
_RATE_LIMIT_NOTIFY_COOLDOWN = 3600
_RATE_LIMIT_MESSAGE = (
    ":warning: El bot ha sido temporalmente bloqueado por YouTube por exceder el límite de "
    "búsquedas/descargas. Debes esperar hasta 1 hora para que se levante el bloqueo. "
    "Intenta más tarde o reduce la frecuencia de búsquedas."
)


async def maybe_notify_rate_limited(guild_id: int, text_channel) -> None:
    if not is_youtube_rate_limited():
        return
    session = guild_session(guild_id)
    now = time.time()
    if now - session.last_rate_limit_notify <= _RATE_LIMIT_NOTIFY_COOLDOWN:
        return
    await text_channel.send(_RATE_LIMIT_MESSAGE)
    session.last_rate_limit_notify = now


async def _resolve_dj_audio_before_track(
    guild_id: int,
    track: dict,
    prefetched_audio: str | None,
) -> str | None:
    if not DJ_ANNOUNCER_ENABLED or _is_user_requested_track(track):
        return None

    session = guild_session(guild_id)
    dj_file = prefetched_audio
    try:
        from src import radio as _radio
        from src.dj_announcer import (
            check_cooldown,
            cleanup_dj_audio,
            generate_dj_comment,
            generate_fun_fact,
            mark_announced,
            synthesize_dj_audio,
        )

        if _radio.is_radio_active(guild_id):
            new_cluster = await _radio.get_track_cluster(track)
            if new_cluster:
                prev_cluster = session.dj_last_genre_cluster
                session.dj_last_genre_cluster = new_cluster
                if not dj_file and prev_cluster and prev_cluster != new_cluster and check_cooldown(guild_id):
                    hour = get_buenos_aires_hour()
                    comment = await generate_dj_comment(
                        prev_cluster,
                        new_cluster,
                        track.get("title", ""),
                        track.get("artist", "Unknown"),
                        hour,
                    )
                    dj_file = await synthesize_dj_audio(comment, guild_id)

        if not dj_file and session.tracks_since_dj_comment >= DJ_FUN_FACT_INTERVAL_TRACKS:
            hour = get_buenos_aires_hour()
            comment = await generate_fun_fact(
                track.get("title", ""),
                track.get("artist", "Unknown"),
                session.dj_last_genre_cluster,
                hour,
                artist_id=track.get("artist_id"),
            )
            dj_file = await synthesize_dj_audio(comment, guild_id)

        if dj_file:
            mark_announced(guild_id)
            session.tracks_since_dj_comment = 0
        return dj_file
    except Exception as exc:
        logger.warning("play_next: DJ announcer error: %s", exc)
        if dj_file:
            cleanup_dj_audio(dj_file)
        return None


def stop_fm_recognition(guild_id: int, *, force: bool = False) -> None:
    """Stop live-stream FM recognition for a guild.

    When *force* is False (default), leave a running rock-radio seed listener alone.
    play_next used to call this for every non-stream track and killed shazamio mid-session.
    """
    if not force:
        try:
            from src.fm_seed_radio import is_seed_listener_running

            if is_seed_listener_running(guild_id):
                return
        except Exception:
            pass
    try:
        from src.fm_recognizer import stop_fm_recognizer

        stop_fm_recognizer(guild_id)
    except Exception as exc:
        logger.debug("stop_fm_recognition: %s", exc)
    try:
        from src.fm_history import close_session

        close_session(guild_id)
    except Exception as exc:
        logger.debug("stop_fm_recognition: history close failed: %s", exc)


def start_fm_recognition(guild_id: int, track: dict, text_channel) -> None:
    """Start continuous Shazam recognition for a live FM stream track."""
    if not track.get("is_radio_stream"):
        stop_fm_recognition(guild_id)
        return
    stream_url = track.get("url") or track.get("url_resolved") or ""
    if not stream_url:
        stop_fm_recognition(guild_id)
        return

    # Live FM playback and rock-radio seed listener share the recognizer slot — pick one.
    try:
        from src.fm_seed_radio import stop_fm_seed_listener

        stop_fm_seed_listener(guild_id)
    except Exception:
        pass

    from src.fm_recognizer import start_fm_recognizer

    try:
        from src.fm_history import open_session

        open_session(guild_id, track)
    except Exception as exc:
        logger.debug("start_fm_recognition: history open failed: %s", exc)

    def _is_active() -> bool:
        session = guild_session(guild_id)
        current = session.now_playing
        if not current or not current.get("is_radio_stream"):
            return False
        if session.paused:
            return False
        current_url = current.get("url") or current.get("url_resolved") or ""
        return current_url == stream_url

    async def _on_match(gid: int, match: dict) -> None:
        session = guild_session(gid)
        current = session.now_playing
        if not current or not current.get("is_radio_stream"):
            return
        updated = dict(current)
        updated["recognized_title"] = match.get("title") or ""
        updated["recognized_artist"] = match.get("artist") or ""
        updated["recognized_album"] = match.get("album")
        updated["recognized_cover_url"] = match.get("cover_url")
        updated["recognized_shazam_url"] = match.get("shazam_url")
        updated["recognized_at"] = match.get("recognized_at")
        session.now_playing = updated

        try:
            from src.fm_history import append_detection

            append_detection(gid, match)
        except Exception as exc:
            logger.debug("fm on_match: history append failed: %s", exc)

        guild = bot.get_guild(gid)
        channel = session.player_channel or text_channel
        if guild is not None and channel is not None:
            try:
                await update_player_embed(guild, channel)
            except Exception as exc:
                logger.debug("fm on_match: embed update failed: %s", exc)
        status = f"{match.get('artist', '')} — {match.get('title', '')}".strip(" —")
        if status and guild is not None:
            try:
                await _update_status(guild, status[:100])
            except Exception:
                pass

    async def _on_stale(gid: int) -> None:
        # Keep last match visible; only clear cover-ish noise after prolonged misses.
        logger.debug("fm_recognizer: stale misses guild=%s", gid)

    start_fm_recognizer(
        guild_id,
        stream_url,
        on_match=_on_match,
        is_active=_is_active,
        text_channel=text_channel,
        on_stale=_on_stale,
    )


async def play_next(guild: discord.Guild, vc: discord.VoiceClient, text_channel):
    session = guild_session(guild.id)
    if session.playback_busy:
        # Concurrent transition in progress (e.g. track end + error path racing); let current drain queue.
        asyncio.create_task(_ensure_playback_continues(guild, vc, text_channel))
        return
    session.playback_busy = True
    try:
        session.cancel_prefetch()

        if not session.queue:
            session.now_playing = None
            session.paused = False
            stop_fm_recognition(guild.id)
            await _update_status(guild, None)
            await update_player_embed(guild, text_channel)
            await _handle_empty_playback_queue(guild, vc, text_channel)
            return

        track = session.queue.popleft()
        track = await _resolve_url(track)
        if not track:
            if is_youtube_rate_limited():
                await maybe_notify_rate_limited(guild.id, text_channel)
                await text_channel.send("No hay copia local y YouTube está bloqueado, saltando...", delete_after=8)
            else:
                await text_channel.send("No se encontro en YouTube, saltando...", delete_after=5)
            asyncio.create_task(play_next(guild, vc, text_channel))
            return

        session.now_playing = track
        session.paused = False

        from src import radio as _radio
        asyncio.ensure_future(_radio.record_played(guild.id, track))
        if not track.get("is_radio_stream"):
            record_play(track)

        if _radio.is_radio_active(guild.id) and len(session.queue) < RADIO_QUEUE_TARGET_SIZE:
            asyncio.ensure_future(_radio.fill_radio_queue(guild, vc, text_channel))

        if session.queue:
            session.prefetch_task = asyncio.create_task(_prefetch_next(guild.id))

        prefetched_dj = session.prefetched_dj_audio
        session.prefetched_dj_audio = None
        session.tracks_since_dj_comment += 1

        if _is_user_requested_track(track):
            if prefetched_dj:
                from src.dj_announcer import cleanup_dj_audio
                cleanup_dj_audio(prefetched_dj)
            dj_file = None
        else:
            dj_file = await _resolve_dj_audio_before_track(guild.id, track, prefetched_dj)

        ffmpeg_opts = dict(FFMPEG_LOCAL_OPTIONS if track.get("local") else FFMPEG_OPTIONS)
        if track.get("is_radio_stream"):
            url = track.get("url", "")
            if ".m3u8" in url or "playlist" in url or "/live/" in url.lower():
                # Extra robustness for HLS / live radio streams that tend to cut
                base_before = ffmpeg_opts.get("before_options", "")
                extra = (
                    " -protocol_whitelist file,http,https,tcp,tls,crypto,data,httpproxy"
                    " -allowed_extensions ALL -live_start_index -1"
                )
                ffmpeg_opts["before_options"] = (base_before + extra).strip()
        try:
            source = discord.FFmpegOpusAudio(track["url"], **ffmpeg_opts)
            logger.info(
                "play_next: reproduciendo '%s' (local=%s, codec=%s, abr=%s)",
                track["title"],
                track.get("local", False),
                track.get("acodec", "?"),
                track.get("abr", "?"),
            )
        except Exception as e:
            logger.warning(f"play_next: video no disponible '{track['title']}': {e}, saltando...")
            await maybe_notify_rate_limited(guild.id, text_channel)
            from src.youtube import _url_cache
            from src.scoring import _normalize_text as _n
            _url_cache.pop(_n(track.get("yt_query", "")), None)
            asyncio.create_task(play_next(guild, vc, text_channel))
            return

        if not track.get("local") and not track.get("is_radio_stream"):
            try:
                await enqueue_download(
                    track,
                    track.get("video_id") or track.get("webpage_url"),
                )
            except Exception as exc:
                logger.warning("play_next: background download enqueue failed: %s", exc)

        def after(error):
            if error:
                logger.error(f"Error en reproduccion: {error}")
            asyncio.run_coroutine_threadsafe(play_next(guild, vc, text_channel), bot.loop)

        if dj_file:
            from src.dj_announcer import (
                cleanup_dj_audio,
                get_dj_ffmpeg_options,
                mix_dj_over_local_track,
            )
            from src.config import DJ_MIXER_ENABLED

            mix_path = None
            song_url = track.get("url") or ""
            is_local_file = bool(track.get("local")) or (
                song_url and not song_url.startswith(("http://", "https://"))
            )
            if DJ_MIXER_ENABLED and is_local_file and song_url:
                try:
                    mix_path = await mix_dj_over_local_track(song_url, dj_file, guild.id)
                except Exception as exc:
                    logger.debug("play_next: DJ mix failed: %s", exc)
                    mix_path = None

            if mix_path:
                # Single immersive stream: voice ducked under music
                cleanup_dj_audio(dj_file)
                dj_file = None

                def after_mix(error):
                    cleanup_dj_audio(mix_path)
                    after(error)

                try:
                    mix_source = discord.FFmpegOpusAudio(
                        mix_path, **get_dj_ffmpeg_options()
                    )
                    vc.play(mix_source, after=after_mix)
                    logger.info("play_next: DJ mixer active for '%s'", track.get("title"))
                except Exception as e:
                    logger.warning("play_next: mixed source failed: %s", e)
                    cleanup_dj_audio(mix_path)
                    vc.play(source, after=after)
            else:
                def after_dj(error):
                    cleanup_dj_audio(dj_file)
                    if error:
                        logger.warning("play_next: DJ TTS playback error: %s", error)
                    try:
                        song_source = discord.FFmpegOpusAudio(track["url"], **ffmpeg_opts)
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

        if track.get("is_radio_stream"):
            start_fm_recognition(guild.id, track, text_channel)
        else:
            stop_fm_recognition(guild.id)

        await update_player_embed(guild, text_channel)
        await _update_status(guild, track["title"])
    finally:
        session.playback_busy = False


async def start_radio_with_welcome(
    guild: discord.Guild,
    vc: discord.VoiceClient,
    text_channel,
) -> None:
    from src import radio as _radio
    from src.config import DJ_WELCOME_TIMEOUT_SEC

    session = guild_session(guild.id)
    gid = guild.id

    if session.radio_welcome_in_progress:
        logger.info("start_radio_with_welcome: already active for guild=%s, skipping", gid)
        return
    session.radio_welcome_in_progress = True

    async def _gen_welcome() -> str | None:
        if not DJ_ANNOUNCER_ENABLED:
            return None
        from src.dj_announcer import generate_welcome_message, synthesize_dj_audio
        mood = _radio.get_mood(gid)
        hour = get_buenos_aires_hour()
        text = await generate_welcome_message(mood, hour)
        return await synthesize_dj_audio(text, gid)

    async def _fill():
        # auto_play=False so cold-fill does not steal the voice channel before welcome
        await _radio.fill_radio_queue(
            guild, vc, text_channel, auto_play=False, early_play=False,
        )

    try:
        fill_task = asyncio.create_task(_fill())
        welcome_task = asyncio.create_task(_gen_welcome()) if DJ_ANNOUNCER_ENABLED else None

        dj_file: str | None = None
        if welcome_task:
            try:
                dj_file = await asyncio.wait_for(
                    asyncio.shield(welcome_task),
                    timeout=DJ_WELCOME_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.warning("start_radio_with_welcome: welcome gen timed out, skipping")
            except Exception as exc:
                logger.warning("start_radio_with_welcome: welcome gen error: %s", exc)

        # Ensure queue is ready before deciding playback order
        try:
            await fill_task
        except Exception as exc:
            logger.warning("start_radio_with_welcome: fill error: %s", exc)

        if dj_file:
            from src.dj_announcer import cleanup_dj_audio, get_dj_ffmpeg_options

            # Queue may already have cold-fill tracks — still play welcome first
            # unless something is already audible (user track / prior radio).
            if vc.is_playing() or vc.is_paused():
                logger.info(
                    "start_radio_with_welcome: already playing, skip welcome guild=%s",
                    gid,
                )
                cleanup_dj_audio(dj_file)
            else:
                done_event = asyncio.Event()

                def after_welcome(error):
                    cleanup_dj_audio(dj_file)
                    if error:
                        logger.warning("start_radio_with_welcome: TTS error: %s", error)
                    bot.loop.call_soon_threadsafe(done_event.set)

                try:
                    dj_source = discord.FFmpegOpusAudio(dj_file, **get_dj_ffmpeg_options())
                    vc.play(dj_source, after=after_welcome)
                    logger.info("start_radio_with_welcome: playing welcome for guild=%s", gid)
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=20.0)
                    except asyncio.TimeoutError:
                        pass
                except Exception as exc:
                    logger.warning("start_radio_with_welcome: welcome play failed: %s", exc)
                    cleanup_dj_audio(dj_file)

        if not (vc.is_playing() or vc.is_paused()):
            if session.queue:
                await play_next(guild, vc, text_channel)
            elif _radio.is_radio_active(gid):
                logger.info("start_radio_with_welcome: queue empty after fill, retrying")
                await _radio.fill_radio_queue(guild, vc, text_channel, auto_play=True)
    except Exception as exc:
        logger.warning("start_radio_with_welcome: error: %s", exc)
    finally:
        session.radio_welcome_in_progress = False


async def _update_status(guild: discord.Guild, title: str | None):
    activity = (
        discord.Activity(type=discord.ActivityType.listening, name=title)
        if title else None
    )
    await bot.change_presence(activity=activity)

    # discord.py 2.3 has no voice-status API — raw HTTP route required
    vc = guild.voice_client
    if not vc:
        logger.debug("_update_status: no hay voice_client activo, no se puede actualizar estado")
        return
    status_text = title or ""
    logger.info(f"_update_status: actualizando canal {vc.channel.id} con estado: '{status_text}'")
    try:
        route = discord.http.Route(
            "PUT", "/channels/{channel_id}/voice-status",
            channel_id=vc.channel.id
        )
        await bot.http.request(route, json={"status": status_text})
        logger.info("_update_status: estado del canal actualizado correctamente")
    except Exception as e:
        if "403" in str(e):
            logger.warning(f"_update_status: permisos insuficientes para actualizar estado del canal")
        else:
            logger.error(f"_update_status: error al actualizar estado del canal de voz: {e}", exc_info=True)
