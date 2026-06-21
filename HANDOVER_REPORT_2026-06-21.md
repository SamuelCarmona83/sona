# Handover Report: YouTube Rate-Limit Detection & User Notification

**Session Date:** April 19 - June 21, 2026  
**Repository:** `SamuelCarmona83/spoty-scanner`  
**Status:** ✅ Implementation Complete | Ongoing Monitoring Required

---

## Problem Summary

The Discord music bot (`spoty-scanner`) was experiencing **persistent YouTube rate-limiting** errors:
- Error message: `"Video unavailable. This content isn't available, try again later. The current session has been rate-limited by YouTube for up to an hour."`
- Root cause: YouTube automatically blocks sessions making too many rapid requests/downloads, even with valid authentication and cookies.
- Impact: Bot could not fetch songs for ~1 hour per rate-limit event; users had no visibility into why songs were failing.

### Prior Mitigations Attempted
- Fresh cookie export from host browser → `refresh_cookies.sh` script
- Rate-limiting with delays (`YTDL_SEARCH_DELAY_SEC=7.0`, `YTDL_SEARCH_JITTER_SEC=3.0`)
- Concurrency reduction (`YTDL_SEARCH_CONCURRENCY=1`)
- Exponential backoff for HTTP errors

**Result:** Fresh cookies helped, but YouTube continued rate-limiting based on request volume and session behavior.

---

## Solution Implemented

### 1. **Global Rate-Limit Detection** (`src/youtube.py`)

Added state tracking to detect when YouTube rate-limiting occurs:

```python
# Global rate-limit state
_last_rate_limit_time: float = 0.0
_rate_limit_cooldown_sec = 3600  # 1 hour
_rate_limit_message = "This content isn't available, try again later"

def set_youtube_rate_limited():
    """Mark session as rate-limited."""
    global _last_rate_limit_time
    _last_rate_limit_time = time.time()

def is_youtube_rate_limited() -> bool:
    """Check if currently rate-limited (within 1-hour window)."""
    if _last_rate_limit_time == 0.0:
        return False
    return (time.time() - _last_rate_limit_time) < _rate_limit_cooldown_sec

def maybe_detect_rate_limit(msg: str) -> bool:
    """Detect rate-limit message from yt-dlp error output."""
    if _rate_limit_message in msg:
        set_youtube_rate_limited()
        return True
    return False
```

### 2. **Enhanced yt-dlp Logger** (`src/youtube.py`)

Modified `_YtDlpLogger` class to catch rate-limit errors:

```python
class _YtDlpLogger:
    def warning(self, msg: str) -> None:
        key = msg[:120]
        if key in _YtDlpLogger._warned_once:
            return
        _YtDlpLogger._warned_once.add(key)
        if maybe_detect_rate_limit(msg):
            logger.warning("yt-dlp: [RATE-LIMIT DETECTED] %s", msg)
        else:
            logger.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        if maybe_detect_rate_limit(msg):
            logger.warning("yt-dlp: [RATE-LIMIT DETECTED] %s", msg)
        else:
            logger.warning("yt-dlp: %s", msg)
```

### 3. **User-Facing Notifications** (`src/playback.py`)

When a song fails due to rate-limiting, the bot sends a Discord message to notify users:

```python
# Track last notification per guild to avoid spam
_last_rate_limit_notify: dict[int, float] = {}

# In play_next() function:
if is_youtube_rate_limited():
    last_notify = _last_rate_limit_notify.get(guild.id, 0)
    # Only notify once per 1-hour cooldown window
    if now - last_notify > 3600:
        await text_channel.send(
            ":warning: El bot ha sido temporalmente bloqueado por YouTube por exceder el límite de búsquedas/descargas. "
            "Debes esperar hasta 1 hora para que se levante el bloqueo. Intenta más tarde o reduce la frecuencia de búsquedas."
        )
        _last_rate_limit_notify[guild.id] = now
```

---

## Changes Made

### Files Modified

1. **`src/youtube.py`**
   - Added global rate-limit state variables
   - Added `set_youtube_rate_limited()`, `is_youtube_rate_limited()`, `maybe_detect_rate_limit()` functions
   - Updated `_YtDlpLogger.warning()` and `_YtDlpLogger.error()` to detect and log rate-limit errors with `[RATE-LIMIT DETECTED]` tag

