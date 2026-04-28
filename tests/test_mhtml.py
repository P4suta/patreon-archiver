"""Tests for ``scripts/_mhtml.py`` (MHTML auto-detection)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from _mhtml import MHTML_DIR, latest_mhtml


class TestLatestMhtml:
    def test_picks_newest_by_mtime(self, tmp_path: Path) -> None:
        d = tmp_path / "mhtml"
        d.mkdir()
        old = d / "old.mhtml"
        new = d / "new.mhtml"
        old.write_bytes(b"x")
        new.write_bytes(b"y")
        # Force old to be older than new regardless of FS mtime resolution.
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        assert latest_mhtml(d) == new

    def test_only_matches_mhtml_extension(self, tmp_path: Path) -> None:
        d = tmp_path / "mhtml"
        d.mkdir()
        (d / "a.html").write_bytes(b"x")
        target = d / "b.mhtml"
        target.write_bytes(b"y")
        assert latest_mhtml(d) == target

    def test_missing_directory_raises_systemexit(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="does not exist"):
            latest_mhtml(tmp_path / "nope")

    def test_empty_directory_raises_systemexit(self, tmp_path: Path) -> None:
        d = tmp_path / "mhtml"
        d.mkdir()
        with pytest.raises(SystemExit, match="contains no"):
            latest_mhtml(d)

    def test_default_uses_module_constant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import _mhtml

        d = tmp_path / "mhtml"
        d.mkdir()
        target = d / "snap.mhtml"
        target.write_bytes(b"y")
        monkeypatch.setattr(_mhtml, "MHTML_DIR", d)
        assert latest_mhtml() == target


def test_module_constant_points_at_data_subdir() -> None:
    assert Path("/data/mhtml") == MHTML_DIR


class TestLatestMhtmlRobustness:
    """Edge / regression cases for `latest_mhtml`. See its docstring."""

    def test_case_insensitive_extension_match(self, tmp_path: Path) -> None:
        # Linux FS is case-sensitive; auto-detect must still pick up a
        # `.MHTML` capitalised by a Windows export.
        d = tmp_path / "mhtml"
        d.mkdir()
        target = d / "Snap.MHTML"
        target.write_bytes(b"x")
        assert latest_mhtml(d) == target

    def test_mixed_case_extensions_compete_by_mtime(self, tmp_path: Path) -> None:
        d = tmp_path / "mhtml"
        d.mkdir()
        old_lower = d / "old.mhtml"
        new_upper = d / "new.MHTML"
        old_lower.write_bytes(b"x")
        new_upper.write_bytes(b"y")
        os.utime(old_lower, (1_000_000, 1_000_000))
        os.utime(new_upper, (2_000_000, 2_000_000))
        assert latest_mhtml(d) == new_upper

    def test_directory_named_like_mhtml_is_ignored(self, tmp_path: Path) -> None:
        # Pathological: a directory whose name ends in `.mhtml` (e.g. a
        # half-extracted archive) must not become a download "candidate".
        d = tmp_path / "mhtml"
        d.mkdir()
        bogus_dir = d / "trash.mhtml"
        bogus_dir.mkdir()
        target = d / "real.mhtml"
        target.write_bytes(b"y")
        assert latest_mhtml(d) == target

    def test_directory_only_with_no_files_raises(self, tmp_path: Path) -> None:
        # If the *only* `*.mhtml` entry is a directory, we should raise
        # the same "no files" error as an empty dir, not return the dir.
        d = tmp_path / "mhtml"
        d.mkdir()
        (d / "bogus.mhtml").mkdir()
        with pytest.raises(SystemExit, match="contains no"):
            latest_mhtml(d)

    def test_mtime_tie_break_is_deterministic(self, tmp_path: Path) -> None:
        # Two files with identical mtime — the lexicographically smaller
        # name must win, every call.
        d = tmp_path / "mhtml"
        d.mkdir()
        a = d / "alpha.mhtml"
        b = d / "beta.mhtml"
        a.write_bytes(b"x")
        b.write_bytes(b"y")
        same_time = (1_500_000, 1_500_000)
        os.utime(a, same_time)
        os.utime(b, same_time)
        # 100 calls must return the same answer every time.
        results = {latest_mhtml(d) for _ in range(100)}
        assert results == {a}

    def test_skips_non_mhtml_extensions(self, tmp_path: Path) -> None:
        d = tmp_path / "mhtml"
        d.mkdir()
        (d / "ignore.html").write_bytes(b"x")
        (d / "ignore.txt").write_bytes(b"x")
        (d / "ignore.mhtml.bak").write_bytes(b"x")
        target = d / "real.mhtml"
        target.write_bytes(b"y")
        assert latest_mhtml(d) == target

    def test_file_disappearing_between_iterdir_and_stat_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate the race where a candidate is removed after iterdir
        # listed it but before stat could read its mtime. Pre-fix the sort
        # would call stat() per comparison and crash with FileNotFoundError;
        # the cached-stat impl drops the casualty cleanly.
        d = tmp_path / "mhtml"
        d.mkdir()
        survivor = d / "survivor.mhtml"
        survivor.write_bytes(b"x")
        ghost = d / "ghost.mhtml"
        ghost.write_bytes(b"y")

        import _mhtml

        real_stat = Path.stat

        def stat_with_disappearance(self: Path, **kw: object) -> object:
            if self == ghost:
                raise FileNotFoundError(self)
            return real_stat(self, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", stat_with_disappearance)
        # Ghost should be filtered out, survivor returned.
        assert _mhtml.latest_mhtml(d) == survivor

    def test_handles_many_files_without_quadratic_stat(self, tmp_path: Path) -> None:
        # Soft-perf regression: 200 candidates should resolve in under a
        # second. The cached-stat impl is O(N) syscalls; the old impl was
        # O(N log N). Threshold is generous to keep CI noise low.
        import time

        d = tmp_path / "mhtml"
        d.mkdir()
        for i in range(200):
            (d / f"snap_{i:04d}.mhtml").write_bytes(b"x")
        start = time.monotonic()
        result = latest_mhtml(d)
        elapsed = time.monotonic() - start
        assert result.parent == d
        assert elapsed < 1.0, f"latest_mhtml on 200 files took {elapsed:.3f}s"
