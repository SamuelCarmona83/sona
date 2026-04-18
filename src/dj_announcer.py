"""DJ Announcer – generates and synthesizes radio transition comments.

When radio mode detects a genre cluster change between tracks, this module:
1. Generates a short DJ comment via Anthropic LLM (with template fallback).
2. Synthesizes it to an audio file via edge-tts.
3. Returns the file path for playback before the next song.
"""
import asyncio
import logging
import os
import pathlib
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import edge_tts

from src.config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    DJ_VOICE,
    DJ_ANNOUNCE_COOLDOWN,
    DJ_VOLUME,
    DJ_FUN_FACT_INTERVAL,
)

logger = logging.getLogger(__name__)

_DJ_CACHE_DIR = pathlib.Path(".cache/dj_audio")
_DJ_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Per-guild cooldown: guild_id → last announce timestamp
_last_announce: dict[int, float] = {}

# ---------------------------------------------------------------------------
# Cluster display names (for LLM prompt and templates)
# ---------------------------------------------------------------------------
_CLUSTER_NAMES: dict[str, str] = {
    "metal": "Metal",
    "hiphop": "Hip-Hop",
    "techno": "Electrónica",
    "rock": "Rock",
    "pop": "Pop",
    "chill": "Chill / Lo-Fi",
    "latin": "Latina",
    "jazz": "Jazz",
    "classical": "Clásica",
    "reggae": "Reggae",
    "country": "Country / Folk",
}

# ---------------------------------------------------------------------------
# Fallback templates (used when LLM unavailable)
# ---------------------------------------------------------------------------
_TEMPLATES = [
    "¡Cambio de vibra! Pasamos de {old} a {new}. Viene {artist} con {title}.",
    "Se viene un giro musical. De {old} nos vamos a {new}. Prepárense.",
    "¡Atentos! Cambiamos el mood de {old} a {new}. Lo siguiente: {artist}.",
    "Transición. Dejamos atrás el {old} y entramos en zona {new}. ¡Disfruten!",
    "¡Nuevo set! De {old} a {new}. {artist} toma el control.",
]

_WELCOME_TEMPLATES = [
    "¡Bienvenidos a la radio! Hoy estamos en modo {mood}. ¡Que empiece la música!",
    "¡La radio está en el aire! Modo {mood} activado. ¡Disfruten el viaje musical!",
    "¡Arrancamos! Radio en modo {mood}. Prepárense para una buena sesión.",
    "¡Hola a todos! Aquí su DJ. Modo {mood} encendido. ¡Vamos con todo!",
    "¡La fiesta empieza ya! Modo {mood}. ¡Conecten y disfruten!",
]

_WELCOME_TEMPLATES_MORNING = [
    "¡Buenos días! La radio despierta en modo {mood}. ¡Que comience la música matinal!",
    "¡Buen día a todos! Arrancamos el día con {mood}. ¡A disfrutar!",
    "¡Hola! Mañana en la radio con modo {mood}. ¡Vamos con energía!",
]

_WELCOME_TEMPLATES_AFTERNOON = [
    "¡Buenas tardes! Radio en el aire, modo {mood}. ¡Que disfruten!",
    "¡Hola gente! Es momento de {mood}. ¡La tarde es nuestra!",
    "¡Tardes! Radio con modo {mood}. ¡A conectarse!",
]

_WELCOME_TEMPLATES_EVENING = [
    "¡Buenas noches! Radio en vivo, modo {mood}. ¡Que comience la jornada nocturna!",
    "¡Hola a todos! Atardecer musical en modo {mood}. ¡Disfruten!",
    "¡Llega la noche! Radio con modo {mood}. ¡Prepárense para lo mejor!",
]

_WELCOME_TEMPLATES_NIGHT = [
    "¡Buenas noches! La radio llega a la madrugada en modo {mood}. ¡Que siga la fiesta!",
    "¡Hola madrugadores! Modo {mood} encendido. ¡Conecten y disfruten!",
    "¡La noche es larga! Radio en modo {mood}. ¡Vamos con todo!",
]


def check_cooldown(guild_id: int) -> bool:
    """Return True if enough time has passed since last announcement."""
    last = _last_announce.get(guild_id, 0)
    return (time.time() - last) >= DJ_ANNOUNCE_COOLDOWN


def mark_announced(guild_id: int) -> None:
    _last_announce[guild_id] = time.time()


