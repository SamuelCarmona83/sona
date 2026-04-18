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
- Mounts `cookies.txt` (auto-updated by yt-dlp)
- Handles FFmpeg + all deps

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

## ⚙️ Config (`src/config.py`)

```python
YTDL_OPTIONS["cookiefile"] = "/app/cookies.txt"  # yt-dlp refreshes auto
LLM_SCORE_MARGIN = 4.5         # LLM only if candidates within 4.5 points
LLM_ENABLED_FOR_ALBUM_TRACKS = 3   # LLM on first 3 tracks only
MIN_SEARCH_SCORE = 6.0         # Reject YouTube matches below 6.0
NORMALIZE_AUDIO = "true"       # Real-time loudness (dynaudnorm filter)
DJ_ANNOUNCER_ENABLED = "true"  # TTS between genre changes
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
| "Sign in to confirm you're not a bot" | Update cookies.txt; `pip install --upgrade yt-dlp>=2025.1.0` |
| Bot won't start (Docker) | `docker-compose build --no-cache` |
| Commands not working | Check `ALLOWED_CHANNEL_ID` in config |
| No audio | Check bot "Speak" perms in voice channel |
| YouTube fails | `deno --version` (must be installed); refresh cookies.txt |
| "No se encontró nada" | Invalid query; try exact song name |

## 📝 License

MIT License — Feel free to use and modify.

---

**Now playing: Your Spotify library on Discord! 🎶**
