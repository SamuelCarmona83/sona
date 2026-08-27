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
    update_player_embed,
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


async def _ack_interaction(interaction: discord.Interaction) -> bool:
    """Defer a component interaction; return False if the token already expired.

    Discord requires a response within ~3s. If the event loop was busy (yt-dlp),
    defer raises NotFound (10062). Callers must still run cancel/apply logic.
    """
    try:
        if interaction.response.is_done():
            return True
        await interaction.response.defer()
        return True
    except discord.NotFound:
        logger.warning(
            "request_channel: interaction expired (10062) — continuing action offline"
        )
        return False
    except discord.HTTPException as exc:
        logger.warning("request_channel: interaction ack failed: %s", exc)
        return False


async def _await_unless_aborted(coro, should_abort, *, poll_sec: float = 0.25):
    """Await *coro* but cancel it as soon as should_abort() is true.

    yt-dlp work in a worker thread may keep running until the thread finishes,
    but the plan loop unblocks immediately so Cancel feels responsive.
    """
    if should_abort is not None:
        try:
            if should_abort():
                # Close coroutine if never started
                if asyncio.iscoroutine(coro):
                    coro.close()
                return None
        except Exception:
            pass

    task = asyncio.ensure_future(coro)
    try:
        while not task.done():
            if should_abort is not None:
                try:
                    aborted = bool(should_abort())
                except Exception:
                    aborted = False
                if aborted:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    return None
            try:
                await asyncio.wait({task}, timeout=poll_sec)
            except Exception:
                break
        if task.cancelled():
            return None
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            raise exc
        return task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise


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

    def _lock_all_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

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
        was_running = prev == "apply" or self.phase == "running"
        self.phase = "cancelled"
        # Free the guild immediately so a new request is not stuck behind a dying plan.
        _pending_guilds.discard(self.guild_id)
        self._lock_all_buttons()
        self._update_auto_button()

        # Mid-apply: execute_plan watches should_abort / interruptible resolve.
        # _apply's finally will stop the view; we still refresh UI now.
        if was_running:
            if not any("Cancelado" in n for n in self.result_notes):
                self.result_notes.append("Cancelado — se detiene el resolve.")
            await self._edit(embed=self.build_embed(), view=self)
            return

        if not any("Cancelado" in n for n in self.result_notes):
            self.result_notes.append("Cancelado por el usuario.")
        await self._edit(embed=self.build_embed(), view=self)
        self.stop()

    # No fixed custom_id: temporary views get unique IDs (avoids sticky handlers).
    @discord.ui.button(label="Aplicar", style=discord.ButtonStyle.success, row=0)
    async def apply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _can_apply_or_cancel(interaction.user.id, self.author_id):
            try:
                await interaction.response.send_message(
                    "Solo quien pidió el plan o un admin puede aplicar.", ephemeral=True
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        await _ack_interaction(interaction)
        await self._apply(reason="button")

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, row=0)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _can_apply_or_cancel(interaction.user.id, self.author_id):
            try:
                await interaction.response.send_message(
                    "Solo quien pidió el plan o un admin puede cancelar.", ephemeral=True
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        acked = await _ack_interaction(interaction)
        try:
            await self._cancel()
        except Exception:
            logger.exception("request_channel: cancel handler failed")
            if acked:
                try:
                    await interaction.followup.send(
                        "No pude cancelar el plan (error interno).", ephemeral=True
                    )
                except (discord.NotFound, discord.HTTPException):
                    pass
            return
        if acked:
            try:
                await interaction.followup.send("Plan cancelado.", ephemeral=True)
            except (discord.NotFound, discord.HTTPException):
                pass

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
    shazam_id = None

    # Shazam search as safety net: typos / wrong LLM title / Spotify miss.
    if not info.get("spotify_refined"):
        try:
            from src import shazam_catalog as _shazam_cat

            hit = await _shazam_cat.best_search_match(query)
            if hit and hit.get("query"):
                shazam_id = hit.get("shazam_id")
                yt_query = hit["query"]
                logger.info(
                    "request_channel: shazam refined %r → %r (id=%s)",
                    query, yt_query, shazam_id,
                )
                # Re-try Spotify with the cleaner Shazam title for ids/artwork.
                info2 = await _get_spotify_track_info(yt_query)
                if info2.get("spotify_refined"):
                    info = info2
                    yt_query = info2.get("query") or yt_query
                else:
                    info = {
                        "query": yt_query,
                        "spotify_id": None,
                        "artist_id": None,
                        "spotify_refined": False,
                        "artist": hit.get("artist"),
                    }
        except Exception as exc:
            logger.debug("request_channel: shazam refine failed: %s", exc)

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
        trusted=bool(info.get("spotify_refined") or shazam_id),
        urgent=True,
    )
    if not yt_info:
        return None
    track = _track_from_yt_and_spotify(yt_info, info, requester)
    if shazam_id and not track.get("shazam_id"):
        track["shazam_id"] = shazam_id
    if info.get("artist") and (not track.get("artist") or track.get("artist") == "Unknown"):
        track["artist"] = info["artist"]
    return track


