"""Song-request channel: NL agent, plan embed, cancel-window apply, auto mode."""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import re
import time
from typing import Any

import discord
from discord.ext import commands

from src.bot_instance import bot
from src.config import (
    BOT_TEXT_CHANNEL_ID,
    OAUTH_ADMIN_USER_ID,
    REQUEST_AGENT_ENABLED,
    REQUEST_AUTO_APPLY,
    REQUEST_CONFIRM_TIMEOUT_SEC,
    REQUEST_COOLDOWN_SEC,
    REQUEST_MAX_QUERY_LEN,
    REQUEST_MAX_QUEUE_USER_TRACKS,
    REQUEST_MAX_TRACKS_PER_PLAN,
    effective_request_channel_id,
)
from src.library import record_request
from src.playback import (
    build_queue_snapshot,
    clear_user_tracks_from_queue,
    count_user_tracks_in_queue,
    enqueue_user_tracks,
    move_queue_track,
    play_next,
    priority_queue_track,
    refresh_player_embed_fresh,
    remove_queue_track_at,
    remove_queue_track_match,
)
from src.request_agent import (
    RequestPlan,
    build_request_plan,
    planned_track_labels,
    summarize_actions,
    validate_and_cap_plan,
)
from src.scoring import _split_query_parts
from src.spotify import (
    _get_spotify_track_info,
    _get_tracks_from_spotify_url,
    _is_spotify_url,
)
from src.youtube import (
    _is_youtube_url,
    extract_youtube_tracks,
    is_youtube_rate_limited,
    search_youtube,
)

logger = logging.getLogger(__name__)

_SETTINGS_PATH = pathlib.Path(".cache/request_settings.json")
_guild_auto: dict[int, bool] = {}
_cooldowns: dict[int, float] = {}  # user_id -> last ts
_pending_guilds: set[int] = set()
_settings_loaded = False


def _ensure_settings_loaded() -> None:
    global _settings_loaded
    if _settings_loaded:
        return
    _settings_loaded = True
    if not _SETTINGS_PATH.exists():
        return
    try:
        data = json.loads(_SETTINGS_PATH.read_text())
        for gid_str, cfg in (data or {}).items():
            if isinstance(cfg, dict) and "auto" in cfg:
                _guild_auto[int(gid_str)] = bool(cfg["auto"])
    except Exception as exc:
        logger.warning("request_channel: settings load failed: %s", exc)


def _save_settings() -> None:
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {str(gid): {"auto": auto} for gid, auto in _guild_auto.items()}
        _SETTINGS_PATH.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.warning("request_channel: settings save failed: %s", exc)


def get_auto_apply(guild_id: int) -> bool:
    _ensure_settings_loaded()
    if guild_id in _guild_auto:
        return _guild_auto[guild_id]
    return REQUEST_AUTO_APPLY


def set_auto_apply(guild_id: int, enabled: bool) -> None:
    _ensure_settings_loaded()
    _guild_auto[guild_id] = bool(enabled)
    _save_settings()


def _is_admin(user_id: int) -> bool:
    return user_id == OAUTH_ADMIN_USER_ID


def _can_apply_or_cancel(user_id: int, author_id: int) -> bool:
    return user_id == author_id or _is_admin(user_id)


def _status_emoji(status: str) -> str:
    return {
        "pending": "⏸",
        "running": "⏳",
        "ok": "✅",
        "fail": "❌",
        "skipped": "➖",
    }.get(status, "⏸")


