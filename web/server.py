#!/usr/bin/env python3
"""Static file server + disk usage API for the data explorer."""
import json
import os
import pathlib
import sys
import time
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


def _delete_library_track(cache_dir: pathlib.Path, track_id: str) -> dict:
    """Delete one library track from disk + index (explorer admin)."""
    from src.library import delete_track_entry  # noqa: E402

    track_id = (track_id or "").strip()
    if not track_id:
        return {"deleted": False, "error": "track_id required", "status": 400}

    index_path = cache_dir / "library_index.json"
    library_dir = cache_dir / "library"
    covers_dir = library_dir / "covers"
    if not index_path.is_file() and not library_dir.is_dir():
        return {"deleted": False, "error": "Library not found", "status": 404}

    index: dict = {}
    if index_path.is_file():
        try:
            data = json.loads(index_path.read_text())
            if isinstance(data, dict):
                index = data
        except Exception as exc:
            return {"deleted": False, "error": f"Failed to read index: {exc}", "status": 500}

    if track_id not in index:
        # Still try to remove orphan files matching the id.
        has_orphan = library_dir.is_dir() and any(library_dir.glob(f"{track_id}.*"))
        if not has_orphan:
            return {"deleted": False, "error": f"Track not found: {track_id}", "status": 404}

    result = delete_track_entry(
        index,
        track_id,
        library_dir=library_dir,
        covers_dir=covers_dir if covers_dir.is_dir() else None,
    )
    if not result.get("deleted"):
        return {**result, "error": "Nothing deleted", "status": 404}

    try:
        tmp = index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, indent=2))
        tmp.replace(index_path)
    except Exception as exc:
        return {
            **result,
            "error": f"Files removed but index save failed: {exc}",
            "status": 500,
        }
    return {**result, "status": 200}

CACHE_DIRS = (ROOT / ".cache", ROOT / "spotify_cache")
SKIP_SUFFIXES = {".part", ".ytdl"}

# Short TTL cache so explorer startup does not re-stat the whole library every load
_DISK_USAGE_TTL_SEC = 45.0
_disk_usage_cache: dict | None = None
_disk_usage_cache_at: float = 0.0


def build_disk_usage(*, force: bool = False) -> dict:
    global _disk_usage_cache, _disk_usage_cache_at
    now = time.time()
    if (
        not force
        and _disk_usage_cache is not None
        and (now - _disk_usage_cache_at) < _DISK_USAGE_TTL_SEC
    ):
        return _disk_usage_cache

    cache_dir = resolve_cache_dir(ROOT)
    if not cache_dir:
        result = {
            "total_bytes": 0,
            "files": {},
            "tracks_on_disk": 0,
            "library_path": None,
        }
        _disk_usage_cache = result
        _disk_usage_cache_at = now
        return result

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

    result = {
        "total_bytes": total,
        "files": files,
        "tracks_on_disk": len(files),
        "library_path": str(library_dir),
    }
    _disk_usage_cache = result
    _disk_usage_cache_at = now
    return result


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
        if path == "/api/library/track/delete":
            cache_dir = resolve_cache_dir(ROOT)
            if not cache_dir:
                self._send_json({"error": "No cache directory found"}, status=404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode() or "{}")
            except Exception as exc:
                self._send_json({"error": f"Invalid JSON body: {exc}"}, status=400)
                return
            track_id = body.get("track_id") or body.get("tid") or ""
            result = _delete_library_track(cache_dir, track_id)
            status = int(result.pop("status", 200))
            if status >= 400:
                self._send_json(result, status=status)
                return
            # Force disk-usage refresh after delete
            build_disk_usage(force=True)
            self._send_json(result)
            return
        self.send_error(404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        # Allow REST-style DELETE /api/library/track/{track_id}
        prefix = "/api/library/track/"
        if path.startswith(prefix):
            track_id = path[len(prefix):].strip("/")
            cache_dir = resolve_cache_dir(ROOT)
            if not cache_dir:
                self._send_json({"error": "No cache directory found"}, status=404)
                return
            if not track_id:
                self._send_json({"error": "track_id required"}, status=400)
                return
            result = _delete_library_track(cache_dir, track_id)
            status = int(result.pop("status", 200))
            if status < 400:
                build_disk_usage(force=True)
            self._send_json(result, status=status)
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