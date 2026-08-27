"""YouTube 403 classification and player-client defaults."""
import os
import unittest

os.environ.setdefault("BOT_TOKEN", "test-token")

from src import config as app_config  # noqa: E402
from src import youtube  # noqa: E402
from src.config import ffmpeg_options_for_url  # noqa: E402


class YoutubeAuthClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        youtube.clear_youtube_auth_failed()

    def tearDown(self) -> None:
        youtube.clear_youtube_auth_failed()

    def test_media_403_is_not_auth_failure(self) -> None:
        msg = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        self.assertTrue(youtube.is_media_forbidden_error(msg))
        self.assertFalse(youtube.maybe_detect_auth_failure(msg))
        self.assertFalse(youtube.is_youtube_auth_failed())

    def test_ffmpeg_403_is_not_auth_failure(self) -> None:
        msg = "Error opening input files: Server returned 403 Forbidden (access denied)"
        self.assertTrue(youtube.is_media_forbidden_error(msg))
        self.assertFalse(youtube.maybe_detect_auth_failure(msg))
        self.assertFalse(youtube.is_youtube_auth_failed())

    def test_itag18_is_not_audio_only(self) -> None:
        self.assertFalse(
            youtube.is_audio_only_format({
                "url": "https://rr2.googlevideo.com/videoplayback?itag=18&mime=video%2Fmp4",
                "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2",
            })
        )
        self.assertTrue(
            youtube.is_audio_only_format({
                "url": "https://rr2.googlevideo.com/videoplayback?itag=140",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
            })
        )
        self.assertFalse(
            youtube.is_audio_only_format({
                "url": "https://www.youtube.com/watch?v=j7L-payNDG4",
                "id": "j7L-payNDG4",
            })
        )

    def test_search_entries_drop_itag18_url(self) -> None:
        cands = youtube._parse_search_entries(
            "query",
            {
                "entries": [{
                    "id": "j7L-payNDG4",
                    "title": "Ad",
                    "url": "https://rr2.googlevideo.com/videoplayback?itag=18&mime=video%2Fmp4",
                    "vcodec": "avc1.42001E",
                    "acodec": "mp4a.40.2",
                    "availability": "public",
                }],
            },
        )
        self.assertEqual(len(cands), 1)
        self.assertIsNone(cands[0]["url"])
        self.assertEqual(cands[0]["video_id"], "j7L-payNDG4")

    def test_search_entries_keep_flat_watch_results(self) -> None:
        cands = youtube._parse_search_entries(
            "query",
            {
                "entries": [{
                    "id": "gfnHmM_Z9Zg",
                    "title": "Caramelos De Cianuro - Retrovisor",
                    "url": "https://www.youtube.com/watch?v=gfnHmM_Z9Zg",
                    "duration": 240,
                    "uploader": "Topic",
                }],
            },
        )
        self.assertEqual(len(cands), 1)
        self.assertIsNone(cands[0]["url"])
        self.assertEqual(cands[0]["video_id"], "gfnHmM_Z9Zg")
        self.assertIn("gfnHmM_Z9Zg", cands[0]["webpage_url"])

    def test_bot_check_is_auth_failure(self) -> None:
        msg = "ERROR: [youtube] Sign in to confirm you're not a bot"
        self.assertTrue(youtube.maybe_detect_auth_failure(msg))
        self.assertTrue(youtube.is_youtube_auth_failed())

    def test_logger_media_403_does_not_trip_auth(self) -> None:
        youtube._YtDlpLogger().error(
            "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        )
        self.assertFalse(youtube.is_youtube_auth_failed())

    def test_po_token_warning_is_not_media_forbidden(self) -> None:
        msg = (
            "[youtube] abc: ios client https formats require a GVS PO Token "
            "which was not provided. They will be skipped as they may yield HTTP Error 403."
        )
        self.assertFalse(youtube.is_media_forbidden_error(msg))
        self.assertFalse(youtube.maybe_detect_auth_failure(msg))


class YoutubePlayerClientTests(unittest.TestCase):
    def test_cookieless_opts_include_android_vr(self) -> None:
        clients = (
            app_config.YTDL_OPTIONS_NO_COOKIES.get("extractor_args", {})
            .get("youtube", {})
            .get("player_client", [])
        )
        self.assertIn("android_vr", clients)
        self.assertIn("tv", clients)

    def test_default_opts_prefer_ios_over_tv(self) -> None:
        clients = (
            app_config.YTDL_OPTIONS.get("extractor_args", {})
            .get("youtube", {})
            .get("player_client", [])
        )
        self.assertEqual(clients[0], "ios")
        self.assertNotEqual(clients[0], "android_vr")

    def test_search_format_is_audio_only(self) -> None:
        fmt = app_config.YTDL_OPTIONS.get("format") or ""
        self.assertIn("bestaudio", fmt)
        self.assertFalse(fmt.endswith("/best") or "/best[" in fmt)

    def test_download_format_allows_low_res_fallback(self) -> None:
        self.assertIn("bestaudio", app_config.YTDL_DOWNLOAD_FORMAT)
        self.assertIn("best[height<=480]", app_config.YTDL_DOWNLOAD_FORMAT)

    def test_default_opts_do_not_force_desktop_ua(self) -> None:
        headers = app_config.YTDL_OPTIONS.get("http_headers") or {}
        if not app_config._explicit_ytdl_user_agent:
            self.assertNotIn("User-Agent", headers)


class FfmpegStreamHeaderTests(unittest.TestCase):
    def test_remote_adds_referer(self) -> None:
        opts = ffmpeg_options_for_url(local=False, headers={})
        self.assertIn("-referer", opts["before_options"])
        self.assertIn("youtube.com", opts["before_options"])

    def test_radio_does_not_add_youtube_referer(self) -> None:
        opts = ffmpeg_options_for_url(local=False, headers=None)
        self.assertNotIn("-referer", opts["before_options"])
        self.assertNotIn("-user_agent", opts["before_options"])

    def test_remote_uses_extract_user_agent(self) -> None:
        opts = ffmpeg_options_for_url(
            local=False,
            headers={"User-Agent": "com.google.android.apps.youtube.vr/1.56"},
        )
        self.assertIn("-user_agent", opts["before_options"])
        self.assertIn("youtube.vr", opts["before_options"])

    def test_local_does_not_add_http_headers(self) -> None:
        opts = ffmpeg_options_for_url(local=True, headers={"User-Agent": "x"})
        self.assertNotIn("-referer", opts["before_options"])
        self.assertNotIn("-user_agent", opts["before_options"])


if __name__ == "__main__":
    unittest.main()