class PlanView(discord.ui.View):
    def __init__(
        self,
        *,
        guild_id: int,
        author_id: int,
        plan: RequestPlan,
        track_items: list[dict[str, str]],
        timeout: float,
    ):
        # View timeout only disables UI; apply is owned by our countdown task.
        # Use None so discord.py does not auto-apply / race with Cancel.
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.author_id = author_id
        self.plan = plan
        self.track_items = track_items  # {label, status}
        self.message: discord.Message | None = None
        self._decision_lock = asyncio.Lock()
        self._decision: str | None = None  # None | "apply" | "cancel"
        self._abort = asyncio.Event()  # set on Cancel — stops execute mid-flight
        self._timer_task: asyncio.Task | None = None
        self._apply_task: asyncio.Task | None = None
        self.phase = "pending"  # pending | running | done | cancelled
        self.result_notes: list[str] = []
        self._update_auto_button()

    def is_aborted(self) -> bool:
        return self._abort.is_set()

    def _update_auto_button(self) -> None:
        auto = get_auto_apply(self.guild_id)
        for item in self.children:
            if isinstance(item, discord.ui.Button) and (item.label or "").startswith("Auto:"):
                item.label = f"Auto: {'on' if auto else 'off'}"
                item.style = (
                    discord.ButtonStyle.success if auto else discord.ButtonStyle.secondary
                )

    def build_embed(self, *, seconds_left: int | None = None) -> discord.Embed:
        from src import radio as _radio

        phase_label = {
            "pending": "Pendiente",
            "running": "Ejecutando",
            "done": "Hecho",
            "cancelled": "Cancelado",
        }.get(self.phase, self.phase)
        color = {
            "pending": 0xFEE75C,
            "running": 0x5865F2,
            "done": 0x57F287,
            "cancelled": 0xED4245,
        }.get(self.phase, 0x5865F2)

        embed = discord.Embed(
            title=f"🎧 Plan · {phase_label}",
            description=self.plan.reply or summarize_actions(self.plan),
            color=color,
        )
        snap = build_queue_snapshot(self.guild_id)
        if snap.get("radio_active") or _radio.is_radio_active(self.guild_id):
            embed.add_field(
                name="Radio",
                value="📻 Activa — tus temas irán delante de la auto-cola.",
                inline=False,
            )
        np = snap.get("now_playing")
        if np:
            embed.add_field(
                name="Ahora",
                value=f"▶ {np.get('title')} — {np.get('requester')}",
                inline=False,
            )

        summary = summarize_actions(self.plan)
        if summary:
            embed.add_field(name="Acciones", value=summary[:1024], inline=False)

        if self.track_items:
            lines = []
            for i, item in enumerate(self.track_items[:REQUEST_MAX_TRACKS_PER_PLAN], 1):
                lines.append(
                    f"{i}. {_status_emoji(item.get('status', 'pending'))} {item.get('label', '?')}"
                )
            embed.add_field(name="Tracks", value="\n".join(lines)[:1024], inline=False)

        if self.result_notes:
            embed.add_field(
                name="Resultado",
                value="\n".join(self.result_notes)[:1024],
                inline=False,
            )

        auto = get_auto_apply(self.guild_id)
        user_n = count_user_tracks_in_queue(self.guild_id)
        footer_bits = [
            f"Auto: {'on' if auto else 'off'}",
            f"cola user {user_n}/{REQUEST_MAX_QUEUE_USER_TRACKS}",
            f"plan ≤{REQUEST_MAX_TRACKS_PER_PLAN}",
            f"via {self.plan.source}" + (f"/{self.plan.model}" if self.plan.model else ""),
        ]
        if self.phase == "pending" and seconds_left is not None and not auto:
            footer_bits.insert(0, f"Se aplica en {seconds_left}s")
        embed.set_footer(text=" · ".join(footer_bits)[:2048])
        return embed

    async def _edit(self, **kwargs) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(**kwargs)
        except Exception as exc:
            logger.debug("request_channel: embed edit failed: %s", exc)

    async def start_timer_or_auto(self) -> None:
        if get_auto_apply(self.guild_id):
            self._apply_task = asyncio.create_task(self._apply(reason="auto"))
            return
        self._timer_task = asyncio.create_task(self._countdown())

    async def _countdown(self) -> None:
        remaining = REQUEST_CONFIRM_TIMEOUT_SEC
        try:
            while remaining > 0 and self._decision is None and not self.is_aborted():
                await self._edit(embed=self.build_embed(seconds_left=remaining), view=self)
                step = 5 if remaining > 10 else 1
                await asyncio.sleep(min(step, remaining))
                remaining -= step
            if self._decision is None and not self.is_aborted():
                await self._apply(reason="timeout")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("request_channel: countdown error: %s", exc)

    def _cancel_timer(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    def _disable_decision_buttons(self) -> None:
        for item in self.children:
            if not isinstance(item, discord.ui.Button):
                continue
            cid = getattr(item, "custom_id", None) or ""
            # Match by label when custom_id is auto-generated
            if cid in ("req_apply", "req_cancel") or item.label in ("Aplicar", "Cancelar"):
                item.disabled = True

    async def _apply(self, *, reason: str) -> None:
        async with self._decision_lock:
            if self._decision == "cancel" or self.is_aborted():
                logger.info("request_channel: apply skipped (cancelled) reason_was=%s", reason)
                return
            if self._decision == "apply":
                return
            self._decision = "apply"
        self._cancel_timer()
        if self.is_aborted():
            return
        self.phase = "running"
        self._disable_decision_buttons()
        # Cancel stays enabled so user can abort mid-resolve
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == "Cancelar":
                item.disabled = False
        await self._edit(embed=self.build_embed(), view=self)
        logger.info(
            "request_channel: applying plan guild=%s reason=%s actions=%s",
            self.guild_id,
            reason,
            [a.get("type") for a in self.plan.actions],
        )
        try:
            await execute_plan(
                self.guild_id,
                self.author_id,
                self.plan,
                track_items=self.track_items,
                notes_out=self.result_notes,
                should_abort=self.is_aborted,
            )
            if self.is_aborted():
                self.phase = "cancelled"
                if not any("Cancelado" in n for n in self.result_notes):
                    self.result_notes.append("Cancelado durante la ejecución.")
            else:
                self.phase = "done"
        except Exception as exc:
            logger.exception("request_channel: execute failed: %s", exc)
            self.result_notes.append(f"Error: {exc}")
            self.phase = "cancelled" if self.is_aborted() else "done"
        finally:
            _pending_guilds.discard(self.guild_id)
            self._disable_decision_buttons()
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.label == "Cancelar":
                    item.disabled = True
            self._update_auto_button()
            await self._edit(embed=self.build_embed(), view=self)
            self.stop()

    async def _cancel(self) -> None:
        """Abort pending timer and/or in-flight execute_plan."""
        self._abort.set()
        async with self._decision_lock:
            prev = self._decision
            if prev == "cancel":
                return
            self._decision = "cancel"
        self._cancel_timer()
        logger.info(
            "request_channel: cancel guild=%s prev_decision=%s phase=%s",
            self.guild_id,
            prev,
            self.phase,
        )
        # If apply is running, let it wind down via should_abort; update UI now
        if prev == "apply" or self.phase == "running":
            self.phase = "cancelled"
            self.result_notes.append("Cancelado — se detiene el resolve.")
            await self._edit(embed=self.build_embed(), view=self)
            return

        self.phase = "cancelled"
        self.result_notes.append("Cancelado por el usuario.")
        _pending_guilds.discard(self.guild_id)
        self._disable_decision_buttons()
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == "Cancelar":
                item.disabled = True
        await self._edit(embed=self.build_embed(), view=self)
        self.stop()

    # No fixed custom_id: temporary views get unique IDs (avoids sticky handlers).
    @discord.ui.button(label="Aplicar", style=discord.ButtonStyle.success, row=0)
    async def apply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _can_apply_or_cancel(interaction.user.id, self.author_id):
            await interaction.response.send_message(
                "Solo quien pidió el plan o un admin puede aplicar.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self._apply(reason="button")

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, row=0)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _can_apply_or_cancel(interaction.user.id, self.author_id):
            await interaction.response.send_message(
                "Solo quien pidió el plan o un admin puede cancelar.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self._cancel()

    @discord.ui.button(label="Auto: off", style=discord.ButtonStyle.secondary, row=0)
    async def auto_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message(
                "Solo un admin puede cambiar el modo auto.", ephemeral=True
            )
            return
        new_val = not get_auto_apply(self.guild_id)
        set_auto_apply(self.guild_id, new_val)
        self._update_auto_button()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


def _track_from_library(tid: str, entry: dict, requester: str) -> dict:
    from src.library import entry_to_queue_track

    return entry_to_queue_track(tid, entry, requester=requester)


def _track_from_yt_and_spotify(yt_info: dict, spotify_info: dict, requester: str) -> dict:
    artist, _ = _split_query_parts(spotify_info.get("query") or "")
    refined = bool(spotify_info.get("spotify_refined"))
    return {
        "title": yt_info["title"],
        "yt_query": spotify_info.get("query") or yt_info.get("title"),
        "url": yt_info["url"],
        "requester": requester,
        "artist": artist or "Unknown",
        "duration": yt_info.get("duration") or 0,
        "thumbnail": yt_info.get("thumbnail") or "",
        "cover_url": yt_info.get("cover_url") or "",
        "spotify_id": spotify_info.get("spotify_id") if refined else None,
        "artist_id": spotify_info.get("artist_id") if refined else None,
        "spotify_refined": refined,
        "video_id": yt_info.get("video_id"),
        "webpage_url": yt_info.get("webpage_url", ""),
        "acodec": yt_info.get("acodec", "?"),
        "abr": yt_info.get("abr", 0),
    }


async def _resolve_one_query(query: str, requester: str) -> dict | None:
    """Resolve a free-text / URL query to a single queue track dict.

    Prefer library → Spotify-refined title → YouTube (no LLM tie-break for speed).
    """
    query = (query or "").strip()
    if not query:
        return None

    if _is_youtube_url(query):
        yt_tracks = await extract_youtube_tracks(query)
        if not yt_tracks:
            return None
        raw = yt_tracks[0]
        return {
            "title": raw["title"],
            "yt_query": raw["yt_query"],
            "url": raw.get("url"),
            "requester": requester,
            "artist": raw.get("uploader") or "Unknown",
            "duration": raw.get("duration") or 0,
            "thumbnail": raw.get("thumbnail") or "",
            "video_id": raw.get("video_id"),
            "webpage_url": raw.get("webpage_url", ""),
            "acodec": raw.get("acodec", "?"),
            "abr": raw.get("abr", 0),
        }

    if _is_spotify_url(query):
        infos = await _get_tracks_from_spotify_url(query)
        if not infos:
            return None
        info = infos[0]
        # Library hit on the refined query first
        from src.library import search_index

        lib_hits = search_index(info["query"], limit=1)
        if lib_hits:
            return _track_from_library(lib_hits[0][0], lib_hits[0][1], requester)
        yt_info = await search_youtube(
            info["query"], enable_llm=False, trusted=True, urgent=True
        )
        if not yt_info:
            return None
        return _track_from_yt_and_spotify(yt_info, info, requester)

    if re.search(r"https?://", query):
        return None

    # Local library before network (fast + accurate for known tracks)
    try:
        from src.library import search_index

        lib_hits = search_index(query, limit=1)
        if lib_hits:
            tid, entry = lib_hits[0]
            logger.info("request_channel: library hit for %r → %s", query, tid)
            return _track_from_library(tid, entry, requester)
    except Exception as exc:
        logger.debug("request_channel: library search failed: %s", exc)

    info = await _get_spotify_track_info(query)
    # Prefer Spotify's canonical "Artist - Title" for YT matching
    yt_query = info.get("query") or query
    if info.get("spotify_refined"):
        # Second library pass with refined name
        try:
            from src.library import search_index

            lib_hits = search_index(yt_query, limit=1)
            if lib_hits:
                return _track_from_library(lib_hits[0][0], lib_hits[0][1], requester)
        except Exception:
            pass

    yt_info = await search_youtube(
        yt_query,
        enable_llm=False,  # agent path: skip Anthropic tie-break (latency)
        trusted=bool(info.get("spotify_refined")),
        urgent=True,
    )
    if not yt_info:
        return None
    return _track_from_yt_and_spotify(yt_info, info, requester)


async def _genre_queries(genre: str, count: int, hints: str) -> list[str]:
    """Propose specific 'Artist - Title' lines via Spotify; no vague filler queries."""
    genre = (genre or "").strip()
    hints = (hints or "").strip()
    count = max(1, min(count, 10))
    queries: list[str] = []
    seen: set[str] = set()

    def _add_from_result(result: dict) -> None:
        for item in (result.get("tracks") or {}).get("items") or []:
            name = (item.get("name") or "").strip()
            artists = ", ".join(
                a.get("name", "") for a in (item.get("artists") or []) if a.get("name")
            )
            if not name:
                continue
            line = f"{artists} - {name}" if artists else name
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            queries.append(line)
            if len(queries) >= count:
                return

    try:
        from src.config import sp

        if sp is not None:
            # Ordered attempts: specific → broader
            search_terms = []
            if hints:
                search_terms.append(f"{genre} {hints}")
            search_terms.append(genre)
            # Spotify genre filter works for many mainstream tags
            if " " not in genre and len(genre) < 32:
                search_terms.append(f'genre:"{genre}"')

            for term in search_terms:
                if len(queries) >= count:
                    break

                def _search(q=term, limit=min(count * 2, 20)):
                    return sp.search(q=q, type="track", limit=limit)

                result = await asyncio.to_thread(_search)
                _add_from_result(result or {})
    except Exception as exc:
        logger.debug("request_channel: genre spotify search failed: %s", exc)

    # Do not invent "mix song N" — bad YT matches. Return what we have.
    if not queries:
        # Last resort: one specific-looking seed the resolver can Spotify-refine
        queries.append(f"{genre} {hints}".strip() or genre)
    return queries[:count]


async def execute_plan(
    guild_id: int,
    author_id: int,
    plan: RequestPlan,
    *,
    track_items: list[dict[str, str]],
    notes_out: list[str],
    should_abort: Any | None = None,
) -> None:
    def _aborted() -> bool:
        if should_abort is None:
            return False
        try:
            return bool(should_abort())
        except Exception:
            return False

    guild = bot.get_guild(guild_id)
    if guild is None:
        notes_out.append("Guild no disponible.")
        return

    if _aborted():
        notes_out.append("Cancelado antes de ejecutar.")
        return

    member = guild.get_member(author_id)
    requester = member.display_name if member else str(author_id)
    text_channel = bot.get_channel(BOT_TEXT_CHANNEL_ID) or bot.get_channel(
        effective_request_channel_id()
    )

    # Apply set_auto first
    for action in plan.actions:
        if _aborted():
            notes_out.append("Cancelado.")
            return
        if action["type"] == "set_auto":
            set_auto_apply(guild_id, bool(action.get("enabled")))
            notes_out.append(f"Auto → {'on' if action.get('enabled') else 'off'}")

    # Mutate queue (non-resolve)
    for action in plan.actions:
        if _aborted():
            notes_out.append("Cancelado antes de mutar la cola.")
            return
        t = action["type"]
        if t == "move":
            track = move_queue_track(guild_id, int(action["from_pos"]), int(action["to_pos"]))
            notes_out.append(
                f"Movido: {track.get('title')}" if track else f"Move inválido #{action.get('from_pos')}"
            )
        elif t == "remove":
            track = remove_queue_track_at(guild_id, int(action["pos"]))
            notes_out.append(
                f"Quitado: {track.get('title')}" if track else f"Remove inválido #{action.get('pos')}"
            )
        elif t == "remove_match":
            track = remove_queue_track_match(guild_id, str(action.get("query") or ""))
            notes_out.append(
                f"Quitado: {track.get('title')}" if track else f"No match «{action.get('query')}»"
            )
        elif t == "priority":
            track = priority_queue_track(guild_id, int(action["pos"]))
            notes_out.append(
                f"Priority: {track.get('title')}" if track else f"Priority inválido #{action.get('pos')}"
            )
        elif t == "clear_user_tracks":
            n = clear_user_tracks_from_queue(guild_id)
            notes_out.append(f"Limpiados {n} pedidos de usuario.")
        elif t == "show_queue":
            snap = build_queue_snapshot(guild_id)
            notes_out.append("Cola:\n" + ("\n".join(snap["queue_lines"]) or "(vacía)"))
        elif t == "skip":
            vc = guild.voice_client
            if vc and (vc.is_playing() or vc.is_paused()):
                vc.stop()
                notes_out.append("Skip.")
            else:
                notes_out.append("Skip: nada sonando.")

    if _aborted():
        notes_out.append("Cancelado.")
        return

    # Expand enqueue / genre into queries then resolve
    resolve_jobs: list[tuple[str, str]] = []  # (label, position)
    for action in plan.actions:
        if _aborted():
            notes_out.append("Cancelado.")
            return
        if action["type"] == "enqueue":
            pos = action.get("position") or "end"
            for q in action.get("queries") or []:
                resolve_jobs.append((q, pos))
        elif action["type"] == "genre_playlist":
            genre_qs = await _genre_queries(
                str(action.get("genre") or ""),
                int(action.get("count") or 1),
                str(action.get("hints") or ""),
            )
            if _aborted():
                notes_out.append("Cancelado durante genre_playlist.")
                return
            for q in genre_qs:
                resolve_jobs.append((q, "end"))

    # Align track_items labels with jobs
    while len(track_items) < len(resolve_jobs):
        track_items.append({"label": resolve_jobs[len(track_items)][0], "status": "pending"})
    for i, (label, _) in enumerate(resolve_jobs):
        if i < len(track_items):
            track_items[i]["label"] = label
            track_items[i]["status"] = "pending"

    if _aborted():
        for item in track_items:
            if item.get("status") == "pending":
                item["status"] = "skipped"
        notes_out.append("Cancelado antes de buscar en YouTube.")
        return

    vc = guild.voice_client
    if member and member.voice:
        voice_channel = member.voice.channel
        if vc is None:
            try:
                vc = await voice_channel.connect(timeout=20.0)
            except Exception as exc:
                notes_out.append(f"No pude unirme al voice: {exc}")
                for item in track_items:
                    if item.get("status") in ("pending", "running"):
                        item["status"] = "fail"
                return
        elif vc.channel != voice_channel:
            try:
                await vc.move_to(voice_channel, timeout=20.0)
            except Exception as exc:
                notes_out.append(f"No pude moverme de voice: {exc}")

    if _aborted():
        for item in track_items:
            if item.get("status") == "pending":
                item["status"] = "skipped"
        notes_out.append("Cancelado.")
        return

    playback_active = bool(vc and (vc.is_playing() or vc.is_paused()))
    enqueued_any = False
    front_inserted = 0
    started_playback = False

    async def _kick_playback_if_idle() -> None:
        """Start audio as soon as the first track is ready (do not wait for the full plan)."""
        nonlocal playback_active, started_playback, vc
        if started_playback or not guild or not text_channel:
            return
        if vc is None:
            return
        if vc.is_playing() or vc.is_paused():
            playback_active = True
            started_playback = True
            return
        try:
            await play_next(guild, vc, text_channel)
        except Exception as exc:
            logger.warning("request_channel: early play_next failed: %s", exc)
            return
        playback_active = bool(vc.is_playing() or vc.is_paused())
        started_playback = True

    for i, (query, position) in enumerate(resolve_jobs):
        if _aborted():
            for j in range(i, len(track_items)):
                if track_items[j].get("status") in ("pending", "running"):
                    track_items[j]["status"] = "skipped"
            notes_out.append(f"Cancelado tras {i}/{len(resolve_jobs)} búsquedas.")
            break
        if i < len(track_items):
            track_items[i]["status"] = "running"
        # Tracks already enqueued count toward the user-queue cap.
        slots_left = REQUEST_MAX_QUEUE_USER_TRACKS - count_user_tracks_in_queue(guild_id)
        if slots_left <= 0:
            if i < len(track_items):
                track_items[i]["status"] = "skipped"
            notes_out.append("Cola de usuario llena; resto omitido.")
            for j in range(i + 1, len(track_items)):
                track_items[j]["status"] = "skipped"
            break
        try:
            track = await _resolve_one_query(query, requester)
        except Exception as exc:
            logger.warning("request_channel: resolve failed q=%r: %s", query, exc)
            track = None
        if _aborted():
            if i < len(track_items):
                track_items[i]["status"] = "skipped"
            for j in range(i + 1, len(track_items)):
                track_items[j]["status"] = "skipped"
            notes_out.append("Cancelado — no se encola lo pendiente.")
            # Track just resolved is not enqueued; already-playing tracks stay.
            break
        if not track:
            if i < len(track_items):
                track_items[i]["status"] = "fail"
                track_items[i]["label"] = query
            if is_youtube_rate_limited():
                notes_out.append(f"Rate-limit / sin match: {query}")
            else:
                notes_out.append(f"Sin match: {query}")
            continue
        record_request(track)
        pos = (position or "end").lower()
        enqueue_user_tracks(
            guild_id,
            [track],
            playback_active=playback_active,
            position=pos,
            front_offset=front_inserted if pos == "front" else 0,
        )
        if pos == "front":
            front_inserted += 1
        enqueued_any = True
        if i < len(track_items):
            track_items[i]["status"] = "ok"
            track_items[i]["label"] = track.get("title") or query
        # First successful resolve → start playback immediately; rest keep resolving.
        await _kick_playback_if_idle()

    if text_channel and guild:
        if enqueued_any:
            # Fallback if early start failed (e.g. VC not ready on first track).
            if vc and not (vc.is_playing() or vc.is_paused()):
                try:
                    await play_next(guild, vc, text_channel)
                except Exception as exc:
                    logger.warning("request_channel: final play_next failed: %s", exc)
            else:
                try:
                    await refresh_player_embed_fresh(guild, text_channel)
                except Exception:
                    pass
        elif any(
            a["type"] in ("move", "remove", "remove_match", "priority", "clear_user_tracks", "skip")
            for a in plan.actions
        ):
            try:
                await refresh_player_embed_fresh(guild, text_channel)
            except Exception:
                pass


async def handle_request_message(message: discord.Message) -> bool:
    """
    Process a free-text request. Returns True if handled (caller should not
    treat as something else).
    """
    if not REQUEST_AGENT_ENABLED:
        return False
    if message.guild is None or message.author.bot:
        return False
    if message.channel.id != effective_request_channel_id():
        return False

    content = (message.content or "").strip()
    if not content:
        return False
    # Prefix commands are handled elsewhere
    if content.startswith("!"):
        return False
    if len(content) > REQUEST_MAX_QUERY_LEN:
        await message.channel.send(
            f"Pedido demasiado largo (máx {REQUEST_MAX_QUERY_LEN} caracteres).",
            delete_after=8,
        )
        return True

    if not isinstance(message.author, discord.Member) or not message.author.voice:
        await message.reply(
            "Entrá a un canal de voz para pedir música.",
            delete_after=10,
            mention_author=False,
        )
        return True

    gid = message.guild.id
    if gid in _pending_guilds:
        await message.reply(
            "Hay un plan pendiente. Aplicalo, cancelalo o esperá el timeout.",
            delete_after=12,
            mention_author=False,
        )
        return True

    now = time.time()
    last = _cooldowns.get(message.author.id, 0.0)
    if REQUEST_COOLDOWN_SEC > 0 and now - last < REQUEST_COOLDOWN_SEC:
        await message.add_reaction("⏳")
        return True
    _cooldowns[message.author.id] = now

    try:
        await message.add_reaction("🤖")
    except Exception:
        pass

    _pending_guilds.add(gid)
    thinking = discord.Embed(
        title="🎧 Plan",
        description="Pensando…",
        color=0x5865F2,
    )
    thinking.set_footer(text="LLM + cola")
    plan_msg: discord.Message | None = None
    try:
        plan_msg = await message.channel.send(embed=thinking)
    except Exception as exc:
        _pending_guilds.discard(gid)
        logger.warning("request_channel: could not send thinking embed: %s", exc)
        return True

    auto = get_auto_apply(gid)
    snapshot = build_queue_snapshot(gid)
    user_slots = max(0, REQUEST_MAX_QUEUE_USER_TRACKS - int(snapshot.get("queue_user_count") or 0))

    try:
        plan = await build_request_plan(
            content,
            snapshot,
            auto_enabled=auto,
            user_slots_left=user_slots,
        )
        plan = validate_and_cap_plan(plan, user_slots_left=user_slots)

        labels = planned_track_labels(plan)
        track_items = [{"label": lab, "status": "pending"} for lab in labels]

        timeout = float(REQUEST_CONFIRM_TIMEOUT_SEC)
        view = PlanView(
            guild_id=gid,
            author_id=message.author.id,
            plan=plan,
            track_items=track_items,
            timeout=timeout + 30,
        )
        view.message = plan_msg
        embed = view.build_embed(
            seconds_left=None if auto else REQUEST_CONFIRM_TIMEOUT_SEC
        )
        await plan_msg.edit(embed=embed, view=view)
        await view.start_timer_or_auto()
    except Exception as exc:
        _pending_guilds.discard(gid)
        logger.exception("request_channel: failed to build/post plan: %s", exc)
        try:
            await plan_msg.edit(
                content=f"No pude crear el plan: {exc}",
                embed=None,
                view=None,
            )
        except Exception:
            pass
    return True


def register_request_channel_handlers() -> None:
    """Attach on_message if not already using commands.py wiring."""
    pass  # wired from commands.py
