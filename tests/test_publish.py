"""Tests for ``scripts/publish.py``.

Atomic-publish guarantees are checked from multiple angles: happy path,
mid-copy disk-full simulation, archive merge with/without trailing newline,
manifest parsing edge cases, and stale .tmp sweeping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import publish
from publish import (
    PUBLISH_TMP_GLOB,
    PublishedItem,
    append_archive,
    atomic_publish,
    parse_manifest,
    prepare_staging,
    publish_outputs,
    seed_skip_archive,
    sweep_publish_tmps,
)


class TestPrepareStaging:
    def test_creates_unique_dir_per_call(self, staging_root: Path) -> None:
        a = prepare_staging()
        b = prepare_staging()
        assert a.home != b.home
        assert a.home.is_dir()
        assert b.home.is_dir()
        assert a.home.parent == staging_root

    def test_manifest_and_skip_paths_under_home(self, staging_root: Path) -> None:
        run = prepare_staging()
        assert run.manifest.parent == run.home
        assert run.skip_archive.parent == run.home
        assert run.manifest.name == publish.MANIFEST_NAME
        assert run.skip_archive.name == publish.SKIP_ARCHIVE_NAME

    def test_creates_root_when_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sanity: prepare_staging mkdir-p's the root if a fresh deploy
        # hasn't initialised it yet.
        new_root = tmp_path / "fresh"
        monkeypatch.setattr(publish, "STAGING_ROOT", new_root)
        prepare_staging()
        assert new_root.is_dir()


class TestStagingRunCleanup:
    def test_cleanup_removes_tree(self, staging_root: Path) -> None:
        run = prepare_staging()
        (run.home / "leftover.bin").write_bytes(b"x")
        run.cleanup()
        assert not run.home.exists()

    def test_cleanup_is_idempotent(self, staging_root: Path) -> None:
        run = prepare_staging()
        run.cleanup()
        run.cleanup()  # would raise on a naive rmtree


class TestSeedSkipArchive:
    def test_copies_when_source_exists(self, staging_root: Path, data_dir: Path) -> None:
        (data_dir / "archive.txt").write_text("cloudflarestream abc\n")
        run = prepare_staging()
        seed_skip_archive(run)
        assert run.skip_archive.read_text() == "cloudflarestream abc\n"

    def test_touches_empty_when_source_missing(self, staging_root: Path, data_dir: Path) -> None:
        run = prepare_staging()
        seed_skip_archive(run)
        assert run.skip_archive.exists()
        assert run.skip_archive.read_text() == ""


class TestParseManifest:
    def test_returns_empty_when_manifest_missing(self, staging_root: Path) -> None:
        run = prepare_staging()
        assert parse_manifest(run) == []

    def test_parses_well_formed_rows(self, staging_root: Path) -> None:
        run = prepare_staging()
        run.manifest.write_text(
            "CloudflareStream\tabc123\t/var/lib/pa/staging/run-x/foo/bar.mp4\n",
            encoding="utf-8",
        )
        rows = parse_manifest(run)
        assert rows == [
            ("CloudflareStream", "abc123", Path("/var/lib/pa/staging/run-x/foo/bar.mp4")),
        ]

    def test_skips_blank_and_malformed_rows(self, staging_root: Path) -> None:
        run = prepare_staging()
        run.manifest.write_text(
            "\nmalformed only-one-field\nGood\tid1\t/p/a.mp4\n",
            encoding="utf-8",
        )
        rows = parse_manifest(run)
        assert rows == [("Good", "id1", Path("/p/a.mp4"))]


class TestAtomicPublish:
    def test_copy_then_replace(self, tmp_path: Path) -> None:
        src = tmp_path / "src.bin"
        src.write_bytes(b"payload")
        dst = tmp_path / "out" / "final.bin"
        atomic_publish(src, dst)
        assert dst.read_bytes() == b"payload"

    def test_no_tmp_file_left_after_success(self, tmp_path: Path) -> None:
        src = tmp_path / "src.bin"
        src.write_bytes(b"x")
        dst = tmp_path / "out" / "final.bin"
        atomic_publish(src, dst)
        leftovers = list((tmp_path / "out").glob(PUBLISH_TMP_GLOB))
        assert leftovers == []

    def test_tmp_removed_when_copy_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = tmp_path / "src.bin"
        src.write_bytes(b"x")
        dst = tmp_path / "out" / "final.bin"
        # Allow the copy to create the tmp, then explode before replace.
        original_copy2 = publish.shutil.copy2

        def boom_copy(s: object, t: object) -> None:
            original_copy2(s, t)
            raise OSError("disk full")

        monkeypatch.setattr(publish.shutil, "copy2", boom_copy)
        with pytest.raises(OSError, match="disk full"):
            atomic_publish(src, dst)
        # Final file must NOT exist; tmp must NOT linger.
        assert not dst.exists()
        leftovers = list((tmp_path / "out").glob(PUBLISH_TMP_GLOB))
        assert leftovers == []

    def test_overwrite_existing_destination(self, tmp_path: Path) -> None:
        src = tmp_path / "src.bin"
        src.write_bytes(b"new")
        dst = tmp_path / "final.bin"
        dst.write_bytes(b"old")
        atomic_publish(src, dst)
        assert dst.read_bytes() == b"new"


class TestAppendArchive:
    def test_no_op_on_empty_lines(self, data_dir: Path) -> None:
        archive = data_dir / "archive.txt"
        append_archive([])
        assert not archive.exists()

    def test_creates_archive_when_missing(self, data_dir: Path) -> None:
        archive = data_dir / "archive.txt"
        append_archive(["cloudflarestream abc"])
        assert archive.read_text() == "cloudflarestream abc\n"

    def test_appends_with_trailing_newline_normalised(self, data_dir: Path) -> None:
        archive = data_dir / "archive.txt"
        archive.write_text("cloudflarestream abc")  # NO trailing newline
        append_archive(["cloudflarestream def"])
        assert archive.read_text() == "cloudflarestream abc\ncloudflarestream def\n"

    def test_appends_when_existing_has_trailing_newline(self, data_dir: Path) -> None:
        archive = data_dir / "archive.txt"
        archive.write_text("cloudflarestream abc\n")
        append_archive(["cloudflarestream def", "cloudflarestream ghi"])
        body = archive.read_text()
        assert body.splitlines() == [
            "cloudflarestream abc",
            "cloudflarestream def",
            "cloudflarestream ghi",
        ]
        assert body.endswith("\n")

    def test_tmp_cleaned_when_replace_raises(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = data_dir / "archive.txt"
        archive.write_text("cloudflarestream abc\n")
        # Patch Path.replace so the rename step fails after the tmp body
        # was already written. The except clause must remove the tmp.
        real_replace = Path.replace

        def maybe_boom(self: Path, target: Path) -> Path:
            if ".tmp" in self.name:
                raise OSError("boom")
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", maybe_boom)
        with pytest.raises(OSError, match="boom"):
            append_archive(["cloudflarestream def"])
        leftovers = list(data_dir.glob(".archive.txt.*.tmp"))
        assert leftovers == []
        # The original content survives.
        assert archive.read_text() == "cloudflarestream abc\n"


class TestSweepPublishTmps:
    def test_removes_dotfiles_at_root_and_nested(self, data_dir: Path) -> None:
        (data_dir / ".pa-publish.deadbeef.tmp").write_bytes(b"junk")
        nested = data_dir / "creator"
        nested.mkdir()
        (nested / ".pa-publish.cafebabe.tmp").write_bytes(b"junk")
        (nested / "real.mp4").write_bytes(b"keep")
        sweep_publish_tmps()
        assert not (data_dir / ".pa-publish.deadbeef.tmp").exists()
        assert not (nested / ".pa-publish.cafebabe.tmp").exists()
        assert (nested / "real.mp4").exists()

    def test_no_op_when_root_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # data_dir doesn't exist; sweep should not raise.
        monkeypatch.setattr(publish, "DATA_DIR", tmp_path / "missing")
        sweep_publish_tmps()

    def test_skips_dir_matching_pattern(self, data_dir: Path) -> None:
        # A directory that happens to match the glob is left alone — only
        # regular files get unlinked. Defensive against weird user content.
        weird = data_dir / ".pa-publish.deadbeef.tmp"
        weird.mkdir()
        sweep_publish_tmps()
        assert weird.is_dir()


class TestPublishOutputs:
    def test_relocates_each_manifest_row(self, staging_root: Path, data_dir: Path) -> None:
        run = prepare_staging()
        # Mirror yt-dlp's home-relative layout: <run.home>/<uploader>/<file>.mp4.
        (run.home / "Foo").mkdir()
        src = run.home / "Foo" / "2026-04-23_bar.mp4"
        src.write_bytes(b"video-bytes")
        run.manifest.write_text(
            f"CloudflareStream\tabc123\t{src}\n",
            encoding="utf-8",
        )
        items = publish_outputs(run)
        assert items == [
            PublishedItem(
                extractor="CloudflareStream",
                video_id="abc123",
                final_path=data_dir / "Foo" / "2026-04-23_bar.mp4",
            )
        ]
        assert (data_dir / "Foo" / "2026-04-23_bar.mp4").read_bytes() == b"video-bytes"

    def test_archive_line_lowercases_extractor(self) -> None:
        item = PublishedItem(extractor="CloudflareStream", video_id="abc123", final_path=Path("/x"))
        assert item.archive_line == "cloudflarestream abc123"

    def test_no_op_when_manifest_empty(self, staging_root: Path, data_dir: Path) -> None:
        run = prepare_staging()
        # No manifest written — yt-dlp printed nothing (e.g. URL was already
        # in the archive and skipped).
        assert publish_outputs(run) == []

    def test_appends_are_atomic_against_in_flight_crashes(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Property: if the tmp -> replace step fails mid-write, the original
        # archive must remain intact (no partial new content visible). The
        # only way the user-facing archive can change is via the atomic
        # POSIX rename at the end.
        archive = data_dir / "archive.txt"
        archive.write_text("cloudflarestream existing\n")

        real_replace = Path.replace

        def boom_on_tmp_replace(self: Path, dst: Path) -> Path:
            if ".tmp" in self.name:
                raise OSError("disk full")
            return real_replace(self, dst)

        monkeypatch.setattr(Path, "replace", boom_on_tmp_replace)
        with pytest.raises(OSError, match="disk full"):
            append_archive(["cloudflarestream new"])
        # Existing content must still be exactly what was there before.
        assert archive.read_text() == "cloudflarestream existing\n"
        # And no leftover .tmp shards.
        assert sorted(p.name for p in data_dir.glob(".archive.txt.*.tmp")) == []

    def test_concurrent_writers_lose_one_writes_warning(self, data_dir: Path) -> None:
        """Document the single-writer assumption in executable form.

        ``append_archive`` is **not** safe under concurrent writers — see its
        docstring. Two writers each loading the same baseline and replacing
        the file in turn will silently drop the earlier append. This test
        pins that behaviour so anyone making it concurrent-safe in the future
        will see this test fail and know to update both the impl and the
        invariant docs.
        """
        # Writer A starts: reads "" baseline, prepares "a\n".
        archive = data_dir / "archive.txt"
        baseline_a = archive.read_text() if archive.exists() else ""
        # Writer B sneaks in: reads same baseline, writes "b\n".
        # (Simulated by calling append_archive directly.)
        append_archive(["b"])
        assert archive.read_text() == "b\n"
        # Writer A finishes its tmp+replace: it had "a\n" prepared based on
        # the original (empty) baseline. Replacing the file overwrites B's work.
        # We simulate this manually:
        body = baseline_a + "a\n"
        tmp = data_dir / ".archive.txt.race.tmp"
        tmp.write_text(body)
        tmp.replace(archive)
        # B's append is gone. This is the documented hazard.
        on_disk = archive.read_text()
        assert on_disk == "a\n"
        assert "b" not in on_disk

    def test_dst_root_overrides_data_dir(self, staging_root: Path, data_dir: Path) -> None:
        # `--retest` flow: caller passes a sandbox dir so the file lands
        # there instead of the canonical /data tree.
        run = prepare_staging()
        (run.home / "Foo").mkdir()
        src = run.home / "Foo" / "2026-04-23_bar.mp4"
        src.write_bytes(b"video-bytes")
        run.manifest.write_text(
            f"CloudflareStream\tabc123\t{src}\n",
            encoding="utf-8",
        )
        sandbox = data_dir / ".retest" / "20260428_120000-deadbeef"
        items = publish_outputs(run, dst_root=sandbox)
        assert items == [
            PublishedItem(
                extractor="CloudflareStream",
                video_id="abc123",
                final_path=sandbox / "Foo" / "2026-04-23_bar.mp4",
            )
        ]
        # Canonical tree must remain empty.
        assert not (data_dir / "Foo").exists()
        assert (sandbox / "Foo" / "2026-04-23_bar.mp4").read_bytes() == b"video-bytes"


class TestStagingRunDataclass:
    def test_is_frozen(self, staging_root: Path) -> None:
        run = prepare_staging()
        # FrozenInstanceError is a subclass of AttributeError.
        with pytest.raises(AttributeError):
            run.home = Path("/nope")  # type: ignore[misc]


class TestPublishedItemDataclass:
    def test_is_frozen(self) -> None:
        item = PublishedItem(extractor="X", video_id="y", final_path=Path("/x"))
        with pytest.raises(AttributeError):
            item.video_id = "z"  # type: ignore[misc]
