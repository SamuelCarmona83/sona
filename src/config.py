import logging
import os
import time

import spotipy
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError

from poc_setlistfm import load_dotenv_values, get_config_value

logging.basicConfig(level=logging.INFO)
logging.getLogger("discord").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
dotenv_values = load_dotenv_values()


def _env_bool(key: str, default: str = "true") -> bool:
    return get_config_value(key, dotenv_values, default).lower() in ("1", "true", "yes")


def _env_float(key: str, default: float) -> float:
    try:
        return max(0.0, float(get_config_value(key, dotenv_values, str(default))))
    except ValueError:
        return default


SPOTIFY_OAUTH_SCOPES = (
    "user-modify-playback-state "
    "user-read-playback-state "
    "user-read-currently-playing "
    "playlist-read-private "
    "user-library-read "
    "user-top-read "
    "user-read-recently-played"
)

TASTE_CACHE_TTL_SEC = max(300, int(get_config_value("TASTE_CACHE_TTL_SEC", dotenv_values, "21600")))
SPOTIFY_TOKEN_CACHE_PATH = get_config_value("SPOTIFY_CACHE_PATH", dotenv_values, ".cache/spotify.cache")

BOT_TEXT_CHANNEL_ID = 1163479541029810226
BOT_VOICE_CHANNEL_ID = 1397428777876721716
OAUTH_ADMIN_USER_ID = 221081593790332929
SPOTIFY_OAUTH_PORT = 8888

_YTDL_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_YTDL_FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec=opus]/bestaudio/best"

_ytdl_base_options = {
    "format": _YTDL_FORMAT,
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "source_address": "0.0.0.0",
    "ignoreerrors": True,
    "extractor_retries": 3,
    # YouTube throttles chunks >10MB — https://github.com/yt-dlp/yt-dlp/wiki/FAQ
    "http_chunk_size": 10_485_760,
    "retry_sleep_functions": {
        "extractor": lambda n: 2 ** n,
        "http": lambda n: 2 ** n,
    },
    "http_headers": {
        "User-Agent": get_config_value("YTDL_USER_AGENT", dotenv_values, _YTDL_DEFAULT_USER_AGENT),
    },
}

COOKIES_FILE = get_config_value("YTDL_COOKIES_FILE", dotenv_values, "/app/cookies.txt")
COOKIES_BROWSER = get_config_value("YTDL_COOKIES_FROM_BROWSER", dotenv_values, "").strip()
COOKIES_PREFERENCE = get_config_value("YTDL_COOKIES_PREFERENCE", dotenv_values, "auto").strip().lower()
COOKIE_BROWSER_ENABLED = COOKIES_BROWSER.lower() not in ("", "0", "false", "no")
YTDL_COOKIE_MAX_AGE_HOURS = max(1, int(get_config_value("YTDL_COOKIE_MAX_AGE_HOURS", dotenv_values, "24")))
COOKIE_HEALTH_CHECK_INTERVAL_SEC = max(300, int(get_config_value("COOKIE_HEALTH_CHECK_INTERVAL_SEC", dotenv_values, "1800")))
COOKIE_ALERT_COOLDOWN_SEC = max(3600, int(get_config_value("COOKIE_ALERT_COOLDOWN_SEC", dotenv_values, "86400")))

_cookie_mtime: float = 0.0
_last_cookie_reload: float = 0.0


def _count_cookie_lines(path: str) -> int:
    if not os.path.isfile(path):
        return 0
    count = 0
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    count += 1
    except OSError:
        return 0
    return count


def get_cookie_status() -> dict:
    exists = bool(COOKIES_FILE and os.path.isfile(COOKIES_FILE))
    age_h = None
    fresh = False
    mtime = 0.0
    if exists:
        mtime = os.path.getmtime(COOKIES_FILE)
        age_h = (time.time() - mtime) / 3600
        fresh = age_h <= YTDL_COOKIE_MAX_AGE_HOURS
    return {
        "path": COOKIES_FILE,
        "exists": exists,
        "age_h": age_h,
        "fresh": fresh,
        "mtime": mtime,
        "count": _count_cookie_lines(COOKIES_FILE) if exists else 0,
        "preference": COOKIES_PREFERENCE,
        "last_reload": _last_cookie_reload,
        "using_file": "cookiefile" in _ytdl_base_options,
        "using_browser": "cookiesfrombrowser" in _ytdl_base_options,
    }


