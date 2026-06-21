#!/usr/bin/env python3
"""Refresh YouTube cookies from a desktop browser into cookies.txt format.

Run this on the host machine (macOS/Linux/Windows), not inside Docker.
The resulting cookies.txt is already mounted into the container by docker-compose.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
from http.cookiejar import Cookie

try:
    import browser_cookie3
except ImportError as exc:  # pragma: no cover - friendly CLI failure path
    raise SystemExit(
        "browser-cookie3 is required. Install dependencies with: pip install -r requirements.txt"
    ) from exc

DOMAINS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "google.com",
    "accounts.google.com",
    "youtubei.googleapis.com",
)

BROWSERS = {
    "chrome": browser_cookie3.chrome,
    "chromium": getattr(browser_cookie3, "chromium", browser_cookie3.chrome),
    "edge": getattr(browser_cookie3, "edge", browser_cookie3.chrome),
    "firefox": browser_cookie3.firefox,
    "opera": getattr(browser_cookie3, "opera", browser_cookie3.chrome),
}


def _netscape_line(cookie: Cookie) -> str:
    domain = cookie.domain or ""
    include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
    path = cookie.path or "/"
    secure = "TRUE" if cookie.secure else "FALSE"
    expires = str(int(cookie.expires or (time.time() + 86400 * 30)))
    name = cookie.name or ""
    value = cookie.value or ""
    return "\t".join([domain, include_subdomains, path, secure, expires, name, value])


def export_cookies(browser: str, output: pathlib.Path) -> int:
    loader = BROWSERS[browser]
    collected: dict[tuple[str, str, str], Cookie] = {}

    for domain in DOMAINS:
        try:
            jar = loader(domain_name=domain)
        except Exception as exc:
            print(f"warning: could not read {browser} cookies for {domain}: {exc}", file=sys.stderr)
            continue
        for cookie in jar:
            if not cookie.domain:
                continue
            if "youtube" not in cookie.domain and "google" not in cookie.domain:
                continue
            collected[(cookie.domain, cookie.path, cookie.name)] = cookie

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        fh.write("# This file was generated automatically from a local browser session.\n\n")
        for cookie in sorted(collected.values(), key=lambda c: (c.domain, c.path, c.name)):
            fh.write(_netscape_line(cookie) + "\n")

    return len(collected)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export fresh YouTube cookies from a local browser")
    parser.add_argument("--browser", choices=sorted(BROWSERS), default="chrome", help="Browser to read cookies from")
    parser.add_argument("--output", default="cookies.txt", help="Where to write the Netscape cookie file")
    args = parser.parse_args()

    output = pathlib.Path(args.output).expanduser().resolve()
    count = export_cookies(args.browser, output)
    if count <= 0:
        print(
            "No YouTube or Google cookies were exported. Make sure you are logged into YouTube in the selected browser.",
            file=sys.stderr,
        )
        return 1

    print(f"Exported {count} cookies to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
