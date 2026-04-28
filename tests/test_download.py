"""Tests for ``scripts/download.py``.

The actual ``yt-dlp`` invocation is mocked at ``subprocess.run``;
``resolve.resolve`` is replaced with an identity stub so we don't reach
the network. Atomic-publish helpers from ``publish`` get exercised end to
end via the ``staging_root`` + ``data_dir`` fixtures so a passing run_one
test really does prove the file landed in /data and the archive line was
appended.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import download
import publish
from download import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    META_KEYS,
    UrlBlock,
    _escape_colons,
    _sleep_seconds,
    build_yt_dlp_args,
    derive_defaults,
    emit_meta_flags,
    main,
    parse_batch,
    resolve_config_path,
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
    def test_unparsable_url_yields_empty(self, url: str) -> None:
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


@pytest.fixture
def bare_staging(tmp_path: Path) -> publish.StagingRun:
    """Lightweight StagingRun for argv-shape tests that don't invoke yt-dlp."""
    home = tmp_path / "stg"
    home.mkdir()
    return publish.StagingRun(
        home=home,
        manifest=home / publish.MANIFEST_NAME,
        skip_archive=home / publish.SKIP_ARCHIVE_NAME,
    )


class TestBuildYtDlpArgs:
    def test_basic_args_include_config(self, bare_staging: publish.StagingRun) -> None:
        args = build_yt_dlp_args(UrlBlock(url="https://x"), [], None, bare_staging)
        assert args[0] == "--config-location"

    def test_config_path_defaults_to_polite_preset(
        self, bare_staging: publish.StagingRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)
        args = build_yt_dlp_args(UrlBlock(url="https://x"), [], None, bare_staging)
        assert args[1] == str(DEFAULT_CONFIG_PATH)

    def test_config_path_override_via_env(
        self, bare_staging: publish.StagingRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CONFIG_PATH_ENV, "/work/config/yt-dlp-fast.conf")
        args = build_yt_dlp_args(UrlBlock(url="https://x"), [], None, bare_staging)
        assert args[1] == "/work/config/yt-dlp-fast.conf"

    def test_empty_env_falls_back_to_default(
        self, bare_staging: publish.StagingRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Empty string must NOT be treated as "use empty path" — yt-dlp would
        # then read from the cwd's yt-dlp.conf or fail; an unset-equivalent
        # value should fall through to the bundled default.
        monkeypatch.setenv(CONFIG_PATH_ENV, "")
        assert resolve_config_path() == DEFAULT_CONFIG_PATH

    def test_paths_pin_yt_dlp_to_staging(self, bare_staging: publish.StagingRun) -> None:
        args = build_yt_dlp_args(UrlBlock(url="https://x"), [], None, bare_staging)
        idx = args.index("--paths")
        assert args[idx + 1] == f"home:{bare_staging.home}"

    def test_download_archive_points_at_staging_skip_copy(
        self, bare_staging: publish.StagingRun
    ) -> None:
        args = build_yt_dlp_args(UrlBlock(url="https://x"), [], None, bare_staging)
        idx = args.index("--download-archive")
        assert args[idx + 1] == str(bare_staging.skip_archive)

    def test_manifest_print_to_file(self, bare_staging: publish.StagingRun) -> None:
        args = build_yt_dlp_args(UrlBlock(url="https://x"), [], None, bare_staging)
        idx = args.index("--print-to-file")
        # Template + manifest path follow the flag, in that order.
        assert args[idx + 1] == download.MANIFEST_TEMPLATE
        assert args[idx + 2] == str(bare_staging.manifest)

    def test_cookies_appended_when_provided(self, bare_staging: publish.StagingRun) -> None:
        args = build_yt_dlp_args(UrlBlock(url="https://x"), [], "/path/cookies.txt", bare_staging)
        idx = args.index("--cookies")
        assert args[idx + 1] == "/path/cookies.txt"

    def test_metadata_block_overrides_derived_defaults(
        self, bare_staging: publish.StagingRun
    ) -> None:
        block = UrlBlock(
            url="https://stream.x.com/20260101_slug_tok/",
            meta={"title": "Real Title"},
        )
        args = build_yt_dlp_args(block, [], None, bare_staging)
        # Real Title (from block.meta) wins over "slug" (from derive).
        assert any("Real Title" in a for a in args)
        assert not any(":= %(title)s" in a and "slug" in a for a in args)

    def test_extra_flags_passed_through(self, bare_staging: publish.StagingRun) -> None:
        args = build_yt_dlp_args(UrlBlock(url="https://x"), ["--simulate"], None, bare_staging)
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

    @given(
        min_s=st.integers(min_value=-100, max_value=100),
        max_s=st.integers(min_value=-100, max_value=100),
    )
    def test_never_returns_negative(self, min_s: int, max_s: int) -> None:
        # Property: a hostile / typo'd YTDLP_BATCH_SLEEP_MIN must not let
        # a negative value reach `time.sleep` — it would raise ValueError
        # mid-batch and abort otherwise-fine downloads.
        assert _sleep_seconds(min_s, max_s) >= 0

    def test_negative_min_is_clamped_to_zero(self) -> None:
        # Regression: pre-fix, `_sleep_seconds(-5, -1)` returned -5.
        assert _sleep_seconds(-5, -1) == 0
        assert _sleep_seconds(-5, 5) >= 0


class TestRunOneValueErrorContained:
    def test_publish_value_error_returns_one_and_keeps_data_clean(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Simulate the "user passed --paths /elsewhere" footgun: yt-dlp
        # writes outside staging.home, so publish_outputs blows up with
        # ValueError on relative_to. Without the (OSError, ValueError)
        # broadening, the whole batch would abort.
        def fake(
            argv: list[str],
            check: bool = False,
            **_: object,
        ) -> MagicMock:
            manifest_idx = argv.index("--print-to-file")
            manifest = Path(argv[manifest_idx + 2])
            # Manifest points at a path *outside* the staging home.
            outside = data_dir / "stray.mp4"
            outside.write_bytes(b"orphan")
            manifest.write_text(
                f"CloudflareStream\tabc123\t{outside}\n",
                encoding="utf-8",
            )
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", MagicMock(side_effect=fake))
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = run_one(UrlBlock(url="https://stream.x.com/20260101_a_t/"), [], None)
        err = capsys.readouterr().err
        assert rc == 1
        assert "[publish] failed" in err
        # No archive.txt mutation — the whole publish path bailed cleanly.
        assert not (data_dir / "archive.txt").exists()


def _fake_yt_dlp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    produces: list[tuple[str, str, str]] | None = None,
) -> MagicMock:
    """Replace subprocess.run so it pretends to be yt-dlp.

    On success, write the requested fake mp4 files into the staging
    ``home`` directory and append a matching row to the manifest file,
    just like a real yt-dlp run would after ``after_move``. ``produces``
    is a list of ``(extractor, video_id, relative_filepath)`` tuples;
    relative paths are resolved against the staging home pulled from the
    ``--paths home:`` flag in argv.
    """
    rows = produces or []

    def fake(
        argv: list[str],
        check: bool = False,  # match subprocess.run signature
        **_: object,
    ) -> MagicMock:
        if returncode == 0 and rows:
            paths_idx = argv.index("--paths")
            home = Path(argv[paths_idx + 1].removeprefix("home:"))
            manifest_idx = argv.index("--print-to-file")
            manifest = Path(argv[manifest_idx + 2])
            lines: list[str] = []
            for extractor, vid, rel in rows:
                target = home / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"fake-mp4-bytes")
                lines.append(f"{extractor}\t{vid}\t{target}")
            manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return MagicMock(returncode=returncode)

    mock = MagicMock(side_effect=fake)
    monkeypatch.setattr("subprocess.run", mock)
    return mock


