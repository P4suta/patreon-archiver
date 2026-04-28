#!/usr/bin/env python3
"""Convert a Patreon page MHTML snapshot into a curation-friendly post list.

For each post we emit a Markdown section plus a fenced code block that
copy-pastes verbatim into ``urls/urls.txt``: the block carries the stream
URL together with ``# key: value`` metadata lines that ``download.sh``
reads back to build the output filename and embedded mp4 tags.

Pipe to a file::

    just inventory ~/Downloads/foo.mhtml > urls/posts.md
"""

from __future__ import annotations

import argparse
import datetime
import email
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from _mhtml import latest_mhtml

VIDEO_LEN_RE = re.compile(r"(?:Video length|動画の長さ|🔴)\s*(\d{1,3}:\d{2}(?::\d{2})?)")

# Stream URL shape we know how to mine date and host from. Mirrors the
# regex in resolve.sh / download.sh. The publisher embeds the upload date
# as an 8-digit prefix, which is more reliable than parsing the localized
# "N日前" string Patreon renders.
STREAM_URL_RE = re.compile(
    r"^(?P<scheme>https?)://(?P<host>stream\.[^/]+)/"
    r"(?P<date>\d{8})_(?P<slug>[^_/]+)_[^/]+/?$"
)


def read_html_from_mhtml(path: Path) -> str:
    with path.open("rb") as f:
        msg = email.message_from_binary_file(f)
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload.decode("utf-8", errors="replace")
    raise SystemExit(f"no text/html part found in {path}")


def text_of(node: Tag | None) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def find_first(card: Tag, tag: str) -> Tag | None:
    return card.find(attrs={"data-tag": tag})


def _href_of(anchor: Tag) -> str | None:
    """Return the ``href`` attribute as a plain string (BS4 returns a union type)."""
    raw = anchor.get("href")
    return raw if isinstance(raw, str) else None


def post_url(card: Tag) -> str | None:
    candidates: list[Tag | None] = [find_first(card, "post-title"), card]
    for node in candidates:
        if node is None:
            continue
        for a in node.find_all("a", href=True):
            href = _href_of(a)
            if href is None or "/posts/" not in href:
                continue
            return href if href.startswith("http") else f"https://www.patreon.com{href}"
    return None


def stream_urls(card: Tag) -> list[str]:
    """Mine CF-Stream-fronted publisher URLs out of a post card.

    The match key is :data:`STREAM_URL_RE` — `https?://stream.<host>/<date>_
    <slug>_<token>/` — which fits any creator who fronts Cloudflare Stream
    with a `stream.<creator>.<tld>` publisher domain (the common convention
    when the Patreon embed iframe is hidden behind a creator-owned host).
    Edit the regex if your publisher uses a different host prefix.
    """
    seen: set[str] = set()
    out: list[str] = []
    for a in card.find_all("a", href=True):
        href = _href_of(a)
        if href is None:
            continue
        if STREAM_URL_RE.match(href) and href not in seen:
            seen.add(href)
            out.append(href)
    return out


def video_length(card: Tag) -> str | None:
    body = find_first(card, "post-content-content") or find_first(card, "post-content")
    if not body:
        return None
    match = VIDEO_LEN_RE.search(body.get_text(" "))
    return match.group(1) if match else None


def page_uploader(soup: BeautifulSoup) -> str:
    """Best-effort creator display name from the MHTML <title>.

    Patreon page titles look like ``<creator> | <subtitle> | Patreon``.
    Strip the trailing " Official" most ASMR creators append so the
    folder name on disk stays compact.
    """
    title_node = soup.find("title")
    raw = text_of(title_node) if title_node else ""
    head = raw.split(" | ", 1)[0].strip()
    return re.sub(r"\s+Official$", "", head) or "Unknown"


def meta_block(stream: str, title: str, uploader: str, post: str) -> list[str]:
    """The metadata + URL lines that download.sh consumes verbatim."""
    match = STREAM_URL_RE.match(stream)
    date_iso = ""
    if match:
        d = match.group("date")
        date_iso = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    inside: list[str] = [f"# title: {title}"]
    if uploader:
        inside.append(f"# uploader: {uploader}")
    if date_iso:
        inside.append(f"# date: {date_iso}")
    if post:
        inside.append(f"# post: {post}")
    inside.append(stream)
    return inside