# ---------------------------------------------------------------------------
# Daytime awareness (Buenos Aires timezone)
# ---------------------------------------------------------------------------

def get_buenos_aires_hour() -> int:
    """Get current hour in Buenos Aires timezone (America/Argentina/Buenos_Aires)."""
    tz_ba = ZoneInfo("America/Argentina/Buenos_Aires")
    now_ba = datetime.now(tz_ba)
    return now_ba.hour


def get_daytime_period(hour: int) -> str:
    """Return daytime period based on hour (0-23). Buenos Aires-aware."""
    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    else:
        return "night"


# ---------------------------------------------------------------------------
# LLM comment generation
# ---------------------------------------------------------------------------

async def generate_dj_comment(
    old_cluster: str | None,
    new_cluster: str,
    next_title: str,
    next_artist: str,
    hour: int,
) -> str:
    """Generate a short DJ transition comment. Falls back to template on failure."""
    old_name = _CLUSTER_NAMES.get(old_cluster or "", old_cluster or "variado")
    new_name = _CLUSTER_NAMES.get(new_cluster, new_cluster)
    daytime = get_daytime_period(hour)

    # Try LLM first
    if ANTHROPIC_API_KEY:
        try:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            resp = await asyncio.wait_for(
                client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=120,
                    system=(
                        "Eres un DJ de radio en español, carismático y conciso. "
                        "Genera UNA frase corta (máximo 2 oraciones) anunciando "
                        "la transición de género musical. Sé natural, con energía. "
                        "No uses hashtags ni emojis. Solo texto hablado. "
                        f"Es {daytime} en Buenos Aires."
                    ),
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Transición de {old_name} a {new_name}. "
                            f"Siguiente canción: '{next_title}' de {next_artist}."
                        ),
                    }],
                ),
                timeout=6.0,
            )
            text = resp.content[0].text.strip()
            if text:
                logger.info("dj_announcer: LLM generated: %s", text[:80])
                return text
        except Exception as exc:
            logger.warning("dj_announcer: LLM failed, using template: %s", exc)

    # Fallback template
    return random.choice(_TEMPLATES).format(
        old=old_name, new=new_name, artist=next_artist, title=next_title,
    )


# ---------------------------------------------------------------------------
# Welcome message generation
# ---------------------------------------------------------------------------

async def generate_welcome_message(mood: str, hour: int) -> str:
    """Generate a radio startup welcome message in Spanish, aware of daytime."""
    mood_display = _CLUSTER_NAMES.get(mood, mood.capitalize())
    daytime = get_daytime_period(hour)

    # Select templates based on daytime
    if daytime == "morning":
        templates = _WELCOME_TEMPLATES_MORNING
    elif daytime == "afternoon":
        templates = _WELCOME_TEMPLATES_AFTERNOON
    elif daytime == "evening":
        templates = _WELCOME_TEMPLATES_EVENING
    else:  # night
        templates = _WELCOME_TEMPLATES_NIGHT

    if ANTHROPIC_API_KEY:
        try:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            resp = await asyncio.wait_for(
                client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=100,
                    system=(
                        "Eres un DJ de radio en español, carismático y conciso. "
                        "Genera UNA frase corta (máximo 2 oraciones) dando la bienvenida "
                        "a los oyentes y anunciando el mood musical. Sé natural, con energía. "
                        "No uses hashtags ni emojis. Solo texto hablado. "
                        f"Es {daytime} en Buenos Aires."
                    ),
                    messages=[{
                        "role": "user",
                        "content": f"Da la bienvenida a la radio. El mood actual es: {mood_display}.",
                    }],
                ),
                timeout=6.0,
            )
            text = resp.content[0].text.strip()
            if text:
                logger.info("dj_announcer: welcome LLM: %s", text[:80])
                return text
        except Exception as exc:
            logger.warning("dj_announcer: welcome LLM failed: %s", exc)

    return random.choice(templates).format(mood=mood_display)


# ---------------------------------------------------------------------------
# Fun-fact generation (periodic, every N songs)
# ---------------------------------------------------------------------------

_FUN_FACT_TEMPLATES = [
    "¿Sabían que {artist} es considerado uno de los referentes del {cluster}?",
    "Dato curioso: el {cluster} tiene sus raíces en los años 70. ¡Y sigue más vivo que nunca!",
    "Aquí va un dato: {artist} ha influenciado a generaciones enteras dentro del {cluster}.",
    "¿Lo sabían? El {cluster} es uno de los géneros más escuchados en plataformas de streaming.",
    "Dato musical: la canción que viene, '{title}', es todo un clásico de {artist}.",
]

