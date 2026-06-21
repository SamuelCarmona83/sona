"""Cookie health watchdog — hot-reload, admin alerts, status summary."""
import asyncio
import logging
import time

import discord

from src.config import (
    ADMIN_USER_ID,
    ALLOWED_CHANNEL_ID,
    COOKIE_ALERT_COOLDOWN_SEC,
    COOKIE_HEALTH_CHECK_INTERVAL_SEC,
    YTDL_COOKIE_MAX_AGE_HOURS,
    get_cookie_status,
    reload_cookies_if_changed,
)
from src.bot_instance import bot

logger = logging.getLogger(__name__)

_watchdog_task: asyncio.Task | None = None
_last_alert_time: float = 0.0
_last_auth_failure_time: float = 0.0
_auth_failure_count: int = 0


def record_auth_failure() -> None:
    global _last_auth_failure_time, _auth_failure_count
    _last_auth_failure_time = time.time()
    _auth_failure_count += 1


def get_health_summary() -> dict:
    status = get_cookie_status()
    from src.library import get_stats
    lib = get_stats()
    from src.youtube import is_youtube_auth_failed, is_youtube_rate_limited
    return {
        **status,
        "max_age_h": YTDL_COOKIE_MAX_AGE_HOURS,
        "auth_failed": is_youtube_auth_failed(),
        "rate_limited": is_youtube_rate_limited(),
        "last_auth_failure": _last_auth_failure_time,
        "auth_failure_count": _auth_failure_count,
        "library_on_disk": lib.get("on_disk", 0),
        "library_size_mb": lib.get("size_mb", 0),
    }


def needs_admin_attention() -> bool:
    status = get_cookie_status()
    if not status["exists"]:
        return True
    if status["age_h"] is not None and status["age_h"] > YTDL_COOKIE_MAX_AGE_HOURS:
        return True
    from src.youtube import is_youtube_auth_failed
    if is_youtube_auth_failed():
        return True
    if _last_auth_failure_time and (time.time() - _last_auth_failure_time) < 3600:
        return True
    return False


async def _send_admin_alert(message: str) -> None:
    global _last_alert_time
    now = time.time()
    if now - _last_alert_time < COOKIE_ALERT_COOLDOWN_SEC:
        return
    _last_alert_time = now

    try:
        admin = await bot.fetch_user(ADMIN_USER_ID)
        await admin.send(message)
        logger.info("cookie_health: alert sent to admin")
        return
    except Exception as exc:
        logger.warning("cookie_health: could not DM admin: %s", exc)

    try:
        channel = await bot.fetch_channel(ALLOWED_CHANNEL_ID)
        await channel.send(message)
        logger.info("cookie_health: alert sent to allowed channel")
    except Exception as exc:
        logger.warning("cookie_health: could not send channel alert: %s", exc)


async def _watchdog_loop() -> None:
    while True:
        try:
            await asyncio.sleep(COOKIE_HEALTH_CHECK_INTERVAL_SEC)
            reloaded = reload_cookies_if_changed()
            if reloaded:
                from src.youtube import clear_youtube_auth_failed
                clear_youtube_auth_failed()

            if needs_admin_attention():
                status = get_cookie_status()
                age = status["age_h"]
                age_str = f"{age:.0f}h" if age is not None else "desconocida"
                await _send_admin_alert(
                    ":warning: **Cookies de YouTube necesitan atención**\n"
                    f"Edad del archivo: **{age_str}** (máx recomendado: {YTDL_COOKIE_MAX_AGE_HOURS}h)\n"
                    "En el Mac, ejecuta:\n"
                    "```\n./refresh_cookies.sh chrome\n```\n"
                    "El bot detectará el cambio automáticamente (sin reiniciar Docker).\n"
                    "Mientras tanto, la biblioteca local y fallbacks sin login siguen activos."
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("cookie_health: watchdog error: %s", exc)


def start_cookie_watchdog() -> None:
    global _watchdog_task
    if _watchdog_task and not _watchdog_task.done():
        return
    _watchdog_task = asyncio.create_task(_watchdog_loop())
    status = get_cookie_status()
    logger.info(
        "cookie_health: watchdog started (interval=%ds, cookies age=%s)",
        COOKIE_HEALTH_CHECK_INTERVAL_SEC,
        f"{status['age_h']:.0f}h" if status.get("age_h") is not None else "n/a",
    )