def url_block(stream: str, title: str, uploader: str, post: str) -> list[str]:
    return ["```text", *meta_block(stream, title, uploader, post), "```"]


def render_post(idx: int, card: Tag, uploader: str) -> str:
    title = text_of(find_first(card, "post-title")) or "(no title)"
    date = text_of(find_first(card, "post-published-at"))
    url = post_url(card) or ""
    streams = stream_urls(card)
    length = video_length(card)

    lines: list[str] = [f"## {idx}. {title}"]
    meta: list[str] = []
    if date:
        meta.append(date)
    if length:
        meta.append(f"⏱ {length}")
    if url:
        meta.append(f"[post]({url})")
    if meta:
        lines.append(" · ".join(meta))
    if streams:
        lines.append("")
        for stream in streams:
            lines.extend(url_block(stream, title, uploader, url))
    lines.append("")
    return "\n".join(lines)


def load_seen(path: Path) -> set[str]:
    """Read a list of canonical Patreon post URLs to skip during inventory."""
    if not path.exists():
        return set()
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        seen.add(line)
    return seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mhtml",
        nargs="?",
        default=None,
        help="path to .mhtml snapshot; if omitted, the newest *.mhtml under /data/mhtml/ is used",
    )
    parser.add_argument(
        "--seen-file",
        type=Path,
        default=None,
        help="Path to a file of canonical Patreon post URLs (one per line, "
        "'#' comments OK). Posts whose URL appears here are skipped — "
        "lets `just sync` produce a diff instead of the full inventory.",
    )
    parser.add_argument(
        "--minimal",
        action="store_true",
        help="Emit only the metadata + URL lines that download.sh consumes "
        "(no markdown headers, no code fences). Suitable for piping "
        "straight into urls/urls.txt.",
    )
    args = parser.parse_args()
    mhtml: Path = Path(args.mhtml) if args.mhtml else latest_mhtml()

    html = read_html_from_mhtml(mhtml)
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.find_all(attrs={"data-tag": "post-card"})

    if not cards:
        print(f"no post-card elements found in {mhtml}", file=sys.stderr)
        return 1

    title_node = soup.find("title")
    page_title = text_of(title_node) if title_node else "Patreon snapshot"
    uploader = page_uploader(soup)

    seen: set[str] = load_seen(args.seen_file) if args.seen_file else set()
    selected = [c for c in cards if (post_url(c) or "") not in seen]

    # Surface the MHTML's video-post date range on stderr. The sync recipe
    # parses this line to compare the oldest visible date against the
    # persisted coverage floor in `urls/coverage.txt` — that's where the
    # actual gap-vs-no-gap decision lives. Inventory itself stays opinion-
    # free; it just reports what's in the snapshot.
    all_video_dates: list[str] = []
    for card in cards:
        for stream in stream_urls(card):
            m = STREAM_URL_RE.match(stream)
            if m:
                d = m.group("date")
                iso = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
                # The regex only verifies *shape* (\d{8}), not validity.
                # `99999999` or `20260230` (Feb 30) would slip through
                # and break sync's string-compare gap detection. Drop
                # anything that doesn't parse as a real ISO date.
                try:
                    datetime.date.fromisoformat(iso)
                except ValueError:
                    continue
                all_video_dates.append(iso)
    if all_video_dates:
        print(
            f"[inventory] mhtml_date_range: {min(all_video_dates)} .. "
            f"{max(all_video_dates)} ({len(all_video_dates)} video posts)",
            file=sys.stderr,
        )

    if args.minimal:
        for card in selected:
            title = text_of(find_first(card, "post-title")) or "(no title)"
            purl = post_url(card) or ""
            for stream in stream_urls(card):
                print("\n".join(meta_block(stream, title, uploader, purl)))
                print()
        return 0

    print(f"# {page_title}")
    print()
    skipped = len(cards) - len(selected)
    if args.seen_file:
        print(
            f"{len(selected)} new post(s) ({skipped} already in "
            f"`{args.seen_file.name}`) from `{mhtml.name}`."
        )
    else:
        print(f"{len(cards)} posts captured from `{mhtml.name}`.")
    print()
    print("---")
    print()
    for index, card in enumerate(selected, 1):
        print(render_post(index, card, uploader))
    return 0


if __name__ == "__main__":
    sys.exit(main())
