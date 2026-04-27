#!/usr/bin/env python3
"""Diff an MHTML against on-disk state, batch-download the new posts, advance state.

End-to-end ``pa sync`` flow:

1. Run ``inventory.py --seen-file /state/seen_posts.txt --minimal`` to
   produce a urls.txt body containing only posts not yet handled.
2. Read the MHTML's video-post date range out of inventory's stderr and
   compare it against the persisted "coverage anchor" in
   ``/state/coverage.txt`` to decide whether a gap exists.
3. If there are new posts, hand the urls.txt to ``download.py
   --batch-file`` and append the successfully-handled post URLs to the
   seen-set.
4. Advance the coverage anchor forward when the MHTML reaches back into
   prior coverage; hold it in place when it doesn't (gap pending).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

STATE_DIR = Path("/state")
SCRIPTS_DIR = Path("/work/scripts")
SEEN_FILE = STATE_DIR / "seen_posts.txt"
COVERAGE_FILE = STATE_DIR / "coverage.txt"
URLS_FILE = STATE_DIR / "urls.txt"

DATE_RANGE_RE: re.Pattern[str] = re.compile(
    r"\[inventory\] mhtml_date_range: (?P<oldest>[0-9-]+) \.\. (?P<newest>[0-9-]+)"
)
URL_RE: re.Pattern[str] = re.compile(r"^https?://", re.MULTILINE)
POST_LINE_RE: re.Pattern[str] = re.compile(r"^# post:\s*(?P<url>\S+)", re.MULTILINE)


def _ensure_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.touch(exist_ok=True)


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
        COVERAGE_FILE.write_text(anchor + "\n", encoding="utf-8")


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
    SEEN_FILE.write_text("\n".join(merged) + "\n", encoding="utf-8")
    return len(merged)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mhtml", type=Path, help="path to .mhtml snapshot")
    args = parser.parse_args(argv)

    _ensure_state()

    inventory_stdout, inventory_stderr = run_inventory(args.mhtml)
    URLS_FILE.write_text(inventory_stdout, encoding="utf-8")

    oldest, newest = parse_date_range(inventory_stderr)
    prev_anchor = read_anchor()
    new_anchor, gap_msg = evaluate_anchor(prev_anchor, oldest, newest)

    if new_anchor and new_anchor != prev_anchor:
        if prev_anchor is None:
            print(f"[sync] coverage anchor initialized at {new_anchor} (first sync).")
        else:
            print(f"[sync] coverage anchor advanced: {prev_anchor} -> {new_anchor}.")

    new_post_urls = POST_LINE_RE.findall(URLS_FILE.read_text(encoding="utf-8"))
    has_urls = URL_RE.search(URLS_FILE.read_text(encoding="utf-8")) is not None

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
