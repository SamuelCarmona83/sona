#!/usr/bin/env python3
"""Deduplicate library_index.json by YouTube video_id and reclaim disk space."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any, Optional

SKIP_SUFFIXES = {".part", ".ytdl"}


def resolve_cache_dir(root: Optional[pathlib.Path] = None) -> Optional[pathlib.Path]:
    root = root or pathlib.Path(__file__).resolve().parent.parent
    best_dir = None
    best_score = -1
    for name in (".cache", "spotify_cache"):
        cache_dir = root / name
        if not cache_dir.is_dir():
            continue
        score = 0
        if (cache_dir / "library_index.json").is_file():
            score += 10
        library_dir = cache_dir / "library"
        if library_dir.is_dir():
            score += sum(
                1 for path in library_dir.iterdir()
                if path.is_file() and path.suffix not in SKIP_SUFFIXES
            )
        if score > best_score:
            best_score = score
            best_dir = cache_dir
    return best_dir


def _file_on_disk(cache_dir: pathlib.Path, entry: dict) -> Optional[pathlib.Path]:
    file_path = entry.get("file_path")
    if file_path:
        path = pathlib.Path(file_path)
        if path.is_file():
            return path
        basename = path.name
        candidate = cache_dir / "library" / basename
        if candidate.is_file():
            return candidate
    return None


def _canonical_tid(video_id: str) -> str:
    return f"yt_{video_id}"


def _pick_canonical(items: list[tuple[str, dict, Optional[pathlib.Path]]]) -> tuple[str, dict, Optional[pathlib.Path]]:
    def sort_key(item: tuple[str, dict, Optional[pathlib.Path]]) -> tuple:
        tid, entry, path = item
        return (
            entry.get("play_count", 0),
            1 if path else 0,
            entry.get("cached_at", 0),
            entry.get("last_played", 0),
        )

    return max(items, key=sort_key)


def analyze(cache_dir: pathlib.Path) -> dict[str, Any]:
    index_path = cache_dir / "library_index.json"
    library_dir = cache_dir / "library"
    index = json.loads(index_path.read_text()) if index_path.is_file() else {}

    by_video: dict[str, list[tuple[str, dict, Optional[pathlib.Path]]]] = {}
    orphan_index: list[str] = []

    for tid, entry in index.items():
        path = _file_on_disk(cache_dir, entry)
        video_id = entry.get("video_id")
        if not video_id:
            if not path and entry.get("play_count", 0) == 0:
                orphan_index.append(tid)
            continue
        by_video.setdefault(video_id, []).append((tid, entry, path))

    duplicate_groups = []
    files_to_delete: list[str] = []
    id_remap: dict[str, str] = {}
    wasted_bytes = 0

    for video_id, items in by_video.items():
        if len(items) == 1:
            tid, entry, path = items[0]
            canonical_tid = _canonical_tid(video_id)
            if tid != canonical_tid:
                id_remap[tid] = canonical_tid
            continue

        canonical_tid, canonical_entry, canonical_path = _pick_canonical(items)
        canonical_tid_target = _canonical_tid(video_id)
        group_waste = 0
        dup_tids = []

        for tid, entry, path in items:
            id_remap[tid] = canonical_tid_target
            if tid == canonical_tid and path:
                continue
            if path and path != canonical_path:
                group_waste += path.stat().st_size
                files_to_delete.append(str(path))
            dup_tids.append(tid)

        if canonical_tid != canonical_tid_target:
            id_remap[canonical_tid] = canonical_tid_target

        wasted_bytes += group_waste
        duplicate_groups.append({
            "video_id": video_id,
            "title": canonical_entry.get("title", "?"),
            "copies": len(items),
            "wasted_bytes": group_waste,
            "canonical_tid": canonical_tid_target,
            "duplicate_tids": dup_tids,
        })

    orphan_files = []
    if library_dir.is_dir():
        indexed_paths = {
            str(path.resolve())
            for entry in index.values()
            for path in [_file_on_disk(cache_dir, entry)]
            if path
        }
        for path in library_dir.iterdir():
            if not path.is_file() or path.suffix in SKIP_SUFFIXES:
                continue
            if str(path.resolve()) not in indexed_paths:
                orphan_files.append(str(path))
                wasted_bytes += path.stat().st_size

    return {
        "cache_dir": str(cache_dir),
        "total_entries": len(index),
        "unique_videos": len(by_video),
        "duplicate_groups": len(duplicate_groups),
        "wasted_bytes": wasted_bytes,
        "orphan_index_entries": len(orphan_index),
        "orphan_files": len(orphan_files),
        "groups": sorted(duplicate_groups, key=lambda g: g["wasted_bytes"], reverse=True),
        "files_to_delete": files_to_delete + orphan_files,
        "id_remap": id_remap,
        "orphan_index_tids": orphan_index,
    }


def _merge_entries(items: list[tuple[str, dict, Optional[pathlib.Path]]]) -> dict:
    _, canonical_entry, _ = _pick_canonical(items)
    merged = dict(canonical_entry)
    merged["play_count"] = sum(e.get("play_count", 0) for _, e, _ in items)
    merged["request_count"] = sum(e.get("request_count", 0) for _, e, _ in items)
    merged["last_played"] = max(
        (e.get("last_played", 0) for _, e, _ in items),
        default=0,
    )
    merged["last_requested"] = max(
        (e.get("last_requested", 0) for _, e, _ in items),
        default=0,
    )
    merged["cached_at"] = max(
        (e.get("cached_at", 0) for _, e, _ in items),
        default=0,
    )
    return merged


def _remap_ids(data: dict, id_remap: dict[str, str]) -> dict:
    if not id_remap:
        return data

    def remap(tid: str) -> str:
        seen = set()
        while tid in id_remap and tid not in seen:
            seen.add(tid)
            tid = id_remap[tid]
        return tid

    if all(isinstance(v, list) for v in data.values()):
        for guild_id, ids in data.items():
            remapped = []
            seen = set()
            for tid in ids:
                new_tid = remap(tid)
                if new_tid not in seen:
                    remapped.append(new_tid)
                    seen.add(new_tid)
            data[guild_id] = remapped
        return data

    for guild_id, users in data.items():
        if not isinstance(users, dict):
            continue
        for user_id, tracks in users.items():
            if not isinstance(tracks, list):
                continue
            for track in tracks:
                if "track_id" in track:
                    track["track_id"] = remap(track["track_id"])
    return data


def apply(cache_dir: pathlib.Path, *, dry_run: bool = True) -> dict[str, Any]:
    preview = analyze(cache_dir)
    if dry_run:
        return {**preview, "applied": False}

    index_path = cache_dir / "library_index.json"
    library_dir = cache_dir / "library"
    index = json.loads(index_path.read_text()) if index_path.is_file() else {}

    by_video: dict[str, list[tuple[str, dict, Optional[pathlib.Path]]]] = {}
    kept_without_video: dict[str, dict] = {}

    for tid, entry in index.items():
        path = _file_on_disk(cache_dir, entry)
        video_id = entry.get("video_id")
        if not video_id:
            if path or entry.get("play_count", 0) > 0:
                kept_without_video[tid] = entry
            continue
        by_video.setdefault(video_id, []).append((tid, entry, path))

    new_index: dict[str, dict] = dict(kept_without_video)
    renamed_files = 0
    deleted_files = 0
    bytes_freed = 0

    for video_id, items in by_video.items():
        canonical_tid = _canonical_tid(video_id)
        merged = _merge_entries(items)
        _, _, canonical_path = _pick_canonical(items)

        for tid, entry, path in items:
            if path and (not canonical_path or path != canonical_path):
                bytes_freed += path.stat().st_size
                path.unlink(missing_ok=True)
                deleted_files += 1

        if canonical_path:
            target_path = library_dir / f"{canonical_tid}{canonical_path.suffix}"
            if canonical_path != target_path:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                if target_path.exists():
                    target_path.unlink()
                canonical_path.rename(target_path)
                renamed_files += 1
                merged["file_path"] = str(target_path.resolve())
                size = target_path.stat().st_size
                merged["file_size_bytes"] = size
            else:
                merged["file_path"] = str(canonical_path.resolve())
                merged["file_size_bytes"] = canonical_path.stat().st_size
        else:
            merged.pop("file_path", None)
            merged.pop("file_size_bytes", None)

        merged["video_id"] = video_id
        new_index[canonical_tid] = merged

    for orphan_path in preview["files_to_delete"]:
        path = pathlib.Path(orphan_path)
        if path.is_file():
            bytes_freed += path.stat().st_size
            path.unlink(missing_ok=True)
            deleted_files += 1

    index_path.write_text(json.dumps(new_index, indent=2))

    id_remap = preview["id_remap"]
    for filename, remap_fn in (
        ("likes.json", _remap_ids),
        ("played_ids.json", _remap_ids),
    ):
        file_path = cache_dir / filename
        if not file_path.is_file():
            continue
        data = json.loads(file_path.read_text())
        file_path.write_text(json.dumps(remap_fn(data, id_remap)))

    return {
        **preview,
        "applied": True,
        "entries_after": len(new_index),
        "deleted_files": deleted_files,
        "renamed_files": renamed_files,
        "bytes_freed": bytes_freed,
    }


def _format_bytes(num: int) -> str:
    if num <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    size = float(num)
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    return f"{size:.1f} {units[unit]}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deduplicate Spoty Scanner library by video_id")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--cache-dir", type=pathlib.Path, default=None)
    args = parser.parse_args(argv)

    cache_dir = args.cache_dir or resolve_cache_dir()
    if not cache_dir:
        print("No cache directory found (.cache or spotify_cache)", file=sys.stderr)
        return 1

    result = apply(cache_dir, dry_run=not args.apply)

    print(f"Cache: {cache_dir}")
    print(f"Entries: {result['total_entries']} -> {result.get('entries_after', result['total_entries'])}")
    print(f"Duplicate groups: {result['duplicate_groups']}")
    print(f"Wasted space: {_format_bytes(result['wasted_bytes'])}")
    print(f"Orphan index entries: {result['orphan_index_entries']}")
    print(f"Orphan files: {result['orphan_files']}")

    for group in result["groups"][:10]:
        print(
            f"  - {group['title'][:50]}: {group['copies']} copies, "
            f"waste {_format_bytes(group['wasted_bytes'])}"
        )

    if args.apply:
        print(f"Deleted files: {result.get('deleted_files', 0)}")
        print(f"Bytes freed: {_format_bytes(result.get('bytes_freed', 0))}")
        print("Done.")
    else:
        print("\nDry run. Use --apply to execute.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())