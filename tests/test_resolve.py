"""Tests for ``scripts/resolve.py``.

Network code is mocked at ``urllib.request.urlopen``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import resolve
from resolve import (
    CF_IFRAME_RE,
    PASSTHROUGH_PATTERN,
    PASSTHROUGH_PREFIXES,
    ResolveError,
    fetch,
    find_iframe,
    is_passthrough,
)
from resolve import main as resolve_main
from resolve import resolve as resolve_url


class TestPassthrough:
    @pytest.mark.parametrize(
        "url",
        [
            "https://iframe.videodelivery.net/abc123",
            "https://iframe.videodelivery.net/eyJ0eXAiOiJKV1Q.signature",
            "https://watch.videodelivery.net/uid42",
            "https://customer-x9z.cloudflarestream.com/uid42/manifest.mpd",
        ],
    )
    def test_known_cf_url_is_passthrough(self, url: str) -> None:
        assert is_passthrough(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://stream.example.com/20260423_slug_token/",
            "http://stream.example.com/20260423_slug_token/",
            "https://random.example.com/path",
            "https://customer.cloudflarestream.com/wrong-shape/",
        ],
    )
    def test_non_cf_url_is_not_passthrough(self, url: str) -> None:
        assert is_passthrough(url) is False


class TestFindIframe:
    def test_finds_iframe_via_bs4(self) -> None:
        html = (
            '<html><body><iframe src="https://iframe.videodelivery.net/UID42">'
            "</iframe></body></html>"
        )
        assert find_iframe(html) == "https://iframe.videodelivery.net/UID42"

    def test_finds_iframe_via_regex_fallback(self) -> None:
        # No <iframe> tag — only the URL appears in a script blob.
        html = (
            '<html><body><script>var x = "https://iframe.videodelivery.net/'
            'UID77"</script></body></html>'
        )
        assert find_iframe(html) == "https://iframe.videodelivery.net/UID77"

    def test_skips_iframes_with_other_src(self) -> None:
        html = '<html><body><iframe src="https://other.example.com/foo"></iframe></body></html>'
        assert find_iframe(html) is None

    def test_returns_none_when_nothing_matches(self) -> None:
        assert find_iframe("<html><body>nothing here</body></html>") is None

    def test_skips_iframe_without_string_src(self) -> None:
        # Pathological case: src attr present but not string-shaped
        # (BS4 sometimes returns multi-valued attrs as a list).
        html = '<html><body><iframe src="https://other.example.com/x"></iframe></body></html>'
        assert find_iframe(html) is None


class TestFetch:
    def test_fetch_decodes_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = b"<html>hi</html>"
        response = MagicMock()
        response.read.return_value = body
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: response)
        assert fetch("https://example.com") == "<html>hi</html>"


class TestResolve:
    def test_passthrough_returns_input(self) -> None:
        url = "https://iframe.videodelivery.net/abc"
        assert resolve_url(url) == url

    def test_resolution_returns_iframe_src(self, monkeypatch: pytest.MonkeyPatch) -> None:
        html = '<iframe src="https://iframe.videodelivery.net/UID-X">'
        monkeypatch.setattr(resolve, "fetch", lambda _url: html)
        assert (
            resolve_url("https://stream.example.com/x") == "https://iframe.videodelivery.net/UID-X"
        )

    def test_no_iframe_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(resolve, "fetch", lambda _url: "<html>nope</html>")
        with pytest.raises(ResolveError, match="no Cloudflare Stream iframe"):
            resolve_url("https://stream.example.com/x")

    def test_fetch_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib.error import URLError

        def boom(_url: str) -> str:
            raise URLError("connection refused")

        monkeypatch.setattr(resolve, "fetch", boom)
        with pytest.raises(ResolveError, match="fetch failed"):
            resolve_url("https://stream.example.com/x")


class TestMain:
    def test_main_prints_resolved(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(resolve, "resolve", lambda _u: "https://iframe.videodelivery.net/X")
        rc = resolve_main(["https://stream.example.com/y"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "https://iframe.videodelivery.net/X"

    def test_main_reports_error_on_resolve_failure(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_u: str) -> str:
            raise ResolveError("nothing here")

        monkeypatch.setattr(resolve, "resolve", boom)
        rc = resolve_main(["https://stream.example.com/y"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "resolve: nothing here" in captured.err


class TestRegexes:
    def test_cf_iframe_re_matches_known_shape(self) -> None:
        assert CF_IFRAME_RE.search("https://iframe.videodelivery.net/abc.123_-XYZ")

    def test_passthrough_pattern_only_matches_customer_subdomain(self) -> None:
        assert PASSTHROUGH_PATTERN.match("https://customer-abc.cloudflarestream.com/x")
        assert not PASSTHROUGH_PATTERN.match("https://other.cloudflarestream.com/x")

    def test_passthrough_prefixes_complete(self) -> None:
        assert "https://iframe.videodelivery.net/" in PASSTHROUGH_PREFIXES
        assert "https://watch.videodelivery.net/" in PASSTHROUGH_PREFIXES