# Safe templates (metadata-free; never hallucinate without artist/genre data)
_SAFE_FUN_FACT_TEMPLATES = [
    "¿Sabían que {artist} se especializa en {cluster}? ¡Disfrútenlo!",
    "Dato: el {cluster} es uno de los géneros más energéticos. {artist} lo domina.",
    "Escuchamos: '{title}' de {artist}. ¡Que suene!",
    "{artist} trayendo {cluster}. ¡Conecten!",
    "Género: {cluster}. Artista: {artist}. Momento: ahora. ¡Disfruta!",
]


async def _get_artist_metadata(artist_id: str | None) -> dict | None:
    """Fetch artist metadata from Spotify if available. Return dict or None."""
    if not artist_id:
        return None
    
    from src.config import SPOTIFY_AVAILABLE
    if not SPOTIFY_AVAILABLE:
        return None
    
    try:
        from src.spotify import _get_artist_genres
        genres = await _get_artist_genres(artist_id)
        if genres:
            return {"genres": genres, "genres_str": ", ".join(genres[:3])}
    except Exception as exc:
        logger.debug(f"_get_artist_metadata: error fetching for {artist_id}: {exc}")
    
    return None


async def generate_fun_fact(
    title: str,
    artist: str,
    cluster: str | None,
    hour: int,
    artist_id: str | None = None,
) -> str:
    """Generate a short interesting fact about the track, artist or genre.
    
    If artist_id available, fetch metadata for enhanced LLM context.
    If metadata unavailable, use safe templates (never hallucinate).
    """
    cluster_name = _CLUSTER_NAMES.get(cluster or "", cluster or "música")
    daytime = get_daytime_period(hour)
    metadata = await _get_artist_metadata(artist_id)

    if ANTHROPIC_API_KEY and metadata:
        # Tier 1: LLM with metadata context (less likely to hallucinate)
        try:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            resp = await asyncio.wait_for(
                client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=120,
                    system=(
                        "Eres un DJ de radio en español, carismático y conciso. "
                        "Genera UNA frase corta (máximo 2 oraciones) con un dato "
                        "curioso o interesante verificable sobre el artista, la canción o el "
                        "género musical. SOLO datos que puedas confirmar. "
                        "No inventes eras, nacionalidad, historia de bandas. "
                        "Sé informativo y entretenido. "
                        "No uses hashtags ni emojis. Solo texto hablado. "
                        f"Es {daytime} en Buenos Aires."
                    ),
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Canción: '{title}' de {artist}. "
                            f"Género: {cluster_name}. "
                            f"Géneros confirmados: {metadata.get('genres_str', '')}. "
                            "Comparte un dato curioso verificable."
                        ),
                    }],
                ),
                timeout=6.0,
            )
            text = resp.content[0].text.strip()
            if text:
                logger.info("dj_announcer: fun fact LLM: %s", text[:80])
                return text
        except Exception as exc:
            logger.warning("dj_announcer: fun fact LLM failed: %s", exc)
    
    # Tier 3: Safe template (no hallucination risk)
    return random.choice(_SAFE_FUN_FACT_TEMPLATES).format(
        artist=artist, title=title, cluster=cluster_name,
    )


# ---------------------------------------------------------------------------
# TTS synthesis
# ---------------------------------------------------------------------------

async def synthesize_dj_audio(text: str, guild_id: int) -> str | None:
    """Synthesize text to an audio file via edge-tts. Returns file path or None."""
    out_path = _DJ_CACHE_DIR / f"dj_{guild_id}_{int(time.time())}.mp3"
    try:
        communicate = edge_tts.Communicate(text, DJ_VOICE)
        await communicate.save(str(out_path))
        logger.info("dj_announcer: synthesized %d chars → %s", len(text), out_path)
        return str(out_path)
    except Exception as exc:
        logger.error("dj_announcer: TTS synthesis failed: %s", exc)
        return None


def cleanup_dj_audio(file_path: str) -> None:
    """Delete a TTS audio file after playback."""
    try:
        os.remove(file_path)
    except OSError:
        pass


def get_dj_ffmpeg_options() -> dict:
    """FFmpeg options for TTS playback (local file, no reconnect needed)."""
    return {
        "before_options": "",
        "options": f"-vn -af volume={DJ_VOLUME}",
    }
