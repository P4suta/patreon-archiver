"""End-to-end integration tests against the built ``patreon-archiver:local`` image.

These tests reproduce the offline shakedown that used to be done by hand
after every wrapper / compose / scripts change:

* ``smoke`` recipe passes inside the container.
* ``version`` prints something that looks like a yt-dlp release tag.
* default recipe lists every recipe we expect ``pa`` to expose.
* ``sync-dry`` against a fixture MHTML reports the right URLs **and**
  leaves the bind-mounted ``/data`` completely empty (the dry-run
  must not touch ``seen_posts.txt`` / ``coverage.txt`` / ``urls.txt``).
* ``sync-dry`` is idempotent — back-to-back invocations yield identical
  output and identical (empty) state.
* Pre-seeding ``seen_posts.txt`` removes exactly the seeded post from
  the would-download list.
* When multiple ``*.mhtml`` files coexist in ``/data/mhtml/``, the
  newest mtime wins (Windows double-click workflow relies on that).
* ``inventory`` produces full Markdown sections; ``inventory --minimal``
  produces only the meta + URL blocks.

No network: every test stays inside the locally bind-mounted /data and
calls only recipes whose code paths do not reach Cloudflare / Patreon
(``simulate`` / ``retest`` / ``download`` / ``sync`` / ``batch`` /
``resolve`` are explicitly skipped).

The whole module is auto-skipped when:

* ``docker`` (or ``docker.exe`` on Windows) is missing from PATH, or
* the docker daemon is not reachable, or
* ``patreon-archiver:local`` is not built yet.

So a fresh clone can run ``uv run pytest`` and just see ``s``-skips
until ``pa.cmd build`` produces the image; once it's there the same
``pytest`` invocation starts running these checks alongside the unit
tests.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from conftest import FakePost, write_mhtml

if TYPE_CHECKING:
    from collections.abc import Iterator

IMAGE_TAG = "patreon-archiver:local"
EXPECTED_RECIPES: tuple[str, ...] = (
    "sync",
    "sync-dry",
    "simulate",
    "retest",
    "inventory",
    "batch",
    "download",
    "resolve",
    "fast",
    "fast-batch",
    "fast-sync",
    "smoke",
    "version",
    "shell",
)


def _docker_cmd() -> str:
    return "docker.exe" if os.name == "nt" else "docker"


def _docker_available() -> tuple[bool, str]:
    """Return ``(available, reason)``; reason is empty on success."""
    docker = _docker_cmd()
    if shutil.which(docker) is None:
        return False, f"{docker} not on PATH"
    try:
        info = subprocess.run(
            [docker, "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"docker info failed: {exc}"
    if info.returncode != 0:
        return False, "docker daemon not reachable"
    try:
        img = subprocess.run(
            [docker, "image", "inspect", IMAGE_TAG],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"image inspect failed: {exc}"
    if img.returncode != 0:
        return False, f"image {IMAGE_TAG} not built (run `pa.cmd build`)"
    return True, ""


_AVAILABLE, _SKIP_REASON = _docker_available()

pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=_SKIP_REASON)


def _run_pa(data_dir: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Invoke ``patreon-archiver:local <recipe> [args]`` against *data_dir*.

    Bypasses ``compose.yaml`` so each test gets its own isolated /data
    bind without ever touching the user's real save tree.
    """
    docker = _docker_cmd()
    # Windows native docker.exe wants Windows-shaped paths for -v.
    bind_src = (
        subprocess.check_output(["wslpath", "-w", str(data_dir)], text=True).strip()
        if os.name != "nt"
        and Path("/proc/version").exists()
        and "microsoft" in Path("/proc/version").read_text().lower()
        and str(data_dir).startswith("/mnt/")
        else str(data_dir)
    )
    return subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "-v",
            f"{bind_src}:/data",
            "-e",
            "JUST_JUSTFILE=/work/justfile",
            "-w",
            "/data",
            IMAGE_TAG,
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


@pytest.fixture
def integration_data_dir(tmp_path: Path) -> Iterator[Path]:
    """Spawn a fresh ``/data``-shaped tempdir with an empty ``mhtml/`` subdir.

    The dir is made world-writable so the container's ``app`` user (uid
    10001) can touch state files even when the host's tempdir is owned by
    a different uid (typical on Linux/WSL ext4 — on Windows + Docker
    Desktop NTFS the permission shim makes this a no-op).
    """
    sd = tmp_path / "data"
    sd.mkdir()
    sd.chmod(0o777)
    mhtml = sd / "mhtml"
    mhtml.mkdir()
    mhtml.chmod(0o777)
    yield sd


def _seed_mhtml(data_dir: Path, posts: list[FakePost], name: str = "snap.mhtml") -> Path:
    return write_mhtml(data_dir / "mhtml" / name, posts)


def _two_video_posts() -> list[FakePost]:
    return [
        FakePost(
            title="Newest video",
            post_path="/posts/newest-1",
            date_yyyymmdd="20260423",
            slug="newest1",
        ),
        FakePost(
            title="Older video",
            post_path="/posts/older-2",
            date_yyyymmdd="20260415",
            slug="older2",
        ),
    ]


# ============================================================
# Container-internal sanity (no /data bind interaction needed)
# ============================================================


