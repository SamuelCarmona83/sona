# 🎵 Spoty Scanner — Discord Music Bot

Play Spotify/YouTube/search on Discord.
Solves modern YouTube bot detection. LLM-ranked to reduce costs 70%.

## ✨ What It Does

- **URL aware** — Spotify/YouTube/YouTube Music links routed correctly
- **YouTube JS challenges** — Deno + EJS solve modern signatures
- **Smart YouTube search** — Heuristic + LLM tie-break finds best match
- **Cheap** — Search cache + limited LLM calls (first 3 album tracks only)
- **Queue magic** — Spotify albums/playlists auto-expand
- **Status flex** — Shows now-playing in voice channel status

## 🚀 Quick Start

```bash
# Setup (one-time)
./setup.sh

# Run in Docker (recommended) — bot + data explorer
docker compose up -d

# Or local
./run.sh
```

**Data explorer (optional):** http://localhost:8080/web/explorer.html — browse searches, library, likes, and disk usage.

Then in Discord (in your voice channel):
```
!play <song|spotify|youtube>   # Play directly
!search <song|spotify>          # Pick from 5 candidates
!queue / !skip / !pause / !resume
!np / !stop / !leave
```

## 🔧 Setup

### Docker (No Extra Steps)
- **`bot`** — Discord bot (port 8888 for Spotify OAuth)
- **`explorer`** — data explorer UI (port 8080)
- Builds Python 3.12 + Deno + EJS
- Mounts `cookies.txt` and `spotify_cache/` (persisted cache + library audio)
- Handles FFmpeg + all deps

### Cookie maintenance (zero-cron)

The bot monitors `cookies.txt` and reloads it automatically when it changes (yt-dlp token writeback or host refresh). No cron required.

1. Stay logged into YouTube in Chrome (one-time)
2. Day-to-day: zero maintenance — fallbacks (cookieless clients + local library) keep playback going
3. If the bot alerts you (or `!cookies` shows stale cookies), run once on the Mac:

```bash
./refresh_cookies.sh chrome
```

The bot picks up the new file without restarting Docker. Use `./refresh_cookies.sh chrome --restart` only if you want to force a container restart.

### Local
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="..."
export SPOTIFY_CLIENT_ID="..."
export SPOTIFY_CLIENT_SECRET="..."
```

**Export browser cookies** to `./cookies.txt` via [cookies.txt extension](https://github.com/salsifis/cookies.txt).

## 🎯 URL Routing

| Input | !play | !search |
|-------|-------|----------|
| YouTube (track/mix/playlist) | play | play |
| Spotify track | YouTube search | 5 candidates |
| Spotify album/playlist | queue all | queue all |
| Unknown URL (SoundCloud, etc.) | error | error |
| Text | YouTube search | 5 candidates |

## 📁 Codebase

```
src/
├── commands.py      — !play, !search, !queue handlers (robust URL routing)
├── youtube.py       — URL detection, extraction, ranking
├── spotify.py       — URL parsing, track fetching
├── config.py        — YTDL_OPTIONS, LLM settings, Spotify creds
├── playback.py      — Queue + voice playback
├── scoring.py       — Heuristic ranking + LLM tie-breaking
├── radio.py         — Genre-based auto-queue
├── library.py       — Local audio cache, dedup-safe track IDs
└── dj_announcer.py  — TTS between songs
web/
├── explorer.html    — Static UI (Tailwind) to browse cached JSON data
├── server.py        — Static server + disk/dedupe API
└── dedupe_library.py — Library deduplication logic
scripts/
└── dedupe_library.py — CLI to preview/apply deduplication
```

## ⚙️ Config

### Via `.env.example`

Copy `.env.example` to `.env` and edit:
```bash
cp .env.example .env
# Edit .env with your credentials
```

Full reference in [.env.example](.env.example).

### Key Variables

```bash
# Required
BOT_TOKEN=discord_token_here
SPOTIFY_CLIENT_ID=spotify_id_here
SPOTIFY_CLIENT_SECRET=spotify_secret_here

# YouTube (export cookies from browser via extension)
YTDL_COOKIES_FILE=./cookies.txt
YTDL_USER_AGENT=your_browser_ua_here  # Must match cookie browser

# Optional
NORMALIZE_AUDIO=true              # Loudness normalization
ANTHROPIC_API_KEY=...             # For LLM tie-breaking
DJ_ANNOUNCER_ENABLED=true         # TTS between songs
DJ_VOICE=es-MX-DaliaNeural        # Edge-TTS voice
```

## 📊 Cost Reduction

- **Search cache** — Reuse YouTube results for identical queries
- **LLM margin** — Only call LLM if top 2 candidates within 4.5 points
- **Album limit** — LLM on first 3 tracks, heuristic for rest
- **Aggressive filtering** — "live", "remix", "cover" penalized hard

**Result**: 10-track album = 10 API calls → 3 calls (70% cut)

## 🔐 Credentials

### Spotify
1. [Developer Dashboard](https://developer.spotify.com/dashboard)
2. Create app → copy ID/Secret
3. Redirect URI: `http://localhost:8888/callback`