class TestRunOne:
    def test_invokes_yt_dlp_with_resolved_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        fake = _fake_yt_dlp(monkeypatch)
        monkeypatch.setattr(download, "resolve", lambda u: f"resolved::{u}")
        block = UrlBlock(url="https://stream.x.com/20260101_a_t/")
        rc = run_one(block, [], None)
        assert rc == 0
        argv = fake.call_args.args[0]
        assert argv[0] == "yt-dlp"
        assert argv[-1] == "resolved::https://stream.x.com/20260101_a_t/"

    def test_publishes_manifest_files_into_data_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        _fake_yt_dlp(
            monkeypatch,
            produces=[("CloudflareStream", "abc123", "Foo/2026-01-01_bar.mp4")],
        )
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = run_one(UrlBlock(url="https://stream.x.com/20260101_a_t/"), [], None)
        assert rc == 0
        published = data_dir / "Foo" / "2026-01-01_bar.mp4"
        assert published.read_bytes() == b"fake-mp4-bytes"
        assert (data_dir / "archive.txt").read_text() == "cloudflarestream abc123\n"

    def test_yt_dlp_failure_leaves_data_untouched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        _fake_yt_dlp(monkeypatch, returncode=7)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = run_one(UrlBlock(url="https://stream.x.com/20260101_a_t/"), [], None)
        assert rc == 7
        assert list(data_dir.iterdir()) == []

    def test_staging_cleaned_after_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        _fake_yt_dlp(
            monkeypatch,
            produces=[("CloudflareStream", "abc123", "Foo/file.mp4")],
        )
        monkeypatch.setattr(download, "resolve", lambda u: u)
        run_one(UrlBlock(url="https://stream.x.com/20260101_a_t/"), [], None)
        # Every per-run staging dir must be gone — only the root remains.
        assert list(staging_root.iterdir()) == []

    def test_publish_failure_returns_one_and_keeps_data_untouched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # yt-dlp succeeds and prints a manifest line, but the file it
        # claims to have produced doesn't exist on disk — atomic_publish
        # then raises FileNotFoundError (an OSError subclass).
        def fake(
            argv: list[str],
            check: bool = False,
            **_: object,
        ) -> MagicMock:
            paths_idx = argv.index("--paths")
            home = Path(argv[paths_idx + 1].removeprefix("home:"))
            manifest_idx = argv.index("--print-to-file")
            manifest = Path(argv[manifest_idx + 2])
            phantom = home / "phantom.mp4"
            manifest.write_text(
                f"CloudflareStream\tabc123\t{phantom}\n",
                encoding="utf-8",
            )
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", MagicMock(side_effect=fake))
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = run_one(UrlBlock(url="https://stream.x.com/20260101_a_t/"), [], None)
        err = capsys.readouterr().err
        assert rc == 1
        assert "[publish] failed" in err
        assert not (data_dir / "archive.txt").exists()
        assert list(data_dir.iterdir()) == []


