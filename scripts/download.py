#!/usr/bin/env python3
"""yt-dlp wrapper that injects per-URL metadata at download time.

The Cloudflare Stream extractor in yt-dlp returns null for ``uploader``,
``title``, ``upload_date`` and friends, so a naive download lands files as
``NA/NA_<hex>_[<hex>].mp4`` with the UID hex string baked into the mp4
``title`` tag. This wrapper fixes that by translating per-URL metadata
(supplied either via ``# key: value`` comment blocks in a ``--batch-file``
or derived from the publisher URL itself) into ``--parse-metadata`` flags
and invoking ``yt-dlp`` once per URL.

Recognised metadata keys (all optional):

* ``title``    — drives ``%(title)s`` in the output template and the mp4
  ``title`` tag.
* ``uploader`` — folder name and mp4 ``artist`` tag.
* ``date``     — ``YYYY-MM-DD``; becomes ``%(upload_date)s``
  (yt-dlp wants ``YYYYMMDD`` internally; the wrapper converts).
* ``post``     — canonical Patreon post URL; embedded as the mp4
  ``comment`` instead of the JWT iframe URL yt-dlp would otherwise use.

Unknown keys are silently ignored.

In batch mode the wrapper sleeps a random duration in
``[YTDLP_BATCH_SLEEP_MIN, YTDLP_BATCH_SLEEP_MAX]`` seconds between URLs.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from resolve import resolve

if TYPE_CHECKING:
    from collections.abc import Iterator

CONFIG_PATH = Path("/work/config/yt-dlp.conf")
PUBLISHER_URL_RE: re.Pattern[str] = re.compile(
    r"^https?://(?P<host>stream\.[^/]+)/"
    r"(?P<date>\d{8})_(?P<slug>[^_/]+)_[^/]+/?$"
)
META_KEYS: tuple[str, ...] = ("title", "uploader", "date", "post")


@dataclass(frozen=True)
class UrlBlock:
    """One URL plus the key/value metadata that preceded it in a batch file."""

    url: str
    meta: dict[str, str] = field(default_factory=lambda: {})


def derive_defaults(url: str) -> dict[str, str]:
    """Mine baseline metadata out of a publisher URL.

    Lets a single-shot ``download.py <URL>`` call still produce a
    halfway-decent filename without an inventory block to lean on.
    """
    match = PUBLISHER_URL_RE.match(url)
    if not match:
        return {}
    host = match.group("host")
    date_compact = match.group("date")
    slug = match.group("slug")
    handle = host.removeprefix("stream.").split(".", maxsplit=1)[0]
    return {
        "uploader": handle,
        "date": f"{date_compact[0:4]}-{date_compact[4:6]}-{date_compact[6:8]}",
        "title": slug,
        "post": f"https://{host}/{date_compact}_{slug}/",
    }


def _escape_colons(value: str) -> str:
    return value.replace(":", r"\:")


def emit_meta_flags(key: str, raw_value: str) -> list[str]:
    """Translate one ``key=value`` metadata pair into yt-dlp ``--parse-metadata`` args.

    Why the ``"= "`` sentinel prefix on both FROM and TO:

    yt-dlp's ``MetadataParserPP.field_to_template`` (see
    ``yt_dlp/postprocessor/metadataparser.py``) auto-wraps any pure-
    alphabetic FROM string (``[a-zA-Z_]+``) into ``%(<from>)s`` — i.e. it
    treats an unspaced word as a *field name*, not a literal. For values
    like a bare lowercase handle (no digits, no spaces) the injected
    metadata then resolves to the info-dict's ``NA`` default. Adding a
    non-alphabetic sentinel ("= ") to both sides defeats the auto-wrap;
    the matching prefix in the TO regex strips the sentinel before the
    named-group capture, so the captured value equals the original.
    """
    if not raw_value:
        return []
    if key == "date":
        literal = _escape_colons(raw_value.replace("-", ""))
        return ["--parse-metadata", f"= {literal}:= %(upload_date)s"]
    literal = _escape_colons(raw_value)
    if key == "title":
        # %(title)s drives the output template; meta_title becomes the mp4
        # \xa9nam tag via --embed-metadata.
        return [
            "--parse-metadata",
            f"= {literal}:= %(title)s",
            "--parse-metadata",
            f"= {literal}:= %(meta_title)s",
        ]
    if key == "uploader":
        return ["--parse-metadata", f"= {literal}:= %(uploader)s"]
    if key == "post":
        # ffmpeg "comment" + "purl" tags. Replaces the JWT iframe URL
        # yt-dlp would otherwise embed as the comment.
        return [
            "--parse-metadata",
            f"= {literal}:= %(meta_comment)s",
            "--parse-metadata",
            f"= {literal}:= %(meta_purl)s",
        ]
    return []


def parse_batch(path: Path) -> Iterator[UrlBlock]:
    """Yield one ``UrlBlock`` per URL line, attaching any preceding metadata comments."""
    current_meta: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            continue
        if line.startswith("#"):
            rest = line[1:].lstrip()
            if ":" in rest:
                key, _, value = rest.partition(":")
                current_meta[key.strip()] = value.strip()
            continue
        if line.startswith(("http://", "https://")):
            yield UrlBlock(url=line, meta=current_meta)
            current_meta = {}


def build_yt_dlp_args(
    block: UrlBlock,
    extra_flags: list[str],
    cookies: str | None,
) -> list[str]:
    args: list[str] = ["--config-location", str(CONFIG_PATH)]
    if cookies:
        args += ["--cookies", cookies]
    args += list(extra_flags)
    merged_meta = {**derive_defaults(block.url), **block.meta}
    for key in META_KEYS:
        value = merged_meta.get(key)
        if value:
            args += emit_meta_flags(key, value)
    return args


def run_one(block: UrlBlock, extra_flags: list[str], cookies: str | None) -> int:
    resolved = resolve(block.url)
    args = build_yt_dlp_args(block, extra_flags, cookies)
    return subprocess.run(["yt-dlp", *args, resolved], check=False).returncode


def _sleep_seconds(min_s: int, max_s: int) -> int:
    if max_s <= min_s:
        return min_s
    # secrets.randbelow gives a non-deterministic, non-PRNG-seeded delay —
    # nicer for "polite jitter" than random.randint() and silences ruff S311.
    return min_s + secrets.randbelow(max_s - min_s + 1)


def run_batch(path: Path, extra_flags: list[str], cookies: str | None) -> int:
    sleep_min = int(os.environ.get("YTDLP_BATCH_SLEEP_MIN", "5"))
    sleep_max = int(os.environ.get("YTDLP_BATCH_SLEEP_MAX", "15"))
    last_rc = 0
    for index, block in enumerate(parse_batch(path)):
        if index > 0:
            delay = _sleep_seconds(sleep_min, sleep_max)
            print(f"[batch] sleeping {delay}s before next URL", flush=True)
            time.sleep(delay)
        rc = run_one(block, extra_flags, cookies)
        if rc != 0:
            last_rc = rc
    return last_rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="yt-dlp wrapper with per-URL metadata injection.",
        epilog="Unknown flags are forwarded verbatim to yt-dlp.",
    )
    parser.add_argument("--batch-file", type=Path, default=None)
    parsed, extras = parser.parse_known_args(argv)

    cookies_env = os.environ.get("YTDLP_COOKIES", "")
    cookies = cookies_env if cookies_env and Path(cookies_env).is_file() else None

    extra_flags: list[str] = []
    urls: list[str] = []
    for arg in extras:
        if arg.startswith(("http://", "https://")):
            urls.append(arg)
        else:
            extra_flags.append(arg)

    if parsed.batch_file is not None:
        return run_batch(parsed.batch_file, extra_flags, cookies)
    if urls:
        last_rc = 0
        for url in urls:
            rc = run_one(UrlBlock(url=url), extra_flags, cookies)
            if rc != 0:
                last_rc = rc
        return last_rc
    print(
        "usage: download.py [yt-dlp flags] <URL>... | --batch-file <path>",
        file=sys.stderr,
    )
    return 64


if __name__ == "__main__":
    sys.exit(main())
