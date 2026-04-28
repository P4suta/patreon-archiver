#!/usr/bin/env python3
"""Diff an MHTML against on-disk state, batch-download the new posts, advance state.

End-to-end ``pa sync`` flow:

1. Run ``inventory.py --seen-file /data/seen_posts.txt --minimal`` to
   produce a urls.txt body containing only posts not yet handled.
2. Read the MHTML's video-post date range out of inventory's stderr and
   compare it against the persisted "coverage anchor" in
   ``/data/coverage.txt`` to decide whether a gap exists.
3. If there are new posts, hand the urls.txt to ``download.py
   --batch-file`` and append the successfully-handled post URLs to the
   seen-set.
4. Advance the coverage anchor forward when the MHTML reaches back into
   prior coverage; hold it in place when it doesn't (gap pending).
"""

from __future__ import annotations

import argparse
import re
import secrets
import subprocess
import sys
from pathlib import Path

from _mhtml import latest_mhtml

DATA_DIR = Path("/data")
SCRIPTS_DIR = Path("/work/scripts")
SEEN_FILE = DATA_DIR / "seen_posts.txt"
COVERAGE_FILE = DATA_DIR / "coverage.txt"
URLS_FILE = DATA_DIR / "urls.txt"

DATE_RANGE_RE: re.Pattern[str] = re.compile(
    r"\[inventory\] mhtml_date_range: (?P<oldest>[0-9-]+) \.\. (?P<newest>[0-9-]+)"
)
URL_RE: re.Pattern[str] = re.compile(r"^https?://", re.MULTILINE)
POST_LINE_RE: re.Pattern[str] = re.compile(r"^# post:\s*(?P<url>\S+)", re.MULTILINE)


def _ensure_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.touch(exist_ok=True)


def _atomic_write_text(path: Path, body: str) -> None:
    """tmp + os.replace: the visible *path* is either the prior content or *body*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def run_inventory(mhtml: Path) -> tuple[str, str]:
    """Return ``(stdout, stderr)`` of an inventory run filtered against the seen-set."""
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "inventory.py"),
            str(mhtml),
            "--seen-file",
            str(SEEN_FILE),
            "--minimal",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()
    return proc.stdout, proc.stderr


def parse_date_range(stderr_text: str) -> tuple[str | None, str | None]:
    match = DATE_RANGE_RE.search(stderr_text)
    if not match:
        return None, None
    return match.group("oldest"), match.group("newest")


def read_anchor() -> str | None:
    if not COVERAGE_FILE.exists():
        return None
    text = COVERAGE_FILE.read_text(encoding="utf-8").strip()
    return text or None


def write_anchor(anchor: str | None) -> None:
    if anchor:
        _atomic_write_text(COVERAGE_FILE, anchor + "\n")


def evaluate_anchor(
    prev: str | None, oldest: str | None, newest: str | None
) -> tuple[str | None, str | None]:
    """Return ``(new_anchor, gap_message)``.

    * No gap, anchor advances if MHTML's newest extends past it.
    * Gap pending if MHTML's oldest is *strictly newer* than the prior
      anchor — i.e. the snapshot doesn't reach back to bridge the
      already-known continuous-coverage range.
    """
    if oldest is None or newest is None:
        return prev, None
    if prev is None:
        return newest, None
    if oldest <= prev:
        return (max(prev, newest)), None
    gap = (
        f"gap pending — dates ({prev}, {oldest}) may have un-handled "
        f"posts. Visible-page diff is being downloaded; the system keeps "
        f"the gap pending until a future MHTML reaches back to {prev} or "
        f"earlier."
    )
    return prev, gap


def run_batch_download(urls_file: Path) -> int:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "download.py"),
            "--batch-file",
            str(urls_file),
        ],
        check=False,
    ).returncode


def append_seen(post_urls: list[str]) -> int:
    existing: set[str] = set()
    if SEEN_FILE.exists():
        existing = set(SEEN_FILE.read_text(encoding="utf-8").splitlines())
    merged = sorted(existing | set(post_urls))
    _atomic_write_text(SEEN_FILE, "\n".join(merged) + "\n")
    return len(merged)


def _report_dry_run(
    new_post_urls: list[str],
    has_urls: bool,
    prev_anchor: str | None,
    new_anchor: str | None,
    gap_msg: str | None,
) -> None:
    """Emit what *would* happen without touching state.

    Pure read-only: no inventory output written to /data/urls.txt, no
    yt-dlp invocation, no seen_posts.txt or coverage.txt update. Re-runnable
    indefinitely and rolls back to nothing.
    """
    tag = "[sync --dry-run]"
    if not has_urls:
        print(f"{tag} no new posts since last sync.")
    else:
        print(f"{tag} would download {len(new_post_urls)} new post(s):")
        for url in new_post_urls:
            print(f"  - {url}")
    if new_anchor and new_anchor != prev_anchor:
        if prev_anchor is None:
            print(f"{tag} would initialize coverage anchor at {new_anchor}.")
        else:
            print(f"{tag} would advance coverage anchor: {prev_anchor} -> {new_anchor}.")
    if gap_msg:
        print(f"{tag} {gap_msg}")
    print(f"{tag} no state changed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mhtml",
        nargs="?",
        default=None,
        help="path to .mhtml snapshot; if omitted, the newest *.mhtml under "
        "/data/mhtml/ is used (double-click workflow)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen without changing any state. yt-dlp is "
        "not invoked; seen_posts.txt / coverage.txt / urls.txt are left "
        "untouched. Re-runnable any number of times.",
    )
    args = parser.parse_args(argv)
    mhtml = Path(args.mhtml) if args.mhtml else latest_mhtml()

    if not args.dry_run:
        _ensure_state()

    inventory_stdout, inventory_stderr = run_inventory(mhtml)

    oldest, newest = parse_date_range(inventory_stderr)
    prev_anchor = read_anchor()
    new_anchor, gap_msg = evaluate_anchor(prev_anchor, oldest, newest)
    new_post_urls = POST_LINE_RE.findall(inventory_stdout)
    has_urls = URL_RE.search(inventory_stdout) is not None

    if args.dry_run:
        _report_dry_run(new_post_urls, has_urls, prev_anchor, new_anchor, gap_msg)
        return 0

    _atomic_write_text(URLS_FILE, inventory_stdout)

    if new_anchor and new_anchor != prev_anchor:
        if prev_anchor is None:
            print(f"[sync] coverage anchor initialized at {new_anchor} (first sync).")
        else:
            print(f"[sync] coverage anchor advanced: {prev_anchor} -> {new_anchor}.")

    if not has_urls:
        print("[sync] no new posts since last run.")
        URLS_FILE.unlink(missing_ok=True)
    else:
        print(f"[sync] {len(new_post_urls)} new post(s) queued; running batch...")
        rc = run_batch_download(URLS_FILE)
        if rc != 0:
            print(f"[sync] batch exited with rc={rc}; not advancing seen-set.", file=sys.stderr)
            return rc
        total = append_seen(new_post_urls)
        print(f"[sync] {len(new_post_urls)} post(s) marked as seen ({total} total).")

    write_anchor(new_anchor)
    if gap_msg:
        print()
        print(f"[sync] {gap_msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
