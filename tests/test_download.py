"""Tests for ``scripts/download.py``.

The actual ``yt-dlp`` invocation is mocked at ``subprocess.run``;
``resolve.resolve`` is replaced with an identity stub so we don't reach
the network.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

import download
from download import (
    META_KEYS,
    UrlBlock,
    _escape_colons,
    _sleep_seconds,
    build_yt_dlp_args,
    derive_defaults,
    emit_meta_flags,
    main,
    parse_batch,
    run_batch,
    run_one,
)


class TestDeriveDefaults:
    def test_publisher_url_yields_full_metadata(self) -> None:
        meta = derive_defaults("https://stream.foo.com/20260423_slug123_tok/")
        assert meta == {
            "uploader": "foo",
            "date": "2026-04-23",
            "title": "slug123",
            "post": "https://stream.foo.com/20260423_slug123/",
        }

    def test_http_scheme_also_works(self) -> None:
        assert derive_defaults("http://stream.x.com/20260101_a_t/")["uploader"] == "x"

    @pytest.mark.parametrize(
        "url",
        [
            "https://other.example.com/path",
            "https://iframe.videodelivery.net/abc",
            "https://stream.foo.com/no-date-prefix/",
            "not-a-url",
        ],
    )
    def test_unparseable_url_yields_empty(self, url: str) -> None:
        assert derive_defaults(url) == {}


class TestEscapeColons:
    @given(st.text())
    def test_no_unescaped_colons_remain(self, text: str) -> None:
        escaped = _escape_colons(text)
        # Every ":" in the output must be preceded by a "\".
        for i, ch in enumerate(escaped):
            if ch == ":":
                assert i > 0 and escaped[i - 1] == "\\"


class TestEmitMetaFlags:
    def test_empty_value_yields_no_flags(self) -> None:
        assert emit_meta_flags("title", "") == []

    def test_title_emits_two_parse_metadata_pairs(self) -> None:
        flags = emit_meta_flags("title", "Hello")
        assert flags.count("--parse-metadata") == 2
        assert "= Hello:= %(title)s" in flags
        assert "= Hello:= %(meta_title)s" in flags

    def test_uploader_emits_single_parse_metadata(self) -> None:
        flags = emit_meta_flags("uploader", "Some Creator")
        assert flags == ["--parse-metadata", "= Some Creator:= %(uploader)s"]

    def test_date_strips_dashes_and_emits_compact_form(self) -> None:
        flags = emit_meta_flags("date", "2026-04-23")
        assert flags == ["--parse-metadata", "= 20260423:= %(upload_date)s"]

    def test_post_emits_comment_and_purl(self) -> None:
        flags = emit_meta_flags("post", "https://www.patreon.com/posts/foo")
        # Colons in URL must be escaped.
        assert all("https\\:" in f for f in flags if f.startswith("=") or f.startswith("https"))
        assert flags.count("--parse-metadata") == 2
        assert any("%(meta_comment)s" in f for f in flags)
        assert any("%(meta_purl)s" in f for f in flags)

    def test_unknown_key_yields_no_flags(self) -> None:
        assert emit_meta_flags("ignored", "value") == []


class TestParseBatch:
    def test_blank_and_comment_lines_handled(self, tmp_path: Path) -> None:
        path = tmp_path / "urls.txt"
        path.write_text(
            "# title: First\n"
            "# uploader: Alice\n"
            "https://stream.x.com/20260101_a_t/\n"
            "\n"
            "# title: Second\n"
            "https://stream.x.com/20260102_b_t/\n"
        )
        blocks = list(parse_batch(path))
        assert len(blocks) == 2
        assert blocks[0].meta == {"title": "First", "uploader": "Alice"}
        assert blocks[1].meta == {"title": "Second"}

    def test_comment_without_colon_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "urls.txt"
        path.write_text(
            "# just a note\nhttps://stream.x.com/20260101_a_t/\n",
        )
        blocks = list(parse_batch(path))
        assert blocks[0].meta == {}

    def test_carriage_returns_stripped(self, tmp_path: Path) -> None:
        path = tmp_path / "urls.txt"
        path.write_text("# title: Win\r\nhttps://stream.x.com/20260101_a_t/\r\n")
        blocks = list(parse_batch(path))
        assert blocks[0].meta == {"title": "Win"}

    def test_unrecognised_line_is_silently_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "urls.txt"
        path.write_text("garbage line not a url\nhttps://stream.x.com/20260101_a_t/\n")
        blocks = list(parse_batch(path))
        assert len(blocks) == 1
        assert blocks[0].url == "https://stream.x.com/20260101_a_t/"

    def test_meta_resets_between_blocks(self, tmp_path: Path) -> None:
        path = tmp_path / "urls.txt"
        path.write_text(
            "# title: A\nhttps://stream.x.com/20260101_a_t/\nhttps://stream.x.com/20260102_b_t/\n"
        )
        blocks = list(parse_batch(path))
        assert blocks[0].meta == {"title": "A"}
        assert blocks[1].meta == {}


class TestBuildYtDlpArgs:
    def test_basic_args_include_config(self) -> None:
        args = build_yt_dlp_args(UrlBlock(url="https://x"), [], None)
        assert args[0] == "--config-location"

    def test_cookies_appended_when_provided(self) -> None:
        args = build_yt_dlp_args(UrlBlock(url="https://x"), [], "/path/cookies.txt")
        idx = args.index("--cookies")
        assert args[idx + 1] == "/path/cookies.txt"

    def test_metadata_block_overrides_derived_defaults(self) -> None:
        block = UrlBlock(
            url="https://stream.x.com/20260101_slug_tok/",
            meta={"title": "Real Title"},
        )
        args = build_yt_dlp_args(block, [], None)
        # Real Title (from block.meta) wins over "slug" (from derive).
        assert any("Real Title" in a for a in args)
        assert not any(":= %(title)s" in a and "slug" in a for a in args)

    def test_extra_flags_passed_through(self) -> None:
        args = build_yt_dlp_args(UrlBlock(url="https://x"), ["--simulate"], None)
        assert "--simulate" in args


class TestSleepSeconds:
    @given(
        min_s=st.integers(min_value=0, max_value=100), max_s=st.integers(min_value=0, max_value=100)
    )
    def test_result_in_inclusive_range(self, min_s: int, max_s: int) -> None:
        result = _sleep_seconds(min_s, max_s)
        if max_s <= min_s:
            assert result == min_s
        else:
            assert min_s <= result <= max_s

    def test_collapsed_range_is_deterministic(self) -> None:
        assert _sleep_seconds(7, 7) == 7
        assert _sleep_seconds(7, 5) == 7


class TestRunOne:
    def test_invokes_yt_dlp_with_resolved_url(
        self, monkeypatch: pytest.MonkeyPatch, fake_run: MagicMock
    ) -> None:
        fake_run.return_value = MagicMock(returncode=0)
        monkeypatch.setattr(download, "resolve", lambda u: f"resolved::{u}")
        block = UrlBlock(url="https://stream.x.com/20260101_a_t/")
        rc = run_one(block, [], None)
        assert rc == 0
        argv = fake_run.call_args.args[0]
        assert argv[0] == "yt-dlp"
        assert argv[-1] == "resolved::https://stream.x.com/20260101_a_t/"


class TestRunBatch:
    def test_sleeps_between_urls(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_run: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_run.return_value = MagicMock(returncode=0)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        slept: list[int] = []
        monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
        monkeypatch.setenv("YTDLP_BATCH_SLEEP_MIN", "1")
        monkeypatch.setenv("YTDLP_BATCH_SLEEP_MAX", "1")
        path = tmp_path / "urls.txt"
        path.write_text("https://stream.x.com/20260101_a_t/\nhttps://stream.x.com/20260102_b_t/\n")
        run_batch(path, [], None)
        assert slept == [1]
        assert "[batch] sleeping 1s" in capsys.readouterr().out

    def test_returns_nonzero_when_any_url_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_run: MagicMock,
    ) -> None:
        fake_run.side_effect = [MagicMock(returncode=2), MagicMock(returncode=0)]
        monkeypatch.setattr(download, "resolve", lambda u: u)
        monkeypatch.setattr("time.sleep", lambda _s: None)
        monkeypatch.setenv("YTDLP_BATCH_SLEEP_MIN", "0")
        monkeypatch.setenv("YTDLP_BATCH_SLEEP_MAX", "0")
        path = tmp_path / "urls.txt"
        path.write_text("https://stream.x.com/20260101_a_t/\nhttps://stream.x.com/20260102_b_t/\n")
        assert run_batch(path, [], None) == 2


class TestMain:
    def test_no_args_prints_usage(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YTDLP_COOKIES", "")
        rc = main([])
        assert rc == 64
        assert "usage" in capsys.readouterr().err

    def test_single_url_invokes_run_one(
        self, monkeypatch: pytest.MonkeyPatch, fake_run: MagicMock
    ) -> None:
        fake_run.return_value = MagicMock(returncode=0)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = main(["https://stream.x.com/20260101_a_t/"])
        assert rc == 0
        assert fake_run.call_count == 1

    def test_multiple_urls_aggregate_failure_rc(
        self, monkeypatch: pytest.MonkeyPatch, fake_run: MagicMock
    ) -> None:
        fake_run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=3)]
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = main(
            [
                "https://stream.x.com/20260101_a_t/",
                "https://stream.x.com/20260102_b_t/",
            ]
        )
        assert rc == 3

    def test_batch_file_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_run: MagicMock
    ) -> None:
        fake_run.return_value = MagicMock(returncode=0)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        monkeypatch.setattr("time.sleep", lambda _s: None)
        path = tmp_path / "urls.txt"
        path.write_text("https://stream.x.com/20260101_a_t/\n")
        rc = main(["--batch-file", str(path)])
        assert rc == 0

    def test_cookies_env_picked_up_when_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_run: MagicMock
    ) -> None:
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n")
        fake_run.return_value = MagicMock(returncode=0)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        monkeypatch.setenv("YTDLP_COOKIES", str(cookies))
        main(["https://stream.x.com/20260101_a_t/"])
        argv = fake_run.call_args.args[0]
        assert "--cookies" in argv
        assert str(cookies) in argv

    def test_cookies_env_ignored_when_file_missing(
        self, monkeypatch: pytest.MonkeyPatch, fake_run: MagicMock
    ) -> None:
        fake_run.return_value = MagicMock(returncode=0)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        monkeypatch.setenv("YTDLP_COOKIES", "/nonexistent/path")
        main(["https://stream.x.com/20260101_a_t/"])
        argv = fake_run.call_args.args[0]
        assert "--cookies" not in argv

    def test_main_partitions_extra_flags_from_urls(
        self, monkeypatch: pytest.MonkeyPatch, fake_run: MagicMock
    ) -> None:
        fake_run.return_value = MagicMock(returncode=0)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = main(["--simulate", "https://stream.x.com/20260101_a_t/", "-v"])
        assert rc == 0
        argv = fake_run.call_args.args[0]
        # both --simulate and -v should be forwarded to yt-dlp
        assert "--simulate" in argv
        assert "-v" in argv


class TestConstants:
    def test_meta_keys_are_what_emit_recognises(self) -> None:
        for k in META_KEYS:
            assert emit_meta_flags(k, "value")
        assert not emit_meta_flags("not-a-real-key", "value")
