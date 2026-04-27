# syntax=docker/dockerfile:1.9
#
# Multi-stage build:
#   1. uv      — borrow the pinned Astral uv binary
#   2. runtime — slim Python + ffmpeg + yt-dlp installed via uv

ARG UV_VERSION=0.11.7
ARG JUST_VERSION=1.50.0
ARG PYTHON_IMAGE=python:3.13-slim-bookworm

# ===== Stage 1: pinned uv binary =====
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ===== Stage 2: pinned just binary =====
FROM debian:bookworm-slim AS just
ARG JUST_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL "https://github.com/casey/just/releases/download/${JUST_VERSION}/just-${JUST_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
    | tar -xz -C /usr/local/bin just

# ===== Stage 3: runtime =====
FROM ${PYTHON_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_DEV=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# ffmpeg is required for HLS muxing/remux. tini gives clean SIGINT delivery
# so a yt-dlp run can be Ctrl-C'd mid-fragment and resumed via --continue.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=just /usr/local/bin/just /usr/local/bin/just

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --create-home --shell /bin/bash app \
 && mkdir -p /downloads /secrets /work /opt/venv \
 && chown -R app:app /downloads /secrets /work /opt/venv

WORKDIR /work

# Layer 1: dependencies only.
# pyproject.toml + uv.lock arrive via bind mounts so an unrelated source
# change does not bust the dep cache. uv populates /opt/venv (set via
# UV_PROJECT_ENVIRONMENT) which is later chowned to the runtime user.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    --mount=type=bind,source=uv.lock,target=/work/uv.lock \
    --mount=type=bind,source=pyproject.toml,target=/work/pyproject.toml \
    uv sync --locked --no-install-project --no-dev \
 && chown -R app:app /opt/venv

# Layer 2: project files. Source is small, so a single COPY is fine.
COPY --chown=app:app pyproject.toml uv.lock justfile ./
COPY --chown=app:app config/ ./config/
COPY --chown=app:app scripts/ ./scripts/
RUN chmod +x ./scripts/*.sh

USER app

# `just` is the in-container dispatcher. Subcommands become recipes; the
# default recipe lists everything available (so `pa` with no args = help).
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/just"]
CMD []