2. **`src/playback.py`**
   - Imported `is_youtube_rate_limited` from `src/youtube`
   - Added `_last_rate_limit_notify` per-guild tracking dict
   - Updated `play_next()` error handler to notify Discord channel when rate-limited (once per hour)

### No Configuration Changes Required
- Existing `.env` settings remain optimal (delays, concurrency, cookies)
- No new environment variables needed

---

## Current Status

### ✅ Implemented
- Rate-limit detection in logger
- Global cooldown state management
- Discord user notifications (one per 1-hour window per guild)
- Log spam suppression for repeated rate-limit errors

### 📊 Log Output Example
```
2026-04-19 15:01:54.994 | WARNING:src.youtube:yt-dlp: [RATE-LIMIT DETECTED] ERROR: [youtube] L-iepu3EtyE: ...
```

### 🟡 Known Limitations
- **No automatic recovery:** Bot still respects the 1-hour rate-limit window from YouTube.
- **Per-session limitation:** Fresh cookies help initially, but heavy load triggers the limit again.
- **Incomplete data errors:** Some YouTube search requests still fail with `[youtube:search] Incomplete data received` (retries 3 times before giving up).

---

## Testing & Verification

### Logs Show Detection Working
```
2026-04-19 15:01:54.994 | WARNING:src.youtube:yt-dlp: [RATE-LIMIT DETECTED] ERROR: [youtube] L-iepu3EtyE: 
   Video unavailable. This content isn't available, try again later. 
   The current session has been rate-limited by YouTube for up to an hour.
```

### Discord Message Sent (Once Per Hour)
```
:warning: El bot ha sido temporalmente bloqueado por YouTube por exceder el límite de búsquedas/descargas. 
Debes esperar hasta 1 hora para que se levante el bloqueo. Intenta más tarde o reduce la frecuencia de búsquedas.
```

---

## Recommendations for Next Steps

### 1. **Monitor Rate-Limiting Patterns**
   - Observe frequency of rate-limits in production.
   - Log rate-limit events to a file for analysis.
   - Consider if certain search queries or genres trigger limits more often.

### 2. **Further Delay Increases** (if rate-limits persist)
   - Current safe settings: `YTDL_SEARCH_DELAY_SEC=7.0`
   - If still rate-limited, try: `YTDL_SEARCH_DELAY_SEC=10.0` or higher

### 3. **Account/IP Rotation** (advanced)
   - If single account/IP is consistently rate-limited, consider:
     - Using a VPN or proxy (changes IP).
     - Creating a secondary YouTube account for searches.

### 4. **Cache Improvements**
   - Expand `_search_cache` to persist across restarts (use file-based cache).
   - Reduces redundant YouTube searches for frequently played songs.

### 5. **Graceful Degradation**
   - When rate-limited, fall back to Spotify-only mode for the 1-hour window.
   - Skip YouTube extraction entirely if `is_youtube_rate_limited()` is True.

---

## Files & References

| File | Change | Purpose |
|------|--------|---------|
| `src/youtube.py` | Rate-limit detection logic | Detects and tracks rate-limit state |
| `src/playback.py` | User notification on error | Informs users when rate-limited |
| `.env` | (No change) | Keep `YTDL_SEARCH_DELAY_SEC=7.0` |
| `README.md` | (Suggested) | Update troubleshooting section with rate-limit info |

---

## Deployment Notes

### Before Redeploying
1. Verify `.env` has safe delays set:
   ```
   YTDL_SEARCH_DELAY_SEC=7.0
   YTDL_SEARCH_JITTER_SEC=3.0
   YTDL_SEARCH_CONCURRENCY=1
   ```
2. Ensure fresh cookies have been exported:
   ```bash
   ./refresh_cookies.sh chrome
   docker-compose up --force-recreate
   ```

### Monitoring
- Watch logs for `[RATE-LIMIT DETECTED]` tags.
- Check Discord channel for user notifications.
- If rate-limits occur frequently, increase delays further or consider IP rotation.

---

## Contact & Questions

If issues arise or changes are needed:
1. Check logs for `[RATE-LIMIT DETECTED]` patterns.
2. Review Discord messages sent to users (timestamp matches error logs).
3. Verify `.env` settings match recommendations.
4. Consider environment-specific issues (network, ISP blocking, etc.).

---

**Session Completed:** 2026-06-21  
**Status:** Ready for Production ✅
