"""Music catalog aggregation for the data explorer (Spotify-like browsing).

Reads library_index + fm_sessions + likes from a cache dir and exposes
normalized tracks, artists, and albums. No Discord / network I/O.
"""

from __future__ import annotations

import json
import pathlib
import re
import time
import unicodedata
from typing import Any, Optional
from urllib.parse import parse_qs

_CACHE_TTL_SEC = 45.0
_cache_payload: dict[str, Any] | None = None
_cache_key: str | None = None
_cache_ts: float = 0.0

_UNKNOWN_ARTISTS = frozenset({"", "unknown", "—", "?", "desconocido", "radio"})


def normalize_key(text: str) -> str:
    raw = (text or "").strip().lower()
    raw = unicodedata.normalize("NFD", raw)
    raw = "".join(c for c in raw if unicodedata.category(c) != "Mn")
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def soft_match_key(artist: str, title: str) -> str:
    return f"{normalize_key(artist)}\0{normalize_key(title)}"


def parse_artist_title(
    artist: str | None,
    title: str | None,
) -> tuple[str, str]:
    """Return (display_artist, display_title). Split 'Artist - Title' if needed."""
    a = (artist or "").strip()
    t = (title or "").strip()
    unknown = normalize_key(a) in _UNKNOWN_ARTISTS or not a
    if unknown and t:
        m = re.match(r"^(.+?)\s*[-–—]\s+(.+)$", t)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return "Desconocido", t
    if not a:
        a = "Desconocido"
    if not t:
        t = "?"
    return a, t


def infer_sources(entry: dict) -> list[str]:
    existing = entry.get("sources")
    if isinstance(existing, list) and existing:
        out = []
        for s in existing:
            if isinstance(s, str) and s and s not in out:
                out.append(s)
        if out:
            return out

    sources: list[str] = []
    if int(entry.get("request_count") or 0) > 0:
        sources.append("request")
    origin = (entry.get("origin") or entry.get("source") or "").strip().lower()
    if origin in ("request", "radio", "fm", "play", "like") and origin not in sources:
        sources.append(origin)
    if entry.get("from_fm_seed") or entry.get("is_radio_stream"):
        if "radio" not in sources:
            sources.append("radio")
    if int(entry.get("play_count") or 0) > 0 and "request" not in sources and "radio" not in sources:
        sources.append("play")
    if not sources:
        sources.append("unknown")
    return sources


def apply_metadata_normalize(entry: dict) -> dict:
    """Return a shallow-normalized copy of a library entry (no disk write)."""
    out = dict(entry)
    artist, title = parse_artist_title(out.get("artist"), out.get("title"))
    out["display_artist"] = artist
    out["display_title"] = title
    # Prefer parsed artist when stored value is Unknown
    if normalize_key(str(out.get("artist") or "")) in _UNKNOWN_ARTISTS:
        out["artist"] = artist
    if title and (not out.get("title") or out.get("title") == "?"):
        out["title"] = title
    album = (out.get("album") or "").strip()
    out["album"] = album
    out["album_display"] = album or "Sin álbum"
    out["artist_key"] = normalize_key(artist) or "desconocido"
    out["album_key"] = normalize_key(out["album_display"]) or "sin-album"
    out["sources"] = infer_sources(out)
    out["match_key"] = soft_match_key(artist, title)
    return out


