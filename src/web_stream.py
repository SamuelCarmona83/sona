import asyncio
import logging
import queue
import subprocess
import threading
from typing import Optional

from flask import Flask, Response, jsonify

logger = logging.getLogger(__name__)

# Global web stream instance (singleton)
_web_stream_instance: Optional["WebStreamManager"] = None
_web_stream_lock = threading.Lock()


class WebStreamManager:
    """Manages Flask app + parallel FFmpeg subprocess for audio streaming."""

    def __init__(self, port: int = 5000):
        self.port = port
        self.app = Flask(__name__)
        self.audio_queue: queue.Queue = queue.Queue(maxsize=100)
        
        # Shared playback state
        self.now_playing: Optional[dict] = None
        self.is_paused = False
        self.current_url: Optional[str] = None
        self.ffmpeg_process: Optional[subprocess.Popen] = None
        self.track_version = 0  # Increments on each track change (for browser refresh detection)
        
        # Thread for Flask
        self.flask_thread: Optional[threading.Thread] = None
        self.flask_running = False
        
        # Thread for FFmpeg streaming
        self.stream_thread: Optional[threading.Thread] = None
        self.stream_running = False
        
        # Lock for state updates
        self.state_lock = threading.Lock()
        
        self._setup_routes()
    
    def _setup_routes(self):
        """Setup Flask routes."""
        
        @self.app.route("/")
        def index():
            """Serve HTML player page."""
            return """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🎵 Spoty Scanner - Web Stream</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 100%);
            color: #fff;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        
        .container {
            max-width: 500px;
            width: 100%;
            background: #121212;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
            border: 1px solid #282828;
        }
        
        .title {
            text-align: center;
            font-size: 24px;
            margin-bottom: 30px;
            color: #1DB954;
            font-weight: bold;
        }
        
        .now-playing {
            margin-bottom: 30px;
            text-align: center;
        }
        
        .album-art {
            width: 200px;
            height: 200px;
            margin: 0 auto 20px;
            border-radius: 8px;
            background: #282828;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }
        
        .album-art img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        
        .track-title {
            font-size: 18px;
            font-weight: bold;
            margin-bottom: 8px;
            color: #fff;
        }
        
        .track-artist {
            font-size: 14px;
            color: #b3b3b3;
            margin-bottom: 4px;
        }
        
        .track-requester {
            font-size: 12px;
            color: #717171;
            margin-bottom: 12px;
        }
        
        .queue-info {
            font-size: 12px;
            color: #717171;
            padding: 8px 12px;
            background: #1DB954;
            background: rgba(29, 185, 84, 0.2);
            border-radius: 4px;
            color: #1DB954;
        }
        
        audio {
            width: 100%;
            margin-bottom: 20px;
            outline: none;
        }
        
        .loading {
            text-align: center;
            color: #717171;
            font-size: 14px;
            margin: 40px 0;
        }
        
        .spinner {
            display: inline-block;
            width: 12px;
            height: 12px;
            border: 2px solid #1DB954;
            border-top: 2px solid transparent;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin-right: 8px;
            vertical-align: middle;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        .status {
            text-align: center;
            font-size: 12px;
            color: #717171;
            margin-top: 20px;
        }
        
        .error {
            color: #ff6b6b;
        }
        
        .controls-info {
            background: rgba(29, 185, 84, 0.1);
            border-left: 3px solid #1DB954;
            padding: 12px;
            margin-top: 15px;
            border-radius: 4px;
            font-size: 13px;
            color: #b3b3b3;
        }
        
        .controls-info strong {
            color: #1DB954;
        }
        
        .progress-bar {
            width: 100%;
            height: 4px;
            background: #282828;
            border-radius: 2px;
            margin: 10px 0;
            overflow: hidden;
        }
        
        .progress-fill {
            height: 100%;
            background: #1DB954;
            width: 0%;
            transition: width 0.1s linear;
        }
        
        #playButton {
            display: none;
            background: #1DB954;
            color: #000;
            border: none;
            padding: 12px 24px;
            font-size: 14px;
            font-weight: bold;
            border-radius: 24px;
            cursor: pointer;
            margin: 15px auto;
            transition: background 0.2s;
        }
        
        #playButton:hover {
            background: #1ed760;
        }
        
        #playButton:active {
            transform: scale(0.98);
        }
        
        .volume-controls {
            display: flex;
            align-items: center;
            gap: 10px;
            margin: 15px 0;
            padding: 12px;
            background: rgba(29, 185, 84, 0.1);
            border-radius: 8px;
        }
        
        #muteButton {
            background: none;
            border: none;
            color: #1DB954;
            font-size: 20px;
            cursor: pointer;
            flex-shrink: 0;
            padding: 4px 8px;
        }
        
        #muteButton:hover {
            color: #1ed760;
        }
        
        #volumeSlider {
            flex: 1;
            height: 4px;
            border-radius: 2px;
            background: #282828;
            outline: none;
            -webkit-appearance: none;
            appearance: none;
        }
        
        #volumeSlider::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #1DB954;
            cursor: pointer;
        }
        
        #volumeSlider::-moz-range-thumb {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #1DB954;
            cursor: pointer;
            border: none;
        }
        
        #volumePercent {
            min-width: 30px;
            text-align: right;
            font-size: 12px;
            color: #1DB954;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="title">🎵 Spoty Scanner</div>
        
        <div class="now-playing" id="nowPlaying">
            <div class="loading">
                <span class="spinner"></span>
                Conectando...
            </div>
        </div>
        
        <button id="playButton">▶ Escuchar en vivo</button>
        
        <div class="progress-bar">
            <div class="progress-fill" id="progressFill"></div>
        </div>
        
        <audio id="audioPlayer" autoplay>
            <source src="/stream" type="audio/mpeg">
            Tu navegador no soporta audio. Intenta con otro navegador.
        </audio>
        
        <div class="volume-controls">
            <button id="muteButton">🔊</button>
            <input type="range" id="volumeSlider" min="0" max="100" value="70">
            <span id="volumePercent">70%</span>
        </div>
        
        <div class="controls-info">
            <strong>📻 Escucha en vivo</strong><br>
            Controla la reproducción desde <strong>Discord</strong> (!play, !skip, !pause)
        </div>
        
        <div class="status" id="status">-</div>
    </div>
    
    <script>
        const audioPlayer = document.getElementById('audioPlayer');
        const nowPlayingDiv = document.getElementById('nowPlaying');
        const statusDiv = document.getElementById('status');
        
        let lastTrackTitle = '';
        let lastTrackVersion = 0;
        
        async function updateNowPlaying() {
            try {
                const response = await fetch('/api/now-playing');
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();
                
                // Detect track change via version increment
                if (data.track_version && data.track_version !== lastTrackVersion) {
                    lastTrackVersion = data.track_version;
                    // Reload stream by resetting audio source
                    audioPlayer.src = '/stream?t=' + Date.now();
                    audioPlayer.load();
                    audioPlayer.play().catch(() => {});  // Auto-play if allowed
                }
                
                if (data.error) {
                    nowPlayingDiv.innerHTML = `
                        <div class="loading error">
                            ${data.error}
                        </div>
                    `;
                    statusDiv.textContent = 'No hay música reproduciéndose';
                    return;
                }
                
                const { title, artist, requester, duration, thumbnail, queue_size } = data;
                
                // Only rebuild if track changed
                if (title !== lastTrackTitle) {
                    lastTrackTitle = title;
                    const thumbHtml = thumbnail ? 
                        `<img src="${thumbnail}" alt="Album art">` : 
                        '<div style="font-size: 48px; color: #1DB954;">🎵</div>';
                    
                    const durationStr = duration ? 
                        `${Math.floor(duration / 60)}:${(duration % 60).toString().padStart(2, '0')}` : 
                        '--:--';
                    
                    nowPlayingDiv.innerHTML = `
                        <div class="album-art">${thumbHtml}</div>
                        <div class="track-title">${escapeHtml(title)}</div>
                        <div class="track-artist">${escapeHtml(artist || 'Unknown')}</div>
                        <div class="track-requester">Solicitado por: ${escapeHtml(requester)}</div>
                        <div class="queue-info">
                            ⏱ ${durationStr} · 📋 ${queue_size} en cola
                        </div>
                    `;
                }
                
                // Update status
                const playerState = audioPlayer.paused ? '⏸ Pausado' : '▶ Reproduciendo';
                statusDiv.textContent = playerState;
                
            } catch (err) {
                statusDiv.textContent = `Error: ${err.message}`;
                console.error('Failed to fetch now-playing:', err);
            }
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        // Update every 2 seconds
        updateNowPlaying();
        setInterval(updateNowPlaying, 2000);
        
        // Update progress bar as audio plays
        audioPlayer.addEventListener('timeupdate', () => {
            const progressFill = document.getElementById('progressFill');
            if (audioPlayer.duration) {
                const percent = (audioPlayer.currentTime / audioPlayer.duration) * 100;
                progressFill.style.width = percent + '%';
            }
        });
        
        const playButton = document.getElementById('playButton');
        
        // Handle audio events
        audioPlayer.addEventListener('play', () => {
            statusDiv.textContent = '▶ Reproduciendo en directo';
            playButton.style.display = 'none';
        });
        
        audioPlayer.addEventListener('pause', () => {
            statusDiv.textContent = '⏸ Pausado (solo lectura — controla desde Discord)';
            playButton.style.display = 'block';
        });
        
        audioPlayer.addEventListener('error', (e) => {
            statusDiv.textContent = '❌ Error de conexión de audio';
            playButton.style.display = 'block';
        });
        
        // Play button click handler
        playButton.addEventListener('click', () => {
            audioPlayer.play().catch(() => {
                playButton.style.display = 'block';
                statusDiv.textContent = '❌ Navegador bloqueó autoplay';
            });
        });
        
        // Show play button initially (autoplay might fail)
        playButton.style.display = 'block';
        
        // Try autoplay
        audioPlayer.play().catch(() => {
            console.log('Autoplay blocked, user must click play button');
            playButton.style.display = 'block';
        });
        
        // Volume controls
        const muteButton = document.getElementById('muteButton');
        const volumeSlider = document.getElementById('volumeSlider');
        const volumePercent = document.getElementById('volumePercent');
        let lastVolume = 70;
        
        // Set initial volume
        audioPlayer.volume = 0.7;
        
        // Volume slider change
        volumeSlider.addEventListener('input', (e) => {
            const vol = parseInt(e.target.value);
            audioPlayer.volume = vol / 100;
            volumePercent.textContent = vol + '%';
            if (vol > 0) lastVolume = vol;
            
            // Update mute button icon
            if (vol === 0) {
                muteButton.textContent = '🔇';
            } else if (vol < 30) {
                muteButton.textContent = '🔉';
            } else {
                muteButton.textContent = '🔊';
            }
        });
        
        // Mute button toggle
        muteButton.addEventListener('click', () => {
            if (audioPlayer.volume > 0) {
                lastVolume = parseInt(volumeSlider.value);
                volumeSlider.value = 0;
                audioPlayer.volume = 0;
                volumePercent.textContent = '0%';
                muteButton.textContent = '🔇';
            } else {
                volumeSlider.value = lastVolume;
                audioPlayer.volume = lastVolume / 100;
                volumePercent.textContent = lastVolume + '%';
                if (lastVolume < 30) {
                    muteButton.textContent = '🔉';
                } else {
                    muteButton.textContent = '🔊';
                }
            }
        });
    </script>
</body>
</html>"""
        
        @self.app.route("/stream")
        def stream():
            """Stream audio chunks from FFmpeg subprocess."""
            def generate():
                # Clear old chunks when client connects
                try:
                    while not self.audio_queue.empty():
                        self.audio_queue.get_nowait()
                except queue.Empty:
                    pass
                
                # Send chunks as they arrive
                timeout_count = 0
                while True:
                    try:
                        chunk = self.audio_queue.get(timeout=2.0)
                        timeout_count = 0
                        yield chunk
                    except queue.Empty:
                        timeout_count += 1
                        # After 5 timeouts (10 sec), client probably disconnected
                        if timeout_count > 5:
                            break
                    except Exception as e:
                        logger.warning(f"stream: error yielding chunk: {e}")
                        break
            
            return Response(
                generate(),
                mimetype="audio/mpeg",
                headers={"Cache-Control": "no-cache"}
            )
        
        @self.app.route("/api/now-playing")
        def api_now_playing():
            """Return current playback state as JSON."""
            with self.state_lock:
                if self.now_playing is None:
                    return jsonify({
                        "error": "No hay música reproduciéndose",
                        "track_version": self.track_version,
                    }), 200
                
                return jsonify({
                    "title": self.now_playing.get("title", "Unknown"),
                    "artist": self.now_playing.get("artist", "Unknown"),
                    "requester": self.now_playing.get("requester", "Unknown"),
                    "duration": self.now_playing.get("duration", 0),
                    "thumbnail": self.now_playing.get("thumbnail"),
                    "queue_size": self.now_playing.get("queue_size", 0),
                    "track_version": self.track_version,
                }), 200
    
    def start(self):
        """Start Flask server in background thread."""
        if self.flask_running:
            logger.info("web_stream: Flask already running")
            return
        
        def run_flask():
            try:
                logger.info(f"web_stream: starting Flask on port {self.port}")
                self.app.run(
                    host="0.0.0.0",
                    port=self.port,
                    debug=False,
                    use_reloader=False,
                    threaded=True,
                )
            except Exception as e:
                logger.error(f"web_stream: Flask error: {e}")
            finally:
                self.flask_running = False
        
        self.flask_running = True
        self.flask_thread = threading.Thread(daemon=True, target=run_flask)
        self.flask_thread.start()
        logger.info("web_stream: Flask thread started")
    
    def stream_track(self, url: str, now_playing_info: dict, queue_size: int):
        """Start streaming a new track via FFmpeg subprocess.
        
        Args:
            url: YouTube/Spotify URL to stream
            now_playing_info: Track metadata dict (title, artist, duration, thumbnail, requester)
            queue_size: Number of tracks in queue
        """
        if not url:
            logger.warning("web_stream: stream_track called with empty URL")
            return
        
        # Update state
        with self.state_lock:
            self.now_playing = {**now_playing_info, "queue_size": queue_size}
            self.track_version += 1  # Signal track change to browser clients
        
        # Kill old FFmpeg if running
        if self.ffmpeg_process:
            try:
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait(timeout=2.0)
            except Exception as e:
                logger.warning(f"web_stream: failed to kill old FFmpeg: {e}")
                self.ffmpeg_process.kill()
            self.ffmpeg_process = None
        
        # Clear audio queue
        try:
            while not self.audio_queue.empty():
                self.audio_queue.get_nowait()
        except queue.Empty:
            pass
        
        self.current_url = url
        
        # Start FFmpeg in background thread
        if not self.stream_running:
            self.stream_running = True
            self.stream_thread = threading.Thread(
                daemon=True,
                target=self._ffmpeg_stream_loop,
            )
            self.stream_thread.start()
    
    def _ffmpeg_stream_loop(self):
        """Background thread: run FFmpeg and pipe audio to queue."""
        from src.config import FFMPEG_OPTIONS
        
        while self.stream_running:
            if not self.current_url:
                # Wait for new URL
                threading.Event().wait(0.5)
                continue
            
            try:
                # Build FFmpeg command
                before_opts = FFMPEG_OPTIONS["before_options"]
                options = FFMPEG_OPTIONS["options"]
                
                # For web stream, output MP3 (compatible with all browsers)
                cmd = [
                    "ffmpeg",
                    *before_opts.split(),
                    "-i", self.current_url,
                    *options.split(),
                    "-f", "mp3",
                    "-q:a", "5",  # Quality 5 (good quality, ~128kb/s)
                    "pipe:1",
                ]
                
                logger.info(f"web_stream: starting FFmpeg for {self.current_url}")
                
                self.ffmpeg_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=4096,
                )
                
                # Read and queue audio chunks
                while self.ffmpeg_process and self.stream_running:
                    chunk = self.ffmpeg_process.stdout.read(4096)
                    if not chunk:
                        break
                    
                    try:
                        self.audio_queue.put_nowait(chunk)
                    except queue.Full:
                        # Drop oldest chunk if queue full (client can't keep up)
                        try:
                            self.audio_queue.get_nowait()
                            self.audio_queue.put_nowait(chunk)
                        except queue.Empty:
                            pass
                
                # FFmpeg exited or was killed
                if self.ffmpeg_process:
                    rc = self.ffmpeg_process.wait(timeout=1.0)
                    if rc != 0 and rc != -15:  # -15 = SIGTERM
                        stderr = self.ffmpeg_process.stderr.read().decode("utf-8", errors="ignore")
                        logger.warning(f"web_stream: FFmpeg exited with code {rc}: {stderr[:200]}")
                    self.ffmpeg_process = None
                
                # Wait for next track URL (avoid tight loop)
                threading.Event().wait(1.0)
                
            except Exception as e:
                logger.error(f"web_stream: FFmpeg stream error: {e}")
                self.ffmpeg_process = None
                threading.Event().wait(2.0)
    
    def update_now_playing(self, track_info: dict, queue_size: int):
        """Update now-playing state without restarting FFmpeg."""
        with self.state_lock:
            if self.now_playing:
                self.now_playing.update(track_info)
                self.now_playing["queue_size"] = queue_size
    
    def stop(self):
        """Stop web stream."""
        logger.info("web_stream: stopping")
        self.stream_running = False
        
        if self.ffmpeg_process:
            try:
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait(timeout=2.0)
            except Exception as e:
                logger.warning(f"web_stream: failed to stop FFmpeg: {e}")
                self.ffmpeg_process.kill()
            self.ffmpeg_process = None


def get_web_stream() -> WebStreamManager:
    """Get or create singleton web stream instance."""
    global _web_stream_instance
    
    if _web_stream_instance is None:
        with _web_stream_lock:
            if _web_stream_instance is None:
                _web_stream_instance = WebStreamManager(port=5000)
    
    return _web_stream_instance


def start_web_stream():
    """Initialize and start the web stream server."""
    stream = get_web_stream()
    stream.start()
    logger.info("web_stream: initialized (running on http://localhost:5000/)")


async def stream_track_async(url: str, now_playing_info: dict, queue_size: int):
    """Async wrapper to stream a track."""
    stream = get_web_stream()
    # Run in executor to avoid blocking event loop
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        stream.stream_track,
        url,
        now_playing_info,
        queue_size,
    )