def _clear_ytdl_cookie_options() -> None:
    _ytdl_base_options.pop("cookiefile", None)
    _ytdl_base_options.pop("cookiesfrombrowser", None)


def _apply_ytdl_cookie_file(status: dict, *, log_stale: bool) -> None:
    _ytdl_base_options["cookiefile"] = COOKIES_FILE
    logger.info("yt-dlp: using cookies file: %s", COOKIES_FILE)
    if log_stale and status["age_h"] is not None and not status["fresh"]:
        logger.warning(
            "yt-dlp: cookies file is %.0fh old — YouTube tokens may have expired. "
            "Run ./refresh_cookies.sh chrome on the host if you see bot-check errors.",
            status["age_h"],
        )


def _apply_ytdl_browser_cookies() -> None:
    browser_spec = COOKIES_BROWSER or "chrome,firefox"
    _ytdl_base_options["cookiesfrombrowser"] = browser_spec
    logger.info("yt-dlp: using browser cookies: %s", browser_spec)


def apply_cookie_strategy(*, log_stale: bool = True) -> None:
    global _cookie_mtime, _last_cookie_reload

    preference = COOKIES_PREFERENCE
    if preference not in ("auto", "file", "browser"):
        logger.warning("yt-dlp: invalid YTDL_COOKIES_PREFERENCE=%s, using auto", preference)
        preference = "auto"

    status = get_cookie_status()
    _clear_ytdl_cookie_options()

    exists = status["exists"]
    fresh = status["fresh"]

    if preference == "browser":
        if COOKIE_BROWSER_ENABLED:
            _apply_ytdl_browser_cookies()
        elif exists:
            logger.warning("yt-dlp: browser cookies requested but not enabled; falling back to cookies file")
            _apply_ytdl_cookie_file(status, log_stale=log_stale)
        else:
            logger.warning("yt-dlp: no browser cookies configured and no cookies file found")
    elif preference == "file":
        if exists:
            _apply_ytdl_cookie_file(status, log_stale=log_stale)
        elif COOKIE_BROWSER_ENABLED:
            logger.warning("yt-dlp: cookies file not found; falling back to browser cookies")
            _apply_ytdl_browser_cookies()
        else:
            logger.warning("yt-dlp: no cookies file or browser cookies configured")
    else:
        if exists and fresh:
            _apply_ytdl_cookie_file(status, log_stale=log_stale)
        elif COOKIE_BROWSER_ENABLED:
            if status["age_h"] is not None:
                logger.info(
                    "yt-dlp: cookies file is %.0fh old — preferring fresh browser cookies automatically",
                    status["age_h"],
                )
            _apply_ytdl_browser_cookies()
        elif exists:
            _apply_ytdl_cookie_file(status, log_stale=log_stale)
        else:
            logger.warning("yt-dlp: no cookies file or browser cookies configured")

    if exists:
        _cookie_mtime = status["mtime"]
    _last_cookie_reload = time.time()


def reload_cookies_if_changed() -> bool:
    if not COOKIES_FILE or not os.path.isfile(COOKIES_FILE):
        return False
    mtime = os.path.getmtime(COOKIES_FILE)
    if mtime == _cookie_mtime:
        return False
    logger.info("cookie_health: cookies.txt changed on disk, reloading session")
    apply_cookie_strategy(log_stale=True)
    return True


apply_cookie_strategy()
YTDL_OPTIONS = _ytdl_base_options

YTDL_OPTIONS_NO_COOKIES = {
    **_ytdl_base_options,
    "extractor_args": {
        "youtube": {
            "player_client": ["android_vr", "web", "web_safari"],
        },
    },
}
YTDL_OPTIONS_NO_COOKIES.pop("cookiefile", None)
YTDL_OPTIONS_NO_COOKIES.pop("cookiesfrombrowser", None)

