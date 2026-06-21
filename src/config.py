import logging
import os
import time

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from poc_setlistfm import load_dotenv_values, get_config_value

logging.basicConfig(level=logging.INFO)
# Suppress verbose discord.py internals (voice, gateway, player)
logging.getLogger("discord").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
dotenv_values = load_dotenv_values()


SCOPES = (
    "user-modify-playback-state "
    "user-read-playback-state "
    "user-read-currently-playing "
    "playlist-read-private"
)

ALLOWED_CHANNEL_ID = 1163479541029810226
VOICE_CHANNEL_ID   = 1397428777876721716
CACHE_PATH = get_config_value("SPOTIFY_CACHE_PATH", dotenv_values, ".cache/spotify.cache")
OAUTH_PORT = 8888

# Only this user can run !auth; the OAuth URL is sent via DM (never visible in the channel)
ADMIN_USER_ID = 221081593790332929

# ---------------------------------------------------------------------------
# YT-DLP
# ---------------------------------------------------------------------------

# Build yt-dlp options with optional cookie support
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_ytdl_base_options = {
    # Prefer m4a/AAC: one transcode (AAC→Opus) is cleaner than opus→PCM→Opus.
    # Falls back to webm/opus if m4a is not available, then any best audio.
    "format": "bestaudio[ext=m4a]/bestaudio[acodec=opus]/bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "source_address": "0.0.0.0",
    "ignoreerrors": True,  # Skip unavailable videos in search results instead of failing
    "extractor_retries": 3,
    # YouTube throttles chunks >10MB (see FAQ: https://github.com/yt-dlp/yt-dlp/wiki/FAQ)
    "http_chunk_size": 10_485_760,
    # Exponential backoff on HTTP 429 (Too Many Requests) and 5xx errors
    "retry_sleep_functions": {
        "extractor": lambda n: 2 ** n,  # 1s, 2s, 4s, 8s...
        "http": lambda n: 2 ** n,
    },
    # YouTube bot detection workarounds — UA must match the browser that exported cookies
    "http_headers": {
        "User-Agent": get_config_value("YTDL_USER_AGENT", dotenv_values, _DEFAULT_USER_AGENT),
    },
}

# Cookie strategy:
# - auto (default): use file while fresh; prefer browser cookies when the file is stale
# - file: always prefer the exported cookie file
# - browser: always prefer browser cookies when enabled
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


def _clear_cookie_opts() -> None:
    _ytdl_base_options.pop("cookiefile", None)
    _ytdl_base_options.pop("cookiesfrombrowser", None)


def apply_cookie_strategy(*, log_stale: bool = True) -> None:
    """Re-evaluate cookie source and update YTDL_OPTIONS in place."""
    global _cookie_mtime, _last_cookie_reload

    preference = COOKIES_PREFERENCE
    if preference not in ("auto", "file", "browser"):
        logger.warning("yt-dlp: invalid YTDL_COOKIES_PREFERENCE=%s, using auto", preference)
        preference = "auto"

    status = get_cookie_status()
    _clear_cookie_opts()

    def _use_file() -> None:
        _ytdl_base_options["cookiefile"] = COOKIES_FILE
        logger.info("yt-dlp: using cookies file: %s", COOKIES_FILE)
        if log_stale and status["age_h"] is not None and not status["fresh"]:
            logger.warning(
                "yt-dlp: cookies file is %.0fh old — YouTube tokens may have expired. "
                "Run ./refresh_cookies.sh chrome on the host if you see bot-check errors.",
                status["age_h"],
            )

    def _use_browser() -> None:
        browser_spec = COOKIES_BROWSER or "chrome,firefox"
        _ytdl_base_options["cookiesfrombrowser"] = browser_spec
        logger.info("yt-dlp: using browser cookies: %s", browser_spec)

    exists = status["exists"]
    fresh = status["fresh"]

    if preference == "browser":
        if COOKIE_BROWSER_ENABLED:
            _use_browser()
        elif exists:
            logger.warning("yt-dlp: browser cookies requested but not enabled; falling back to cookies file")
            _use_file()
        else:
            logger.warning("yt-dlp: no browser cookies configured and no cookies file found")
    elif preference == "file":
        if exists:
            _use_file()
        elif COOKIE_BROWSER_ENABLED:
            logger.warning("yt-dlp: cookies file not found; falling back to browser cookies")
            _use_browser()
        else:
            logger.warning("yt-dlp: no cookies file or browser cookies configured")
    else:
        if exists and fresh:
            _use_file()
        elif COOKIE_BROWSER_ENABLED:
            if status["age_h"] is not None:
                logger.info(
                    "yt-dlp: cookies file is %.0fh old — preferring fresh browser cookies automatically",
                    status["age_h"],
                )
            _use_browser()
        elif exists:
            _use_file()
        else:
            logger.warning("yt-dlp: no cookies file or browser cookies configured")

    if exists:
        _cookie_mtime = status["mtime"]
    _last_cookie_reload = time.time()


