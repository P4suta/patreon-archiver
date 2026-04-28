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
import datetime
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import publish
from resolve import ResolveError, resolve

if TYPE_CHECKING:
    from collections.abc import Iterator

RETEST_ROOT_NAME: str = ".retest"

CONFIG_PATH = Path("/work/config/yt-dlp.conf")
# yt-dlp's --print-to-file template emits one TSV row per `after_move`
# event so the wrapper can locate freshly produced files. Tabs are safer
# than spaces because mp4 paths can legitimately contain spaces.
MANIFEST_TEMPLATE: str = "after_move:%(extractor_key)s\t%(id)s\t%(filepath)s"
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
    """Yield one ``UrlBlock`` per URL line, attaching any preceding metadata comments.

    ``utf-8-sig`` strips a BOM if present — Windows users who edit
    ``urls.txt`` in older versions of Notepad can end up with one, and a
    leading U+FEFF would silently break the first line's ``#`` / ``http``
    detection.
    """
    current_meta: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
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
    staging: publish.StagingRun,
) -> list[str]:
    """Assemble the yt-dlp argv for one URL.

    Per-invocation flags pin yt-dlp's outputs to the private staging dir
    (``--paths home:``), give it a private archive copy to dedup against
    (``--download-archive``), and have it emit a machine-readable
    after-move manifest (``--print-to-file``) for the publish step.
    """
    args: list[str] = [
        "--config-location",
        str(CONFIG_PATH),
        "--paths",
        f"home:{staging.home}",
        "--download-archive",
        str(staging.skip_archive),
        "--print-to-file",
        MANIFEST_TEMPLATE,
        str(staging.manifest),
    ]
    if cookies:
        args += ["--cookies", cookies]
    args += list(extra_flags)
    merged_meta = {**derive_defaults(block.url), **block.meta}
    for key in META_KEYS:
        value = merged_meta.get(key)
        if value:
            args += emit_meta_flags(key, value)
    return args


def run_one(
    block: UrlBlock,
    extra_flags: list[str],
    cookies: str | None,
    *,
    retest_root: Path | None = None,
) -> int:
    """Download one URL transactionally — staged then atomically published.

    yt-dlp writes every byte (fragments, the merged mp4, embedded
    thumbnail intermediates) inside the per-run staging dir. Only after
    yt-dlp returns 0 does :func:`publish.publish_outputs` atomic-rename
    the finished mp4 into ``/data`` and append the archive entry.

    Failure modes:
      * yt-dlp exits non-zero  → staging cleaned, /data untouched.
      * publish fails mid-copy → tmp removed, /data has no partial; rc=1.
      * archive append fails   → file already in /data (atomic), but
        archive lacks the line so the next run will redownload — we
        still return non-zero so the caller knows state is incomplete.

    When *retest_root* is provided the archive is bypassed (yt-dlp's
    ``--download-archive`` sees an empty file → no URL is ever skipped),
    the published mp4 lands under *retest_root* instead of
    :data:`publish.DATA_DIR`, and ``archive.txt`` is left untouched.
    """
    retest = retest_root is not None
    try:
        resolved = resolve(block.url)
    except ResolveError as exc:
        # A single URL failing to resolve must not bring down a whole batch.
        # Print, return non-zero, let the caller move on to the next block.
        print(
            f"[resolve] failed for {block.url}: {exc}; skipping.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    staging = publish.prepare_staging()
    try:
        if retest:
            # Empty archive seed → yt-dlp can't skip anything as "already done".
            staging.skip_archive.touch()
        else:
            publish.seed_skip_archive(staging)
        args = build_yt_dlp_args(block, extra_flags, cookies, staging)
        rc = subprocess.run(["yt-dlp", *args, resolved], check=False).returncode
        if rc != 0:
            return rc
        try:
            published = publish.publish_outputs(staging, dst_root=retest_root)
            if not retest:
                publish.append_archive([item.archive_line for item in published])
        except (OSError, ValueError) as exc:
            # OSError covers disk-full / cross-fs / permission failures during
            # copy + replace. ValueError covers `Path.relative_to` blowing up
            # when yt-dlp wrote outside the staging home — typically because
            # the user passed an extra `--paths` flag that overrode our
            # `--paths home:` pin. In either case the canonical tree must
            # stay untouched and the batch must keep going.
            target_label = str(retest_root) if retest else "/data"
            print(
                f"[publish] failed for {block.url}: {exc}; {target_label} is untouched.",
                file=sys.stderr,
                flush=True,
            )
            return 1
        return 0
    finally:
        staging.cleanup()


def _sleep_seconds(min_s: int, max_s: int) -> int:
    """Pick a polite sleep delay in ``[min_s, max_s]``, saturated at 0.

    The result is clamped non-negative so that pathological env vars
    (``YTDLP_BATCH_SLEEP_MIN=-5``) cannot smuggle a negative argument into
    ``time.sleep``, which raises ``ValueError`` on POSIX and would tear
    down a long-running batch mid-flight.
    """
    if max_s <= min_s:
        return max(0, min_s)
    # secrets.randbelow gives a non-deterministic, non-PRNG-seeded delay —
    # nicer for "polite jitter" than random.randint() and silences ruff S311.
    return max(0, min_s + secrets.randbelow(max_s - min_s + 1))


def run_batch(
    path: Path,
    extra_flags: list[str],
    cookies: str | None,
    *,
    retest_root: Path | None = None,
) -> int:
    sleep_min = int(os.environ.get("YTDLP_BATCH_SLEEP_MIN", "5"))
    sleep_max = int(os.environ.get("YTDLP_BATCH_SLEEP_MAX", "15"))
    publish.sweep_publish_tmps()
    last_rc = 0
    for index, block in enumerate(parse_batch(path)):
        if index > 0:
            delay = _sleep_seconds(sleep_min, sleep_max)
            print(f"[batch] sleeping {delay}s before next URL", flush=True)
            time.sleep(delay)
        rc = run_one(block, extra_flags, cookies, retest_root=retest_root)
        if rc != 0:
            last_rc = rc
    return last_rc


def _new_retest_root(now: datetime.datetime | None = None) -> Path:
    """Return a fresh ``/data/.retest/<YYYYmmdd_HHMMSS>-<rand>/`` path."""
    moment = now if now is not None else datetime.datetime.now(tz=datetime.UTC)
    stamp = moment.strftime("%Y%m%d_%H%M%S")
    return publish.DATA_DIR / RETEST_ROOT_NAME / f"{stamp}-{secrets.token_hex(4)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="yt-dlp wrapper with per-URL metadata injection.",
        epilog="Unknown flags are forwarded verbatim to yt-dlp.",
    )
    parser.add_argument("--batch-file", type=Path, default=None)
    parser.add_argument(
        "--retest",
        action="store_true",
        help="real download, but bypass archive.txt (re-download even if "
        "already archived) and land output under /data/.retest/<ts>/ "
        "instead of the canonical tree. archive.txt stays unchanged. "
        "Wipe with `rm -rf .retest/` when done.",
    )
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

    retest_root: Path | None = None
    if parsed.retest:
        retest_root = _new_retest_root()
        print(f"[retest] output dir: {retest_root}", flush=True)

    if parsed.batch_file is not None:
        return run_batch(parsed.batch_file, extra_flags, cookies, retest_root=retest_root)
    if urls:
        publish.sweep_publish_tmps()
        last_rc = 0
        for url in urls:
            rc = run_one(UrlBlock(url=url), extra_flags, cookies, retest_root=retest_root)
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
