"""Tests for ``scripts/inventory.py``."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakePost, write_mhtml
from inventory import (
    STREAM_URL_RE,
    VIDEO_LEN_RE,
    load_seen,
    main,
    meta_block,
    page_uploader,
    post_url,
    read_html_from_mhtml,
    stream_urls,
    text_of,
    url_block,
    video_length,
)


class TestReadMhtml:
    def test_reads_html_from_valid_mhtml(self, sample_mhtml: Path) -> None:
        html = read_html_from_mhtml(sample_mhtml)
        assert "<html" in html.lower()
        assert "Newest video post" in html

    def test_raises_on_no_html_part(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.mhtml"
        bad.write_bytes(b"From: x\nSubject: x\nContent-Type: text/plain\n\njust text\n")
        with pytest.raises(SystemExit, match="no text/html part found"):
            read_html_from_mhtml(bad)

    def test_text_html_part_with_non_bytes_payload_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defensive narrowing branch: if get_payload(decode=True) returns
        a non-bytes object (e.g. None for a malformed multipart text/html
        container), we should fall through to the next part / raise.
        """
        from email.message import EmailMessage
        from unittest.mock import MagicMock

        import inventory

        # Build a Message walk yielding two parts: the first claims to be
        # text/html but returns None for decoded payload; the second is
        # absent → SystemExit.
        bogus = MagicMock(spec=EmailMessage)
        bogus.get_content_type.return_value = "text/html"
        bogus.get_payload.return_value = None
        outer = MagicMock(spec=EmailMessage)
        outer.walk.return_value = [bogus]
        monkeypatch.setattr(
            inventory, "email", MagicMock(message_from_binary_file=lambda _f: outer)
        )
        bad = tmp_path / "synthetic.mhtml"
        bad.write_bytes(b"")
        with pytest.raises(SystemExit, match="no text/html part found"):
            read_html_from_mhtml(bad)


