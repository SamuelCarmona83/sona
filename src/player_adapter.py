"""Audio playback adapter (Adapter Pattern).

Provides an abstract AudioPlayer interface with two concrete implementations:
  - DiscordAudioPlayer: wraps discord.VoiceClient + discord.FFmpegOpusAudio (production)
  - FfplayAudioPlayer:  uses a subprocess ffplay process (local development)

The implementation to use is selected via the APP_MODE environment variable:
  APP_MODE=discord (default) → DiscordAudioPlayer
  APP_MODE=local             → FfplayAudioPlayer
"""

import abc
import asyncio
import logging
import os
import shlex
import signal
import subprocess
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class AudioPlayer(abc.ABC):
    """Abstract audio player interface."""

    @abc.abstractmethod
    async def play(
        self,
        url: str,
        ffmpeg_options: dict,
        after: Callable[[Optional[Exception]], None],
    ) -> None:
        """Start playing audio from *url*. Calls ``after(error)`` when done."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop playback immediately."""

    @abc.abstractmethod
    def pause(self) -> None:
        """Pause playback."""

    @abc.abstractmethod
    def resume(self) -> None:
        """Resume playback."""

    @abc.abstractmethod
    def is_playing(self) -> bool:
        """Return True if actively playing (not paused)."""

    @abc.abstractmethod
    def is_paused(self) -> bool:
        """Return True if currently paused."""


class DiscordAudioPlayer(AudioPlayer):
    """Adapts a discord.VoiceClient to the AudioPlayer interface."""

    def __init__(self, voice_client) -> None:
        self._vc = voice_client

    async def play(
        self,
        url: str,
        ffmpeg_options: dict,
        after: Callable[[Optional[Exception]], None],
    ) -> None:
        import discord

        source = discord.FFmpegOpusAudio(url, **ffmpeg_options)
        self._vc.play(source, after=after)

    def stop(self) -> None:
        self._vc.stop()

    def pause(self) -> None:
        self._vc.pause()

    def resume(self) -> None:
        self._vc.resume()

    def is_playing(self) -> bool:
        return self._vc.is_playing()

    def is_paused(self) -> bool:
        return self._vc.is_paused()


class FfplayAudioPlayer(AudioPlayer):
    """Uses an ffplay subprocess for local audio playback."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._paused: bool = False
        self._monitor_task: Optional[asyncio.Task] = None

    async def play(
        self,
        url: str,
        ffmpeg_options: dict,
        after: Callable[[Optional[Exception]], None],
    ) -> None:
        self.stop()

        before_options = ffmpeg_options.get("before_options", "")
        options = ffmpeg_options.get("options", "")

        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
        if before_options:
            cmd += shlex.split(before_options)
        # Translate FFmpegOpusAudio options into ffplay-compatible flags.
        # -vn        → skip video (supported by ffplay)
        # -bufsize    → ignored (ffplay doesn't accept -bufsize as a global option)
        # -af <filter> → supported by ffplay
        extra_opts = [p for p in shlex.split(options) if not p.startswith("-bufsize")]
        cmd += extra_opts
        cmd.append(url)

        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._paused = False
            self._monitor_task = asyncio.create_task(self._monitor(after))
        except FileNotFoundError:
            logger.error("ffplay no encontrado. Instala ffmpeg/ffplay.")
            after(Exception("ffplay not found — instala ffmpeg"))

    async def _monitor(self, after: Callable[[Optional[Exception]], None]) -> None:
        """Wait for the ffplay process to finish and call *after*."""
        if self._process is None:
            return
        try:
            await asyncio.to_thread(self._process.wait)
            rc = self._process.returncode
            # Negative return code means killed by a signal (intentional stop)
            if rc is not None and rc >= 0:
                after(None)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            after(exc)
        finally:
            self._process = None
            self._paused = False

    def stop(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            self._monitor_task = None
        proc = self._process
        if proc and proc.poll() is None:
            if self._paused:
                try:
                    os.kill(proc.pid, signal.SIGCONT)
                except OSError:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._process = None
        self._paused = False

    def pause(self) -> None:
        proc = self._process
        if proc and proc.poll() is None and not self._paused:
            try:
                os.kill(proc.pid, signal.SIGSTOP)
                self._paused = True
            except OSError as exc:
                logger.warning("FfplayAudioPlayer.pause: %s", exc)

    def resume(self) -> None:
        proc = self._process
        if proc and proc.poll() is None and self._paused:
            try:
                os.kill(proc.pid, signal.SIGCONT)
                self._paused = False
            except OSError as exc:
                logger.warning("FfplayAudioPlayer.resume: %s", exc)

    def is_playing(self) -> bool:
        return (
            self._process is not None
            and self._process.poll() is None
            and not self._paused
        )

    def is_paused(self) -> bool:
        return (
            self._process is not None
            and self._process.poll() is None
            and self._paused
        )