YTDL_SEARCH_CONCURRENCY = max(1, int(get_config_value("YTDL_SEARCH_CONCURRENCY", dotenv_values, "2")))
YTDL_SEARCH_DELAY_SEC = _env_float("YTDL_SEARCH_DELAY_SEC", 0.75)
YTDL_SEARCH_JITTER_SEC = _env_float("YTDL_SEARCH_JITTER_SEC", 0.35)
YTDL_SEARCH_DELAY_URGENT_SEC = _env_float("YTDL_SEARCH_DELAY_URGENT_SEC", 0.5)

FFMPEG_LOUDNESS_NORMALIZE_FILTER = "dynaudnorm=p=0.9:s=5"
normalize_audio_enabled = _env_bool("NORMALIZE_AUDIO")


def _build_ffmpeg_options(*, for_streaming: bool) -> dict:
    normalize_suffix = f" -af {FFMPEG_LOUDNESS_NORMALIZE_FILTER}" if normalize_audio_enabled else ""
    if for_streaming:
        return {
            "before_options": (
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
                "-probesize 10M -analyzeduration 2000000 "
                "-thread_queue_size 4096"
            ),
            "options": f"-vn -bufsize 512k -strict -2{normalize_suffix}",
        }
    return {
        "before_options": "-nostdin",
        "options": f"-vn -strict -2{normalize_suffix}",
    }


FFMPEG_OPTIONS = _build_ffmpeg_options(for_streaming=True)

SEARCH_RESULT_COUNT = 5
MIN_SEARCH_SCORE = 6.0
RADIO_REQUESTER_LABEL = "📻 Radio"
RADIO_QUEUE_REFILL_THRESHOLD = 3
RADIO_QUEUE_TARGET_SIZE = 5
LLM_SCORE_MARGIN = 4.5
LLM_RANKING_TIMEOUT = 8.0
LLM_ALBUM_TRACK_RANKING_LIMIT = 3
ANTHROPIC_API_KEY = get_config_value("ANTHROPIC_API_KEY", dotenv_values, "")
ANTHROPIC_MODEL = "claude-haiku-4-5"

DJ_ANNOUNCER_ENABLED = _env_bool("DJ_ANNOUNCER_ENABLED")
DJ_VOICE = get_config_value("DJ_VOICE", dotenv_values, "es-MX-DaliaNeural")
DJ_ANNOUNCE_COOLDOWN_SEC = int(get_config_value("DJ_ANNOUNCE_COOLDOWN", dotenv_values, "120"))
DJ_VOLUME = get_config_value("DJ_VOLUME", dotenv_values, "1.2")
DJ_FUN_FACT_INTERVAL_TRACKS = int(get_config_value("DJ_FUN_FACT_INTERVAL", dotenv_values, "5"))

LIBRARY_ENABLED = _env_bool("LIBRARY_ENABLED")
LIBRARY_PATH = get_config_value("LIBRARY_PATH", dotenv_values, ".cache/library")
LIBRARY_MAX_TRACKS = max(10, int(get_config_value("LIBRARY_MAX_TRACKS", dotenv_values, "500")))
LIBRARY_MAX_MB = max(100, int(get_config_value("LIBRARY_MAX_MB", dotenv_values, "2048")))
LIBRARY_AUTO_DOWNLOAD = _env_bool("LIBRARY_AUTO_DOWNLOAD")
LIBRARY_MIN_PLAYS_TO_PIN = max(1, int(get_config_value("LIBRARY_MIN_PLAYS_TO_PIN", dotenv_values, "3")))
LIBRARY_FETCH_COVERS = _env_bool("LIBRARY_FETCH_COVERS")
LIBRARY_AUTO_ENRICH = _env_bool("LIBRARY_AUTO_ENRICH", "false")  # background enrich on plays *after* first discovery; first-time library additions always enrich for artwork/metadata
LIBRARY_EMBED_METADATA = _env_bool("LIBRARY_EMBED_METADATA")
YOUTUBE_URL_CACHE_TTL_SEC = max(60, int(get_config_value("YOUTUBE_URL_CACHE_TTL_SEC", dotenv_values, "1800")))

GENIUS_CLIENT_ID = get_config_value("GENIUS_CLIENT_ID", dotenv_values, "")
GENIUS_CLIENT_SECRET = get_config_value("GENIUS_CLIENT_SECRET", dotenv_values, "")
GENIUS_ACCESS_TOKEN = get_config_value("GENIUS_ACCESS_TOKEN", dotenv_values, "")