def _organic_merge_pools(pools: list[list[str]], total: int) -> list[str]:
    """Mix several genre pools into one organic playlist (not sequential 5+5 blocks).

    Each round picks a random non-empty pool (weighted by remaining size) so
    genres interleave unevenly instead of fixed halves.
    """
    import random as _random

    remaining = [list(p) for p in pools if p]
    for p in remaining:
        _random.shuffle(p)
    out: list[str] = []
    seen: set[str] = set()
    total = max(0, int(total))
    while remaining and len(out) < total:
        weights = [max(1, len(p)) for p in remaining]
        idx = _random.choices(range(len(remaining)), weights=weights, k=1)[0]
        q = remaining[idx].pop(0)
        key = q.lower()
        if key not in seen:
            seen.add(key)
            out.append(q)
        if not remaining[idx]:
            remaining.pop(idx)
    return out[:total]


async def materialize_genre_playlists(
    plan: RequestPlan,
    *,
    guild_id: int | None = None,
    user_id: int | None = None,
) -> RequestPlan:
    """Expand genre_playlist / style-blob enqueues → concrete Artist - Title lines.

    Multiple genres are merged into one organically shuffled enqueue so the
    plan embed shows real songs (never 'Limp Bizkit nu-metal' as a track).
    Also sets radio sticky vibe (does not change !mood) when guild_id is set.
    """
    from src.request_agent import (
        is_concrete_artist_title,
        is_style_blob_query,
        style_blobs_to_genre_actions,
    )

    # Convert style-blob enqueues into genre_playlist first
    converted: list[dict] = []
    for a in plan.actions:
        if a.get("type") != "enqueue":
            converted.append(a)
            continue
        queries = [str(q).strip() for q in (a.get("queries") or []) if str(q).strip()]
        style_qs = [q for q in queries if is_style_blob_query(q)]
        concrete_qs = [q for q in queries if not is_style_blob_query(q)]
        if style_qs:
            converted.extend(style_blobs_to_genre_actions(style_qs, default_count=5))
            if concrete_qs:
                converted.append({
                    "type": "enqueue",
                    "queries": concrete_qs,
                    "position": a.get("position") or "end",
                })
        else:
            converted.append(a)
    plan = RequestPlan(
        reply=plan.reply,
        actions=converted,
        source=plan.source,
        model=plan.model,
    )

    if not any(a.get("type") == "genre_playlist" for a in plan.actions):
        return plan

    genre_actions = [a for a in plan.actions if a.get("type") == "genre_playlist"]
    pools: list[list[str]] = []
    total = 0
    for a in genre_actions:
        count = max(3, int(a.get("count") or 5))  # style playlists: never 1
        total += count
        fetch_n = min(12, count + max(3, count // 2))
        qs = await _genre_queries(
            str(a.get("genre") or ""),
            fetch_n,
            str(a.get("hints") or ""),
        )
        # Drop non-song leftovers (last-resort blobs)
        qs = [q for q in qs if is_concrete_artist_title(q) or " - " in q]
        pools.append(qs)
        logger.info(
            "request_channel: materialized genre=%r hints=%r wanted=%d got=%d sample=%s",
            a.get("genre"),
            (a.get("hints") or "")[:40],
            count,
            len(qs),
            (qs[0] if qs else None),
        )

    combined = _organic_merge_pools(pools, total)
    # Keep any already-concrete enqueues from the plan
    concrete_keep: list[str] = []
    for a in plan.actions:
        if a.get("type") == "enqueue":
            for q in a.get("queries") or []:
                if is_concrete_artist_title(str(q)) or (
                    not is_style_blob_query(str(q)) and " - " in str(q)
                ):
                    concrete_keep.append(str(q).strip())

    if concrete_keep:
        seen = {c.lower() for c in concrete_keep}
        for q in combined:
            if q.lower() not in seen:
                concrete_keep.append(q)
                seen.add(q.lower())
        combined = concrete_keep[: max(total, len(concrete_keep))]

    if not combined:
        logger.warning(
            "request_channel: materialize produced 0 concrete tracks; leaving plan"
        )
        return plan

    new_actions: list[dict] = []
    inserted = False
    for a in plan.actions:
        if a.get("type") == "genre_playlist":
            if not inserted:
                new_actions.append({
                    "type": "enqueue",
                    "queries": combined,
                    "position": "end",
                })
                inserted = True
            continue
        if a.get("type") == "enqueue":
            # Already merged into combined
            continue
        new_actions.append(a)
    if not inserted:
        new_actions.append({
            "type": "enqueue",
            "queries": combined,
            "position": "end",
        })

    if guild_id is not None:
        try:
            from src import radio as _radio

            genres = [str(a.get("genre") or "").strip() for a in genre_actions]
            hints = [str(a.get("hints") or "").strip() for a in genre_actions if a.get("hints")]
            genres = [g for g in genres if g]
            hints = [h for h in hints if h]
            if genres or hints:
                _radio.set_sticky_vibe(
                    guild_id,
                    genres=genres,
                    hints=hints,
                    user_id=user_id,
                )
        except Exception as exc:
            logger.debug("request_channel: sticky vibe set failed: %s", exc)

    return RequestPlan(
        reply=plan.reply,
        actions=new_actions,
        source=plan.source,
        model=plan.model,
    )


# Offline style packs used when Spotify/Shazam return nothing (auth, 403, shazam JSON errors).
_CURATED_NU_METAL = [
    "Limp Bizkit - Rollin' (Air Raid Vehicle)",
    "Limp Bizkit - Break Stuff",
    "Korn - Freak on a Leash",
    "Korn - Got the Life",
    "Linkin Park - One Step Closer",
    "Linkin Park - Crawling",
    "Papa Roach - Last Resort",
    "Disturbed - Down with the Sickness",
    "System Of A Down - Chop Suey!",
    "System Of A Down - Toxicity",
    "Slipknot - Wait and Bleed",
    "Mudvayne - Dig",
    "Drowning Pool - Bodies",
    "P.O.D. - Youth of the Nation",
    "Incubus - Pardon Me",
]
_CURATED_THPS_SKATE = [
    "Goldfinger - Superman",
    "Sum 41 - Fat Lip",
    "Blink-182 - First Date",
    "The Offspring - Want You Bad",
    "Millencolin - No Cigar",
    "Less Than Jake - All My Best Friends Are Metalheads",
    "Reel Big Fish - Sell Out",
    "Rage Against The Machine - Guerrilla Radio",
    "Powerman 5000 - When Worlds Collide",
    "Papa Roach - Broken Home",
    "Alien Ant Farm - Smooth Criminal",
    "Crazy Town - Butterfly",
    "Zebrahead - Check",
    "CKY - 96 Quite Bitter Beings",
    "JFA - Beach Blanket Bongout",
]
_CURATED_EARLY_2000S_ROCK = [
    "Foo Fighters - All My Life",
    "The White Stripes - Seven Nation Army",
    "Jet - Are You Gonna Be My Girl",
    "The Vines - Get Free",
    "Audioslave - Cochise",
    "Queens of the Stone Age - No One Knows",
    "Red Hot Chili Peppers - By the Way",
    "Nickelback - How You Remind Me",
    "3 Doors Down - Kryptonite",
    "Evanescence - Bring Me to Life",
]
# Underground / abstract / boom-bap adjacent to MF DOOM & chillhop/lofi hip-hop.
_CURATED_MF_DOOM_HIPHOP = [
    "MF DOOM - Doomsday",
    "MF DOOM - Rhinestone Cowboy",
    "Madvillain - All Caps",
    "Madvillain - Accordion",
    "MF DOOM - One Beer",
    "Danger Doom - Sofa King",
    "Madlib - Slim's Return",
    "J Dilla - So Far to Go",
    "J Dilla - Workinonit",
    "A Tribe Called Quest - Electric Relaxation",
    "Gang Starr - Mass Appeal",
    "Mos Def - Mathematics",
    "Talib Kweli - Get By",
    "Common - The Light",
    "Nas - The World Is Yours",
    "Pete Rock & C.L. Smooth - They Reminisce Over You (T.R.O.Y.)",
    "De La Soul - Stakes Is High",
    "The Pharcyde - Passin' Me By",
    "Black Star - Definition",
    "KMD - Peachfuzz",
]
_CURATED_LOFI_HIPHOP = [
    "Nujabes - Aruarian Dance",
    "Nujabes - Feather",
    "Nujabes - Luv (sic) Part 3",
    "J Dilla - Don't Cry",
    "J Dilla - Time: The Donut of the Heart",
    "Madlib - Road of the Lonely Ones",
    "MF DOOM - Soft Landing",
    "Emancipator - Soon It Will Be Cold Enough",
    "Bonobo - Kong",
    "DJ Shadow - Midnight in a Perfect World",
    "Blockhead - The Music Scene",
    "Blue Sky Black Death - And Stars, Ringed",
    "Knxwledge - [bc] tm_rpm.001",
    "Idealism - both of us",
    "Tomppabeats - won't leave you",
]


def _curated_style_queries(genre: str, hints: str, count: int) -> list[str]:
    """Always-available Artist - Title packs for common vibes.

    Order matters: match specific anchors (DOOM, lofi, nu-metal) before generic
    'rock', and never default hip-hop requests to early-2000s rock.
    """
    import random as _random

    blob = f"{genre} {hints}".lower()
    pool: list[str] = []

    # --- Hip-hop / DOOM / lofi (must win over generic rock default) ---
    if re.search(
        r"\b(mf\s*doom|madvillain|doom\b|madlib|dilla|nujabes|tribe\s*called|"
        r"gang\s*starr|abstract\s*hip|boom[\s\-]?bap)\b",
        blob,
    ):
        pool.extend(_CURATED_MF_DOOM_HIPHOP)
    if re.search(
        r"\b(lo[\s\-]?fi|lofi|chillhop|chill\s*hop|study\s*beats|jazzy\s*hip)\b",
        blob,
    ):
        pool.extend(_CURATED_LOFI_HIPHOP)
        # lofi + doom vibe often overlap
        if not any("DOOM" in q or "Madvillain" in q for q in pool):
            pool.extend(_CURATED_MF_DOOM_HIPHOP[:6])
    if re.search(
        r"\b(hip[\s\-]?hop|hiphop|rap\b|underground\s*rap|east\s*coast)\b",
        blob,
    ) and len(pool) < count:
        pool.extend(_CURATED_MF_DOOM_HIPHOP)

    # --- Rock / metal packs ---
    if re.search(r"limp|bizkit|nu[\s\-]?metal|korn|linkin|papa\s*roach|slipknot", blob):
        pool.extend(_CURATED_NU_METAL)
    if re.search(
        r"hawk|skater|thps|goldfinger|millencolin|skate|soundtrack|ost\b|sum\s*41",
        blob,
    ):
        pool.extend(_CURATED_THPS_SKATE)
    # "rock" only if we didn't already pick hip-hop/lofi (avoid "hip-hop" false paths)
    if (
        re.search(r"\b(rock|punk|pop.?punk|indie\s*rock)\b", blob)
        and not re.search(r"hip[\s\-]?hop|lo[\s\-]?fi|rap\b|doom", blob)
        and len(pool) < count
    ):
        pool.extend(_CURATED_EARLY_2000S_ROCK)

    if not pool:
        if re.search(r"metal", blob):
            pool.extend(_CURATED_NU_METAL)
        elif re.search(r"hip|rap|doom|lofi|lo-fi", blob):
            pool.extend(_CURATED_MF_DOOM_HIPHOP)
        elif re.search(r"rock|punk|indie", blob):
            pool.extend(_CURATED_EARLY_2000S_ROCK)
        else:
            # Last resort: prefer hip-hop abstract over random rock if unsure
            pool.extend(_CURATED_MF_DOOM_HIPHOP if "hop" in blob or "rap" in blob else _CURATED_EARLY_2000S_ROCK)

    # Drop garbage lines that aren't real tracks
    pool = [q for q in pool if " - " in q and "search:" not in q.lower()]

    seen: set[str] = set()
    out: list[str] = []
    for q in pool:
        k = q.lower()
        if k not in seen:
            seen.add(k)
            out.append(q)
    _random.shuffle(out)
    return out[: max(count, 1)]


async def _genre_queries(genre: str, count: int, hints: str) -> list[str]:
    """Propose specific 'Artist - Title' lines via Spotify (fast), then curated fallback.

    *hints* is a style anchor (artist, soundtrack, era) — not a single track to play.
    Shazam is optional and skipped when its API is failing (JSON decode errors).
    """
    import random as _random

    genre = (genre or "").strip()
    hints = (hints or "").strip()
    count = max(1, min(count, 10))
    queries: list[str] = []
    seen: set[str] = set()
    anchor_artist_norm = ""

    def _add_line(line: str, *, allow_anchor_artist: bool = True) -> None:
        line = (line or "").strip()
        if not line:
            return
        if " - " not in line and " – " not in line:
            return  # only concrete Artist - Title lines in the plan embed
        key = line.lower()
        if key in seen:
            return
        if not allow_anchor_artist and anchor_artist_norm:
            head = re.split(r"\s[–-]\s", key, maxsplit=1)[0]
            if anchor_artist_norm in head or head in anchor_artist_norm:
                return
        seen.add(key)
        queries.append(line)

    def _add_from_tracks(items: list, *, allow_anchor_artist: bool = True) -> None:
        for item in items or []:
            if len(queries) >= count * 3:
                return
            name = (item.get("name") or "").strip()
            artists = ", ".join(
                a.get("name", "") for a in (item.get("artists") or []) if a.get("name")
            )
            if not name:
                continue
            line = f"{artists} - {name}" if artists else name
            _add_line(line, allow_anchor_artist=allow_anchor_artist)

    def _add_from_search_result(result: dict, *, allow_anchor_artist: bool = True) -> None:
        _add_from_tracks(
            (result.get("tracks") or {}).get("items") or [],
            allow_anchor_artist=allow_anchor_artist,
        )

    is_soundtrack = bool(
        re.search(r"soundtrack|ost\b|tony\s*hawk|pro\s*skater|video\s*game", hints, re.I)
    )

    # --- Fast path: simple Spotify track search (most reliable) ---
    try:
        from src.config import sp
        from src.spotify import _spotify_api_ready

        if sp is None:
            logger.warning("request_channel: genre_queries: Spotify client is None")
        elif not await _spotify_api_ready():
            logger.warning(
                "request_channel: genre_queries: Spotify not authorized — using curated fallbacks"
            )
        else:
            search_terms: list[str] = []
            if is_soundtrack and hints:
                search_terms.extend([
                    f"{hints} soundtrack",
                    "tony hawk pro skater soundtrack",
                    "tony hawk soundtrack",
                    hints,
                ])
            if hints and not is_soundtrack:
                search_terms.append(f"artist:{hints.split()[0]}" if hints else "")
                search_terms.append(hints)
                # Peer artists via track search (avoids related-artists 403)
                if re.search(r"limp|bizkit", hints, re.I):
                    search_terms.extend([
                        "nu metal 2000s",
                        "korn papa roach linkin park",
                        "genre:nu metal",
                    ])
            if genre:
                search_terms.append(genre)
            if re.search(r"nu[\s\-]?metal", f"{genre} {hints}", re.I):
                search_terms.extend(["nu metal hits", "nu metal 2001", "genre:nu metal"])
            if re.search(r"rock", genre, re.I) and is_soundtrack:
                search_terms.extend([
                    "early 2000s punk rock",
                    "pop punk 2001",
                    "skate punk 2000s",
                ])

            seen_terms: set[str] = set()
            for term in search_terms:
                term = (term or "").strip()
                if not term or term.lower() in seen_terms:
                    continue
                seen_terms.add(term.lower())
                if len(queries) >= count * 2:
                    break
                try:
                    result = await asyncio.to_thread(
                        lambda q=term, lim=min(count * 2, 10): sp.search(
                            q=q, type="track", limit=lim
                        )
                    )
                    before = len(queries)
                    _add_from_search_result(result or {})
                    if len(queries) > before:
                        logger.info(
                            "request_channel: genre spotify track search q=%r +%d",
                            term, len(queries) - before,
                        )
                except Exception as exc:
                    logger.warning(
                        "request_channel: spotify track search failed q=%r: %s",
                        term, exc,
                    )

            # Artist top tracks (1 call) if we still need more and have an artist hint
            if hints and not is_soundtrack and len(queries) < count:
                try:
                    art_res = await asyncio.to_thread(
                        lambda: sp.search(q=hints, type="artist", limit=3)
                    )
                    artists = (art_res or {}).get("artists", {}).get("items") or []
                    if artists and artists[0].get("id"):
                        anchor_artist_norm = (artists[0].get("name") or "").lower()
                        aid = artists[0]["id"]
                        top = await asyncio.to_thread(
                            lambda: sp.artist_top_tracks(aid, country="US")
                        )
                        _add_from_tracks(((top or {}).get("tracks") or [])[:3])
                        # Search similar vibe tracks instead of related-artists API
                        for peer in (
                            f"{genre} similar to {artists[0].get('name')}",
                            f"{genre} 2000s",
                        ):
                            if len(queries) >= count * 2:
                                break
                            res = await asyncio.to_thread(
                                lambda q=peer: sp.search(q=q, type="track", limit=10)
                            )
                            _add_from_search_result(res or {}, allow_anchor_artist=False)
                except Exception as exc:
                    logger.warning(
                        "request_channel: artist expansion failed hints=%r: %s",
                        hints, exc,
                    )

            # One playlist sample (optional, can be slow / 403)
            if len(queries) < count:
                try:
                    from src.spotify import sample_tracks_from_playlist_search

                    pl_q = f"{genre} {hints}".strip() or genre
                    samples = await sample_tracks_from_playlist_search(
                        pl_q, limit=max(count, 6), max_playlists=2
                    )
                    for info in samples:
                        _add_line(info.get("query") or "")
                    if samples:
                        logger.info(
                            "request_channel: playlist sample q=%r n=%d",
                            pl_q, len(samples),
                        )
                except Exception as exc:
                    logger.info("request_channel: playlist sample skipped: %s", exc)

    except Exception as exc:
        logger.warning("request_channel: genre spotify block failed: %s", exc)

    # Optional Shazam pad (skipped automatically when API is broken)
    if len(queries) < count:
        try:
            from src import shazam_catalog as _shazam_cat

            if _shazam_cat.shazam_available():
                blob = f"{genre} {hints}".strip()
                for item in await _shazam_cat.search_tracks(blob, limit=count):
                    _add_line(item.get("query") or "")
        except Exception as exc:
            logger.debug("request_channel: genre shazam skipped: %s", exc)

    # Curated offline packs — guarantee names in the embed
    if len(queries) < count:
        for line in _curated_style_queries(genre, hints, count * 2):
            _add_line(line)
        logger.info(
            "request_channel: curated fallback genre=%r hints=%r total=%d",
            genre, hints[:40], len(queries),
        )

    if len(queries) > count:
        head = queries[:2] if (anchor_artist_norm or is_soundtrack) else []
        tail = [q for q in queries if q not in head]
        _random.shuffle(tail)
        queries = (head + tail)[:count]

    if not queries:
        logger.warning(
            "request_channel: genre_queries still empty genre=%r hints=%r",
            genre, hints,
        )

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
            # Interruptible: Cancel unblocks without waiting for full yt-dlp search.
            track = await _await_unless_aborted(
                _resolve_one_query(query, requester),
                should_abort,
            )
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
                    # In-place PATCH — avoid POST spam when Discord is flaky.
                    await update_player_embed(guild, text_channel)
                except Exception:
                    pass
        elif any(
            a["type"] in ("move", "remove", "remove_match", "priority", "clear_user_tracks", "skip")
            for a in plan.actions
        ):
            try:
                await update_player_embed(guild, text_channel)
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

        # Expand genre / style-blob placeholders → real "Artist - Title" for the embed.
        needs_materialize = any(
            a.get("type") == "genre_playlist"
            or (
                a.get("type") == "enqueue"
                and any(
                    # style blobs like "Limp Bizkit nu-metal"
                    (" - " not in str(q) and " – " not in str(q))
                    or re.search(
                        r"soundtrack|nu[\s\-]?metal|playlist|early\s*2000",
                        str(q),
                        re.I,
                    )
                    for q in (a.get("queries") or [])
                )
            )
            for a in plan.actions
        )
        if needs_materialize:
            try:
                await plan_msg.edit(
                    embed=discord.Embed(
                        title="🎧 Plan",
                        description="Buscando playlists y canciones del estilo…",
                        color=0x5865F2,
                    )
                )
            except Exception:
                pass
            plan = await materialize_genre_playlists(
                plan, guild_id=gid, user_id=message.author.id
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
