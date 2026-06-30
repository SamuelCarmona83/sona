import json
import logging
import pathlib
from typing import Optional

logger = logging.getLogger(__name__)

_FM_FAVORITES_PATH = pathlib.Path(".cache/fm_favorites.json")
_fm_favorites: dict[int, dict] = {}


def _load() -> None:
    if not _FM_FAVORITES_PATH.exists():
        return
    try:
        data = json.loads(_FM_FAVORITES_PATH.read_text())
        if not isinstance(data, dict):
            return
        for gid_str, payload in data.items():
            try:
                gid = int(gid_str)
            except Exception:
                continue

            if not isinstance(payload, dict):
                _fm_favorites[gid] = {"next_id": 1, "stations": {}}
                continue

            next_id = payload.get("next_id", 1)
            try:
                next_id = int(next_id)
            except Exception:
                next_id = 1

            stations = payload.get("stations", {})
            if not isinstance(stations, dict):
                stations = {}

            _fm_favorites[gid] = {"next_id": max(1, next_id), "stations": stations}
    except Exception as exc:
        logger.warning("fm favorites: could not load: %s", exc)


def _save() -> None:
    try:
        _FM_FAVORITES_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {str(gid): data for gid, data in _fm_favorites.items()}
        _FM_FAVORITES_PATH.write_text(json.dumps(payload, indent=2))
    except Exception as exc:
        logger.warning("fm favorites: could not save: %s", exc)


def _guild_payload(guild_id: int) -> dict:
    payload = _fm_favorites.setdefault(guild_id, {"next_id": 1, "stations": {}})
    payload.setdefault("next_id", 1)
    payload.setdefault("stations", {})
    return payload


def _station_key(station: dict) -> str:
    sid = (station.get("stationuuid") or "").strip()
    if sid:
        return f"sid:{sid}"
    url = (station.get("url_resolved") or station.get("url") or "").strip()
    return f"url:{url}"


def _station_from_track(track: dict) -> dict:
    return {
        "stationuuid": (track.get("stationuuid") or "").strip(),
        "name": (track.get("title") or "FM Station").strip(),
        "url": (track.get("url") or "").strip(),
        "url_resolved": (track.get("url_resolved") or track.get("url") or "").strip(),
        "homepage": (track.get("webpage_url") or "").strip(),
        "favicon": (track.get("favicon") or track.get("thumbnail") or "").strip(),
        "country": (track.get("artist") or "Radio").strip(),
        "countrycode": (track.get("countrycode") or "").strip().upper(),
        "language": (track.get("language") or "").strip(),
        "codec": (track.get("codec") or "").strip(),
        "bitrate": int(track.get("bitrate") or 0),
        "tags": (track.get("tags") or "").strip(),
    }


def list_favorites(guild_id: int) -> list[tuple[int, dict]]:
    stations = _guild_payload(guild_id).get("stations", {})
    out: list[tuple[int, dict]] = []
    for sid, station in stations.items():
        try:
            out.append((int(sid), station))
        except Exception:
            continue
    out.sort(key=lambda item: item[0])
    return out


def get_favorite(guild_id: int, favorite_id: int) -> Optional[dict]:
    return _guild_payload(guild_id).get("stations", {}).get(str(favorite_id))


def delete_favorite(guild_id: int, favorite_id: int) -> bool:
    stations = _guild_payload(guild_id).get("stations", {})
    key = str(favorite_id)
    if key not in stations:
        return False
    del stations[key]
    _save()
    return True


def favorite_id_for_track(guild_id: int, track: dict) -> Optional[int]:
    station = _station_from_track(track)
    key = _station_key(station)
    for sid, existing in _guild_payload(guild_id).get("stations", {}).items():
        if _station_key(existing) == key:
            try:
                return int(sid)
            except Exception:
                return None
    return None


def toggle_favorite(guild_id: int, track: dict) -> tuple[bool, int]:
    payload = _guild_payload(guild_id)
    stations = payload["stations"]
    station = _station_from_track(track)
    key = _station_key(station)

    for sid, existing in list(stations.items()):
        if _station_key(existing) == key:
            del stations[sid]
            _save()
            return False, int(sid)

    favorite_id = int(payload.get("next_id", 1))
    payload["next_id"] = favorite_id + 1
    stations[str(favorite_id)] = station
    _save()
    return True, favorite_id


_load()