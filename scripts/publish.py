"""Atomic publishing helpers — keep partial files out of the user-visible /data.

Every yt-dlp invocation writes its in-progress files to a private staging
directory under :data:`STAGING_ROOT` (an in-container, non-bind-mounted path).
Once yt-dlp has finished merging / remuxing / embedding, ``download.py`` calls
:func:`publish_outputs` to atomically promote the resulting mp4(s) into
``/data`` and append the corresponding archive entries — both via
*tmp + os.replace* so the user-facing path either contains a complete file
or contains nothing at all.

Cross-filesystem note: ``/var/lib/pa/staging`` is on the container rootfs
while ``/data`` is a bind mount, so a plain ``os.rename`` would raise
``EXDEV``. :func:`atomic_publish` does ``copy2 → os.replace`` *within* the
destination filesystem so the visible rename step is always atomic.
"""

from __future__ import annotations

import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

DATA_DIR: Path = Path("/data")
STAGING_ROOT: Path = Path("/var/lib/pa/staging")
ARCHIVE_FILE: Path = DATA_DIR / "archive.txt"
MANIFEST_NAME: str = "manifest.tsv"
SKIP_ARCHIVE_NAME: str = "skip.txt"
PUBLISH_TMP_GLOB: str = ".pa-publish.*.tmp"


@dataclass(frozen=True)
class StagingRun:
    """Per-yt-dlp-invocation private workspace."""

    home: Path
    manifest: Path
    skip_archive: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)


@dataclass(frozen=True)
class PublishedItem:
    """A single mp4 successfully promoted from staging into ``/data``."""

    extractor: str
    video_id: str
    final_path: Path

    @property
    def archive_line(self) -> str:
        return f"{self.extractor.lower()} {self.video_id}"


def prepare_staging(root: Path | None = None) -> StagingRun:
    """Create a fresh, unique staging dir for one yt-dlp invocation.

    The token isolates concurrent or sequential runs from each other so a
    crashed run can never accidentally publish files belonging to a later
    run. ``root`` is read from :data:`STAGING_ROOT` at call time so tests
    can ``monkeypatch.setattr(publish, "STAGING_ROOT", ...)`` reliably.
    """
    target = root if root is not None else STAGING_ROOT
    target.mkdir(parents=True, exist_ok=True)
    home = target / f"run-{secrets.token_hex(8)}"
    home.mkdir(parents=True, exist_ok=False)
    return StagingRun(
        home=home,
        manifest=home / MANIFEST_NAME,
        skip_archive=home / SKIP_ARCHIVE_NAME,
    )


def seed_skip_archive(run: StagingRun, source: Path | None = None) -> None:
    """Copy the canonical archive into staging so yt-dlp can dedup against it.

    yt-dlp itself only sees this private copy. Whatever yt-dlp appends to
    it during the run is discarded with the staging dir; the authoritative
    archive in ``/data`` is updated separately by :func:`append_archive`
    after each successful publish.
    """
    src = source if source is not None else ARCHIVE_FILE
    if src.exists():
        shutil.copy2(src, run.skip_archive)
    else:
        run.skip_archive.touch()


def parse_manifest(run: StagingRun) -> list[tuple[str, str, Path]]:
    """Return ``(extractor, video_id, staging_path)`` rows from yt-dlp's manifest.

    yt-dlp emits one line per ``after_move`` event via
    ``--print-to-file`` — see ``download.py`` for the template.
    """
    if not run.manifest.exists():
        return []
    rows: list[tuple[str, str, Path]] = []
    for raw in run.manifest.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) != 3:
            continue
        extractor, video_id, filepath = parts
        rows.append((extractor, video_id, Path(filepath)))
    return rows


def atomic_publish(src: Path, dst: Path) -> None:
    """Promote *src* to *dst* such that *dst* is either complete or absent.

    Steps:

    1. Ensure ``dst.parent`` exists.
    2. Copy *src* to a hidden tmp inside ``dst.parent`` (same filesystem).
    3. ``os.replace`` the tmp onto *dst* — atomic POSIX rename.
    4. On any failure during the copy, remove the tmp before re-raising so
       no partial file is left in the user-facing tree.

    The hidden ``.pa-publish.<token>.tmp`` name keeps in-flight bytes out
    of a default ``ls`` and avoids colliding with concurrent publishes.

    Uses ``shutil.copyfile`` rather than ``copy2``: yt-dlp's
    ``--no-mtime`` already strips upload timestamps, and the ``copystat``
    half of ``copy2`` (utime + chmod) is rejected with ``EPERM`` when
    ``/data`` is a Windows-NTFS bind mount whose DrvFs metadata mode does
    not authorise the container user to set those attributes.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".pa-publish.{secrets.token_hex(8)}.tmp"
    try:
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def append_archive(lines: list[str], path: Path | None = None) -> None:
    """Atomically append *lines* to the archive at *path*.

    Read-modify-rename is overkill for one short append, but it gives a
    full atomicity guarantee even across crashes / OOM kills: either the
    archive contains all prior entries, or it contains all prior entries
    plus all new ones — never a half-written intermediate state.

    .. note:: **Single-writer assumption.**
       The expected ``pa`` workflow runs at most one container at a time,
       so this function does *not* take a file lock around the read /
       modify / replace. Two concurrent ``pa download`` invocations
       against the same ``data/`` would race: each loads a snapshot of
       ``archive.txt``, adds its own line, and the *later* replace wins —
       silently dropping the earlier append. If you ever need parallel
       writers, this is the function that needs ``fcntl.flock`` (or an
       equivalent advisory lock) on a sibling lockfile.
    """
    if not lines:
        return
    target = path if path is not None else ARCHIVE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    body = existing + "".join(line + "\n" for line in lines)
    tmp = target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
    try:
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def sweep_publish_tmps(root: Path | None = None) -> None:
    """Best-effort cleanup of stale ``.pa-publish.*.tmp`` files in /data.

    A previous run that died mid-copy may have left orphan tmp files. They
    are hidden by the leading dot, but sweeping at the start of each batch
    keeps the tree tidy and prevents unbounded leakage.
    """
    target = root if root is not None else DATA_DIR
    if not target.is_dir():
        return
    for stale in target.rglob(PUBLISH_TMP_GLOB):
        if stale.is_file():
            stale.unlink(missing_ok=True)


def publish_outputs(run: StagingRun, dst_root: Path | None = None) -> list[PublishedItem]:
    """Move every file in the staging manifest into the destination atomically.

    Per row: relocate the staging mp4 to a final ``<dst_root>/<rel-path>``
    that mirrors its position under ``run.home``, then return the
    :class:`PublishedItem` so the caller can persist archive entries only
    after the file is actually at its visible destination.

    *dst_root* defaults to :data:`DATA_DIR`. ``--retest`` flows in
    ``download.py`` override it to a sandboxed ``/data/.retest/<ts>/``
    directory so a forced re-download cannot clobber the canonical tree.
    """
    target = dst_root if dst_root is not None else DATA_DIR
    items: list[PublishedItem] = []
    for extractor, video_id, staging_path in parse_manifest(run):
        rel = staging_path.relative_to(run.home)
        final = target / rel
        atomic_publish(staging_path, final)
        items.append(PublishedItem(extractor=extractor, video_id=video_id, final_path=final))
    return items
