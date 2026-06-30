#!/usr/bin/env python3
"""CLI wrapper for autonomous library metadata + artwork enrichment."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Lazy import so --help works even without full runtime deps (yt_dlp/requests/spotipy)
def _get_fns():
    from src.library import scan_and_enrich_library, get_stats  # noqa: E402
    return scan_and_enrich_library, get_stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Enrich Spoty Scanner library with Spotify/Last.fm/Genius metadata and official artwork (covers, genius links, lyrics state)")
    parser.add_argument("--apply", action="store_true", help="Apply enrichment (default dry: just preview counts)")
    parser.add_argument("--max", type=int, default=200, help="Max entries to process this run")
    parser.add_argument("--force", action="store_true", help="Re-enrich even recent entries")
    args = parser.parse_args(argv)

    if not args.apply:
        print("DRY RUN (use --apply to mutate index + download covers if configured)")
    print("Scanning library for enrichment (Spotify + Genius for artwork + metadata)...")

    scan, getst = _get_fns()
    res = scan(max_items=args.max, force=args.force) if args.apply else None

    # Always report stats
    st = getst()
    print(f"Total indexed: {st['total_indexed']}")
    print(f"With cover: {st.get('with_cover', 0)}")
    print(f"Enriched: {st.get('enriched', 0)}")
    print(f"On disk: {st['on_disk']} ({st['size_mb']} MB)")

    if args.apply:
        print(f"Processed: {res.get('processed', 0)}")
        print(f"Updated: {res.get('updated', 0)}")
        print(f"Skipped: {res.get('skipped', 0)}")
        print(f"Errors: {res.get('errors', 0)}")
        print("Done. Covers downloaded to library/covers/ if LIBRARY_FETCH_COVERS enabled.")
    else:
        print("\nDry run complete. Re-run with --apply to perform enrichment.")
        print("Tip: set LIBRARY_AUTO_ENRICH=true in env to auto-enrich on future plays.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