class TestParserHelpers:
    def test_text_of_handles_none(self) -> None:
        assert text_of(None) == ""

    def test_video_length_finds_japanese_marker(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            '<div data-tag="post-card">'
            '<div data-tag="post-content-content">🔴Video length 1:23:45 ...</div>'
            "</div>",
            "html.parser",
        )
        card = soup.div
        assert card is not None
        assert video_length(card) == "1:23:45"

    def test_video_length_returns_none_when_no_match(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            '<div data-tag="post-card">'
            '<div data-tag="post-content-content">no length info</div>'
            "</div>",
            "html.parser",
        )
        card = soup.div
        assert card is not None
        assert video_length(card) is None

    def test_video_length_returns_none_when_no_body(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup('<div data-tag="post-card"></div>', "html.parser")
        card = soup.div
        assert card is not None
        assert video_length(card) is None

    def test_video_length_falls_back_to_post_content(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            '<div data-tag="post-card">'
            '<div data-tag="post-content">🔴Video length 0:30 fallback</div>'
            "</div>",
            "html.parser",
        )
        card = soup.div
        assert card is not None
        assert video_length(card) == "0:30"


class TestPostUrl:
    def test_relative_url_is_absolutized(self) -> None:
        from bs4 import BeautifulSoup

        html = '<div data-tag="post-card"><a data-tag="post-title" href="/posts/foo-1">x</a></div>'
        soup = BeautifulSoup(html, "html.parser")
        card = soup.div
        assert card is not None
        assert post_url(card) == "https://www.patreon.com/posts/foo-1"

    def test_absolute_url_is_kept(self) -> None:
        from bs4 import BeautifulSoup

        html = (
            '<div data-tag="post-card"><a data-tag="post-title" '
            'href="https://www.patreon.com/posts/foo-1">x</a></div>'
        )
        soup = BeautifulSoup(html, "html.parser")
        card = soup.div
        assert card is not None
        assert post_url(card) == "https://www.patreon.com/posts/foo-1"

    def test_returns_none_when_no_post_link(self) -> None:
        from bs4 import BeautifulSoup

        html = '<div data-tag="post-card"><a href="/about">x</a></div>'
        soup = BeautifulSoup(html, "html.parser")
        card = soup.div
        assert card is not None
        assert post_url(card) is None


class TestStreamUrls:
    def test_extracts_stream_urls_in_order(self) -> None:
        from bs4 import BeautifulSoup

        html = (
            '<div data-tag="post-card">'
            '<a href="https://stream.example.com/20260423_a_t123/">x</a>'
            '<a href="https://stream.example.com/20260424_b_t456/">y</a>'
            '<a href="https://stream.example.com/20260423_a_t123/">duplicate</a>'
            '<a href="https://other.example.com/path">non-stream</a>'
            "</div>"
        )
        soup = BeautifulSoup(html, "html.parser")
        card = soup.div
        assert card is not None
        urls = stream_urls(card)
        assert urls == [
            "https://stream.example.com/20260423_a_t123/",
            "https://stream.example.com/20260424_b_t456/",
        ]

    def test_skips_anchors_with_non_string_href(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If BS4 ever returns a non-string href (multi-valued attr corner case),
        the helper returns None and we drop the anchor from the result instead
        of crashing on a downstream regex.
        """
        from bs4 import BeautifulSoup

        import inventory

        html = (
            '<div data-tag="post-card">'
            '<a href="https://stream.example.com/20260423_a_t/">x</a>'
            "</div>"
        )
        soup = BeautifulSoup(html, "html.parser")
        card = soup.div
        assert card is not None
        monkeypatch.setattr(inventory, "_href_of", lambda _a: None)
        assert stream_urls(card) == []


class TestMainAllVideoDates:
    def test_skips_streams_that_dont_match_url_re(
        self,
        sample_mhtml: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Defensive branch in main(): if ``stream_urls()`` ever returned a URL
        that doesn't satisfy STREAM_URL_RE (shouldn't happen — the function
        already filters by the same regex — but main re-checks as a safety
        net for future refactors), the date harvest must skip it cleanly.
        """
        import sys

        import inventory

        monkeypatch.setattr(
            inventory, "stream_urls", lambda _card: ["bogus://not-matching-stream-url-re"]
        )
        monkeypatch.setattr(sys, "argv", ["inventory.py", str(sample_mhtml)])
        assert inventory.main() == 0
        # No mhtml_date_range emit because all stream URLs were skipped.
        assert "mhtml_date_range" not in capsys.readouterr().err


class TestMainStrictDateValidation:
    """`mhtml_date_range` line must never carry a malformed ISO date.

    The regex `\\d{8}` only checks shape — `99999999` or `20260230` (Feb 30)
    slip through. Once such a string lands in `mhtml_date_range`, sync's
    `parse_date_range` returns it verbatim and `evaluate_anchor` does
    string-comparison gap detection on garbage — at that point coverage
    can advance to a date that no real post will ever match, and gap
    warnings start firing for the wrong reason.
    """

    def test_invalid_calendar_date_in_url_is_dropped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import sys

        import inventory

        # Stream URL whose 8-digit prefix is structurally valid but represents
        # Feb 30. inventory must skip it from the date_range aggregate while
        # still emitting it in the body (the URL itself is fine to download).
        bogus_url = "https://stream.example.com/20260230_slug_tok123/"
        good_url = "https://stream.example.com/20260423_slug_tok123/"
        monkeypatch.setattr(inventory, "stream_urls", lambda _card: [bogus_url, good_url])
        # Use any valid sample MHTML — we only care about the date harvest.
        from conftest import FakePost, write_mhtml

        sample = write_mhtml(
            tmp_path / "x.mhtml",
            [
                FakePost(
                    title="Anything",
                    post_path="/posts/x",
                    date_yyyymmdd="20260423",
                    slug="slug",
                ),
            ],
        )
        monkeypatch.setattr(sys, "argv", ["inventory.py", str(sample), "--minimal"])
        assert inventory.main() == 0
        err = capsys.readouterr().err
        # Only the valid date appears in the date range, and the count
        # reflects only the valid URLs counted.
        assert "2026-02-30" not in err  # bogus dropped
        assert "2026-04-23" in err  # good kept
        # The aggregate must mention exactly one valid video post (the good
        # URL across all cards). At minimum, it must not be 0.
        assert "0 video posts" not in err

    def test_all_invalid_dates_yields_no_date_range_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import sys

        import inventory

        bogus_only = "https://stream.example.com/99999999_slug_tok/"
        monkeypatch.setattr(inventory, "stream_urls", lambda _card: [bogus_only])
        from conftest import FakePost, write_mhtml

        sample = write_mhtml(
            tmp_path / "x.mhtml",
            [FakePost(title="x", post_path="/posts/x", date_yyyymmdd=None, slug=None)],
        )
        monkeypatch.setattr(sys, "argv", ["inventory.py", str(sample), "--minimal"])
        assert inventory.main() == 0
        # All dates were bogus → no `mhtml_date_range` line at all.
        assert "mhtml_date_range" not in capsys.readouterr().err


class TestPageUploader:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Some Creator | creating things | Patreon", "Some Creator"),
            ("Foo Official | art | Patreon", "Foo"),
            ("Bare", "Bare"),
            ("", "Unknown"),
        ],
    )
    def test_extracts_uploader_from_title(self, title: str, expected: str) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(f"<html><title>{title}</title></html>", "html.parser")
        assert page_uploader(soup) == expected

    def test_no_title_tag_yields_unknown(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body></body></html>", "html.parser")
        assert page_uploader(soup) == "Unknown"


class TestBlocks:
    def test_meta_block_includes_all_optional_fields(self) -> None:
        block = meta_block(
            stream="https://stream.example.com/20260101_slug_tok/",
            title="My Title",
            uploader="Me",
            post="https://www.patreon.com/posts/foo",
        )
        assert "# title: My Title" in block
        assert "# uploader: Me" in block
        assert "# date: 2026-01-01" in block
        assert "# post: https://www.patreon.com/posts/foo" in block

    def test_meta_block_omits_uploader_post_when_empty(self) -> None:
        block = meta_block(stream="https://stream.example.com/x", title="T", uploader="", post="")
        assert "# uploader" not in "\n".join(block)
        assert "# post" not in "\n".join(block)

    def test_url_block_wraps_in_fence(self) -> None:
        block = url_block(
            stream="https://stream.example.com/20260101_a_t/",
            title="T",
            uploader="U",
            post="",
        )
        assert block[0] == "```text"
        assert block[-1] == "```"


class TestLoadSeen:
    def test_returns_empty_set_when_file_missing(self, tmp_path: Path) -> None:
        assert load_seen(tmp_path / "absent.txt") == set()

    def test_strips_blank_and_comment_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "seen.txt"
        path.write_text(
            "# comment\nhttps://www.patreon.com/posts/a\n\nhttps://www.patreon.com/posts/b\n"
        )
        seen = load_seen(path)
        assert seen == {"https://www.patreon.com/posts/a", "https://www.patreon.com/posts/b"}


class TestMain:
    def test_full_inventory_render(
        self,
        sample_mhtml: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["inventory.py", str(sample_mhtml)])
        assert main() == 0
        captured = capsys.readouterr()
        assert "Example Creator | Patreon" in captured.out
        assert "Newest video post" in captured.out
        assert "[inventory] mhtml_date_range:" in captured.err

    def test_minimal_emits_only_blocks(
        self,
        sample_mhtml: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["inventory.py", str(sample_mhtml), "--minimal"])
        assert main() == 0
        captured = capsys.readouterr()
        assert "## " not in captured.out  # no markdown headers
        assert "# title: Newest video post" in captured.out
        assert "# date: 2026-04-27" in captured.out

    def test_seen_file_filters_known_posts(
        self,
        sample_mhtml: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = tmp_path / "seen.txt"
        seen.write_text("https://www.patreon.com/posts/newest-1001\n")
        monkeypatch.setattr(
            "sys.argv",
            ["inventory.py", str(sample_mhtml), "--seen-file", str(seen), "--minimal"],
        )
        assert main() == 0
        out = capsys.readouterr().out
        assert "Newest video post" not in out
        assert "Mid video post" in out

    def test_empty_mhtml_returns_error(
        self,
        empty_mhtml: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["inventory.py", str(empty_mhtml)])
        assert main() == 1
        assert "no post-card elements found" in capsys.readouterr().err

    def test_seen_file_filter_nonminimal_announcement_in_header(
        self,
        sample_mhtml: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = tmp_path / "seen.txt"
        seen.write_text("https://www.patreon.com/posts/newest-1001\n")
        monkeypatch.setattr(
            "sys.argv", ["inventory.py", str(sample_mhtml), "--seen-file", str(seen)]
        )
        assert main() == 0
        out = capsys.readouterr().out
        assert "already in" in out

    def test_no_video_dates_no_stderr_range(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MHTML with one post-card but zero video links → no date_range emit.
        mhtml = write_mhtml(
            tmp_path / "novids.mhtml",
            [
                FakePost(
                    title="Just text",
                    post_path="/posts/text-1",
                    date_yyyymmdd=None,
                    slug=None,
                    length=None,
                ),
            ],
        )
        monkeypatch.setattr("sys.argv", ["inventory.py", str(mhtml)])
        assert main() == 0
        assert "mhtml_date_range" not in capsys.readouterr().err


class TestMainAutoDetectsMhtml:
    def test_picks_newest_when_no_arg_given(
        self,
        sample_mhtml: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import _mhtml

        mhtml_dir = tmp_path / "mhtml-input"
        mhtml_dir.mkdir()
        # Move sample_mhtml into the auto-detect dir.
        target = mhtml_dir / "snap.mhtml"
        target.write_bytes(sample_mhtml.read_bytes())
        monkeypatch.setattr(_mhtml, "MHTML_DIR", mhtml_dir)
        monkeypatch.setattr("sys.argv", ["inventory.py"])
        assert main() == 0
        assert "Newest video post" in capsys.readouterr().out

    def test_empty_arg_falls_through_to_auto_detect(
        self,
        sample_mhtml: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import _mhtml

        mhtml_dir = tmp_path / "mhtml-input"
        mhtml_dir.mkdir()
        target = mhtml_dir / "snap.mhtml"
        target.write_bytes(sample_mhtml.read_bytes())
        monkeypatch.setattr(_mhtml, "MHTML_DIR", mhtml_dir)
        # justfile {{MHTML}} expansion injects an empty positional when unset.
        monkeypatch.setattr("sys.argv", ["inventory.py", ""])
        assert main() == 0
        assert "Newest video post" in capsys.readouterr().out


class TestRegexes:
    def test_video_length_re_matches_japanese_marker(self) -> None:
        assert VIDEO_LEN_RE.search("動画の長さ 12:34")
        assert VIDEO_LEN_RE.search("Video length 1:23:45")
        assert VIDEO_LEN_RE.search("🔴 9:05")

    def test_stream_url_re_captures_groups(self) -> None:
        m = STREAM_URL_RE.match("https://stream.example.com/20260101_slug_token/")
        assert m is not None
        assert m.group("date") == "20260101"
        assert m.group("slug") == "slug"
