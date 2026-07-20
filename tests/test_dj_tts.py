"""Unit tests for DJ TTS providers and mixer helpers."""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src import dj_announcer


class TtsProviderChoiceTests(unittest.TestCase):
    def test_use_elevenlabs_auto_with_key(self) -> None:
        with patch.object(dj_announcer, "ELEVENLABS_API_KEY", "sk-test"), patch.object(
            dj_announcer, "DJ_TTS_PROVIDER", "auto"
        ):
            self.assertTrue(dj_announcer._use_elevenlabs())

    def test_use_elevenlabs_off_without_key(self) -> None:
        with patch.object(dj_announcer, "ELEVENLABS_API_KEY", ""), patch.object(
            dj_announcer, "DJ_TTS_PROVIDER", "auto"
        ):
            self.assertFalse(dj_announcer._use_elevenlabs())

    def test_force_edge(self) -> None:
        with patch.object(dj_announcer, "ELEVENLABS_API_KEY", "sk-test"), patch.object(
            dj_announcer, "DJ_TTS_PROVIDER", "edge"
        ):
            self.assertFalse(dj_announcer._use_elevenlabs())


class SynthesizeDjAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_edge_when_no_eleven_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = pathlib.Path(tmp)
            with patch.object(dj_announcer, "_DJ_CACHE_DIR", cache), patch.object(
                dj_announcer, "ELEVENLABS_API_KEY", ""
            ), patch.object(
                dj_announcer, "_synthesize_edge", new_callable=AsyncMock, return_value=True
            ) as edge, patch.object(
                dj_announcer, "_synthesize_elevenlabs", new_callable=AsyncMock
            ) as eleven:
                path = await dj_announcer.synthesize_dj_audio("Hola radio", 1)
                self.assertIsNotNone(path)
                edge.assert_awaited_once()
                eleven.assert_not_awaited()

    async def test_eleven_then_fallback_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = pathlib.Path(tmp)
            with patch.object(dj_announcer, "_DJ_CACHE_DIR", cache), patch.object(
                dj_announcer, "ELEVENLABS_API_KEY", "sk-test"
            ), patch.object(dj_announcer, "DJ_TTS_PROVIDER", "auto"), patch.object(
                dj_announcer, "_synthesize_elevenlabs", new_callable=AsyncMock, return_value=False
            ), patch.object(
                dj_announcer, "_synthesize_edge", new_callable=AsyncMock, return_value=True
            ) as edge:
                path = await dj_announcer.synthesize_dj_audio("Hola", 2)
                self.assertIsNotNone(path)
                edge.assert_awaited_once()


class MixerGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_mix_disabled_returns_none(self) -> None:
        with patch.object(dj_announcer, "DJ_MIXER_ENABLED", False):
            result = await dj_announcer.mix_dj_over_local_track(
                "/tmp/a.m4a", "/tmp/b.mp3", 1
            )
            self.assertIsNone(result)

    async def test_mix_missing_files_returns_none(self) -> None:
        with patch.object(dj_announcer, "DJ_MIXER_ENABLED", True):
            result = await dj_announcer.mix_dj_over_local_track(
                "/nonexistent/song.m4a", "/nonexistent/tts.mp3", 1
            )
            self.assertIsNone(result)

    async def test_short_tts_skips_mix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            song = pathlib.Path(tmp) / "song.m4a"
            tts = pathlib.Path(tmp) / "tts.mp3"
            song.write_bytes(b"x" * 200)
            tts.write_bytes(b"y" * 200)
            with patch.object(dj_announcer, "DJ_MIXER_ENABLED", True), patch.object(
                dj_announcer, "DJ_MIXER_FUN_FACTS", False
            ), patch.object(dj_announcer, "DJ_MIXER_MIN_TTS_SEC", 4.0), patch.object(
                dj_announcer, "probe_audio_duration_sec", return_value=2.6
            ):
                result = await dj_announcer.mix_dj_over_local_track(
                    str(song), str(tts), 1
                )
                self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