if GENIUS_ACCESS_TOKEN:
    logger.info("Genius API configured (GENIUS_ACCESS_TOKEN present) — will enrich artwork + song links via api.genius.com")
elif GENIUS_CLIENT_ID and GENIUS_CLIENT_SECRET:
    logger.info("Genius client_id/secret present (ACCESS_TOKEN recommended for direct API use)")

GENIUS_ENABLED = bool(GENIUS_ACCESS_TOKEN)

FFMPEG_LOCAL_OPTIONS = _build_ffmpeg_options(for_streaming=False)

YOUTUBE_TITLE_NOISE_TERMS = {
    "official",
    "video",
    "audio",
    "lyrics",
    "lyric",
    "hd",
    "hq",
    "4k",
    "mv",
    "music",
    "visualizer",
    "visualiser",
    "clip",
    "version",
    "full",
}
YOUTUBE_TITLE_VARIANT_TERMS = {"live", "remix", "cover", "karaoke", "acoustic", "instrumental"}
YOUTUBE_PREFERRED_CHANNEL_HINTS = ("topic", "vevo", "official", "records", "music")
MIN_SPOTIFY_REFINEMENT_SCORE = 7.5


class NonInteractiveSpotifyOAuth(SpotifyOAuth):
    """Spotify OAuth that never blocks on stdin (required for Docker/headless)."""

    _AUTH_REQUIRED_MSG = "Spotify requiere autorización. Usa !auth en Discord."

    def get_auth_response(self, open_browser=None):
        raise SpotifyOauthError(self._AUTH_REQUIRED_MSG)

    def get_access_token(self, code=None, as_dict=True, check_cache=True):
        if code:
            return super().get_access_token(code=code, as_dict=as_dict, check_cache=False)

        if check_cache:
            token_info = self.validate_token(self.cache_handler.get_cached_token())
            if token_info is not None:
                if self.is_token_expired(token_info):
                    try:
                        token_info = self.refresh_access_token(token_info["refresh_token"])
                    except SpotifyOauthError as exc:
                        cache_path = getattr(self, "cache_path", None)
                        if cache_path and os.path.isfile(cache_path):
                            try:
                                os.remove(cache_path)
                            except OSError:
                                pass
                        raise SpotifyOauthError(
                            f"Token Spotify expirado ({exc}). Usa !auth en Discord."
                        ) from exc
                return token_info if as_dict else token_info["access_token"]

        raise SpotifyOauthError(self._AUTH_REQUIRED_MSG)


def build_spotify_auth_manager(cache_path: str) -> NonInteractiveSpotifyOAuth:
    client_id = get_config_value("SPOTIFY_CLIENT_ID", dotenv_values)
    client_secret = get_config_value("SPOTIFY_CLIENT_SECRET", dotenv_values)
    redirect_uri = get_config_value("SPOTIFY_REDIRECT_URI", dotenv_values, "http://localhost:8888/callback")
    if not client_id or not client_secret:
        raise ValueError("Faltan credenciales Spotify (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET).")
    cache_dir = os.path.dirname(cache_path) or "."
    os.makedirs(cache_dir, exist_ok=True)
    return NonInteractiveSpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SPOTIFY_OAUTH_SCOPES,
        cache_path=cache_path,
        open_browser=False,
    )


def build_spotify_client(_legacy_dotenv: dict) -> spotipy.Spotify:
    return spotipy.Spotify(auth_manager=build_spotify_auth_manager(SPOTIFY_TOKEN_CACHE_PATH))


def init_spotify_client() -> tuple[spotipy.Spotify | None, bool]:
    try:
        client = build_spotify_client(dotenv_values)
        logger.info("Spotify client initialized.")
        return client, True
    except (ValueError, Exception) as exc:
        logger.warning(
            "Spotify unavailable: %s. Radio + fun facts will use fallback mode.",
            exc,
        )
        return None, False


bot_token = get_config_value("BOT_TOKEN", dotenv_values)
if not bot_token:
    raise ValueError("Falta BOT_TOKEN en variables de entorno o en .env.")

sp, SPOTIFY_AVAILABLE = init_spotify_client()