"""Natural-language request planner for the song-request channel.

LLM (via LiteLLM) proposes a strict JSON plan; code validates and caps it.
Without usable models, a small heuristic fallback handles simple cases.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from src.config import (
    REQUEST_AGENT_MODELS,
    REQUEST_AGENT_TIMEOUT_SEC,
    REQUEST_MAX_TRACKS_PER_PLAN,
    REQUEST_MAX_QUEUE_USER_TRACKS,
    export_llm_keys_to_environ,
)

logger = logging.getLogger(__name__)

ALLOWED_ACTION_TYPES = frozenset({
    "enqueue",
    "genre_playlist",
    "move",
    "remove",
    "remove_match",
    "priority",
    "skip",
    "clear_user_tracks",
    "show_queue",
    "set_auto",
})

# Double braces {{ }} are literal JSON for str.format; {max_tracks} is the only placeholder.
_SYSTEM_PROMPT = """Eres el planificador de cola de un bot de música en Discord (español).
Respondes SOLO con un JSON válido (sin markdown) con esta forma:
{{"reply":"texto corto al usuario","actions":[...]}}

Acciones permitidas:
- {{"type":"enqueue","queries":["artista - título",...],"position":"end"|"front"}}
- {{"type":"genre_playlist","genre":"synthwave","count":5,"hints":"opcional"}}
- {{"type":"move","from_pos":3,"to_pos":1}}  (1-based, cola mostrada)
- {{"type":"remove","pos":2}}
- {{"type":"remove_match","query":"despacito"}}
- {{"type":"priority","pos":4}}
- {{"type":"skip"}}
- {{"type":"clear_user_tracks"}}  (solo pedidos humanos; NO borra radio)
- {{"type":"show_queue"}}
- {{"type":"set_auto","enabled":true|false}}

Reglas:
- Máximo {max_tracks} canciones nuevas en total (suma de queries + genre count).
- NO apagues la radio. NO uses stop/clear_all.
- Si piden "ahora/ya/siguiente", usa position front y/o skip si piden saltar.
- Si piden activar/desactivar modo auto, usa set_auto.
- Preferí pocas acciones claras. Posiciones 1-based de la cola del contexto.
- Si solo preguntan el estado del auto o la cola, reply + show_queue o actions vacías.
- En "enqueue.queries" usá SIEMPRE búsquedas específicas: "Artista - Título de la canción".
  Nunca pongas solo un género, "mix", "playlist" o frases vagas en queries.
- Si piden un género o "música de X", usá genre_playlist (no enqueue genérico).
- ESTILOS / "COMO X" (crítico — NO es un solo track):
  * "nu-metal como Limp Bizkit y rock de los 2000 como Tony Hawk" → DOS (o más)
    genre_playlist, NUNCA un enqueue con toda la frase.
  * genre = etiqueta corta (nu-metal, rock, pop). hints = ancla de estilo:
    artista, banda, soundtrack o época (ej. "Limp Bizkit", "Tony Hawk Pro Skater 4",
    "early 2000s skate rock").
  * "como X" / "tipo X" / "estilo X" / "similar a X" / "soundtrack de X" = hints,
    NO el título de una canción a encolar.
  * El bot, al expandir, busca playlists de Spotify inspiradas en genre+hints
    (y related artists / charts). Poné hints ricos para orientar esas playlists.
  * Varios estilos unidos con "y"/"and"/"," → una action genre_playlist por estilo.
    Counts: repartí el total (default ~5–8) de forma orgánica (ej. 5+5 o 6+4),
    el bot mezcla al expandir.
  * Ejemplo pedido: "nu-metal como limp bizkit y rock de principios de los 2000
    como soundtrack de tony hawk pro skater 4"
    → actions: [
        {{"type":"genre_playlist","genre":"nu-metal","count":5,"hints":"Limp Bizkit"}},
        {{"type":"genre_playlist","genre":"rock","count":5,
          "hints":"Tony Hawk Pro Skater 4 soundtrack early 2000s"}}
      ]
