"""Auto-detect the MHTML snapshot to use.

Both ``sync.py`` and ``inventory.py`` accept an optional MHTML path. If
omitted, both consult ``/data/mhtml/`` (= host's ``<repo>/data/mhtml/``)
and pick the newest ``*.mhtml`` by mtime — that's the convention the
Windows double-click workflow relies on (drop the latest snapshot into
``<repo>\\data\\mhtml\\`` and double-click ``pa.cmd`` — no argument
needed).
"""

from __future__ import annotations

from pathlib import Path

MHTML_DIR: Path = Path("/data/mhtml")


def latest_mhtml(mhtml_dir: Path | None = None) -> Path:
    """Return the newest ``*.mhtml`` (by mtime) in *mhtml_dir*.

    Raises ``SystemExit`` with a helpful message if the directory is
    missing or contains no MHTML files.

    Robustness contract:

    * **Case-insensitive extension match** — ``snap.MHTML`` /
      ``Snap.Mhtml`` count just as much as ``snap.mhtml``. Linux is
      case-sensitive at the FS layer, so a plain ``glob("*.mhtml")``
      would silently miss capitalised exports from Windows.
    * **Files only** — a directory misnamed ``backup.mhtml`` is ignored.
    * **Deterministic tie-break** — when two snapshots share an mtime
      (rare but possible after ``cp -p``), the lexicographically smallest
      filename wins, so re-running picks the same one every time.
    """
    target = mhtml_dir if mhtml_dir is not None else MHTML_DIR
    if not target.is_dir():
        raise SystemExit(
            f"no MHTML supplied and {target} does not exist; "
            f"create it and drop a *.mhtml file inside.",
        )
    # Stat each candidate once up front: the sort runs O(N log N) comparisons
    # so a per-comparison stat() call would multiply syscalls *and* opens a
    # window where a candidate can be deleted between two stats and crash
    # us with FileNotFoundError. Cached tuples sidestep both.
    cached: list[tuple[float, str, Path]] = []
    for p in target.iterdir():
        if not p.is_file() or p.suffix.lower() != ".mhtml":
            continue
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            # Disappeared between iterdir() and stat() — treat as if absent.
            continue
        cached.append((mtime, p.name, p))
    if not cached:
        raise SystemExit(f"no MHTML supplied and {target} contains no *.mhtml files.")
    # Sort by (-mtime, name): newest mtime first, lexicographic for ties.
    cached.sort(key=lambda row: (-row[0], row[1]))
    return cached[0][2]
