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

# Run in Docker (recommended)
docker-compose up

# Or local
./run.sh
```

Then in Discord (in your voice channel):
```
!play <song|spotify|youtube>   # Play directly
!search <song|spotify>          # Pick from 5 candidates
!queue / !skip / !pause / !resume
!np / !stop / !leave
```

## 🔧 Setup

### Docker (No Extra Steps)
- Builds Python 3.12 + Deno + EJS
- Mounts `cookies.txt` into the container
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
└── dj_announcer.py  — TTS between songs
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

### Local Library

The bot caches audio after successful playback so frequently played songs don't need re-searching:

- **Index:** `.cache/library_index.json` (play counts, metadata)
- **Audio files:** `.cache/library/` (persisted via Docker `spotify_cache` volume)
- **Offline radio:** When YouTube rate-limits the bot, radio mode plays from the local library
- **Stats:** `!library` shows cached track count and top plays
- **Tuning:** `LIBRARY_MAX_TRACKS`, `LIBRARY_MAX_MB`, `LIBRARY_MIN_PLAYS_TO_PIN` in `.env`

## 📝 License

MIT License — Feel free to use and modify.

---

**Now playing: Your Spotify library on Discord! 🎶**
