"""Cookies are optional: load failures must degrade to cookieless, never crash."""

import pathlib
import tempfile
import unittest


class TestBrowserCookiesSpec(unittest.TestCase):
    def test_parse_simple_browser(self):
        from src.config import _parse_browser_cookies_spec

        self.assertEqual(
            _parse_browser_cookies_spec("chrome"),
            ("chrome", None, None, None),
        )

    def test_parse_legacy_multi_takes_first(self):
        from src.config import _parse_browser_cookies_spec

        self.assertEqual(
            _parse_browser_cookies_spec("chrome,firefox"),
            ("chrome", None, None, None),
        )

    def test_parse_profile(self):
        from src.config import _parse_browser_cookies_spec

        self.assertEqual(
            _parse_browser_cookies_spec("firefox:default"),
            ("firefox", "default", None, None),
        )

    def test_parse_empty(self):
        from src.config import _parse_browser_cookies_spec

        self.assertIsNone(_parse_browser_cookies_spec(""))
        self.assertIsNone(_parse_browser_cookies_spec("   "))


class TestCookieLoadErrorDetection(unittest.TestCase):
    def test_detects_keyword(self):
        from src.config import is_cookie_load_error

        self.assertTrue(is_cookie_load_error(RuntimeError("failed to load cookies")))
        self.assertFalse(is_cookie_load_error(RuntimeError("network timeout")))

    def test_detects_nested_cause(self):
        from src.config import CookieLoadError, is_cookie_load_error

        if CookieLoadError is None:
            self.skipTest("CookieLoadError not available")
        outer = RuntimeError("wrapper")
        outer.__cause__ = CookieLoadError("failed to load cookies")
        self.assertTrue(is_cookie_load_error(outer))


class TestFilePreferenceNoBrowserFallback(unittest.TestCase):
    def test_file_mode_missing_file_stays_cookieless(self):
        """Docker default preference=file must not fall back to browser cookies."""
        import src.config as cfg

        prev_pref = cfg.COOKIES_PREFERENCE
        prev_file = cfg.COOKIES_FILE
        prev_browser = cfg.COOKIE_BROWSER_ENABLED
        prev_unusable = cfg._cookies_unusable
        try:
            cfg.COOKIES_PREFERENCE = "file"
            cfg.COOKIES_FILE = "/tmp/definitely-missing-sona-cookies.txt"
            cfg.COOKIE_BROWSER_ENABLED = True
            cfg._cookies_unusable = False
            cfg.apply_cookie_strategy(log_stale=False)
            status = cfg.get_cookie_status()
            self.assertFalse(status["using_file"])
            self.assertFalse(status["using_browser"])
        finally:
            cfg.COOKIES_PREFERENCE = prev_pref
            cfg.COOKIES_FILE = prev_file
            cfg.COOKIE_BROWSER_ENABLED = prev_browser
            cfg._cookies_unusable = prev_unusable
            cfg.apply_cookie_strategy(log_stale=False)


class TestCookieFallbackYDL(unittest.TestCase):

    def setUp(self):
        import src.config as cfg

        self._cfg = cfg
        self._prev_unusable = cfg._cookies_unusable
        cfg._cookies_unusable = False

    def tearDown(self):
        self._cfg._cookies_unusable = self._prev_unusable

    def test_broken_browser_string_falls_back_cookieless(self):
        from src.config import _CookieFallbackYDL, get_cookie_status

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "ignoreerrors": True,
            # Invalid for Python API (must be a 4-tuple) — used to crash with CookieLoadError.
            "cookiesfrombrowser": "chrome",
        }
        with _CookieFallbackYDL(opts) as ydl:
            self.assertIsNone(ydl.params.get("cookiefile"))
            self.assertIsNone(ydl.params.get("cookiesfrombrowser"))
        self.assertTrue(get_cookie_status()["unusable"])

    def test_directory_cookiefile_falls_back_cookieless(self):
        from src.config import _CookieFallbackYDL, get_cookie_status

        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = pathlib.Path(tmp) / "cookies.txt"
            cookie_path.mkdir()  # Docker can mount a directory if host file is missing
            opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "ignoreerrors": True,
                "cookiefile": str(cookie_path),
            }
            with _CookieFallbackYDL(opts) as ydl:
                self.assertIsNone(ydl.params.get("cookiefile"))
                self.assertIsNone(ydl.params.get("cookiesfrombrowser"))
        self.assertTrue(get_cookie_status()["unusable"])

    def test_valid_cookiefile_still_used(self):
        from src.config import _CookieFallbackYDL

        cookies = pathlib.Path("cookies.txt")
        if not cookies.is_file():
            self.skipTest("cookies.txt not present in workspace")
        self._cfg._cookies_unusable = False
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "ignoreerrors": True,
            "cookiefile": str(cookies.resolve()),
        }
        with _CookieFallbackYDL(opts) as ydl:
            jar = ydl.cookiejar
            self.assertGreater(len(list(jar)), 0)
            self.assertEqual(ydl.params.get("cookiefile"), str(cookies.resolve()))


if __name__ == "__main__":
    unittest.main()
