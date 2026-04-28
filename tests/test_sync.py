"""Tests for ``scripts/sync.py``.

Subprocess invocations (inventory.py, download.py) are mocked. The ``data_dir``
fixture redirects sync's hard-coded paths onto a tmp directory so multiple
tests can run in isolation.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import sync
from sync import (
    DATE_RANGE_RE,
    POST_LINE_RE,
    URL_RE,
    _atomic_write_text,
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
    def test_read_returns_none_when_missing(self, data_dir: Path) -> None:
        assert read_anchor() is None

    def test_write_then_read(self, data_dir: Path) -> None:
        write_anchor("2026-04-23")
        assert read_anchor() == "2026-04-23"

    def test_write_none_is_a_noop(self, data_dir: Path) -> None:
        write_anchor(None)
        assert not (data_dir / "coverage.txt").exists()

    def test_blank_file_reads_as_none(self, data_dir: Path) -> None:
        (data_dir / "coverage.txt").write_text("   \n")
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
    def test_dedups_against_existing_set(self, data_dir: Path) -> None:
        seen = data_dir / "seen_posts.txt"
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

    def test_creates_seen_file_when_missing(self, data_dir: Path) -> None:
        total = append_seen(["https://www.patreon.com/posts/x"])
        assert total == 1
        assert (data_dir / "seen_posts.txt").read_text().splitlines() == [
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
        data_dir: Path,
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
        assert not (data_dir / "urls.txt").exists()

    def test_anchor_advances_on_overlap(
        self,
        data_dir: Path,
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
        data_dir: Path,
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
        data_dir: Path,
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
        assert "https://www.patreon.com/posts/foo" in (data_dir / "seen_posts.txt").read_text()

    def test_batch_failure_does_not_advance_seen(
        self,
        data_dir: Path,
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
        assert not (data_dir / "seen_posts.txt").read_text()


class TestEvaluateAnchorProperties:
    """Property-based invariants for the gap-detection logic.

    The state machine has three regions — first-sync / overlap / gap-pending —
    and each one carries a guarantee that no specific date sample should
    ever break.
    """

    DATE_MIN = date(2020, 1, 1)
    DATE_MAX = date(2030, 12, 31)

    @given(
        oldest_date=st.dates(min_value=DATE_MIN, max_value=DATE_MAX),
        window_days=st.integers(min_value=0, max_value=400),
    )
    def test_first_sync_locks_anchor_to_newest(self, oldest_date: date, window_days: int) -> None:
        # When prev=None (no anchor yet) the new anchor must equal `newest`,
        # regardless of how wide the visible MHTML window is.
        oldest = oldest_date.isoformat()
        newest = (oldest_date + timedelta(days=window_days)).isoformat()
        new_anchor, gap = evaluate_anchor(None, oldest, newest)
        assert gap is None
        assert new_anchor == newest

    @given(
        prev_date=st.dates(min_value=DATE_MIN, max_value=DATE_MAX),
        oldest_date=st.dates(min_value=DATE_MIN, max_value=DATE_MAX),
        window_days=st.integers(min_value=0, max_value=400),
    )
    @settings(suppress_health_check=[HealthCheck.filter_too_much])
    def test_overlap_never_retreats_and_caps_at_newest(
        self, prev_date: date, oldest_date: date, window_days: int
    ) -> None:
        # Filter to the overlap branch: oldest <= prev.
        if oldest_date > prev_date:
            return
        prev = prev_date.isoformat()
        oldest = oldest_date.isoformat()
        newest = (oldest_date + timedelta(days=window_days)).isoformat()
        new_anchor, gap = evaluate_anchor(prev, oldest, newest)
        assert gap is None
        # Monotonic forward: anchor never goes backward.
        assert new_anchor is not None
        assert new_anchor >= prev
        # Anchor never overshoots the newest visible date.
        assert new_anchor <= max(prev, newest)
        # Specifically: it's max(prev, newest) per spec.
        assert new_anchor == max(prev, newest)

    @given(
        prev_date=st.dates(min_value=DATE_MIN, max_value=date(2029, 12, 30)),
        gap_offset_days=st.integers(min_value=1, max_value=300),
        window_days=st.integers(min_value=0, max_value=100),
    )
    def test_gap_pending_freezes_anchor(
        self, prev_date: date, gap_offset_days: int, window_days: int
    ) -> None:
        # Construct the gap branch: oldest is strictly newer than prev.
        oldest_dt = prev_date + timedelta(days=gap_offset_days)
        newest_dt = oldest_dt + timedelta(days=window_days)
        prev = prev_date.isoformat()
        new_anchor, gap = evaluate_anchor(prev, oldest_dt.isoformat(), newest_dt.isoformat())
        assert gap is not None and "gap pending" in gap
        # Anchor MUST stay exactly where it was.
        assert new_anchor == prev

    @given(
        prev_date=st.dates(min_value=DATE_MIN, max_value=DATE_MAX),
    )
    def test_no_dates_keeps_anchor(self, prev_date: date) -> None:
        # Inventory emitted no date_range line — keep anchor as-is, no gap.
        new_anchor, gap = evaluate_anchor(prev_date.isoformat(), None, None)
        assert gap is None
        assert new_anchor == prev_date.isoformat()

    @given(
        prev_date=st.dates(min_value=DATE_MIN, max_value=DATE_MAX),
        oldest_date=st.dates(min_value=DATE_MIN, max_value=DATE_MAX),
        window_days=st.integers(min_value=0, max_value=400),
    )
    @settings(suppress_health_check=[HealthCheck.filter_too_much])
    def test_idempotent_on_same_input(
        self, prev_date: date, oldest_date: date, window_days: int
    ) -> None:
        # Calling evaluate_anchor with the same inputs twice must yield
        # the same result (it's a pure function — no globals).
        oldest = oldest_date.isoformat()
        newest = (oldest_date + timedelta(days=window_days)).isoformat()
        a = evaluate_anchor(prev_date.isoformat(), oldest, newest)
        b = evaluate_anchor(prev_date.isoformat(), oldest, newest)
        assert a == b


class TestEvaluateAnchorBoundaries:
    def test_oldest_equals_anchor_is_overlap(self) -> None:
        # The boundary between "gap pending" and "overlap" is `oldest <= prev`.
        # Same date must land in the overlap branch — anchor advances if newest extends.
        new, gap = evaluate_anchor("2026-04-15", "2026-04-15", "2026-04-20")
        assert gap is None
        assert new == "2026-04-20"

    def test_oldest_equals_anchor_idempotent_when_newest_equals_prev(self) -> None:
        # Same anchor, same window — anchor must hold steady, no gap.
        new, gap = evaluate_anchor("2026-04-15", "2026-04-15", "2026-04-15")
        assert gap is None
        assert new == "2026-04-15"


class TestAtomicWriteTextProperties:
    @given(
        # `_atomic_write_text` is used for line-oriented state files
        # (urls.txt, seen_posts.txt, coverage.txt, archive.txt) where
        # contents arrive with `\n` line terminators only — never bare
        # `\r`. Restrict the property's input space to that domain so
        # we don't false-fail on Python's universal-newline read decode
        # (`read_text` collapses CR/CRLF/LF into LF).
        text=st.text(
            alphabet=st.characters(
                blacklist_categories=("Cs",),
                blacklist_characters="\r",
            ),
            max_size=2048,
        )
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    def test_write_then_read_roundtrips(self, text: str, tmp_path: Path) -> None:
        # Property: whatever line-oriented text we hand `_atomic_write_text`,
        # the file ends up with exactly that content and zero leftover
        # `.tmp` shards.
        target = tmp_path / "state.txt"
        _atomic_write_text(target, text)
        assert target.read_text(encoding="utf-8") == text
        leftovers = list(tmp_path.glob(".state.txt.*.tmp"))
        assert leftovers == []


class TestAppendSeenProperties:
    @given(
        urls=st.lists(
            # The seen-set is a line-oriented file. URLs with any character
            # that ``str.splitlines()`` treats as a line terminator (LF, CR,
            # VT, FF, FS, GS, RS, NEL, U+2028 LS, U+2029 PS) must be filtered
            # out: they are not valid post URLs in the first place, and the
            # on-disk line count would differ from the input length because
            # splitlines would chop them.
            st.text(
                min_size=1,
                max_size=80,
                alphabet=st.characters(
                    blacklist_categories=("Cs",),
                    blacklist_characters="\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029",
                ),
            ),
            min_size=0,
            max_size=20,
        )
    )
    @settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_dedup_and_sort_invariant(self, urls: list[str], data_dir: Path) -> None:
        # Property: after `append_seen`, the file contents are sorted+unique
        # and contain exactly the union of (baseline) and (new urls).
        seen = data_dir / "seen_posts.txt"
        # Each hypothesis example must start from the same baseline.
        seen.unlink(missing_ok=True)
        baseline = ["already_a", "already_b"]
        seen.write_text("\n".join(baseline) + "\n", encoding="utf-8")
        total = append_seen(urls)
        on_disk = [line for line in seen.read_text().splitlines() if line]
        expected = sorted(set(baseline) | set(urls))
        assert on_disk == expected
        assert total == len(expected)


class TestMainDryRun:
    def test_dry_run_lists_urls_without_writing_state(
        self,
        data_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        urls_body = (
            "# title: Foo\n# post: https://www.patreon.com/posts/foo\n"
            "https://stream.x.com/20260423_slug_tok/\n"
            "# title: Bar\n# post: https://www.patreon.com/posts/bar\n"
            "https://stream.x.com/20260424_slug2_tok/\n"
        )
        _stub_inventory(
            monkeypatch,
            stdout=urls_body,
            stderr="[inventory] mhtml_date_range: 2026-04-23 .. 2026-04-24 (2 video posts)\n",
        )
        # If run_batch_download were called, the test would crash because
        # we haven't stubbed it — that's exactly the assertion we want.
        rc = main([str(tmp_path / "fake.mhtml"), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "would download 2 new post(s)" in out
        assert "https://www.patreon.com/posts/foo" in out
        assert "https://www.patreon.com/posts/bar" in out
        assert "would initialize coverage anchor at 2026-04-24" in out
        assert "no state changed" in out
        # State files left untouched — not even created.
        assert not (data_dir / "urls.txt").exists()
        assert not (data_dir / "coverage.txt").exists()
        assert not (data_dir / "seen_posts.txt").exists()
        assert read_anchor() is None

    def test_dry_run_no_new_posts(
        self,
        data_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_anchor("2026-04-23")
        _stub_inventory(
            monkeypatch,
            stdout="",
            stderr="[inventory] mhtml_date_range: 2026-04-15 .. 2026-04-23 (3 video posts)\n",
        )
        rc = main([str(tmp_path / "fake.mhtml"), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "no new posts since last sync" in out
        # Anchor remains exactly where we set it.
        assert read_anchor() == "2026-04-23"

    def test_dry_run_reports_anchor_advance(
        self,
        data_dir: Path,
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
        rc = main([str(tmp_path / "fake.mhtml"), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "would advance coverage anchor: 2026-04-15 -> 2026-04-25" in out
        # Anchor not actually moved.
        assert read_anchor() == "2026-04-15"

    def test_dry_run_reports_gap_warning(
        self,
        data_dir: Path,
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
        rc = main([str(tmp_path / "fake.mhtml"), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "gap pending" in out
        # Anchor frozen.
        assert read_anchor() == "2025-12-01"


class TestMainAutoDetectsMhtml:
    def test_picks_newest_when_no_arg_given(
        self,
        data_dir: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import os

        mhtml_dir = data_dir / "mhtml"
        mhtml_dir.mkdir()
        old = mhtml_dir / "old.mhtml"
        new = mhtml_dir / "new.mhtml"
        old.write_bytes(b"x")
        new.write_bytes(b"y")
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))

        seen: list[Path] = []
        monkeypatch.setattr(sync, "run_inventory", lambda mhtml: seen.append(mhtml) or ("", ""))
        rc = main([])
        assert rc == 0
        assert seen == [new]
        # No URLs queued → polite no-op message.
        assert "no new posts" in capsys.readouterr().out

    def test_empty_string_arg_falls_through_to_auto_detect(
        self,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # justfile passes "" when MHTML var is unset; argparse stores it as
        # empty string, which must not be treated as a real path.
        mhtml_dir = data_dir / "mhtml"
        mhtml_dir.mkdir()
        target = mhtml_dir / "x.mhtml"
        target.write_bytes(b"y")

        seen: list[Path] = []
        monkeypatch.setattr(sync, "run_inventory", lambda mhtml: seen.append(mhtml) or ("", ""))
        rc = main([""])
        assert rc == 0
        assert seen == [target]

    def test_missing_mhtml_dir_raises(self, data_dir: Path) -> None:
        # data_dir fixture redirects MHTML_DIR onto data_dir / "mhtml" but
        # does not create that subdir — exercising the helpful error path.
        with pytest.raises(SystemExit, match="does not exist"):
            main([])

    def test_empty_mhtml_dir_raises(self, data_dir: Path) -> None:
        (data_dir / "mhtml").mkdir()
        with pytest.raises(SystemExit, match="contains no"):
            main([])


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


class TestAtomicWriteText:
    def test_replaces_existing_file_atomically(self, tmp_path: Path) -> None:
        target = tmp_path / "state.txt"
        target.write_text("old\n")
        _atomic_write_text(target, "new\n")
        assert target.read_text() == "new\n"
        # No leftover .tmp file.
        leftovers = list(tmp_path.glob(".state.txt.*.tmp"))
        assert leftovers == []

    def test_tmp_cleaned_when_replace_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "state.txt"
        target.write_text("old\n")
        real_replace = Path.replace

        def maybe_boom(self: Path, dst: Path) -> Path:
            if ".tmp" in self.name:
                raise OSError("boom")
            return real_replace(self, dst)

        monkeypatch.setattr(Path, "replace", maybe_boom)
        with pytest.raises(OSError, match="boom"):
            _atomic_write_text(target, "new\n")
        assert target.read_text() == "old\n"
        leftovers = list(tmp_path.glob(".state.txt.*.tmp"))
        assert leftovers == []


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
