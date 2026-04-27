"""Tests for ``scripts/sync.py``.

Subprocess invocations (inventory.py, download.py) are mocked. The ``state_dir``
fixture redirects sync's hard-coded paths onto a tmp directory so multiple
tests can run in isolation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import sync
from sync import (
    DATE_RANGE_RE,
    POST_LINE_RE,
    URL_RE,
    append_seen,
    evaluate_anchor,
    main,
    parse_date_range,
    read_anchor,
    write_anchor,
)


class TestParseDateRange:
    def test_parses_well_formed_line(self) -> None:
        text = "[inventory] mhtml_date_range: 2025-02-20 .. 2026-04-23 (87 video posts)\n"
        assert parse_date_range(text) == ("2025-02-20", "2026-04-23")

    def test_returns_pair_of_none_when_absent(self) -> None:
        assert parse_date_range("nothing\nmatching\n") == (None, None)


class TestAnchorRoundtrip:
    def test_read_returns_none_when_missing(self, state_dir: Path) -> None:
        assert read_anchor() is None

    def test_write_then_read(self, state_dir: Path) -> None:
        write_anchor("2026-04-23")
        assert read_anchor() == "2026-04-23"

    def test_write_none_is_a_noop(self, state_dir: Path) -> None:
        write_anchor(None)
        assert not (state_dir / "coverage.txt").exists()

    def test_blank_file_reads_as_none(self, state_dir: Path) -> None:
        (state_dir / "coverage.txt").write_text("   \n")
        assert read_anchor() is None


class TestEvaluateAnchor:
    def test_no_dates_keeps_anchor(self) -> None:
        assert evaluate_anchor("2026-01-01", None, None) == ("2026-01-01", None)

    def test_initial_sync_sets_anchor_to_newest(self) -> None:
        assert evaluate_anchor(None, "2025-01-01", "2026-04-23") == ("2026-04-23", None)

    def test_overlap_no_advance_when_newest_equals_anchor(self) -> None:
        new, gap = evaluate_anchor("2026-04-23", "2025-02-20", "2026-04-23")
        assert new == "2026-04-23"
        assert gap is None

    def test_overlap_advances_when_newest_extends(self) -> None:
        new, gap = evaluate_anchor("2026-04-23", "2025-02-20", "2026-05-01")
        assert new == "2026-05-01"
        assert gap is None

    def test_gap_pending_holds_anchor(self) -> None:
        new, gap = evaluate_anchor("2025-12-01", "2026-04-15", "2026-04-23")
        assert new == "2025-12-01"
        assert gap is not None
        assert "gap pending" in gap


class TestAppendSeen:
    def test_dedups_against_existing_set(self, state_dir: Path) -> None:
        seen = state_dir / "seen_posts.txt"
        seen.write_text("https://www.patreon.com/posts/a\n")
        total = append_seen(
            [
                "https://www.patreon.com/posts/a",
                "https://www.patreon.com/posts/b",
            ]
        )
        assert total == 2
        assert sorted(seen.read_text().splitlines()) == [
            "https://www.patreon.com/posts/a",
            "https://www.patreon.com/posts/b",
        ]

    def test_creates_seen_file_when_missing(self, state_dir: Path) -> None:
        total = append_seen(["https://www.patreon.com/posts/x"])
        assert total == 1
        assert (state_dir / "seen_posts.txt").read_text().splitlines() == [
            "https://www.patreon.com/posts/x"
        ]


def _stub_inventory(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    stderr: str = "",
) -> None:
    """Replace ``sync.run_inventory`` with a stub returning fixed stdout/stderr."""
    monkeypatch.setattr(sync, "run_inventory", lambda _mhtml: (stdout, stderr))


class TestMain:
    def test_first_sync_initializes_anchor_and_no_op(
        self,
        state_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_inventory(
            monkeypatch,
            stdout="",
            stderr="[inventory] mhtml_date_range: 2026-04-15 .. 2026-04-23 (3 video posts)\n",
        )
        rc = main([str(tmp_path / "fake.mhtml")])
        out = capsys.readouterr().out
        assert rc == 0
        assert "coverage anchor initialized at 2026-04-23" in out
        assert "no new posts since last run" in out
        assert read_anchor() == "2026-04-23"
        assert not (state_dir / "urls.txt").exists()

    def test_anchor_advances_on_overlap(
        self,
        state_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_anchor("2026-04-15")
        _stub_inventory(
            monkeypatch,
            stdout="",
            stderr="[inventory] mhtml_date_range: 2026-04-10 .. 2026-04-25 (3 video posts)\n",
        )
        rc = main([str(tmp_path / "fake.mhtml")])
        out = capsys.readouterr().out
        assert rc == 0
        assert "coverage anchor advanced: 2026-04-15 -> 2026-04-25" in out
        assert read_anchor() == "2026-04-25"

    def test_gap_pending_emits_warning(
        self,
        state_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_anchor("2025-12-01")
        _stub_inventory(
            monkeypatch,
            stdout="",
            stderr="[inventory] mhtml_date_range: 2026-04-15 .. 2026-04-23 (3 video posts)\n",
        )
        rc = main([str(tmp_path / "fake.mhtml")])
        out = capsys.readouterr().out
        assert rc == 0
        assert "gap pending" in out
        assert read_anchor() == "2025-12-01"

    def test_runs_batch_when_new_urls_present(
        self,
        state_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        urls_body = (
            "# title: Foo\n# post: https://www.patreon.com/posts/foo\n"
            "https://stream.x.com/20260423_slug_tok/\n"
        )
        _stub_inventory(
            monkeypatch,
            stdout=urls_body,
            stderr="[inventory] mhtml_date_range: 2026-04-23 .. 2026-04-23 (1 video posts)\n",
        )
        batch_call = MagicMock(return_value=0)
        monkeypatch.setattr(sync, "run_batch_download", batch_call)
        rc = main([str(tmp_path / "fake.mhtml")])
        out = capsys.readouterr().out
        assert rc == 0
        batch_call.assert_called_once()
        assert "1 new post(s) queued" in out
        assert "1 post(s) marked as seen" in out
        assert "https://www.patreon.com/posts/foo" in (state_dir / "seen_posts.txt").read_text()

    def test_batch_failure_does_not_advance_seen(
        self,
        state_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        urls_body = (
            "# post: https://www.patreon.com/posts/foo\nhttps://stream.x.com/20260423_slug_tok/\n"
        )
        _stub_inventory(
            monkeypatch,
            stdout=urls_body,
            stderr="[inventory] mhtml_date_range: 2026-04-23 .. 2026-04-23 (1 video posts)\n",
        )
        monkeypatch.setattr(sync, "run_batch_download", lambda _u: 7)
        rc = main([str(tmp_path / "fake.mhtml")])
        err = capsys.readouterr().err
        assert rc == 7
        assert "not advancing seen-set" in err
        assert not (state_dir / "seen_posts.txt").read_text()


class TestRegexes:
    def test_post_line_re_captures_url(self) -> None:
        text = "# post: https://www.patreon.com/posts/foo\nother\n"
        assert POST_LINE_RE.findall(text) == ["https://www.patreon.com/posts/foo"]

    def test_url_re_matches_http_or_https_at_line_start(self) -> None:
        assert URL_RE.search("first\nhttps://example.com\n")
        assert URL_RE.search("http://example.com\n")
        assert URL_RE.search("  http://example.com\n") is None

    def test_date_range_re_extracts_named_groups(self) -> None:
        m = DATE_RANGE_RE.search(
            "[inventory] mhtml_date_range: 2025-01-01 .. 2026-04-27 (10 video posts)"
        )
        assert m is not None
        assert m.group("oldest") == "2025-01-01"
        assert m.group("newest") == "2026-04-27"


class TestRunInventoryAndBatch:
    def test_run_inventory_streams_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = MagicMock(stdout="urls\n", stderr="[inventory] something\n")
        monkeypatch.setattr("subprocess.run", MagicMock(return_value=result))
        out, err = sync.run_inventory(Path("/in/mhtml"))
        # stderr is mirrored to sys.stderr by run_inventory
        captured = capsys.readouterr()
        assert out == "urls\n"
        assert err == "[inventory] something\n"
        assert "[inventory] something" in captured.err

    def test_run_inventory_silent_when_stderr_empty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = MagicMock(stdout="urls\n", stderr="")
        monkeypatch.setattr("subprocess.run", MagicMock(return_value=result))
        out, err = sync.run_inventory(Path("/in/mhtml"))
        assert out == "urls\n"
        assert err == ""
        assert capsys.readouterr().err == ""

    def test_run_batch_download_invokes_subprocess(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        result = MagicMock(returncode=0)
        run_mock = MagicMock(return_value=result)
        monkeypatch.setattr("subprocess.run", run_mock)
        urls = tmp_path / "urls.txt"
        urls.write_text("https://x")
        rc = sync.run_batch_download(urls)
        assert rc == 0
        argv = run_mock.call_args.args[0]
        assert argv[0].endswith("python") or argv[0].endswith("python3")
        assert "--batch-file" in argv
