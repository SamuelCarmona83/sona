"""Per-Discord-user Spotify OAuth token storage."""
import asyncio
import logging
import pathlib

import spotipy

from src.config import build_spotify_auth_manager

logger = logging.getLogger(__name__)

_USER_CACHE_DIR = pathlib.Path(".cache/spotify_users")
_USER_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def user_cache_path(discord_user_id: int) -> str:
    return str(_USER_CACHE_DIR / f"{discord_user_id}.cache")


def build_user_auth_manager(discord_user_id: int):
    return build_spotify_auth_manager(user_cache_path(discord_user_id))


def build_user_client(discord_user_id: int) -> spotipy.Spotify:
    return spotipy.Spotify(auth_manager=build_user_auth_manager(discord_user_id))


def is_user_linked(discord_user_id: int) -> bool:
    return pathlib.Path(user_cache_path(discord_user_id)).is_file()


async def validate_user_token(discord_user_id: int) -> bool:
    if not is_user_linked(discord_user_id):
        return False
    auth_manager = build_user_auth_manager(discord_user_id)
    cached = await asyncio.to_thread(auth_manager.cache_handler.get_cached_token)
    if not cached:
        return False
    try:
        return bool(await asyncio.to_thread(auth_manager.validate_token, cached))
    except Exception as exc:
        logger.warning("spotify_users: validate failed for user=%s: %s", discord_user_id, exc)
        return False


def unlink_user(discord_user_id: int) -> bool:
    path = pathlib.Path(user_cache_path(discord_user_id))
    if path.is_file():
        path.unlink()
        logger.info("spotify_users: unlinked user=%s", discord_user_id)
        return True
    return False


def get_authorize_url(discord_user_id: int) -> str:
    auth_manager = build_user_auth_manager(discord_user_id)
    return auth_manager.get_authorize_url(state=f"user:{discord_user_id}")


async def complete_user_oauth(discord_user_id: int, code: str) -> None:
    auth_manager = build_user_auth_manager(discord_user_id)
    await asyncio.to_thread(
        lambda: auth_manager.get_access_token(code, as_dict=False, check_cache=False)
    )


async def get_valid_user_client(discord_user_id: int) -> spotipy.Spotify | None:
    if not await validate_user_token(discord_user_id):
        return None
    return build_user_client(discord_user_id)


async def linked_users_in(connected_user_ids: list[int]) -> list[int]:
    linked = []
    for uid in connected_user_ids:
        if await validate_user_token(uid):
            linked.append(uid)
    return linked