"""Shared test fixtures.

The MHTML builder generates a tiny in-memory snapshot that ``inventory.py``
can parse, with configurable post counts / metadata. All fixtures avoid
touching the network or any host paths the real wrapper relies on (``/in``,
``/state``, ``/downloads`` are simulated via ``tmp_path``).
"""

from __future__ import annotations

import email.message
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


@dataclass(frozen=True)
class FakePost:
    title: str
    post_path: str
    date_yyyymmdd: str | None  # e.g. "20260423"; None for non-video posts
    slug: str | None  # e.g. "video-slug-001"
    host: str = "stream.example.com"
    length: str | None = "16:00"
    published_label: str | None = "2日前"  # rendered into post-published-at span


def _post_card_html(post: FakePost) -> str:
    """Render one minimal Patreon post-card the inventory parser understands."""
    parts: list[str] = ['<div data-tag="post-card">']
    if post.post_path:
        parts.append(f'<a data-tag="post-title" href="{post.post_path}">{post.title}</a>')
    else:
        parts.append(f'<span data-tag="post-title">{post.title}</span>')
    if post.published_label:
        parts.append(f'<span data-tag="post-published-at">{post.published_label}</span>')
    if post.length:
        parts.append(
            f'<div data-tag="post-content-content">🔴Video length {post.length} sample body</div>'
        )
    if post.date_yyyymmdd and post.slug:
        url = f"https://{post.host}/{post.date_yyyymmdd}_{post.slug}_tok123abc/"
        parts.append(f'<a href="{url}">stream</a>')
    parts.append("</div>")
    return "".join(parts)


def build_mhtml_html(posts: list[FakePost], page_title: str = "Example Creator | Patreon") -> str:
    body_parts = "".join(_post_card_html(p) for p in posts)
    return (
        f"<!DOCTYPE html><html><head><title>{page_title}</title></head>"
        f"<body>{body_parts}</body></html>"
    )


def write_mhtml(path: Path, posts: list[FakePost], **kwargs: Any) -> Path:
    html = build_mhtml_html(posts, **kwargs)
    msg = email.message.EmailMessage()
    msg["MIME-Version"] = "1.0"
    msg["Subject"] = "test"
    msg.add_attachment(
        html.encode("utf-8"),
        maintype="text",
        subtype="html",
        filename="index.html",
    )
    path.write_bytes(bytes(msg))
    return path


@pytest.fixture
def fake_posts() -> list[FakePost]:
    return [
        FakePost(
            title="Newest video post",
            post_path="/posts/newest-1001",
            date_yyyymmdd="20260427",
            slug="moa001",
        ),
        FakePost(
            title="Mid video post",
            post_path="/posts/mid-1002",
            date_yyyymmdd="20260415",
            slug="moa002",
            length="44:41",
        ),
        FakePost(
            title="Oldest video post",
            post_path="/posts/oldest-1003",
            date_yyyymmdd="20260301",
            slug="moa003",
            length=None,  # no video-length annotation
        ),
        FakePost(
            title="Non-video announcement",
            post_path="/posts/announce-1004",
            date_yyyymmdd=None,
            slug=None,
            length=None,
        ),
        # Bare card — exercises the False side of `if date:` / `if url:` /
        # `if meta:` branches in render_post.
        FakePost(
            title="Bare orphan",
            post_path="",  # post_url() will return None (no /posts/ link)
            date_yyyymmdd=None,
            slug=None,
            length=None,
            published_label=None,
        ),
    ]


@pytest.fixture
def sample_mhtml(tmp_path: Path, fake_posts: list[FakePost]) -> Path:
    return write_mhtml(tmp_path / "sample.mhtml", fake_posts)


@pytest.fixture
def empty_mhtml(tmp_path: Path) -> Path:
    return write_mhtml(tmp_path / "empty.mhtml", [])


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect sync.py's STATE_DIR / SCRIPTS_DIR onto a tmp path.

    Tests can write into and read out of ``state_dir`` directly to assert
    on seen_posts.txt / coverage.txt / urls.txt behaviour.
    """
    import sync

    sd = tmp_path / "state"
    sd.mkdir()
    monkeypatch.setattr(sync, "STATE_DIR", sd)
    monkeypatch.setattr(sync, "SEEN_FILE", sd / "seen_posts.txt")
    monkeypatch.setattr(sync, "COVERAGE_FILE", sd / "coverage.txt")
    monkeypatch.setattr(sync, "URLS_FILE", sd / "urls.txt")
    return sd


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """Replace ``subprocess.run`` everywhere it's used (sync + download)."""
    mock = MagicMock()
    monkeypatch.setattr("subprocess.run", mock)
    yield mock
