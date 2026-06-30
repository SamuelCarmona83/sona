# Sona

A Discord music bot that plays tracks from Spotify URLs, YouTube links, and plain-text search. Built for environments where YouTube aggressively blocks automated access: Deno-backed yt-dlp extraction, browser cookie support, a local audio library for offline fallback, and optional LLM-assisted track matching.

## Table of contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Usage](#usage)
- [Project structure](#project-structure)
- [Data explorer](#data-explorer)
- [Local library](#local-library)
- [Radio modes](#radio-modes)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Features

- **Multi-source playback** — Spotify tracks, albums, and playlists; YouTube and YouTube Music URLs; free-text search.
- **Resilient YouTube access** — Deno + EJS challenge solving, cookie hot-reload, cookieless client fallback, and rate-limit detection with user notification.
- **Smart matching** — Heuristic scoring with optional Claude tie-breaking when top candidates are close; search result caching to reduce API spend.
- **Local library** — Auto-caches played audio to disk; stable track IDs (`spotify_id` → `yt_{video_id}` → SHA-256 fallback); offline radio when YouTube is blocked.
- **24/7 radio** — Mood-based genre seeds, Spotify taste profiles, liked-track priority, DJ announcer (Edge-TTS), and fast-start playback (first track plays while the queue keeps filling).
- **Data explorer** — Lightweight web UI to inspect cached metadata, library usage, and run deduplication.

## Requirements

| Component | Version / notes |
|-----------|-----------------|
| Python | 3.12+ |
| FFmpeg | Required for voice streaming |
| Deno | Bundled in Docker image; required locally for modern YouTube |
| Docker | Recommended for production |
| Discord bot token | [Discord Developer Portal](https://discord.com/developers/applications) |
| Spotify app | [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) |
| YouTube cookies | Export to `cookies.txt` via [Get cookies.txt LOCALLY](https://github.com/salsifis/cookies.txt) |
| Anthropic API key | Optional; enables LLM tie-breaking |

## Quick start

### Docker (recommended)

```bash
# One-time setup
./setup.sh
cp .env.example .env   # fill in credentials

# Start bot + data explorer
docker compose up -d
```

| Service | Port | Purpose |
|---------|------|---------|
| `bot` | 8888 | Discord bot + Spotify OAuth callback |
| `explorer` | 8080 | Data explorer UI |

Open the explorer at [http://localhost:8080/web/explorer.html](http://localhost:8080/web/explorer.html).

### Local development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # configure credentials
export BOT_TOKEN="..."
./run.sh
```

Serve the explorer without Docker:

```bash
./serve_explorer.sh
```

### Discord setup

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable **Message Content** and **Voice States** intents.
3. Invite the bot with `bot` scope and permissions: Send Messages, Connect, Speak, Use Voice Activity.
4. Set your text and voice channel IDs in [`src/config.py`](src/config.py) (`BOT_TEXT_CHANNEL_ID`, `BOT_VOICE_CHANNEL_ID`).

### Spotify setup

1. Create an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Add redirect URI: `http://localhost:8888/callback`
3. Copy client ID and secret into `.env`.
4. Run `!auth` in Discord after the bot starts.

### YouTube cookies

The bot watches `cookies.txt` and reloads it when the file changes — no cron job required.

```bash
# When cookies expire or the bot alerts you
./refresh_cookies.sh chrome
```

The running container picks up the new file without a restart. Use `--restart` only if you want to force-recreate the container.

## Configuration

Copy [`.env.example`](.env.example) to `.env`. Key variables:

```bash
# Required
BOT_TOKEN=
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=

# YouTube
YTDL_COOKIES_FILE=./cookies.txt
YTDL_SEARCH_DELAY_SEC=7.0          # pacing between searches (refill)
YTDL_SEARCH_DELAY_URGENT_SEC=0.5   # first-track / user-request fast path
YTDL_SEARCH_CONCURRENCY=1

# Optional
ANTHROPIC_API_KEY=                 # LLM tie-breaking
NORMALIZE_AUDIO=true               # FFmpeg loudness normalization
DJ_ANNOUNCER_ENABLED=true
LIBRARY_ENABLED=true
LIBRARY_AUTO_DOWNLOAD=true
```

Docker Compose overrides several values for the container environment; see [`docker-compose.yml`](docker-compose.yml).

### Cost-aware LLM usage

LLM calls are limited by design:

- Search cache reuses identical query results.
- LLM runs only when the top two heuristic scores are within 4.5 points.
- Album and playlist imports use LLM on the first three tracks only; the rest use heuristics.

## Usage

Commands are prefixed with `!` and must be sent in the configured text channel.

### Playback

| Command | Description |
|---------|-------------|
| `!play <query\|url>` | Play a track, album, playlist, or YouTube URL |
| `!search <query>` | Show up to five candidates to choose from |
| `!playlist <url>` | Queue a Spotify or YouTube playlist |
| `!pause` / `!resume` | Pause or resume playback |
| `!skip` | Skip the current track |
| `!stop` | Stop and clear the queue |
| `!queue` | Show the queue |
| `!np` | Now playing |
| `!shuffle` | Shuffle the queue |
| `!move <from> <to>` | Reorder a queue item |
| `!remove <pos>` | Remove a queue item |
| `!priority <pos>` | Move a track to the front |
| `!leave` | Disconnect from voice |

### Radio

| Command | Description |
|---------|-------------|
| `!radio` / `!radio on` | Start 24/7 radio |
| `!radio off` | Stop radio and clear auto-queued tracks |
| `!radio profile admin` | Seed from admin Spotify taste (`!auth` required) |
| `!radio profile voice` | Blend tastes of linked users in the voice channel |
| `!radio profile playlist <url>` | Rotate tracks from a playlist |
| `!radio profile off` | Fall back to guild play history |
| `!mood <name>` | Set genre mood for recommendations |

### Library and likes

| Command | Description |
|---------|-------------|
| `!library` | Library stats and top plays |
| `!library search <query>` | Search cached tracks |
| `!likes` | Your liked tracks in this server |

Use the player embed buttons for like, radio toggle, and mood.

### Admin

| Command | Description |
|---------|-------------|
| `!auth` | Spotify OAuth for radio profiles and recommendations |
| `!cookies` | YouTube cookie health summary |
| `!spotify link` | Link your Spotify account for voice-mode radio |

### URL routing

| Input | `!play` | `!search` |
|-------|---------|-----------|
| YouTube track / mix / playlist | Plays or queues | Plays or queues |
| Spotify track | YouTube match + play | Five candidates |
| Spotify album / playlist | Queues all tracks | Queues all tracks |
| Unsupported URL | Error | Error |
| Plain text | YouTube search + play | Five candidates |

## Project structure

```
src/
├── commands.py       # Discord command handlers
├── playback.py       # Queue, voice client, player embed
├── youtube.py        # Search, extraction, rate limiting
├── spotify.py        # URL parsing, OAuth, recommendations
├── scoring.py        # Heuristic ranking + LLM tie-break
├── radio.py          # Radio fill engine, moods, play history
├── library.py        # Local cache, stable IDs, downloads
├── likes.py          # Per-user likes and radio priority
├── spotify_taste.py  # Taste profile builder
├── dj_announcer.py   # Edge-TTS announcements
├── cookie_health.py  # Cookie watchdog and admin alerts
└── config.py         # Environment, yt-dlp, FFmpeg options

web/
├── explorer.html     # Data explorer UI (Tailwind)
├── server.py         # Static server + disk/dedupe API
└── dedupe_library.py # Deduplication engine

scripts/
└── dedupe_library.py # CLI wrapper for library cleanup
```

Entry point: [`bot.py`](bot.py) → [`src/main.py`](src/main.py).

## Data explorer

A read/write UI for cache data on disk. Useful for debugging searches, monitoring disk usage, and cleaning duplicate library entries.

**Endpoints**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/disk-usage` | Library size and per-file breakdown |
| `GET` | `/api/library/dedupe-preview` | Duplicate groups and reclaimable space |
| `POST` | `/api/library/dedupe` | Merge duplicates and delete extra files |

**Tabs**

| Tab | Source | Content |
|-----|--------|---------|
| Búsquedas | `youtube_metadata.json` | Cached search metadata |
| Biblioteca | `library_index.json` | Tracks, play counts, file sizes |
| Likes | `likes.json` | Per-user liked tracks |

Library table supports grouped view (one row per `video_id`) and detailed view (raw index entries).

Cache resolution checks `.cache/` (inside Docker) and `spotify_cache/` (host volume) and uses whichever contains data.

## Local library

After successful playback, audio can be saved under `.cache/library/` (host: `spotify_cache/library/` when using Docker).

| File | Purpose |
|------|---------|
| `library_index.json` | Metadata, play counts, file paths |
| `library/*.m4a` | Cached audio files |
| `likes.json` | Liked tracks (stable IDs) |
| `played_ids.json` | Radio deduplication history |

When YouTube rate-limits the bot, radio mode falls back to local tracks automatically.

### Deduplication

Older builds used Python's `hash()` for some IDs, which changed on every restart and caused duplicate downloads. Current code uses stable IDs and skips re-download when a `video_id` is already on disk.

**CLI**

```bash
python3 scripts/dedupe_library.py          # preview
docker compose stop bot
python3 scripts/dedupe_library.py --apply  # apply
docker compose up -d bot
```

**Explorer UI:** Biblioteca → Tabla → **Limpiar duplicados**

The dedupe pass groups by `video_id`, keeps the highest-play-count entry, merges stats, updates `likes.json` and `played_ids.json`, and deletes orphan `.m4a` files.

### Metadata & Official Artwork Enrichment

The library now autonomously locates official artwork (Spotify album images preferred, complemented by Genius song art + Last.fm) + rich metadata (album, release date, genres, genius links).

- On play (if `LIBRARY_AUTO_ENRICH=true`) or manually, entries get `cover_url` (Spotify `i.scdn.co/...` or Genius `song_art_image_url`), `album`, etc. stored in `library_index.json`.
- Local copies of covers (when `LIBRARY_FETCH_COVERS=true`) saved to `library/covers/{tid}.jpg`.
- yt-dlp sidecar thumbnails + optional embedded tags when `LIBRARY_EMBED_METADATA=true`.
- Genius support via `GENIUS_ACCESS_TOKEN` (get at https://genius.com/api-clients). Adds `genius_url`, `lyrics_state`, high-quality artwork fallback. Set `GENIUS_CLIENT_ID` / `GENIUS_CLIENT_SECRET` too if using OAuth flows later.

**CLI**
```bash
python3 scripts/enrich_library.py           # preview
python3 scripts/enrich_library.py --apply --max 50
```

**Discord**
`!library enrich` (requires admin Spotify auth via `!auth`).

**Explorer UI:** Biblioteca tab → **enriquecer** button (preview + run).

Enrichment reuses existing Spotify auth, Last.fm fallback, and scoring. Safe, cached, and additive (never breaks playback or dedupe).
```

Also update features list mention if wanted, but ok.

## Radio modes

Radio fills the queue when it drops below a threshold. On cold start, the first resolved track begins playback immediately (`early_play`) while remaining slots are filled in the background.

**Profile sources**

- **admin** — liked songs, top tracks, and recently played from the authenticated admin account.
- **voice** — merged profiles from users in the voice channel who ran `!spotify link`.
- **playlist** — tracks from a configured Spotify playlist URL.
- **off** — guild play history and mood genres only.

After changing OAuth scopes, run `!auth` again. Voice mode requires each user to link their account with `!spotify link`.

## Troubleshooting

| Symptom | What to do |
|---------|------------|
| `Sign in to confirm you're not a bot` | Run `./refresh_cookies.sh chrome` on the host |
| Stale cookies (`!cookies`) | Same as above; check `.cache/cookie_refresh.log` |
| Bot hangs with `Enter the URL you were redirected to` | Spotify token expired — run `!auth`; the bot no longer blocks on stdin |
| Commands ignored | Verify `BOT_TEXT_CHANNEL_ID` in [`src/config.py`](src/config.py) |
| No audio | Confirm Connect + Speak permissions in the voice channel |
| YouTube search failures | Ensure Deno is installed (`deno --version`); refresh cookies |
| YouTube rate limit (~1 h) | Bot notifies in Discord; increase `YTDL_SEARCH_DELAY_SEC`; radio uses local library |
| `No se encontró nada` | Refine the query; if rate-limited, only cached/local tracks work |
| Empty local library | Play tracks normally first to seed the cache |
| Duplicate library entries | Run [`scripts/dedupe_library.py`](scripts/dedupe_library.py) or use the explorer |
| Explorer shows no data | Ensure `spotify_cache/` exists; run `docker compose up -d explorer` |
| Explorer dedupe fails after upgrade | `docker compose up -d --force-recreate explorer` |
| Docker build issues | `docker compose build --no-cache bot` |
| Spotify 403 on artist endpoints | API restriction for newer apps; radio degrades gracefully |
| Slow radio start | Expected on first YouTube search; cached/local tracks start in seconds |

## License

MIT — use and modify freely.