### Discord
1. [Developer Portal](https://discord.com/developers/applications)
2. Create app → Add Bot → copy token
3. Intents: Message Content, Voice States
4. OAuth2 scopes: `bot`
5. Permissions: Send Messages, Connect, Speak, Use Voice Activity

## 🐛 Troubleshooting

| Issue | Fix |
|-------|-----|
| "Sign in to confirm you're not a bot" | Run `./refresh_cookies.sh chrome` on the Mac; bot auto-reloads cookies |
| Stale cookies (`!cookies` admin) | Same as above; check `.cache/cookie_refresh.log` for history |
| Bot won't start (Docker) | `docker-compose build --no-cache` |
| Commands not working | Check `ALLOWED_CHANNEL_ID` in config |
| No audio | Check bot "Speak" perms in voice channel |
| YouTube fails | `deno --version` (must be installed); refresh cookies.txt |
| Rate-limited by YouTube (~1h block) | Bot notifies in Discord; increase `YTDL_SEARCH_DELAY_SEC` (try 10.0); radio falls back to local library |
| "No se encontró nada" | Invalid query; try exact song name; if rate-limited, only cached/local tracks work |
| Local library empty | Play songs normally first — bot auto-downloads after playback to `.cache/library/` |
| Duplicate songs in library / disk too large | Run `python3 scripts/dedupe_library.py --apply` (stop bot first) or use **Limpiar duplicados** in the explorer |
| Explorer shows no data | Ensure `spotify_cache/` exists and the bot has run at least once; use `docker compose up -d explorer` |
| Explorer dedupe button fails | Recreate explorer after code changes: `docker compose up -d --force-recreate explorer` |
| Bot hangs then restarts; log shows `Enter the URL you were redirected to` | Spotify token expired — run `!auth` in Discord; bot no longer blocks on stdin after fix |

### Local Library

The bot caches audio after successful playback so frequently played songs don't need re-searching:

- **Index:** `.cache/library_index.json` (play counts, metadata)
- **Audio files:** `.cache/library/` (persisted via Docker `spotify_cache` volume on the host)
- **Offline radio:** When YouTube rate-limits the bot, radio mode plays from the local library
- **Stats:** `!library` shows cached track count and top plays
- **Search:** `!library search <query>` finds indexed tracks and lets you play or seed radio
- **Track IDs:** YouTube tracks use stable IDs (`yt_{video_id}`) so the same song is not re-downloaded after bot restarts

### Data Explorer

A lightweight web UI to inspect everything the bot has cached on disk. No extra dependencies — served by a small Python stdlib server.

| Service | URL | Docker service |
|---------|-----|----------------|
| Explorer | http://localhost:8080/web/explorer.html | `explorer` |
| Spotify OAuth | http://localhost:8888/callback | `bot` |

**Docker (recommended):**

```bash
docker compose up -d          # starts bot + explorer
docker compose up -d explorer # explorer only
```

**Local (without Docker):**

```bash
./serve_explorer.sh
# open http://localhost:8080/web/explorer.html
```

**What you can browse:**

| Tab | Data source | Shows |
|-----|-------------|-------|
| Búsquedas | `youtube_metadata.json` | Cached YouTube search results |
| Biblioteca | `library_index.json` + `library/` | Tracks, play counts, file size on disk |
| Likes | `likes.json` | Per-user liked tracks |

**Views:** card grid or sortable table (click column headers). Library table supports **Agrupado** (one row per `video_id`) and **Detallado** (raw index entries).

**API endpoints** (used by the UI):

- `GET /api/disk-usage` — total bytes and per-file sizes in `library/`
- `GET /api/library/dedupe-preview` — duplicate groups and reclaimable space
- `POST /api/library/dedupe` — merge duplicates and delete extra `.m4a` files

Cache path resolution: `.cache/` (inside Docker) or `spotify_cache/` (host volume) — whichever has data.

### Library deduplication

Older bot versions used Python's `hash()` for track IDs, which changes on every restart and caused the same YouTube video to be downloaded multiple times. Current code uses `yt_{video_id}` and skips download when the file already exists.

If you upgraded from an older build, clean up existing duplicates:

```bash
# Preview (dry run)
python3 scripts/dedupe_library.py

# Apply — stop the bot first to avoid index races
docker compose stop bot
python3 scripts/dedupe_library.py --apply
docker compose up -d bot
```

Or from the explorer UI: open **Biblioteca** → **Tabla** → **Limpiar duplicados** (calls `POST /api/library/dedupe`).

The script:

- Groups entries by `video_id`
- Keeps the copy with the most plays (merges play/request counts)
- Deletes duplicate `.m4a` files and rewrites `library_index.json`
- Updates `likes.json` and `played_ids.json` ID references

### Personalized Radio (Spotify profile)

The radio can use real Spotify taste data instead of only server history:

- `!radio profile admin` — liked songs, top tracks, and recents from the admin account (`!auth`)
- `!radio profile voice` — blend tastes of linked users in the voice channel (`!spotify link`)
- `!radio profile playlist <url>` — rotate tracks from a Spotify playlist
- `!radio profile off` — back to guild history only

After changing OAuth scopes, run `!auth` again. Users run `!spotify link` for voice mode.
- **Tuning:** `LIBRARY_MAX_TRACKS`, `LIBRARY_MAX_MB`, `LIBRARY_MIN_PLAYS_TO_PIN` in `.env`

## 📝 License

MIT License — Feel free to use and modify.

---

**Now playing: Your Spotify library on Discord! 🎶**