class TestContainerBaseline:
    def test_smoke_recipe(self, integration_data_dir: Path) -> None:
        result = _run_pa(integration_data_dir, "smoke")
        assert result.returncode == 0, result.stderr
        assert "smoke OK" in result.stdout

    def test_version_recipe(self, integration_data_dir: Path) -> None:
        result = _run_pa(integration_data_dir, "version")
        assert result.returncode == 0
        last = result.stdout.strip().splitlines()[-1]
        assert re.match(r"^\d{4}\.\d{2}\.\d{2}", last), f"unexpected version output: {last!r}"

    def test_default_recipe_lists_all_expected(self, integration_data_dir: Path) -> None:
        result = _run_pa(integration_data_dir)
        assert result.returncode == 0
        for r in EXPECTED_RECIPES:
            assert r in result.stdout, f"recipe {r!r} missing from `just --list` output"


# ============================================================
# sync-dry: state-free dry-run
# ============================================================


class TestSyncDry:
    def test_with_two_posts_reports_two(self, integration_data_dir: Path) -> None:
        _seed_mhtml(integration_data_dir, _two_video_posts())
        result = _run_pa(integration_data_dir, "sync-dry")
        assert result.returncode == 0, result.stderr
        assert "would download 2 new post(s)" in result.stdout
        assert "https://www.patreon.com/posts/newest-1" in result.stdout
        assert "https://www.patreon.com/posts/older-2" in result.stdout
        assert "no state changed" in result.stdout

    def test_leaves_data_dir_empty(self, integration_data_dir: Path) -> None:
        _seed_mhtml(integration_data_dir, _two_video_posts())
        _run_pa(integration_data_dir, "sync-dry")
        # Only the mhtml/ subdir we put in should remain.
        leftovers = sorted(p.name for p in integration_data_dir.iterdir())
        assert leftovers == ["mhtml"]
        # And mhtml/ contains exactly what we put in.
        assert sorted(p.name for p in (integration_data_dir / "mhtml").iterdir()) == ["snap.mhtml"]

    def test_idempotent_back_to_back(self, integration_data_dir: Path) -> None:
        _seed_mhtml(integration_data_dir, _two_video_posts())
        first = _run_pa(integration_data_dir, "sync-dry").stdout
        second = _run_pa(integration_data_dir, "sync-dry").stdout
        assert first == second
        # State still empty.
        assert sorted(p.name for p in integration_data_dir.iterdir()) == ["mhtml"]

    def test_seen_posts_seed_filters_out_one(self, integration_data_dir: Path) -> None:
        _seed_mhtml(integration_data_dir, _two_video_posts())
        # Pre-seed the canonical Patreon URL of the newer post.
        (integration_data_dir / "seen_posts.txt").write_text(
            "https://www.patreon.com/posts/newest-1\n",
        )
        result = _run_pa(integration_data_dir, "sync-dry")
        assert result.returncode == 0, result.stderr
        assert "would download 1 new post(s)" in result.stdout
        assert "https://www.patreon.com/posts/older-2" in result.stdout
        assert "https://www.patreon.com/posts/newest-1" not in result.stdout

    def test_picks_newest_mhtml_by_mtime(self, integration_data_dir: Path) -> None:
        # Newer file: 2 posts. Older file (different content): 1 post.
        # Auto-pick must use the newer one and report 2 posts.
        old = write_mhtml(
            integration_data_dir / "mhtml" / "old.mhtml",
            [
                FakePost(
                    title="Old only",
                    post_path="/posts/old-only",
                    date_yyyymmdd="20250101",
                    slug="oldonly",
                ),
            ],
        )
        new = _seed_mhtml(integration_data_dir, _two_video_posts(), name="new.mhtml")
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        result = _run_pa(integration_data_dir, "sync-dry")
        assert result.returncode == 0, result.stderr
        assert "would download 2 new post(s)" in result.stdout
        # The "Old only" URL must NOT appear because the newer mhtml was picked.
        assert "/posts/old-only" not in result.stdout


# ============================================================
# inventory: pure local MHTML → markdown / minimal
# ============================================================


class TestInventory:
    def test_full_markdown_has_section_headers(self, integration_data_dir: Path) -> None:
        _seed_mhtml(integration_data_dir, _two_video_posts())
        result = _run_pa(integration_data_dir, "inventory")
        assert result.returncode == 0, result.stderr
        assert "## 1. Newest video" in result.stdout
        assert "## 2. Older video" in result.stdout
        # Fenced code blocks present.
        assert "```text" in result.stdout
        assert result.stdout.count("```") >= 4  # 2 posts: open + close each
        # mhtml_date_range goes to stderr, not stdout.
        assert "mhtml_date_range" in result.stderr

    def test_minimal_emits_only_meta_blocks(self, integration_data_dir: Path) -> None:
        _seed_mhtml(integration_data_dir, _two_video_posts())
        result = _run_pa(integration_data_dir, "inventory", "--minimal")
        assert result.returncode == 0, result.stderr
        assert "## " not in result.stdout  # no markdown section headers
        assert "```" not in result.stdout  # no code fences
        assert "# title: Newest video" in result.stdout
        assert "# title: Older video" in result.stdout
        assert "# date: 2026-04-23" in result.stdout
        assert "# date: 2026-04-15" in result.stdout

    def test_inventory_does_not_mutate_data_dir(self, integration_data_dir: Path) -> None:
        _seed_mhtml(integration_data_dir, _two_video_posts())
        _run_pa(integration_data_dir, "inventory")
        # Only mhtml/ should remain — inventory must be read-only.
        assert sorted(p.name for p in integration_data_dir.iterdir()) == ["mhtml"]
