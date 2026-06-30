#!/usr/bin/env python3
"""Static file server + disk usage API for the data explorer."""
import json
import os
import pathlib
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT))  # for src.*

from dedupe_library import analyze, apply, resolve_cache_dir  # noqa: E402

# Lazy to avoid import side effects until needed
def _get_enrich_fns():
    from src.library import scan_and_enrich_library, get_stats  # noqa: E402
    return scan_and_enrich_library, get_stats

CACHE_DIRS = (ROOT / ".cache", ROOT / "spotify_cache")
SKIP_SUFFIXES = {".part", ".ytdl"}


def build_disk_usage() -> dict:
    cache_dir = resolve_cache_dir(ROOT)
    if not cache_dir:
        return {
            "total_bytes": 0,
            "files": {},
            "tracks_on_disk": 0,
            "library_path": None,
        }

    library_dir = cache_dir / "library"
    files: dict[str, int] = {}
    total = 0
    if library_dir.is_dir():
        for path in library_dir.iterdir():
            if not path.is_file() or path.suffix in SKIP_SUFFIXES:
                continue
            size = path.stat().st_size
            files[path.stem] = size
            total += size

    return {
        "total_bytes": total,
        "files": files,
        "tracks_on_disk": len(files),
        "library_path": str(library_dir),
    }


class ExplorerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/disk-usage":
            self._send_json(build_disk_usage())
            return
        if path == "/api/library/dedupe-preview":
            cache_dir = resolve_cache_dir(ROOT)
            if not cache_dir:
                self._send_json({"error": "No cache directory found"}, status=404)
                return
            self._send_json(analyze(cache_dir))
            return
        if path == "/api/library/enrich-preview":
            try:
                scan, getst = _get_enrich_fns()
                # Preview is cheap: report current state + how many could benefit
                st = getst()
                idx = {}  # we don't want to import full index here; use stats
                # Suggest based on missing artwork (primary goal of the enrichment system)
                missing_artwork = max(0, st.get("total_indexed", 0) - st.get("with_cover", 0))
                self._send_json({
                    "stats": st,
                    "suggest_enrich": missing_artwork,
                    "note": "POST /api/library/enrich to run (targets tracks without cover_url using Spotify/Genius/Last.fm)",
                })
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/library/dedupe":
            cache_dir = resolve_cache_dir(ROOT)
            if not cache_dir:
                self._send_json({"error": "No cache directory found"}, status=404)
                return
            try:
                result = apply(cache_dir, dry_run=False)
                self._send_json(result)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)
            return
        if path == "/api/library/enrich":
            try:
                scan, _ = _get_enrich_fns()
                # Run limited autonomous pass (safe batch size)
                import asyncio
                result = asyncio.run(scan(max_items=100, force=False))
                # Also return fresh stats
                _, getst = _get_enrich_fns()
                result["stats_after"] = getst()
                self._send_json(result)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        if args and isinstance(args[0], str) and args[0].startswith("GET /api/"):
            return
        super().log_message(fmt, *args)


def main():
    port = int(os.environ.get("EXPLORER_PORT", "8080"))
    host = os.environ.get("EXPLORER_HOST", "0.0.0.0")
    server = HTTPServer((host, port), ExplorerHandler)
    print(f"Spoty Scanner — Explorador en http://localhost:{port}/web/explorer.html")
    server.serve_forever()


if __name__ == "__main__":
    main()