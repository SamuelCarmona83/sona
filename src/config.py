import logging
import os

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from poc_setlistfm import load_dotenv_values, get_config_value

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


SCOPES = (
    "user-modify-playback-state "
    "user-read-playback-state "
    "user-read-currently-playing "
    "playlist-read-private"
)

ALLOWED_CHANNEL_ID = 1163479541029810226
VOICE_CHANNEL_ID   = 1397428777876721716
CACHE_PATH = os.getenv("SPOTIFY_CACHE_PATH", ".cache/spotify.cache")
OAUTH_PORT = 8888

# Only this user can run !auth; the OAuth URL is sent via DM (never visible in the channel)
ADMIN_USER_ID = 221081593790332929

# ---------------------------------------------------------------------------
# YT-DLP
# ---------------------------------------------------------------------------

YTDL_OPTIONS = {
    # Prefer m4a/AAC: one transcode (AAC→Opus) is cleaner than opus→PCM→Opus.
    # Falls back to webm/opus if m4a is not available, then any best audio.
    "format": "bestaudio[ext=m4a]/bestaudio[acodec=opus]/bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "source_address": "0.0.0.0",
    "ignoreerrors": True,  # Skip unavailable videos in search results instead of failing
}

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
_normalize_audio = os.getenv("NORMALIZE_AUDIO", "true").lower() in ("1", "true", "yes")

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
        "-probesize 10M -analyzeduration 2000000 "
        "-thread_queue_size 4096"
    ),
    "options": (
        f"-vn -bufsize 512k -af {FFMPEG_NORMALIZE_FILTER}"
        if _normalize_audio else
        "-vn -bufsize 512k"
    ),
}

SEARCH_RESULT_COUNT = 5
MIN_SEARCH_SCORE = 6.0
LLM_SCORE_MARGIN = 4.5  # Increased from 3.0 to reduce LLM calls; only use when candidates very close
LLM_RANKING_TIMEOUT = 8.0
LLM_ENABLED_FOR_ALBUM_TRACKS = 3  # Only use LLM for first N tracks in bulk operations
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-haiku-4-5"

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


dotenv_values = load_dotenv_values()
bot_token = get_config_value("BOT_TOKEN", dotenv_values)
if not bot_token:
    raise ValueError("Falta BOT_TOKEN en variables de entorno o en .env.")

sp = build_spotify_client(dotenv_values)
