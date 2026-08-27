"""Player embed update: no spam on Discord blips."""
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")

from src import playback  # noqa: E402


class _Exc(Exception):
    def __init__(self, msg: str = "", *, status=None, code=None):
        super().__init__(msg)
        self.status = status
        self.code = code


class DiscordErrorClassificationTests(unittest.TestCase):
    def test_transient_503(self):
        self.assertTrue(
            playback._is_transient_discord_error(
                _Exc("503 Service Unavailable (error code: 0): upstream connect", status=503)
            )
        )

    def test_unknown_message(self):
        self.assertTrue(playback._is_unknown_message_error(_Exc("Unknown Message", code=10008)))
        self.assertFalse(playback._is_transient_discord_error(_Exc("Unknown Message", code=10008)))

    def test_403_not_transient(self):
        self.assertFalse(playback._is_transient_discord_error(_Exc("403 Forbidden", status=403)))


class UpdatePlayerEmbedNoSpamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guild = MagicMock()
        self.guild.id = 42
        self.channel = MagicMock()
        self.channel.id = 99
        # Isolate session state
        playback._sessions.clear()
        playback._clear_player_http_cooldown()
        session = playback.guild_session(42)
        self.session = session
        old = MagicMock()
        old.id = 111
        old.channel = self.channel
        session.player_message = old
        session.player_channel = self.channel
        self.old = old

    async def asyncTearDown(self):
        playback._sessions.clear()
        playback._clear_player_http_cooldown()

    async def test_transient_patch_keeps_old_message(self):
        with patch.object(
            playback,
            "_patch_player_message",
            new=AsyncMock(side_effect=_Exc("503 upstream", status=503)),
        ), patch.object(
            playback, "_post_player_message", new=AsyncMock()
        ) as post, patch.object(
            playback, "_build_v2_payload", return_value={"flags": 32768, "components": []}
        ), patch.object(
            playback, "_ensure_player_refresh"
        ):
            await playback.update_player_embed(self.guild, self.channel)

        post.assert_not_called()
        self.assertIs(self.session.player_message, self.old)
        self.assertTrue(playback._player_http_on_cooldown())

    async def test_cooldown_skips_subsequent_calls(self):
        patch_msg = AsyncMock(side_effect=_Exc("503", status=503))
        post = AsyncMock()
        with patch.object(playback, "_patch_player_message", new=patch_msg), patch.object(
            playback, "_post_player_message", new=post
        ), patch.object(
            playback, "_build_v2_payload", return_value={"flags": 32768, "components": []}
        ), patch.object(
            playback, "_ensure_player_refresh"
        ):
            await playback.update_player_embed(self.guild, self.channel)
            await playback.update_player_embed(self.guild, self.channel)
            await playback.refresh_player_embed_fresh(self.guild, self.channel)

        # Only the first failing PATCH should hit the network; cooldown swallows the rest.
        self.assertEqual(patch_msg.await_count, 1)
        post.assert_not_called()

    async def test_unknown_message_posts_replacement(self):
        new_msg = MagicMock()
        new_msg.id = 222
        with patch.object(
            playback,
            "_patch_player_message",
            new=AsyncMock(side_effect=_Exc("Unknown Message", code=10008)),
        ), patch.object(
            playback, "_post_player_message", new=AsyncMock(return_value=new_msg)
        ) as post, patch.object(
            playback, "_delete_player_message_quiet", new=AsyncMock()
        ), patch.object(
            playback, "_build_v2_payload", return_value={"flags": 32768, "components": []}
        ), patch.object(
            playback, "_ensure_player_refresh"
        ):
            await playback.update_player_embed(self.guild, self.channel)

        post.assert_awaited_once()
        self.assertIs(self.session.player_message, new_msg)

    async def test_refresh_fresh_keeps_old_if_post_fails(self):
        patched = MagicMock()
        patched.id = 111
        with patch.object(
            playback,
            "_post_player_message",
            new=AsyncMock(side_effect=_Exc("503", status=503)),
        ), patch.object(
            playback,
            "_patch_player_message",
            new=AsyncMock(return_value=patched),
        ) as patch_msg, patch.object(
            playback, "_delete_player_message_quiet", new=AsyncMock()
        ) as delete, patch.object(
            playback, "_build_v2_payload", return_value={"flags": 32768, "components": []}
        ), patch.object(
            playback, "_ensure_player_refresh"
        ):
            await playback.refresh_player_embed_fresh(self.guild, self.channel)

        delete.assert_not_called()
        # Transient POST → try in-place PATCH of the existing player message.
        patch_msg.assert_awaited_once()
        self.assertIs(self.session.player_message, patched)

    async def test_refresh_fresh_deletes_old_after_successful_post(self):
        new_msg = MagicMock()
        new_msg.id = 333
        with patch.object(
            playback, "_post_player_message", new=AsyncMock(return_value=new_msg)
        ), patch.object(
            playback, "_delete_player_message_quiet", new=AsyncMock()
        ) as delete, patch.object(
            playback, "_build_v2_payload", return_value={"flags": 32768, "components": []}
        ), patch.object(
            playback, "_ensure_player_refresh"
        ):
            await playback.refresh_player_embed_fresh(self.guild, self.channel)

        delete.assert_awaited_once_with(self.old)
        self.assertIs(self.session.player_message, new_msg)


class StreamForbiddenRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        playback._sessions.clear()

    def test_classifies_ffmpeg_403(self):
        self.assertTrue(
            playback._is_forbidden_stream_error(
                "Error opening input files: Server returned 403 Forbidden"
            )
        )
        self.assertTrue(
            playback._is_forbidden_stream_error("FFmpeg exited with code 8. Stderr: <no stderr>")
        )
        self.assertFalse(playback._is_forbidden_stream_error("Connection reset by peer"))

    async def test_requeue_refreshes_url_once(self):
        session = playback.guild_session(7)
        track = {
            "title": "Fotografía",
            "url": "https://rr1.googlevideo.com/stale",
            "video_id": "iBXesLl7IzM",
            "yt_query": "Juanes - Fotografía",
        }
        fresh = {
            "url": "https://rr1.googlevideo.com/fresh",
            "http_headers": {"User-Agent": "tv"},
            "video_id": "iBXesLl7IzM",
        }
        with patch.object(
            playback, "refresh_playback_url", new=AsyncMock(return_value=fresh)
        ):
            ok = await playback._requeue_fresh_stream(session, track)
        self.assertTrue(ok)
        self.assertEqual(track["url"], fresh["url"])
        self.assertEqual(session.queue[0]["url"], fresh["url"])
        self.assertTrue(track["_yt_stream_retried"])

        with patch.object(
            playback, "refresh_playback_url", new=AsyncMock(return_value=fresh)
        ) as refresh:
            ok_again = await playback._requeue_fresh_stream(session, track)
        self.assertFalse(ok_again)
        refresh.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