class TestRunBatch:
    def test_sleeps_between_urls(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _fake_yt_dlp(monkeypatch)
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
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        rcs = iter([2, 0])
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(side_effect=lambda *_a, **_k: MagicMock(returncode=next(rcs))),
        )
        monkeypatch.setattr(download, "resolve", lambda u: u)
        monkeypatch.setattr("time.sleep", lambda _s: None)
        monkeypatch.setenv("YTDLP_BATCH_SLEEP_MIN", "0")
        monkeypatch.setenv("YTDLP_BATCH_SLEEP_MAX", "0")
        path = tmp_path / "urls.txt"
        path.write_text("https://stream.x.com/20260101_a_t/\nhttps://stream.x.com/20260102_b_t/\n")
        assert run_batch(path, [], None) == 2

    def test_sweeps_stale_publish_tmps_before_starting(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        # Stale tmp from a prior crashed publish; batch must wipe it.
        stale = data_dir / ".pa-publish.deadbeef.tmp"
        stale.write_bytes(b"junk")
        _fake_yt_dlp(monkeypatch)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        monkeypatch.setattr("time.sleep", lambda _s: None)
        monkeypatch.setenv("YTDLP_BATCH_SLEEP_MIN", "0")
        monkeypatch.setenv("YTDLP_BATCH_SLEEP_MAX", "0")
        path = tmp_path / "urls.txt"
        path.write_text("https://stream.x.com/20260101_a_t/\n")
        run_batch(path, [], None)
        assert not stale.exists()


class TestMain:
    def test_no_args_prints_usage(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YTDLP_COOKIES", "")
        rc = main([])
        assert rc == 64
        assert "usage" in capsys.readouterr().err

    def test_single_url_invokes_run_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        fake = _fake_yt_dlp(monkeypatch)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = main(["https://stream.x.com/20260101_a_t/"])
        assert rc == 0
        assert fake.call_count == 1

    def test_multiple_urls_aggregate_failure_rc(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        rcs = iter([0, 3])
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(side_effect=lambda *_a, **_k: MagicMock(returncode=next(rcs))),
        )
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = main(
            [
                "https://stream.x.com/20260101_a_t/",
                "https://stream.x.com/20260102_b_t/",
            ]
        )
        assert rc == 3

    def test_main_sweeps_publish_tmps_for_single_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        stale = data_dir / ".pa-publish.deadbeef.tmp"
        stale.write_bytes(b"junk")
        _fake_yt_dlp(monkeypatch)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        main(["https://stream.x.com/20260101_a_t/"])
        assert not stale.exists()

    def test_batch_file_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        _fake_yt_dlp(monkeypatch)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        monkeypatch.setattr("time.sleep", lambda _s: None)
        path = tmp_path / "urls.txt"
        path.write_text("https://stream.x.com/20260101_a_t/\n")
        rc = main(["--batch-file", str(path)])
        assert rc == 0

    def test_cookies_env_picked_up_when_file_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n")
        fake = _fake_yt_dlp(monkeypatch)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        monkeypatch.setenv("YTDLP_COOKIES", str(cookies))
        main(["https://stream.x.com/20260101_a_t/"])
        argv = fake.call_args.args[0]
        assert "--cookies" in argv
        assert str(cookies) in argv

    def test_cookies_env_ignored_when_file_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        fake = _fake_yt_dlp(monkeypatch)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        monkeypatch.setenv("YTDLP_COOKIES", "/nonexistent/path")
        main(["https://stream.x.com/20260101_a_t/"])
        argv = fake.call_args.args[0]
        assert "--cookies" not in argv

    def test_main_partitions_extra_flags_from_urls(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        fake = _fake_yt_dlp(monkeypatch)
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = main(["--simulate", "https://stream.x.com/20260101_a_t/", "-v"])
        assert rc == 0
        argv = fake.call_args.args[0]
        # both --simulate and -v should be forwarded to yt-dlp
        assert "--simulate" in argv
        assert "-v" in argv


class TestResolveErrorIsContained:
    def test_run_one_returns_one_when_resolve_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from resolve import ResolveError

        def boom(_url: str) -> str:
            raise ResolveError("no iframe found")

        monkeypatch.setattr(download, "resolve", boom)
        # subprocess.run must NOT be invoked — yt-dlp shouldn't run if we
        # can't even resolve. Inject a sentinel that would raise if called.
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(side_effect=AssertionError("yt-dlp must not be invoked")),
        )
        rc = run_one(UrlBlock(url="https://stream.x.com/20260101_a_t/"), [], None)
        err = capsys.readouterr().err
        assert rc == 1
        assert "[resolve] failed" in err
        assert "skipping" in err

    def test_run_batch_continues_after_resolve_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        # First URL: resolve fails. Second URL: resolves fine and downloads.
        # Without the fix, the first failure aborts the whole batch.
        from resolve import ResolveError

        good_url = "https://stream.x.com/20260102_b_t/"
        bad_url = "https://stream.x.com/20260101_a_t/"

        def maybe_resolve(url: str) -> str:
            if url == bad_url:
                raise ResolveError("nope")
            return url

        monkeypatch.setattr(download, "resolve", maybe_resolve)
        _fake_yt_dlp(
            monkeypatch,
            produces=[("CloudflareStream", "good_id", "Foo/good.mp4")],
        )
        monkeypatch.setattr("time.sleep", lambda _s: None)
        path = tmp_path / "urls.txt"
        path.write_text(f"{bad_url}\n{good_url}\n")
        rc = run_batch(path, [], None)
        # Aggregated rc is non-zero because the first URL failed, but the
        # second URL must have been processed.
        assert rc == 1
        assert (data_dir / "Foo" / "good.mp4").exists()


class TestParseBatchBomTolerance:
    def test_utf8_bom_does_not_eat_first_comment(self, tmp_path: Path) -> None:
        # Notepad-style UTF-8-BOM at file start. Without `utf-8-sig` the
        # first line would arrive as "﻿# title: ..." and silently fail
        # both the comment-detection ("#") and URL-detection ("http") checks.
        path = tmp_path / "urls.txt"
        path.write_bytes(b"\xef\xbb\xbf# title: BomFirst\nhttps://stream.x.com/20260101_a_t/\n")
        blocks = list(parse_batch(path))
        assert len(blocks) == 1
        assert blocks[0].meta == {"title": "BomFirst"}
        assert blocks[0].url == "https://stream.x.com/20260101_a_t/"

    def test_utf8_bom_in_front_of_url_line(self, tmp_path: Path) -> None:
        # The URL itself sits behind the BOM. Must still be recognised.
        path = tmp_path / "urls.txt"
        path.write_bytes(b"\xef\xbb\xbfhttps://stream.x.com/20260101_a_t/\n")
        blocks = list(parse_batch(path))
        assert len(blocks) == 1
        assert blocks[0].url == "https://stream.x.com/20260101_a_t/"


class TestRetest:
    def test_run_one_with_retest_root_writes_to_sandbox(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        _fake_yt_dlp(
            monkeypatch,
            produces=[("CloudflareStream", "abc123", "Foo/2026-01-01_bar.mp4")],
        )
        monkeypatch.setattr(download, "resolve", lambda u: u)
        sandbox = data_dir / ".retest" / "20260428_120000-aaaabbbb"
        rc = run_one(
            UrlBlock(url="https://stream.x.com/20260101_a_t/"),
            [],
            None,
            retest_root=sandbox,
        )
        assert rc == 0
        # Output landed in sandbox, not the canonical tree.
        assert (sandbox / "Foo" / "2026-01-01_bar.mp4").read_bytes() == b"fake-mp4-bytes"
        assert not (data_dir / "Foo").exists()
        # archive.txt must NOT have been created/updated.
        assert not (data_dir / "archive.txt").exists()

    def test_run_one_retest_seeds_empty_skip_archive(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        # Even with an existing archive.txt that already lists this URL's
        # video_id, retest mode must hand yt-dlp an empty download-archive
        # so the URL re-downloads instead of being skipped.
        (data_dir / "archive.txt").write_text("cloudflarestream abc123\n")
        seed_calls: list[publish.StagingRun] = []
        real_seed = publish.seed_skip_archive
        monkeypatch.setattr(
            publish,
            "seed_skip_archive",
            lambda run, source=None: seed_calls.append(run) or real_seed(run, source),
        )
        _fake_yt_dlp(
            monkeypatch,
            produces=[("CloudflareStream", "abc123", "Foo/x.mp4")],
        )
        monkeypatch.setattr(download, "resolve", lambda u: u)
        sandbox = data_dir / ".retest" / "ts1"
        run_one(
            UrlBlock(url="https://stream.x.com/20260101_a_t/"),
            [],
            None,
            retest_root=sandbox,
        )
        # seed_skip_archive must NOT have been called in retest mode.
        assert seed_calls == []

    def test_run_one_retest_publish_failure_message_mentions_sandbox(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Manifest line points at a non-existent file → atomic_publish raises.
        def fake(
            argv: list[str],
            check: bool = False,
            **_: object,
        ) -> MagicMock:
            paths_idx = argv.index("--paths")
            home = Path(argv[paths_idx + 1].removeprefix("home:"))
            manifest_idx = argv.index("--print-to-file")
            manifest = Path(argv[manifest_idx + 2])
            phantom = home / "phantom.mp4"
            manifest.write_text(
                f"CloudflareStream\tabc123\t{phantom}\n",
                encoding="utf-8",
            )
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", MagicMock(side_effect=fake))
        monkeypatch.setattr(download, "resolve", lambda u: u)
        sandbox = data_dir / ".retest" / "ts2"
        rc = run_one(
            UrlBlock(url="https://stream.x.com/20260101_a_t/"),
            [],
            None,
            retest_root=sandbox,
        )
        err = capsys.readouterr().err
        assert rc == 1
        # The failure message names the sandbox path, not /data.
        assert str(sandbox) in err

    def test_main_retest_creates_sandbox_root_and_routes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _fake_yt_dlp(
            monkeypatch,
            produces=[("CloudflareStream", "abc123", "Foo/x.mp4")],
        )
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = main(["--retest", "https://stream.x.com/20260101_a_t/"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[retest] output dir:" in out
        # Exactly one .retest/<ts>/ subdir was created with the file inside.
        retest_root = data_dir / ".retest"
        assert retest_root.is_dir()
        subs = list(retest_root.iterdir())
        assert len(subs) == 1
        assert (subs[0] / "Foo" / "x.mp4").read_bytes() == b"fake-mp4-bytes"
        # No archive.txt mutation.
        assert not (data_dir / "archive.txt").exists()

    def test_main_retest_propagates_to_batch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
    ) -> None:
        _fake_yt_dlp(
            monkeypatch,
            produces=[("CloudflareStream", "abc123", "Foo/x.mp4")],
        )
        monkeypatch.setattr(download, "resolve", lambda u: u)
        monkeypatch.setattr("time.sleep", lambda _s: None)
        path = tmp_path / "urls.txt"
        path.write_text("https://stream.x.com/20260101_a_t/\n")
        rc = main(["--retest", "--batch-file", str(path)])
        assert rc == 0
        retest_root = data_dir / ".retest"
        assert retest_root.is_dir()
        assert any((sub / "Foo" / "x.mp4").exists() for sub in retest_root.iterdir())
        assert not (data_dir / "archive.txt").exists()


class TestNewRetestRoot:
    def test_path_layout_includes_timestamp_and_random(self) -> None:
        import datetime

        from download import _new_retest_root

        moment = datetime.datetime(2026, 4, 28, 12, 34, 56, tzinfo=datetime.UTC)
        path = _new_retest_root(now=moment)
        # /data/.retest/<YYYYmmdd_HHMMSS>-<8 hex>
        assert path.parent.name == ".retest"
        assert path.name.startswith("20260428_123456-")
        assert len(path.name) == len("20260428_123456-") + 8

    def test_default_uses_current_time(self) -> None:
        from download import _new_retest_root

        path = _new_retest_root()
        # Just verify we get *some* sensible shape; clock value isn't asserted.
        assert path.parent.name == ".retest"
        assert "-" in path.name


class TestConstants:
    def test_meta_keys_are_what_emit_recognises(self) -> None:
        for k in META_KEYS:
            assert emit_meta_flags(k, "value")
        assert not emit_meta_flags("not-a-real-key", "value")


class TestDeriveDefaultsProperties:
    """Round-trip: a synthetic publisher URL must yield self-consistent metadata."""

    @given(
        handle=st.from_regex(r"^[a-z]{1,15}$", fullmatch=True),
        year=st.integers(min_value=2020, max_value=2030),
        month=st.integers(min_value=1, max_value=12),
        day=st.integers(min_value=1, max_value=28),
        slug=st.from_regex(r"^[A-Za-z0-9-]{1,20}$", fullmatch=True),
        token=st.from_regex(r"^[a-z0-9]{8,40}$", fullmatch=True),
    )
    def test_round_trip_full_metadata(
        self,
        handle: str,
        year: int,
        month: int,
        day: int,
        slug: str,
        token: str,
    ) -> None:
        url = f"https://stream.{handle}.com/{year:04d}{month:02d}{day:02d}_{slug}_{token}/"
        meta = derive_defaults(url)
        assert meta["uploader"] == handle
        assert meta["date"] == f"{year:04d}-{month:02d}-{day:02d}"
        assert meta["title"] == slug
        assert meta["post"] == f"https://stream.{handle}.com/{year:04d}{month:02d}{day:02d}_{slug}/"


class TestEmitMetaFlagsProperties:
    @given(value=st.text(min_size=1, max_size=80).filter(lambda v: v.strip()))
    def test_known_keys_always_emit_pairs(self, value: str) -> None:
        # Property: any non-empty value to a known key produces a flag list
        # whose `--parse-metadata` count is exactly 1 (uploader/date) or 2
        # (title/post). Unknown keys produce nothing.
        for key in ("uploader", "date"):
            flags = emit_meta_flags(key, value)
            assert flags.count("--parse-metadata") == 1
        for key in ("title", "post"):
            flags = emit_meta_flags(key, value)
            assert flags.count("--parse-metadata") == 2

    @given(value=st.text(min_size=1, max_size=80))
    def test_colons_in_value_are_always_escaped(self, value: str) -> None:
        # Property: the only `:` characters in the emitted FROM/TO strings
        # that aren't preceded by `\` are the *separator* colons between
        # FROM and TO clauses (`:= ` literal). Every colon embedded in the
        # user value must be backslash-escaped so yt-dlp's regex-style
        # parser doesn't split on it.
        flags = emit_meta_flags("title", value)
        for f in flags:
            if not f.startswith("= "):
                continue  # skip --parse-metadata flag itself
            # Each FROM/TO pair has exactly one ":= " separator.
            sep_count = f.count(":= ")
            assert sep_count == 1, f"unexpected separator count in {f!r}"
            # Every colon other than the separator must be backslash-escaped.
            sep_idx = f.index(":= ")
            for i, ch in enumerate(f):
                if ch == ":" and i != sep_idx:
                    assert i > 0 and f[i - 1] == "\\", f"unescaped colon at {i} in {f!r}"


class TestParseBatchProperties:
    @given(
        urls=st.lists(
            st.from_regex(
                r"^https://stream\.[a-z]{1,10}\.com/\d{8}_[a-z]{1,10}_[a-z0-9]{8,20}/$",
                fullmatch=True,
            ),
            min_size=0,
            max_size=10,
        ),
        with_meta=st.booleans(),
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
    def test_block_count_matches_url_count(
        self, urls: list[str], with_meta: bool, tmp_path: Path
    ) -> None:
        # Property: parse_batch yields exactly one block per URL line,
        # regardless of how many comment lines we sprinkle in front.
        path = tmp_path / "urls.txt"
        # Hypothesis re-uses tmp_path across examples within one test —
        # rewrite from scratch each example so prior content can't leak.
        body_parts: list[str] = []
        for i, url in enumerate(urls):
            if with_meta:
                body_parts.append(f"# title: t{i}\n# uploader: u{i}\n")
            body_parts.append(url + "\n")
            body_parts.append("\n")  # blank line is OK
        path.write_text("".join(body_parts), encoding="utf-8")
        blocks = list(parse_batch(path))
        assert len(blocks) == len(urls)
        for block, expected_url in zip(blocks, urls, strict=True):
            assert block.url == expected_url


class TestRetestPlusSimulate:
    def test_retest_with_simulate_extra_does_nothing_to_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
        staging_root: Path,
        data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # `pa.cmd retest URL --simulate` is a legal combination: yt-dlp
        # gets --simulate (no real download), retest mode means archive
        # is bypassed and we'd publish into a sandbox. With --simulate
        # yt-dlp emits no manifest rows, so nothing is published, no
        # sandbox dir is created, no archive line is written.
        def fake(
            argv: list[str],
            check: bool = False,
            **_: object,
        ) -> MagicMock:
            assert "--simulate" in argv  # forwarded by `pa simulate` recipe path
            # Manifest stays empty — that's the whole point of --simulate.
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", MagicMock(side_effect=fake))
        monkeypatch.setattr(download, "resolve", lambda u: u)
        rc = main(["--retest", "--simulate", "https://stream.x.com/20260101_a_t/"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[retest] output dir:" in out
        # No archive.txt mutation, no .retest sandbox materialised.
        assert not (data_dir / "archive.txt").exists()
        retest_root = data_dir / ".retest"
        # The root dir might be created lazily by atomic_publish, but with
        # an empty manifest publish_outputs is a no-op so nothing exists.
        if retest_root.exists():
            for sub in retest_root.iterdir():
                # Only the run-stamped dir might exist if Python ever
                # mkdir'd it eagerly; in any case, no .mp4 should be inside.
                assert not list(sub.rglob("*.mp4"))


class TestNewRetestRootUniqueness:
    def test_consecutive_calls_return_distinct_paths(self) -> None:
        # Token randomness should keep two same-second calls non-colliding.
        from download import _new_retest_root

        # 1000 draws should not produce a duplicate (8 hex chars + same TS = 16^8 ~ 4B).
        seen = {_new_retest_root() for _ in range(1000)}
        assert len(seen) == 1000
