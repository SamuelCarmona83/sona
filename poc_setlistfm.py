import argparse
import json
import os
import re
import time
from urllib import error, request
from urllib.parse import urlparse

import spotipy
from spotipy.oauth2 import SpotifyOAuth


SETLIST_API_BASE = "https://api.setlist.fm/rest/1.0"


def load_dotenv_values(path=".env"):
    """Parse simple .env files with or without `export` prefixes."""
    values = {}
    if not os.path.exists(path):
        return values

    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_config_value(key, dotenv_values, default=None):
    value = os.getenv(key)
    if value:
        return value
    return dotenv_values.get(key, default)


def extract_setlist_id(setlist_url):
    cleaned = setlist_url.strip()
    parsed = urlparse(cleaned)

    if "setlist.fm" not in parsed.netloc:
        raise ValueError("URL invalida: debe ser un enlace de setlist.fm")

    slug = parsed.path.rstrip("/").split("/")[-1]
    if slug.endswith(".html"):
        slug = slug[:-5]

    # Typical setlist slug ends with -<setlistId>
    if "-" in slug:
        candidate = slug.rsplit("-", 1)[-1]
        if re.fullmatch(r"[a-zA-Z0-9]+", candidate):
            return candidate

    # Fallback for unexpected URL formats
    match = re.search(r"([a-zA-Z0-9]+)$", slug)
    if match:
        return match.group(1)

    raise ValueError("URL de setlist.fm invalida. Debe incluir un setlistId en el slug final.")


def fetch_setlist(setlist_id, token, language="en"):
    url = f"{SETLIST_API_BASE}/setlist/{setlist_id}"
    req = request.Request(
        url,
        headers={
            "x-api-key": token,
            "Accept": "application/json",
            "Accept-Language": language,
        },
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as e:
        details = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Error API setlist.fm ({e.code}): {details}") from e
    except error.URLError as e:
        raise RuntimeError(f"No se pudo conectar con setlist.fm: {e.reason}") from e


def ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def parse_setlist_payload(payload):
    artist_name = payload.get("artist", {}).get("name", "Unknown Artist")
    event_date = payload.get("eventDate", "unknown-date")

    venue = payload.get("venue", {})
    city = venue.get("city", {})
    location_parts = [city.get("name"), city.get("country", {}).get("name")]
    location = ", ".join([x for x in location_parts if x])

    songs = []
    sets_block = payload.get("sets", {})
    for set_item in ensure_list(sets_block.get("set")):
        for song in ensure_list(set_item.get("song")):
            name = (song or {}).get("name", "").strip()
            if name:
                songs.append(name)

    return artist_name, event_date, location, songs


def create_spotify_client(client_id, client_secret, redirect_uri):
    scope = "playlist-modify-public playlist-modify-private"
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=scope,
        )
    )


def find_track_id(sp, artist_name, song_name):
    query = f"track:{song_name} artist:{artist_name}"
    results = sp.search(q=query, type="track", limit=1)
    items = results.get("tracks", {}).get("items", [])
    if not items:
        return None
    return items[0]["id"]


def add_tracks_in_batches(sp, playlist_id, track_ids, batch_size=100):
    for i in range(0, len(track_ids), batch_size):
        batch = track_ids[i : i + batch_size]
        sp.playlist_add_items(playlist_id, batch)
        time.sleep(0.1)


def run_poc(setlist_url):
    dotenv_values = load_dotenv_values()

    client_id = get_config_value("SPOTIFY_CLIENT_ID", dotenv_values)
    client_secret = get_config_value("SPOTIFY_CLIENT_SECRET", dotenv_values)
    redirect_uri = get_config_value(
        "SPOTIFY_REDIRECT_URI", dotenv_values, "http://localhost:8888/callback"
    )
    setlist_token = get_config_value("SETLISTFM_TOKEN", dotenv_values)

    if not client_id or not client_secret:
        raise ValueError("Faltan credenciales Spotify (SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET).")
    if not setlist_token:
        raise ValueError("Falta SETLISTFM_TOKEN en el entorno o en .env.")

    setlist_id = extract_setlist_id(setlist_url)
    payload = fetch_setlist(setlist_id, setlist_token, language="en")
    artist_name, event_date, location, songs = parse_setlist_payload(payload)

    if not songs:
        raise RuntimeError("La setlist no contiene canciones parseables.")

    print("\nSetlist encontrada")
    print(f"Artista: {artist_name}")
    print(f"Fecha: {event_date}")
    if location:
        print(f"Lugar: {location}")
    print(f"Canciones: {len(songs)}")

    sp = create_spotify_client(client_id, client_secret, redirect_uri)

    track_ids = []
    missing = []

    for i, song_name in enumerate(songs, 1):
        track_id = find_track_id(sp, artist_name, song_name)
        if track_id:
            track_ids.append(track_id)
        else:
            missing.append(song_name)
        print(f"[{i}/{len(songs)}] {song_name}")
        time.sleep(0.05)

    if not track_ids:
        raise RuntimeError("No se encontraron canciones en Spotify para esta setlist.")

    playlist_name = f"Setlist - {artist_name} - {event_date}"
    description = "Generated from setlist.fm for non-commercial use."
    user_id = sp.current_user()["id"]
    playlist = sp.user_playlist_create(user_id, playlist_name, description=description)

    add_tracks_in_batches(sp, playlist["id"], track_ids)

    print("\nResumen")
    print(f"Total setlist: {len(songs)}")
    print(f"Agregadas: {len(track_ids)}")
    print(f"No encontradas: {len(missing)}")
    if missing:
        for name in missing[:10]:
            print(f"- {name}")
        if len(missing) > 10:
            print(f"... y {len(missing) - 10} mas")
    print(f"Playlist: {playlist['external_urls']['spotify']}")
    print(f"Source: {setlist_url}")


def main():
    parser = argparse.ArgumentParser(
        description="POC: Convert setlist.fm URL into a Spotify playlist"
    )
    parser.add_argument("url", nargs="?", help="setlist.fm URL")
    args = parser.parse_args()

    setlist_url = args.url or input("Pega URL de setlist.fm: ").strip()
    if not setlist_url:
        raise ValueError("Debes ingresar una URL de setlist.fm.")

    run_poc(setlist_url)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nERROR: {e}")