def _load_json(path: pathlib.Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _file_size(path: pathlib.Path | None) -> int:
    if path is None:
        return 0
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    return 0


def _track_from_library(
    tid: str,
    entry: dict,
    *,
    library_dir: pathlib.Path | None,
) -> dict:
    norm = apply_metadata_normalize(entry)
    file_path = norm.get("file_path") or ""
    size = int(norm.get("file_size_bytes") or 0)
    on_disk = False
    if file_path:
        p = pathlib.Path(file_path)
        if not p.is_absolute() and library_dir is not None:
            p = library_dir / p.name
        size = size or _file_size(p)
        on_disk = p.is_file() if p else False
    elif library_dir is not None and tid:
        # common layout: library/yt_xxx.m4a
        for ext in (".m4a", ".webm", ".opus", ".mp3", ".ogg"):
            cand = library_dir / f"{tid}{ext}"
            if cand.is_file():
                size = cand.stat().st_size
                on_disk = True
                file_path = str(cand)
                break

    cover = (norm.get("cover_url") or norm.get("thumbnail") or "").strip()
    return {
        "id": tid,
        "trackId": tid,
        "title": norm.get("display_title") or norm.get("title") or tid,
        "artist": norm.get("display_artist") or norm.get("artist") or "Desconocido",
        "album": norm.get("album") or "",
        "album_display": norm.get("album_display") or "Sin álbum",
        "artist_key": norm["artist_key"],
        "album_key": norm["album_key"],
        "match_key": norm["match_key"],
        "sources": norm["sources"],
        "origin": norm["sources"][0] if norm["sources"] else "unknown",
        "cover_url": cover,
        "thumbnail": (norm.get("thumbnail") or cover or "").strip(),
        "best_artwork": cover or None,
        "duration": int(norm.get("duration") or 0),
        "play_count": int(norm.get("play_count") or 0),
        "request_count": int(norm.get("request_count") or 0),
        "last_played": float(norm.get("last_played") or 0),
        "last_requested": float(norm.get("last_requested") or 0),
        "cached_at": float(norm.get("cached_at") or 0),
        "spotify_id": norm.get("spotify_id"),
        "video_id": norm.get("video_id"),
        "webpage_url": norm.get("webpage_url") or "",
        "yt_query": norm.get("yt_query") or "",
        "file_size_bytes": size,
        "on_disk": on_disk,
        "source": "disk",
        "liked": False,
        "detect_count": 0,
        "station_name": "",
    }


def _tracks_from_fm(sessions_raw: Any) -> list[dict]:
    if not isinstance(sessions_raw, dict):
        return []
    sessions = sessions_raw.get("sessions") or []
    if not isinstance(sessions, list):
        return []

    by_soft: dict[str, dict] = {}
    for session in sessions:
        if not isinstance(session, dict):
            continue
        station = (session.get("station_name") or "").strip()
        for t in session.get("tracks") or []:
            if not isinstance(t, dict):
                continue
            artist, title = parse_artist_title(t.get("artist"), t.get("title"))
            if not title or title == "?":
                continue
            soft = soft_match_key(artist, title)
            det = float(t.get("detected_at") or 0)
            cover = (t.get("cover_url") or "").strip()
            prev = by_soft.get(soft)
            if prev:
                prev["detect_count"] = int(prev.get("detect_count") or 1) + 1
                prev["play_count"] = prev["detect_count"]
                if det > float(prev.get("last_played") or 0):
                    prev["last_played"] = det
                    prev["cached_at"] = det
                    if cover:
                        prev["cover_url"] = cover
                        prev["best_artwork"] = cover
                        prev["thumbnail"] = cover
                if station and not prev.get("station_name"):
                    prev["station_name"] = station
                continue
            tid = f"fm_{normalize_key(soft).replace(' ', '_')[:100] or 'track'}"
            by_soft[soft] = {
                "id": tid,
                "trackId": tid,
                "title": title,
                "artist": artist,
                "album": "",
                "album_display": "Sin álbum",
                "artist_key": normalize_key(artist) or "desconocido",
                "album_key": "sin-album",
                "match_key": soft,
                "sources": ["fm"],
                "origin": "fm",
                "cover_url": cover,
                "thumbnail": cover,
                "best_artwork": cover or None,
                "duration": 0,
                "play_count": 1,
                "request_count": 0,
                "last_played": det,
                "last_requested": 0,
                "cached_at": det,
                "spotify_id": None,
                "video_id": None,
                "webpage_url": "",
                "yt_query": f"{artist} {title}",
                "file_size_bytes": 0,
                "on_disk": False,
                "source": "fm",
                "liked": False,
                "detect_count": 1,
                "station_name": station,
            }
    return list(by_soft.values())


def _liked_keys(likes_raw: Any) -> set[str]:
    keys: set[str] = set()
    if not isinstance(likes_raw, dict):
        return keys
    for _guild, users in likes_raw.items():
        if not isinstance(users, dict):
            continue
        for _uid, tracks in users.items():
            if not isinstance(tracks, list):
                continue
            for t in tracks:
                if not isinstance(t, dict):
                    continue
                artist, title = parse_artist_title(t.get("artist"), t.get("title"))
                keys.add(soft_match_key(artist, title))
                tid = (t.get("track_id") or "").strip()
                if tid:
                    keys.add(f"id:{tid}")
    return keys


def build_catalog(cache_dir: pathlib.Path) -> dict[str, Any]:
    """Build full catalog payload from a cache directory."""
    global _cache_payload, _cache_key, _cache_ts
    cache_dir = pathlib.Path(cache_dir)
    key = str(cache_dir.resolve())
    now = time.time()
    if (
        _cache_payload is not None
        and _cache_key == key
        and (now - _cache_ts) < _CACHE_TTL_SEC
    ):
        return _cache_payload

    index_path = cache_dir / "library_index.json"
    fm_path = cache_dir / "fm_sessions.json"
    likes_path = cache_dir / "likes.json"
    library_dir = cache_dir / "library"

    raw_index = _load_json(index_path) or {}
    if not isinstance(raw_index, dict):
        raw_index = {}

    tracks: list[dict] = []
    disk_keys: set[str] = set()
    for tid, entry in raw_index.items():
        if not isinstance(entry, dict):
            continue
        tr = _track_from_library(str(tid), entry, library_dir=library_dir if library_dir.is_dir() else None)
        tracks.append(tr)
        disk_keys.add(tr["match_key"])

    for fm_tr in _tracks_from_fm(_load_json(fm_path)):
        if fm_tr["match_key"] in disk_keys:
            continue
        tracks.append(fm_tr)

    liked = _liked_keys(_load_json(likes_path))
    for tr in tracks:
        if tr["match_key"] in liked or f"id:{tr['id']}" in liked:
            tr["liked"] = True
            if "like" not in tr["sources"]:
                tr["sources"] = list(tr["sources"]) + ["like"]

    artists_map: dict[str, dict] = {}
    albums_map: dict[str, dict] = {}

    for tr in tracks:
        ak = tr["artist_key"]
        art = artists_map.get(ak)
        if art is None:
            art = {
                "key": ak,
                "name": tr["artist"],
                "track_count": 0,
                "album_count": 0,
                "play_count": 0,
                "request_count": 0,
                "cover_url": tr.get("cover_url") or "",
                "album_keys": set(),
            }
            artists_map[ak] = art
        art["track_count"] += 1
        art["play_count"] += int(tr.get("play_count") or 0)
        art["request_count"] += int(tr.get("request_count") or 0)
        if tr.get("cover_url") and not art["cover_url"]:
            art["cover_url"] = tr["cover_url"]
        art["album_keys"].add(tr["album_key"])

        al_id = f"{ak}::{tr['album_key']}"
        al = albums_map.get(al_id)
        if al is None:
            al = {
                "key": al_id,
                "artist_key": ak,
                "artist": tr["artist"],
                "name": tr["album_display"],
                "track_count": 0,
                "play_count": 0,
                "cover_url": tr.get("cover_url") or "",
            }
            albums_map[al_id] = al
        al["track_count"] += 1
        al["play_count"] += int(tr.get("play_count") or 0)
        if tr.get("cover_url") and not al["cover_url"]:
            al["cover_url"] = tr["cover_url"]

    artists = []
    for art in artists_map.values():
        artists.append({
            "key": art["key"],
            "name": art["name"],
            "track_count": art["track_count"],
            "album_count": len(art["album_keys"]),
            "play_count": art["play_count"],
            "request_count": art["request_count"],
            "cover_url": art["cover_url"],
        })
    artists.sort(key=lambda a: a["name"].lower())

    albums = list(albums_map.values())
    albums.sort(key=lambda a: (a["artist"].lower(), a["name"].lower()))

    tracks_sorted = sorted(
        tracks,
        key=lambda t: (
            -(float(t.get("last_played") or t.get("cached_at") or 0)),
            t.get("title") or "",
        ),
    )

    payload = {
        "generated_at": now,
        "summary": {
            "tracks": len(tracks_sorted),
            "artists": len(artists),
            "albums": len(albums),
            "on_disk": sum(1 for t in tracks_sorted if t.get("on_disk")),
            "fm": sum(1 for t in tracks_sorted if t.get("source") == "fm"),
            "requests": sum(1 for t in tracks_sorted if int(t.get("request_count") or 0) > 0),
            "liked": sum(1 for t in tracks_sorted if t.get("liked")),
        },
        "tracks": tracks_sorted,
        "artists": artists,
        "albums": albums,
    }
    _cache_payload = payload
    _cache_key = key
    _cache_ts = now
    return payload


def invalidate_catalog_cache() -> None:
    global _cache_payload, _cache_key, _cache_ts
    _cache_payload = None
    _cache_key = None
    _cache_ts = 0.0


def filter_tracks(
    tracks: list[dict],
    *,
    q: str = "",
    source: str = "all",
    artist_key: str = "",
    album_key: str = "",
) -> list[dict]:
    qn = normalize_key(q) if q else ""
    source = (source or "all").strip().lower()
    out = []
    for t in tracks:
        if artist_key and t.get("artist_key") != artist_key:
            continue
        if album_key:
            # album_key may be full "artist::album" or just album part
            tk = t.get("album_key") or ""
            full = f"{t.get('artist_key')}::{tk}"
            if album_key not in (tk, full):
                continue
        if source == "request":
            if int(t.get("request_count") or 0) <= 0 and "request" not in (t.get("sources") or []):
                continue
        elif source == "fm":
            if t.get("source") != "fm" and "fm" not in (t.get("sources") or []):
                continue
        elif source == "radio":
            if "radio" not in (t.get("sources") or []):
                continue
        elif source == "liked":
            if not t.get("liked"):
                continue
        elif source not in ("", "all"):
            if source not in (t.get("sources") or []) and t.get("origin") != source:
                continue
        if qn:
            blob = normalize_key(
                f"{t.get('title')} {t.get('artist')} {t.get('album')} {t.get('yt_query')}"
            )
            if qn not in blob:
                continue
        out.append(t)
    return out


def get_artist(catalog: dict, artist_key: str) -> Optional[dict]:
    for a in catalog.get("artists") or []:
        if a.get("key") == artist_key:
            tracks = filter_tracks(catalog["tracks"], artist_key=artist_key)
            albums = [al for al in catalog.get("albums") or [] if al.get("artist_key") == artist_key]
            return {
                **a,
                "tracks": tracks,
                "albums": albums,
            }
    return None


def get_album(catalog: dict, artist_key: str, album_key: str) -> Optional[dict]:
    full = album_key if "::" in album_key else f"{artist_key}::{album_key}"
    for al in catalog.get("albums") or []:
        if al.get("key") == full or (
            al.get("artist_key") == artist_key and al.get("name") and normalize_key(al["name"]) == album_key
        ):
            tracks = filter_tracks(
                catalog["tracks"],
                artist_key=al["artist_key"],
                album_key=al["key"].split("::", 1)[-1],
            )
            tracks = sorted(tracks, key=lambda t: (t.get("title") or "").lower())
            return {**al, "tracks": tracks}
    return None


def handle_catalog_request(cache_dir: pathlib.Path, path: str, query: str) -> tuple[int, dict]:
    """Route helper for /api/catalog*. Returns (status, json_body)."""
    params = parse_qs(query or "")
    def p(name: str, default: str = "") -> str:
        vals = params.get(name) or []
        return (vals[0] if vals else default).strip()

    try:
        catalog = build_catalog(cache_dir)
    except Exception as exc:
        return 500, {"error": str(exc)}

    if path in ("/api/catalog", "/api/catalog/", "/api/catalog/summary"):
        return 200, {"summary": catalog["summary"], "generated_at": catalog["generated_at"]}

    if path == "/api/catalog/full":
        # Full dump for SPA bootstrap (may be large)
        return 200, catalog

    if path == "/api/catalog/artists":
        q = p("q")
        artists = catalog["artists"]
        if q:
            qn = normalize_key(q)
            artists = [a for a in artists if qn in normalize_key(a.get("name") or "")]
        try:
            limit = max(1, min(500, int(p("limit", "200") or "200")))
        except ValueError:
            limit = 200
        return 200, {"artists": artists[:limit], "total": len(artists)}

    if path == "/api/catalog/albums":
        q = p("q")
        artist_key = p("artist") or p("artist_key")
        albums = catalog["albums"]
        if artist_key:
            albums = [a for a in albums if a.get("artist_key") == artist_key]
        if q:
            qn = normalize_key(q)
            albums = [
                a
                for a in albums
                if qn in normalize_key(a.get("name") or "")
                or qn in normalize_key(a.get("artist") or "")
            ]
        return 200, {"albums": albums, "total": len(albums)}

    if path == "/api/catalog/tracks":
        tracks = filter_tracks(
            catalog["tracks"],
            q=p("q"),
            source=p("source", "all"),
            artist_key=p("artist") or p("artist_key"),
            album_key=p("album") or p("album_key"),
        )
        try:
            limit = max(1, min(2000, int(p("limit", "500") or "500")))
        except ValueError:
            limit = 500
        return 200, {"tracks": tracks[:limit], "total": len(tracks)}

    if path.startswith("/api/catalog/artist/"):
        key = path[len("/api/catalog/artist/") :].strip("/")
        if not key:
            return 400, {"error": "artist key required"}
        # URL may be encoded
        from urllib.parse import unquote

        key = unquote(key)
        detail = get_artist(catalog, key)
        if not detail:
            # try re-normalize
            detail = get_artist(catalog, normalize_key(key))
        if not detail:
            return 404, {"error": "artist not found"}
        return 200, detail

    if path == "/api/catalog/album":
        artist_key = p("artist") or p("artist_key")
        album_key = p("album") or p("album_key")
        if not artist_key or not album_key:
            return 400, {"error": "artist and album required"}
        detail = get_album(catalog, artist_key, album_key)
        if not detail:
            return 404, {"error": "album not found"}
        return 200, detail

    return 404, {"error": "unknown catalog route"}
