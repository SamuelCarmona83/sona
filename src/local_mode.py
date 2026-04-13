"""Local development mode — run the music bot without Discord.

Usage:
    APP_MODE=local python bot.py

Behaviour:
  - Audio is played via an ffplay subprocess (FfplayAudioPlayer adapter).
  - The terminal UI uses *rich* to simulate Discord-style embeds and panels.
  - Commands mirror the Discord bot's prefix-commands (e.g. ``!play``, ``!skip``).
  - Spotify integration works as usual when credentials are present in .env.
"""

import asyncio
import collections
import logging
import threading
from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.player_adapter import FfplayAudioPlayer
from src.config import FFMPEG_OPTIONS
from src.youtube import search_youtube
from src.spotify import (
    _is_spotify_url,
    _get_tracks_from_spotify_url,
    _get_spotify_track_info,
)
from src.scoring import _split_query_parts

logger = logging.getLogger(__name__)

_LOCAL_USER = "local_user"
_HELP_TEXT = (
    "[dim]Comandos:[/dim] "
    "[cyan]!play <cancion>[/cyan]  "
    "[cyan]!search <cancion>[/cyan]  "
    "[cyan]!skip[/cyan]  "
    "[cyan]!pause[/cyan]  "
    "[cyan]!resume[/cyan]  "
    "[cyan]!stop[/cyan]  "
    "[cyan]!queue[/cyan]  "
    "[cyan]!np[/cyan]  "
    "[cyan]!quit[/cyan]"
)


