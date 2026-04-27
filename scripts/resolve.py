#!/usr/bin/env python3
"""Resolve a publisher page URL to its underlying Cloudflare Stream iframe URL.

The CF Stream extractor in yt-dlp expects the opaque
``https://iframe.videodelivery.net/<JWT>`` form. Real-world creator-owned
publisher pages embed that iframe inside a paywall HTML wrapper, so this
helper normalises both shapes:

* If the input is *already* on a Cloudflare Stream domain, it is echoed
  back unchanged.
* Otherwise the publisher page is fetched and the first
  ``iframe.videodelivery.net`` URL we can find is returned. We try a
  BeautifulSoup ``<iframe src="...">`` scan first (semantic), then fall
  back to a raw regex sweep across the response body (covers iframes
  injected via JavaScript / JSON blobs).
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from urllib.error import URLError

from bs4 import BeautifulSoup

PASSTHROUGH_PREFIXES: tuple[str, ...] = (
    "https://iframe.videodelivery.net/",
    "https://watch.videodelivery.net/",
)
PASSTHROUGH_PATTERN: re.Pattern[str] = re.compile(
    r"^https://customer-[^./]+\.cloudflarestream\.com/"
)
CF_IFRAME_RE: re.Pattern[str] = re.compile(r"https://iframe\.videodelivery\.net/[A-Za-z0-9._-]+")
USER_AGENT = "Mozilla/5.0 (compatible; patreon-archiver)"
FETCH_TIMEOUT_SECONDS = 30.0


class ResolveError(RuntimeError):
    """Raised when no CF Stream iframe can be located behind a publisher URL."""


def is_passthrough(url: str) -> bool:
    return url.startswith(PASSTHROUGH_PREFIXES) or bool(PASSTHROUGH_PATTERN.match(url))


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 — caller supplies trusted publisher URLs
    with urllib.request.urlopen(  # noqa: S310 — see above
        request, timeout=FETCH_TIMEOUT_SECONDS
    ) as response:
        body = response.read()
    return body.decode("utf-8", errors="replace")


def find_iframe(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for iframe in soup.find_all("iframe", src=True):
        src = iframe.get("src")
        if isinstance(src, str) and src.startswith("https://iframe.videodelivery.net/"):
            return src
    match = CF_IFRAME_RE.search(html)
    return match.group(0) if match else None


__all__ = [
    "CF_IFRAME_RE",
    "PASSTHROUGH_PATTERN",
    "PASSTHROUGH_PREFIXES",
    "ResolveError",
    "fetch",
    "find_iframe",
    "is_passthrough",
    "main",
    "resolve",
]


def resolve(url: str) -> str:
    if is_passthrough(url):
        return url
    try:
        html = fetch(url)
    except (URLError, TimeoutError) as exc:
        raise ResolveError(f"fetch failed for {url}: {exc}") from exc
    iframe = find_iframe(html)
    if iframe is None:
        raise ResolveError(f"no Cloudflare Stream iframe found at {url}")
    return iframe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="publisher page URL or already-CF iframe URL")
    args = parser.parse_args(argv)
    try:
        print(resolve(args.url))
    except ResolveError as exc:
        print(f"resolve: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
