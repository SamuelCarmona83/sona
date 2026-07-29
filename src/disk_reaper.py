"""Periodic disk maintenance — temps, orphans, DJ cache, library reclaim."""
from __future__ import annotations

import asyncio
import logging
import time

from src.bot_instance import bot
from src.config import (
    BOT_TEXT_CHANNEL_ID,
    LIBRARY_DISK_ALERT_COOLDOWN_SEC,
    LIBRARY_MIN_FREE_MB,
    LIBRARY_RECLAIM_INTERVAL_SEC,
    OAUTH_ADMIN_USER_ID,
)

logger = logging.getLogger(__name__)

_reaper_task: asyncio.Task | None = None
_last_disk_alert_time: float = 0.0


async def _send_disk_alert(message: str) -> None:
    global _last_disk_alert_time
    now = time.time()
    if now - _last_disk_alert_time < LIBRARY_DISK_ALERT_COOLDOWN_SEC:
        return
    _last_disk_alert_time = now

    try:
        admin = await bot.fetch_user(OAUTH_ADMIN_USER_ID)
        await admin.send(message)
        logger.info("disk_reaper: alert sent to admin")
        return
    except Exception as exc:
        logger.warning("disk_reaper: could not DM admin: %s", exc)

    try:
        channel = await bot.fetch_channel(BOT_TEXT_CHANNEL_ID)
        await channel.send(message)
        logger.info("disk_reaper: alert sent to allowed channel")
    except Exception as exc:
        logger.warning("disk_reaper: could not send channel alert: %s", exc)


async def _maybe_alert_disk_pressure(summary: dict) -> None:
    if not summary.get("disk_pressure"):
        return
    free = summary.get("free_mb_after")
    free_str = f"{free:.0f}" if isinstance(free, (int, float)) else "?"
    evicted = (summary.get("reclaim") or {}).get("evicted", 0)
    freed = summary.get("bytes_freed_total") or 0
    await _send_disk_alert(
        ":warning: **Disco bajo en espacio (library)**\n"
        f"Libre: **{free_str} MB** (mínimo: {LIBRARY_MIN_FREE_MB} MB)\n"
        f"Último reclaim: {evicted} tracks, ~{freed // (1024 * 1024)} MB liberados\n"
        "El bot sigue reproduciendo; solo deja de cachear hasta que haya aire.\n"
        "En la VM: `df -h` · `du -sh spotify_cache/library` · `docker image prune -f`"
    )


async def _reaper_loop() -> None:
    # Stagger first run so startup + cookie watchdog are not all at t=0.
    interval = LIBRARY_RECLAIM_INTERVAL_SEC
    await asyncio.sleep(min(60, max(15, interval // 5 or 15)))
    while True:
        try:
            from src.library import run_disk_maintenance

            summary = await asyncio.to_thread(run_disk_maintenance, reason="reaper")
            await _maybe_alert_disk_pressure(summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("disk_reaper: maintenance error: %s", exc)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


def start_disk_reaper() -> None:
    """Start the background reaper if interval > 0 and not already running."""
    global _reaper_task
    if LIBRARY_RECLAIM_INTERVAL_SEC <= 0:
        logger.info("disk_reaper: disabled (LIBRARY_RECLAIM_INTERVAL_SEC=0)")
        return
    if _reaper_task and not _reaper_task.done():
        return
    _reaper_task = asyncio.create_task(_reaper_loop())
    logger.info(
        "disk_reaper: started (interval=%ss, min_free=%sMB)",
        LIBRARY_RECLAIM_INTERVAL_SEC,
        LIBRARY_MIN_FREE_MB,
    )