class LocalBot:
    """Simulates the Discord music bot in a plain terminal session."""

    def __init__(self) -> None:
        self.console = Console()
        self.player = FfplayAudioPlayer()
        self.queue: collections.deque = collections.deque()
        self.now_playing: Optional[dict] = None
        self.paused: bool = False
        self._running: bool = True
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Rich display helpers
    # ------------------------------------------------------------------

    def _player_panel(self) -> Panel:
        """Build a Rich panel that simulates a Discord player embed."""
        if not self.now_playing:
            body = Text("Nada reproduciendose.", style="dim")
            return Panel(
                body,
                title="🎵 Reproductor",
                border_style="grey50",
                subtitle="Usa !play <cancion> para agregar canciones",
            )

        track = self.now_playing
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold cyan", min_width=14)
        grid.add_column()

        grid.add_row("🎵 Título:", Text(track["title"][:80], style="green bold"))
        grid.add_row("🎤 Artista:", track.get("artist", "Unknown"))

        duration = track.get("duration", 0)
        dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else "--:--"
        grid.add_row("⏱ Duración:", dur_str)
        grid.add_row("👤 Pedido por:", track.get("requester", _LOCAL_USER))
        grid.add_row("📋 En cola:", str(len(self.queue)))

        if self.queue:
            next_track = list(self.queue)[0]
            grid.add_row("⏭ Siguiente:", next_track.get("title", "?")[:60])

        status = "⏸ En pausa" if self.paused else "▶ Reproduciendo"
        return Panel(grid, title="🎵 Reproductor", border_style="green", subtitle=status)

    def _print_player(self) -> None:
        self.console.print(self._player_panel())

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    async def play_next(self) -> None:
        """Pop the next track and start playing it."""
        if not self.queue:
            self.now_playing = None
            self.paused = False
            self.console.print("[dim]Cola vacía. Reproducción terminada.[/dim]")
            return

        track = self.queue.popleft()

        # Lazily resolve YouTube URL if not yet fetched
        if not track.get("url"):
            try:
                yt_info = await search_youtube(track["yt_query"])
                if yt_info:
                    track["url"] = yt_info["url"]
                    track["title"] = yt_info["title"]
                else:
                    self.console.print(
                        f"[red]No encontrado en YouTube, saltando:[/red] {track.get('yt_query')}"
                    )
                    await self.play_next()
                    return
            except Exception as exc:
                self.console.print(f"[red]Error buscando en YouTube:[/red] {exc}")
                await self.play_next()
                return

        self.now_playing = track
        self.paused = False
        self.console.print(
            f"\n[green]▶ Ahora reproduciendo:[/green] [bold]{track['title']}[/bold]"
        )
        self._print_player()

        loop = self._loop

        def _after(error: Optional[Exception]) -> None:
            if error:
                logger.error("Error en reproduccion local: %s", error)
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(self.play_next(), loop)

        await self.player.play(track["url"], FFMPEG_OPTIONS, _after)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_play(self, query: str) -> None:
        self.console.print(f"[yellow]🔍 Buscando:[/yellow] {query}")

        if _is_spotify_url(query):
            track_infos = await _get_tracks_from_spotify_url(query)
            if not track_infos:
                self.console.print("[red]No se pudo procesar la URL de Spotify.[/red]")
                return
        else:
            info = await _get_spotify_track_info(query)
            track_infos = [info]

        added = 0
        for info in track_infos:
            yt_info = await search_youtube(info["query"])
            if not yt_info:
                continue
            artist, _ = _split_query_parts(info["query"])
            track = {
                "title":      yt_info["title"],
                "yt_query":   info["query"],
                "url":        yt_info["url"],
                "requester":  _LOCAL_USER,
                "artist":     artist or "Unknown",
                "duration":   yt_info.get("duration") or 0,
                "thumbnail":  yt_info.get("thumbnail") or "",
                "spotify_id": info.get("spotify_id"),
                "artist_id":  info.get("artist_id"),
            }
            self.queue.append(track)
            added += 1

        if added == 0:
            self.console.print(f"[red]No se encontró nada para:[/red] {query}")
            return

        if not self.player.is_playing() and not self.player.is_paused():
            await self.play_next()
        else:
            label = (
                self.queue[-1]["title"] if added == 1 else f"{added} canciones"
            )
            self.console.print(f"[cyan]➕ {label} añadida(s) a la cola.[/cyan]")

    async def _cmd_search(self, query: str) -> None:
        """Interactive search: show top candidates and let user pick one."""
        from src.youtube import get_search_candidates

        self.console.print(f"[yellow]🔍 Buscando candidatos para:[/yellow] {query}")
        candidates = await get_search_candidates(query)
        if not candidates:
            self.console.print(f"[red]No se encontraron resultados para:[/red] {query}")
            return

        table = Table(title=f"Resultados para: {query}", box=box.ROUNDED)
        table.add_column("#", style="dim", width=3)
        table.add_column("Título")
        table.add_column("Canal", style="dim")
        table.add_column("Duración", justify="right")
        for idx, c in enumerate(candidates[:5], 1):
            dur = c.get("duration") or 0
            dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
            table.add_row(str(idx), c.get("title", "?"), c.get("uploader", ""), dur_str)
        self.console.print(table)

        self.console.print("[dim]Elige un número (1-5) o pulsa Enter para cancelar:[/dim]")
        try:
            choice_raw = await asyncio.to_thread(input, "> ")
            choice = int(choice_raw.strip())
            if choice < 1 or choice > len(candidates):
                raise ValueError
        except (ValueError, EOFError):
            self.console.print("[dim]Búsqueda cancelada.[/dim]")
            return

        selected = candidates[choice - 1]
        artist, _ = _split_query_parts(query)
        track = {
            "title":     selected["title"],
            "yt_query":  query,
            "url":       selected["url"],
            "requester": _LOCAL_USER,
            "artist":    artist or selected.get("uploader", "Unknown"),
            "duration":  selected.get("duration") or 0,
            "thumbnail": selected.get("thumbnail") or "",
        }
        self.queue.append(track)

        if not self.player.is_playing() and not self.player.is_paused():
            await self.play_next()
        else:
            self.console.print(f"[cyan]➕ {selected['title']} añadida a la cola.[/cyan]")

    async def _cmd_skip(self) -> None:
        if not self.player.is_playing() and not self.player.is_paused():
            self.console.print("[dim]No hay nada reproduciéndose.[/dim]")
            return
        self.player.stop()

    async def _cmd_pause(self) -> None:
        if self.player.is_playing():
            self.player.pause()
            self.paused = True
            self._print_player()
            self.console.print("[yellow]⏸ Pausado.[/yellow]")
        else:
            self.console.print("[dim]No hay nada reproduciéndose.[/dim]")

    async def _cmd_resume(self) -> None:
        if self.player.is_paused():
            self.player.resume()
            self.paused = False
            self._print_player()
            self.console.print("[green]▶ Reanudado.[/green]")
        else:
            self.console.print("[dim]No hay nada en pausa.[/dim]")

    async def _cmd_stop(self) -> None:
        self.queue.clear()
        self.now_playing = None
        self.paused = False
        self.player.stop()
        self.console.print("[red]⏹ Detenido y cola limpiada.[/red]")

    async def _cmd_queue(self) -> None:
        if not self.now_playing and not self.queue:
            self.console.print("[dim]La cola está vacía.[/dim]")
            return

        table = Table(title="Cola de reproducción", box=box.ROUNDED)
        table.add_column("#", style="dim", width=3)
        table.add_column("Título")
        table.add_column("Duración", justify="right")

        if self.now_playing:
            dur = self.now_playing.get("duration", 0)
            dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "--:--"
            table.add_row(
                "▶",
                Text(self.now_playing["title"], style="green bold"),
                dur_str,
            )

        for i, t in enumerate(list(self.queue)[:15], 1):
            dur = t.get("duration", 0)
            dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "--:--"
            table.add_row(str(i), t.get("title", "?"), dur_str)

        if len(self.queue) > 15:
            table.add_row("...", f"y {len(self.queue) - 15} más", "")

        self.console.print(table)

    async def _cmd_np(self) -> None:
        if not self.now_playing:
            self.console.print("[dim]No hay nada reproduciéndose.[/dim]")
            return
        t = self.now_playing
        dur = t.get("duration", 0)
        dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "--:--"
        self.console.print(
            f"[green]▶ Ahora:[/green] [bold]{t['title']}[/bold] "
            f"[dim]({dur_str})[/dim] — pedido por {t.get('requester', _LOCAL_USER)}"
        )

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    async def _handle_command(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if not line.startswith("!"):
            self.console.print(
                "[dim]Los comandos empiezan con '!'. Escribe !help para ver la lista.[/dim]"
            )
            return

        parts = line[1:].split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "play":
            if arg:
                await self._cmd_play(arg)
            else:
                self.console.print("[dim]Uso: !play <cancion o URL de Spotify>[/dim]")
        elif cmd == "search":
            if arg:
                await self._cmd_search(arg)
            else:
                self.console.print("[dim]Uso: !search <cancion>[/dim]")
        elif cmd == "skip":
            await self._cmd_skip()
        elif cmd == "pause":
            await self._cmd_pause()
        elif cmd == "resume":
            await self._cmd_resume()
        elif cmd == "stop":
            await self._cmd_stop()
        elif cmd in ("queue", "q"):
            await self._cmd_queue()
        elif cmd in ("np", "nowplaying"):
            await self._cmd_np()
        elif cmd == "help":
            self.console.print(Panel(_HELP_TEXT, border_style="dim"))
        elif cmd in ("quit", "exit"):
            await self._cmd_stop()
            self._running = False
        else:
            self.console.print(
                f"[dim]Comando desconocido: [bold]{cmd}[/bold]. Escribe !help.[/dim]"
            )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()

        self.console.print(
            Panel(
                "[green bold]🎵 Spoty Scanner — Modo Local[/green bold]\n\n"
                "[dim]Bot de música en modo desarrollo (sin Discord).\n"
                "El audio se reproduce localmente con ffplay.[/dim]\n\n"
                + _HELP_TEXT,
                border_style="green",
            )
        )

        input_queue: asyncio.Queue[str] = asyncio.Queue()

        def _read_stdin() -> None:
            while self._running:
                try:
                    line = input("\n> ")
                    self._loop.call_soon_threadsafe(input_queue.put_nowait, line)
                except EOFError:
                    self._loop.call_soon_threadsafe(input_queue.put_nowait, "!quit")
                    break
                except Exception:
                    break

        reader = threading.Thread(target=_read_stdin, daemon=True)
        reader.start()

        while self._running:
            try:
                line = await asyncio.wait_for(input_queue.get(), timeout=0.5)
                await self._handle_command(line)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.error("LocalBot error inesperado: %s", exc, exc_info=True)

        self.player.stop()
        self.console.print("[dim]¡Hasta luego![/dim]")


async def run_local() -> None:
    """Entry point for APP_MODE=local."""
    bot = LocalBot()
    await bot.run()
