#!/usr/bin/env python3
"""Clean only suspicious Spotify metadata from library index entries.

Suspicious means the cached title/artist score too low against the stored yt_query,
which often indicates cross-track metadata contamination.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scoring import _score_candidate

DEFAULT_THRESHOLD = 5.0


def _resolve_index_path(root: pathlib.Path) -> pathlib.Path | None:
    for rel in (".cache/library_index.json", "spotify_cache/library_index.json"):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def _entry_score(entry: dict[str, Any]) -> float:
    query = (entry.get("yt_query") or entry.get("title") or "").strip()
    if not query:
        return 0.0
    candidate = {
        "title": entry.get("title") or "",
        "uploader": entry.get("artist") or "",
        "duration": entry.get("duration") or 0,
    }
    return _score_candidate(query, candidate)


def _clean_entry(entry: dict[str, Any]) -> bool:
    touched = False
    for key in ("spotify_id", "artist_id", "cover_url", "local_cover", "album", "release_date"):
        if entry.get(key):
            entry.pop(key, None)
            touched = True
    if entry.get("spotify_refined"):
        entry["spotify_refined"] = False
        touched = True
    return touched


def analyze(index: dict[str, dict[str, Any]], threshold: float) -> dict[str, Any]:
    to_clean: list[tuple[str, float, str, str]] = []
    for tid, entry in index.items():
        if not isinstance(entry, dict):
            continue
        if not tid.startswith("yt_"):
            continue
        if not any(entry.get(k) for k in ("spotify_id", "artist_id", "cover_url", "local_cover", "album", "release_date")):
            continue
        score = _entry_score(entry)
        if score < threshold:
            to_clean.append(
                (
                    tid,
                    score,
                    entry.get("yt_query", ""),
                    entry.get("title", ""),
                )
            )
    return {
        "candidates": sorted(to_clean, key=lambda row: row[1]),
        "count": len(to_clean),
    }


def apply(index: dict[str, dict[str, Any]], tids: list[str]) -> int:
    changed = 0
    for tid in tids:
        entry = index.get(tid)
        if not isinstance(entry, dict):
            continue
        if _clean_entry(entry):
            changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean conflicting Spotify metadata from library_index.json")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--apply", action="store_true", help="Write changes to disk")
    parser.add_argument("--index", type=pathlib.Path, default=None, help="Explicit path to library_index.json")
    args = parser.parse_args()

    root = ROOT
    index_path = args.index or _resolve_index_path(root)
    if not index_path:
        print("No library_index.json found in .cache or spotify_cache")
        return 1

    index = json.loads(index_path.read_text())
    if not isinstance(index, dict):
        print(f"Invalid index format at {index_path}")
        return 1

    report = analyze(index, args.threshold)
    print(f"Index: {index_path}")
    print(f"Threshold: {args.threshold:.2f}")
    print(f"Conflicting entries: {report['count']}")
    for tid, score, query, title in report["candidates"][:20]:
        print(f"  - {tid}: score={score:.2f} | query='{query[:40]}' | title='{title[:40]}'")

    if not args.apply:
        print("Dry run. Use --apply to clean only those entries.")
        return 0

    changed = apply(index, [tid for tid, *_ in report["candidates"]])
    index_path.write_text(json.dumps(index, indent=2))
    print(f"Cleaned entries: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