def reload_cookies_if_changed() -> bool:
    """Hot-reload when cookies.txt mtime changes (yt-dlp writeback or host refresh)."""
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

# Cookieless yt-dlp fallback (no login) for when cookies are stale or auth fails
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

# App-level YouTube search throttling. Bot checks are commonly triggered by
# bursty parallel search traffic even when cookies are valid.
YTDL_SEARCH_CONCURRENCY = max(1, int(get_config_value("YTDL_SEARCH_CONCURRENCY", dotenv_values, "2")))
try:
    YTDL_SEARCH_DELAY_SEC = max(0.0, float(get_config_value("YTDL_SEARCH_DELAY_SEC", dotenv_values, "0.75")))
except ValueError:
    YTDL_SEARCH_DELAY_SEC = 0.75
try:
    YTDL_SEARCH_JITTER_SEC = max(0.0, float(get_config_value("YTDL_SEARCH_JITTER_SEC", dotenv_values, "0.35")))
except ValueError:
    YTDL_SEARCH_JITTER_SEC = 0.35

# -reconnect*        keeps the stream alive on transient network errors.
# -probesize         10M is enough for audio; 200M was stalling the pipeline.
# -analyzeduration   2s is sufficient for audio; 10s added unnecessary startup lag.
# -thread_queue_size large packet queue so the demuxer never starves the decoder.
# -bufsize 512k      output buffer large enough to absorb jitter (128k was causing artifacts).
# Note: do NOT add -ar or -ac here — FFmpegOpusAudio handles Opus encoding internally
# and forcing resample/channel conversion introduces stereo phase artifacts.
# dynaudnorm is applied as an FFmpeg -af filter for real-time loudness normalization on streams.
# p=0.9 targets 90% peak; s=5 uses a 5-second analysis window (responsive without jarring jumps).
# Controlled via the NORMALIZE_AUDIO env var (default: true).
FFMPEG_NORMALIZE_FILTER = "dynaudnorm=p=0.9:s=5"
_normalize_audio = get_config_value("NORMALIZE_AUDIO", dotenv_values, "true").lower() in ("1", "true", "yes")

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
        "-probesize 10M -analyzeduration 2000000 "
        "-thread_queue_size 4096"
    ),
    "options": (
        # -strict -2: enables experimental Opus-in-mp4 support on older ffmpeg builds
        f"-vn -bufsize 512k -strict -2 -af {FFMPEG_NORMALIZE_FILTER}"
        if _normalize_audio else
        "-vn -bufsize 512k -strict -2"
    ),
}

SEARCH_RESULT_COUNT = 5
MIN_SEARCH_SCORE = 6.0
RADIO_QUEUE_MIN = 3   # Fill trigger: radio refills when queue drops below this
RADIO_FILL_COUNT = 5  # Target queue size after a radio fill
LLM_SCORE_MARGIN = 4.5  # Increased from 3.0 to reduce LLM calls; only use when candidates very close
LLM_RANKING_TIMEOUT = 8.0
LLM_ENABLED_FOR_ALBUM_TRACKS = 3  # Only use LLM for first N tracks in bulk operations
ANTHROPIC_API_KEY = get_config_value("ANTHROPIC_API_KEY", dotenv_values, "")
ANTHROPIC_MODEL = "claude-haiku-4-5"