- FIDELIDAD AL TÍTULO (solo si piden canciones concretas, no estilos):
  * Si el usuario nombra un tema, copiá ESE título (corregí typos obvios: mountaind→mountain,
    "mi mind"→"my mind"). NUNCA lo reemplaces por otro hit del mismo artista
    (ej. NO cambiar "My Mind Is a Mountain" por "My Own Summer").
  * Si no estás seguro del título exacto, usá las palabras del usuario, no inventes un clásico.
  * Varios temas en un mensaje ("X and Y", "X y Z", comas) → una query por tema.
  * Ejemplo: "Deftones mi mind is a mountaind and Green Day basket case"
    → queries: ["Deftones - My Mind Is a Mountain", "Green Day - Basket Case"]
"""


@dataclass
class RequestPlan:
    reply: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    source: str = "unknown"  # llm | heuristic | empty
    model: str | None = None

    def track_budget_used(self) -> int:
        total = 0
        for action in self.actions:
            t = action.get("type")
            if t == "enqueue":
                total += len(action.get("queries") or [])
            elif t == "genre_playlist":
                total += int(action.get("count") or 0)
        return total


def _extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def parse_plan_dict(data: dict, *, source: str = "llm", model: str | None = None) -> RequestPlan:
    reply = str(data.get("reply") or "").strip()
    raw_actions = data.get("actions")
    if not isinstance(raw_actions, list):
        raw_actions = []
    actions: list[dict[str, Any]] = []
    for item in raw_actions:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "").strip().lower()
        if t not in ALLOWED_ACTION_TYPES:
            continue
        actions.append(dict(item))
        actions[-1]["type"] = t
    return RequestPlan(reply=reply, actions=actions, source=source, model=model)


def validate_and_cap_plan(
    plan: RequestPlan,
    *,
    user_slots_left: int,
    max_tracks: int | None = None,
    max_actions: int = 8,
) -> RequestPlan:
    """Hard caps: action count, new tracks per plan, user queue slots."""
    max_tracks = max_tracks if max_tracks is not None else REQUEST_MAX_TRACKS_PER_PLAN
    slots = max(0, int(user_slots_left))
    # If queue is full (0 slots), still allow non-enqueue actions
    budget = 0 if slots <= 0 else min(max_tracks, slots)

    capped: list[dict[str, Any]] = []
    tracks_used = 0

    for action in plan.actions[:max_actions]:
        t = action["type"]
        if t == "enqueue":
            queries = [str(q).strip() for q in (action.get("queries") or []) if str(q).strip()]
            if not queries:
                continue
            remaining = budget - tracks_used
            if remaining <= 0:
                continue
            queries = queries[:remaining]
            pos = str(action.get("position") or "end").lower()
            if pos not in ("end", "front"):
                pos = "end"
            capped.append({"type": "enqueue", "queries": queries, "position": pos})
            tracks_used += len(queries)
        elif t == "genre_playlist":
            genre = str(action.get("genre") or "").strip()
            if not genre:
                continue
            try:
                count = int(action.get("count") or 5)
            except (TypeError, ValueError):
                count = 5
            count = max(1, min(count, max_tracks))
            remaining = budget - tracks_used
            if remaining <= 0:
                continue
            count = min(count, remaining)
            capped.append({
                "type": "genre_playlist",
                "genre": genre,
                "count": count,
                "hints": str(action.get("hints") or "").strip(),
            })
            tracks_used += count
        elif t == "move":
            try:
                fp, tp = int(action["from_pos"]), int(action["to_pos"])
            except (KeyError, TypeError, ValueError):
                continue
            capped.append({"type": "move", "from_pos": fp, "to_pos": tp})
        elif t == "remove":
            try:
                pos = int(action["pos"])
            except (KeyError, TypeError, ValueError):
                continue
            capped.append({"type": "remove", "pos": pos})
        elif t == "remove_match":
            q = str(action.get("query") or "").strip()
            if q:
                capped.append({"type": "remove_match", "query": q})
        elif t == "priority":
            try:
                pos = int(action["pos"])
            except (KeyError, TypeError, ValueError):
                continue
            capped.append({"type": "priority", "pos": pos})
        elif t == "set_auto":
            capped.append({"type": "set_auto", "enabled": bool(action.get("enabled"))})
        elif t in ("skip", "clear_user_tracks", "show_queue"):
            capped.append({"type": t})

    reply = plan.reply
    if tracks_used == 0 and budget == 0 and any(
        a["type"] in ("enqueue", "genre_playlist") for a in plan.actions
    ):
        reply = (reply + " Cola de usuario llena.").strip()

    return RequestPlan(reply=reply, actions=capped, source=plan.source, model=plan.model)


def heuristic_plan(message: str, *, auto_enabled: bool) -> RequestPlan:
    """No-LLM fallback: set_auto, move/remove regex, else single enqueue."""
    text = (message or "").strip()
    if not text:
        return RequestPlan(reply="No entendí el pedido.", actions=[], source="heuristic")

    lower = text.lower()

    # Auto mode NL
    if re.search(r"\b(activa|activar|enciende|enable)\b.*\bauto\b", lower) or lower in (
        "auto on",
        "modo auto",
        "auto",
    ):
        if "desactiva" in lower or "apaga" in lower or "off" in lower:
            pass
        else:
            return RequestPlan(
                reply="Modo auto activado: los planes se aplican al instante.",
                actions=[{"type": "set_auto", "enabled": True}],
                source="heuristic",
            )
    if re.search(r"\b(desactiva|apaga|disable|quita)\b.*\bauto\b", lower) or lower in (
        "auto off",
        "modo manual",
        "sin auto",
    ):
        return RequestPlan(
            reply="Modo auto desactivado: ventana de cancelación activa.",
            actions=[{"type": "set_auto", "enabled": False}],
            source="heuristic",
        )
    if re.search(r"\b(est[aá]\s+el\s+auto|modo\s+auto\??)\b", lower):
        state = "activado" if auto_enabled else "desactivado"
        return RequestPlan(
            reply=f"Modo auto: **{state}**.",
            actions=[],
            source="heuristic",
        )

    if re.search(r"\b(qu[eé]\s+hay\s+en\s+cola|muestra(r)?\s+la\s+cola|cola\??)\b", lower):
        return RequestPlan(reply="Cola actual:", actions=[{"type": "show_queue"}], source="heuristic")

    if re.search(r"\b(salta|skip|siguiente)\b", lower) and len(text.split()) <= 3:
        return RequestPlan(reply="Salto la actual.", actions=[{"type": "skip"}], source="heuristic")

    m_move = re.search(r"mueve(?:\s+la)?\s+(\d+)\s+a\s+(?:la\s+)?(\d+)", lower)
    if m_move:
        return RequestPlan(
            reply=f"Mover #{m_move.group(1)} → #{m_move.group(2)}.",
            actions=[{
                "type": "move",
                "from_pos": int(m_move.group(1)),
                "to_pos": int(m_move.group(2)),
            }],
            source="heuristic",
        )

    m_rm = re.search(r"quita(?:\s+la)?\s+(\d+)\b", lower)
    if m_rm:
        return RequestPlan(
            reply=f"Quitar #{m_rm.group(1)}.",
            actions=[{"type": "remove", "pos": int(m_rm.group(1))}],
            source="heuristic",
        )

    m_genre = re.search(
        r"(?:arma|pon(?:me)?|dame|quiero)\s+(\d{1,2})\s+(?:de|del|tema(?:s)?\s+de)\s+(.+)",
        lower,
    )
    if m_genre:
        count = max(1, min(int(m_genre.group(1)), REQUEST_MAX_TRACKS_PER_PLAN))
        genre = m_genre.group(2).strip(" .!")
        return RequestPlan(
            reply=f"Playlist de {count} · {genre}.",
            actions=[{"type": "genre_playlist", "genre": genre, "count": count, "hints": ""}],
            source="heuristic",
        )

    # Multi-style: "nu-metal como X y rock como Y" / "tipo Limp Bizkit"
    style_plan = _heuristic_style_mix(text)
    if style_plan is not None:
        return style_plan

    # strip leading "pon" / "play"
    query = re.sub(r"^(pon(?:me)?|play|reproduce|toca)\s+", "", text, flags=re.I).strip()
    position = "front" if re.search(r"\b(ahora|ya|ya mismo|al frente|playnext)\b", lower) else "end"
    actions: list[dict[str, Any]] = []
    if re.search(r"\b(salta|skip)\b", lower) and query:
        actions.append({"type": "skip"})
    if query:
        actions.append({"type": "enqueue", "queries": [query], "position": position})
    if not actions:
        return RequestPlan(reply="No entendí el pedido.", actions=[], source="heuristic")
    return RequestPlan(
        reply=f"Encolar: {query}" if query else "OK",
        actions=actions,
        source="heuristic",
    )


# Genre-ish tokens used to detect style requests vs concrete song titles.
_STYLE_GENRE_WORDS = (
    r"nu[\s\-]?metal|metal|rock|punk|pop|indie|hip[\s\-]?hop|rap|trap|techno|house|"
    r"edm|electronic|jazz|blues|reggae|reggaeton|latin|synthwave|ska|grunge|"
    r"hardcore|emo|shoegaze|folk|country|disco|funk|soul|r&b|rb"
)
_STYLE_ANCHOR_RE = re.compile(
    r"(?P<head>(?:" + _STYLE_GENRE_WORDS + r")[\w\s\-]*?)\s+"
    r"(?:como|tipo|estilo|similar\s+a|parecido\s+a|soundtrack\s+de|ost\s+de)\s+"
    r"(?P<anchor>.+?)(?=\s+(?:y|and|,)\s+(?:" + _STYLE_GENRE_WORDS + r")\b|$)",
    re.I | re.S,
)


def _heuristic_style_mix(text: str) -> RequestPlan | None:
    """Parse multi-vibe requests into genre_playlist actions with hints.

    Example: "nu-metal como limp bizkit y rock ... tony hawk" → 2 genre_playlists.
    Returns None if the message does not look like a style mix.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    # Concrete "Artist - Title" is not a style mix
    if " - " in raw and not re.search(r"\b(como|tipo|estilo)\b", raw, re.I):
        return None

    matches = list(_STYLE_ANCHOR_RE.finditer(raw))
    if not matches:
        # Single style without "y": "pon nu-metal como limp bizkit"
        m1 = re.search(
            r"(?P<head>(?:" + _STYLE_GENRE_WORDS + r")[\w\s\-]*)\s+"
            r"(?:como|tipo|estilo|similar\s+a|parecido\s+a|soundtrack\s+de)\s+"
            r"(?P<anchor>.+)$",
            raw,
            re.I | re.S,
        )
        if not m1:
            return None
        matches = [m1]

    # Need at least one genre+anchor; multi-style if 2+ or "y" between genres
    actions: list[dict[str, Any]] = []
    n = len(matches)
    default_each = max(3, min(5, REQUEST_MAX_TRACKS_PER_PLAN // max(n, 1)))
    # Balance to budget
    budget = min(REQUEST_MAX_TRACKS_PER_PLAN, default_each * n)
    base = max(1, budget // n)
    rem = budget - base * n

    for i, m in enumerate(matches):
        head = re.sub(r"\s+", " ", (m.group("head") or "").strip(" ,."))
        anchor = re.sub(r"\s+", " ", (m.group("anchor") or "").strip(" ,."))
        # Drop leading verbs
        head = re.sub(
            r"^(?:pon(?:me)?|dame|quiero|arma|play|reproduce|toca)\s+",
            "",
            head,
            flags=re.I,
        ).strip()
        if not head:
            continue
        # genre = first genre-ish words of head
        gm = re.match(r"(?i)(" + _STYLE_GENRE_WORDS + r")(?:\s+\w+){0,3}", head)
        genre = (gm.group(0) if gm else head).strip()
        # Keep era words in hints too
        era = ""
        if re.search(r"2000|90s|90'|ochenta|noventa|early|principios|finales", head, re.I):
            era = re.sub(
                r"(?i)(" + _STYLE_GENRE_WORDS + r")",
                " ",
                head,
            )
            era = re.sub(r"\s+", " ", era).strip()
        hints = anchor
        if era:
            hints = f"{anchor} {era}".strip()
        count = base + (1 if i < rem else 0)
        actions.append({
            "type": "genre_playlist",
            "genre": genre,
            "count": count,
            "hints": hints,
        })

    if not actions:
        return None
    # Require style signal (como/tipo) — already matched
    labels = " + ".join(f"{a['genre']} ({a['hints'][:28]})" for a in actions)
    return RequestPlan(
        reply=f"Mezcla de estilos: {labels}.",
        actions=actions,
        source="heuristic",
    )


def _format_user_prompt(
    message: str,
    snapshot: dict,
    *,
    auto_enabled: bool,
    user_slots_left: int,
    max_tracks: int,
) -> str:
    np = snapshot.get("now_playing")
    if np:
        np_line = f"{np.get('title')} — {np.get('requester')}" + (
            " [radio]" if np.get("is_radio") else ""
        )
    else:
        np_line = "(nada)"
    queue_block = "\n".join(snapshot.get("queue_lines") or ["(vacía)"])
    return (
        f"auto_enabled: {auto_enabled}\n"
        f"radio_active: {snapshot.get('radio_active')}\n"
        f"paused: {snapshot.get('paused')}\n"
        f"now_playing: {np_line}\n"
        f"queue_user_count: {snapshot.get('queue_user_count')} "
        f"queue_radio_count: {snapshot.get('queue_radio_count')} "
        f"user_slots_left: {user_slots_left}\n"
        f"limits: max_tracks_per_plan={max_tracks}\n"
        f"queue:\n{queue_block}\n\n"
        f"user_message: {message}"
    )


async def plan_from_llm(message: str, snapshot: dict, *, auto_enabled: bool, user_slots_left: int) -> RequestPlan | None:
    models = list(REQUEST_AGENT_MODELS or [])
    if not models:
        return None
    try:
        import litellm
    except ImportError:
        logger.warning("request_agent: litellm not installed")
        return None

    # Keys live in mounted .env; LiteLLM only checks process environment.
    export_llm_keys_to_environ()

    system = _SYSTEM_PROMPT.format(max_tracks=REQUEST_MAX_TRACKS_PER_PLAN)
    user = _format_user_prompt(
        message,
        snapshot,
        auto_enabled=auto_enabled,
        user_slots_left=user_slots_left,
        max_tracks=REQUEST_MAX_TRACKS_PER_PLAN,
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    for model in models:
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "timeout": REQUEST_AGENT_TIMEOUT_SEC,
                # Slightly lower temp = more consistent "Artist - Title" queries
                "temperature": 0.2,
            }
            # Skip response_format by default: DeepSeek/xAI often reject it and
            # a retry doubles latency. Parser already extracts JSON fences.
            # Opt-in for providers that handle json_object well.
            model_l = model.lower()
            if model_l.startswith(("openai/", "gpt-", "azure/")):
                try:
                    resp = await litellm.acompletion(
                        **kwargs, response_format={"type": "json_object"}
                    )
                except Exception:
                    resp = await litellm.acompletion(**kwargs)
            else:
                resp = await litellm.acompletion(**kwargs)
            content = ""
            try:
                content = resp.choices[0].message.content or ""
            except Exception:
                content = str(resp)
            data = _extract_json_object(content)
            if not data:
                logger.warning("request_agent: bad JSON from model=%s", model)
                continue
            plan = parse_plan_dict(data, source="llm", model=model)
            logger.info(
                "request_agent: plan from %s actions=%s",
                model,
                [a.get("type") for a in plan.actions],
            )
            return plan
        except Exception as exc:
            logger.warning("request_agent: model %s failed: %s", model, exc)
            continue
    return None


def is_concrete_artist_title(query: str) -> bool:
    """True for 'Artist - Title' song lines (not style blobs)."""
    q = (query or "").strip()
    if not q or len(q) < 4:
        return False
    if re.search(r"\b(soundtrack|playlist|essentials|mix|como|tipo|estilo)\b", q, re.I):
        return False
    if re.search(r"\s[–-]\s", q):
        left, right = re.split(r"\s[–-]\s", q, maxsplit=1)
        left, right = left.strip(), right.strip()
        if len(left) >= 2 and len(right) >= 2:
            # Reject "nu-metal - playlist" style
            if re.fullmatch(r"(?i)(" + _STYLE_GENRE_WORDS + r")", left.strip()):
                return False
            return True
    return False


def is_style_blob_query(query: str) -> bool:
    """True when an enqueue query is a vibe/style phrase, not a song title."""
    q = (query or "").strip()
    if not q:
        return False
    if is_concrete_artist_title(q):
        return False
    if re.search(
        r"soundtrack|ost\b|playlist|essentials|early\s*2000|principios|finales|"
        r"\b2000s\b|\b90s\b|nu[\s\-]?metal|skate|video\s*game",
        q,
        re.I,
    ):
        return True
    if re.search(r"\b(" + _STYLE_GENRE_WORDS + r")\b", q, re.I):
        return True
    # Bare artist / game name without a song title separator
    if " - " not in q and " – " not in q and 1 <= len(q.split()) <= 8:
        return True
    return False


def split_style_blob(query: str) -> tuple[str, str]:
    """Split a style blob into (genre, hints) for genre_playlist expansion."""
    raw = re.sub(r"\s+", " ", (query or "").strip())
    genre_match = re.search(r"(?i)(" + _STYLE_GENRE_WORDS + r")(?:\s+\w+){0,2}", raw)
    genre = (genre_match.group(0) if genre_match else "rock").strip()
    hints = re.sub(r"(?i)(" + _STYLE_GENRE_WORDS + r")", " ", raw)
    hints = re.sub(
        r"(?i)\b(playlist|essentials|mix|songs?|music|tema[s]?)\b",
        " ",
        hints,
    )
    hints = re.sub(r"\s+", " ", hints).strip(" ,.-")
    return genre, hints


def _looks_like_collapsed_style_enqueue(plan: RequestPlan, message: str) -> bool:
    """True when the model stuffed style vibes into enqueue instead of genre_playlist."""
    if not plan.actions:
        return False
    enqueues = [a for a in plan.actions if a.get("type") == "enqueue"]
    genres = [a for a in plan.actions if a.get("type") == "genre_playlist"]
    if genres:
        return False
    if not enqueues:
        return False
    queries: list[str] = []
    for e in enqueues:
        queries.extend(str(q) for q in (e.get("queries") or []) if str(q).strip())
    if not queries:
        return False
    # All / most queries are style blobs (e.g. "Limp Bizkit nu-metal")
    style_n = sum(1 for q in queries if is_style_blob_query(q))
    if style_n == 0:
        return False
    if style_n == len(queries):
        return True
    msg = (message or "").lower()
    if re.search(r"\b(como|tipo|estilo|soundtrack|playlist|mezcla)\b", msg):
        return style_n >= 1
    return False


def style_blobs_to_genre_actions(
    queries: list[str],
    *,
    default_count: int = 5,
) -> list[dict[str, Any]]:
    """Turn style-like enqueue lines into genre_playlist actions with real counts."""
    blobs = [q for q in queries if is_style_blob_query(q)]
    if not blobs:
        return []
    n = len(blobs)
    budget = min(REQUEST_MAX_TRACKS_PER_PLAN, max(default_count, 4) * n)
    base = max(3, budget // n)
    rem = budget - base * n
    actions: list[dict[str, Any]] = []
    for i, blob in enumerate(blobs):
        genre, hints = split_style_blob(blob)
        count = base + (1 if i < rem else 0)
        count = max(3, min(count, REQUEST_MAX_TRACKS_PER_PLAN))
        actions.append({
            "type": "genre_playlist",
            "genre": genre,
            "count": count,
            "hints": hints or blob,
        })
    return actions


def repair_style_plan_if_collapsed(plan: RequestPlan, message: str) -> RequestPlan:
    """Replace style-blob enqueues with multi genre_playlist (count ≥ 3 each)."""
    if not _looks_like_collapsed_style_enqueue(plan, message):
        # Also bump tiny genre counts when hints present (LLM put count=1)
        bumped = False
        new_actions: list[dict[str, Any]] = []
        for a in plan.actions:
            if a.get("type") == "genre_playlist" and a.get("hints"):
                try:
                    c = int(a.get("count") or 1)
                except (TypeError, ValueError):
                    c = 1
                if c < 3:
                    a = dict(a)
                    a["count"] = 5
                    bumped = True
            new_actions.append(a)
        if bumped:
            return RequestPlan(
                reply=plan.reply,
                actions=new_actions,
                source=plan.source,
                model=plan.model,
            )
        return plan

    fixed = _heuristic_style_mix(message)
    if fixed is not None and fixed.actions:
        fixed.model = plan.model
        if plan.reply and len(plan.reply) > 8:
            fixed.reply = plan.reply
        return fixed

    # Fallback: convert enqueue style blobs → genre_playlist×N
    queries: list[str] = []
    other: list[dict[str, Any]] = []
    for a in plan.actions:
        if a.get("type") == "enqueue":
            queries.extend(str(q) for q in (a.get("queries") or []) if str(q).strip())
        else:
            other.append(a)
    genre_actions = style_blobs_to_genre_actions(queries, default_count=5)
    if not genre_actions:
        return plan
    return RequestPlan(
        reply=plan.reply or "Mezcla de estilos.",
        actions=other + genre_actions,
        source=plan.source,
        model=plan.model,
    )


async def build_request_plan(
    message: str,
    snapshot: dict,
    *,
    auto_enabled: bool,
    user_slots_left: int | None = None,
) -> RequestPlan:
    if user_slots_left is None:
        user_count = int(snapshot.get("queue_user_count") or 0)
        user_slots_left = max(0, REQUEST_MAX_QUEUE_USER_TRACKS - user_count)

    llm_plan = await plan_from_llm(
        message,
        snapshot,
        auto_enabled=auto_enabled,
        user_slots_left=user_slots_left,
    )
    if llm_plan is None:
        llm_plan = heuristic_plan(message, auto_enabled=auto_enabled)
    else:
        # LLM often collapses "nu-metal como X y rock como Y" into one enqueue.
        repaired = repair_style_plan_if_collapsed(llm_plan, message)
        if repaired is not llm_plan:
            logger.info(
                "request_agent: repaired collapsed style plan → %s",
                [a.get("type") for a in repaired.actions],
            )
            llm_plan = repaired

    return validate_and_cap_plan(llm_plan, user_slots_left=user_slots_left)


def summarize_actions(plan: RequestPlan) -> str:
    parts: list[str] = []
    for a in plan.actions:
        t = a["type"]
        if t == "enqueue":
            n = len(a.get("queries") or [])
            pos = a.get("position") or "end"
            parts.append(f"+{n} tema(s) ({pos})")
        elif t == "genre_playlist":
            parts.append(f"+{a.get('count')} · {a.get('genre')}")
        elif t == "move":
            parts.append(f"mover #{a.get('from_pos')}→#{a.get('to_pos')}")
        elif t == "remove":
            parts.append(f"quitar #{a.get('pos')}")
        elif t == "remove_match":
            parts.append(f"quitar «{a.get('query')}»")
        elif t == "priority":
            parts.append(f"priority #{a.get('pos')}")
        elif t == "skip":
            parts.append("skip")
        elif t == "clear_user_tracks":
            parts.append("limpiar pedidos user")
        elif t == "show_queue":
            parts.append("mostrar cola")
        elif t == "set_auto":
            parts.append("auto " + ("on" if a.get("enabled") else "off"))
    return " · ".join(parts) if parts else "(sin acciones de cola)"


def planned_track_labels(plan: RequestPlan) -> list[str]:
    """Labels for embed lines.

    Prefer concrete ``Artist - Title`` queries (after genre materialization).
    Genre placeholders are a last resort only if expansion has not run yet.
    """
    labels: list[str] = []
    for a in plan.actions:
        if a["type"] == "enqueue":
            labels.extend(a.get("queries") or [])
        elif a["type"] == "genre_playlist":
            # Prefer pre-resolved queries attached to the action when present
            pre = [str(q).strip() for q in (a.get("queries") or []) if str(q).strip()]
            if pre:
                labels.extend(pre)
            else:
                genre = a.get("genre") or "genre"
                count = int(a.get("count") or 0)
                for i in range(count):
                    labels.append(f"{genre} #{i + 1}")
    return labels[:REQUEST_MAX_TRACKS_PER_PLAN]