# ---------------------------------------------------------------------------
# DJ Announcer (TTS between genre changes in radio mode)
# ---------------------------------------------------------------------------
DJ_ANNOUNCER_ENABLED = get_config_value("DJ_ANNOUNCER_ENABLED", dotenv_values, "true").lower() in ("1", "true", "yes")
DJ_VOICE = get_config_value("DJ_VOICE", dotenv_values, "es-MX-DaliaNeural")  # edge-tts voice
DJ_ANNOUNCE_COOLDOWN = int(get_config_value("DJ_ANNOUNCE_COOLDOWN", dotenv_values, "120"))  # seconds between announcements
DJ_VOLUME = get_config_value("DJ_VOLUME", dotenv_values, "1.2")  # FFmpeg volume filter for TTS
DJ_FUN_FACT_INTERVAL = int(get_config_value("DJ_FUN_FACT_INTERVAL", dotenv_values, "5"))  # songs between fun-fact comments

# ---------------------------------------------------------------------------
# Local music library (offline playback + popularity tracking)
# ---------------------------------------------------------------------------
LIBRARY_ENABLED = get_config_value("LIBRARY_ENABLED", dotenv_values, "true").lower() in ("1", "true", "yes")
LIBRARY_PATH = get_config_value("LIBRARY_PATH", dotenv_values, ".cache/library")
LIBRARY_MAX_TRACKS = max(10, int(get_config_value("LIBRARY_MAX_TRACKS", dotenv_values, "500")))
LIBRARY_MAX_MB = max(100, int(get_config_value("LIBRARY_MAX_MB", dotenv_values, "2048")))
LIBRARY_AUTO_DOWNLOAD = get_config_value("LIBRARY_AUTO_DOWNLOAD", dotenv_values, "true").lower() in ("1", "true", "yes")
LIBRARY_MIN_PLAYS_TO_PIN = max(1, int(get_config_value("LIBRARY_MIN_PLAYS_TO_PIN", dotenv_values, "3")))
YOUTUBE_URL_CACHE_TTL_SEC = max(60, int(get_config_value("YOUTUBE_URL_CACHE_TTL_SEC", dotenv_values, "1800")))

FFMPEG_LOCAL_OPTIONS = {
    "before_options": "-nostdin",
    "options": (
        f"-vn -strict -2 -af {FFMPEG_NORMALIZE_FILTER}"
        if _normalize_audio else
        "-vn -strict -2"
    ),
}

NOISE_TERMS = {
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
VARIANT_TERMS = {"live", "remix", "cover", "karaoke", "acoustic", "instrumental"}
PREFERRED_CHANNEL_HINTS = ("topic", "vevo", "official", "records", "music")
MIN_SPOTIFY_REFINEMENT_SCORE = 7.5


def build_spotify_client(dotenv_values: dict) -> spotipy.Spotify:
    client_id     = get_config_value("SPOTIFY_CLIENT_ID",     dotenv_values)
    client_secret = get_config_value("SPOTIFY_CLIENT_SECRET", dotenv_values)
    redirect_uri  = get_config_value("SPOTIFY_REDIRECT_URI",  dotenv_values, "http://localhost:8888/callback")
    if not client_id or not client_secret:
        raise ValueError("Faltan credenciales Spotify (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET).")
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=SCOPES,
            cache_path=CACHE_PATH,
            open_browser=False,
        )
    )


bot_token = get_config_value("BOT_TOKEN", dotenv_values)
if not bot_token:
    raise ValueError("Falta BOT_TOKEN en variables de entorno o en .env.")

# Try to initialize Spotify client; disable features if credentials missing
sp = None
SPOTIFY_AVAILABLE = False
try:
    sp = build_spotify_client(dotenv_values)
    SPOTIFY_AVAILABLE = True
    logger.info("Spotify client initialized.")
except (ValueError, Exception) as exc:
    logger.warning(f"Spotify unavailable: {exc}. Radio + fun facts will use fallback mode.")
    SPOTIFY_AVAILABLE = False
