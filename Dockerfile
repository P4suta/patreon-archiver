# syntax=docker/dockerfile:1.9
#
# Multi-stage build:
#   1. uv      — borrow the pinned Astral uv binary
#   2. just    — borrow the pinned casey/just binary
#   3. runtime — slim Python + ffmpeg + yt-dlp installed via uv (default leaf
#                for production; pa / pa.win / build.yml all use --target runtime)
#   4. dev     — runtime + ruff/pyright/pytest/lefthook/typos/hadolint/shellcheck
#                for .devcontainer/ use; built only with --target dev

ARG UV_VERSION=0.11.8
ARG JUST_VERSION=1.50.0
ARG PYTHON_IMAGE=python:3.13-slim-bookworm

# ===== Stage 1: pinned uv binary =====
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ===== Stage 2: pinned just binary =====
FROM debian:bookworm-slim AS just
ARG JUST_VERSION
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
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
 && mkdir -p /data /secrets /work /opt/venv /var/lib/pa/staging \
 && chown -R app:app /data /secrets /work /opt/venv /var/lib/pa

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

# ===== Stage 4: dev (devcontainer target) =====
# Inherits the runtime toolchain (uv, just, ffmpeg, tini, python) and adds
# the host-side dev kit. The dev venv is **not** baked into the image —
# devcontainer.json runs `uv sync --group dev` post-create against the
# bind-mounted workspace, which puts .venv under host-owned files so the
# UID-remapped dev user owns it without a chown dance.
#
# Build only this target for devcontainer use:
#   docker build --target dev -t patreon-archiver:dev .
FROM runtime AS dev

ARG LEFTHOOK_VERSION=2.1.6
ARG TYPOS_VERSION=1.45.2
ARG HADOLINT_VERSION=2.14.0

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Host-side dev OS deps. git for vcs, shellcheck for the pre-commit hook,
# sudo so the remoteUser can install ad-hoc packages, less/vim-tiny for
# basic terminal ergonomics inside the container, libatomic1 for the
# prebuilt node that pyright-python downloads on first invocation.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        less \
        libatomic1 \
        shellcheck \
        sudo \
        vim-tiny \
    && rm -rf /var/lib/apt/lists/*

# Pinned static binaries for tools lefthook/typos/hadolint expect on PATH.
# Versions are tracked by Renovate via custom regex managers in renovate.json.
RUN curl -fsSL "https://github.com/evilmartians/lefthook/releases/download/v${LEFTHOOK_VERSION}/lefthook_${LEFTHOOK_VERSION}_Linux_x86_64.gz" \
        | gunzip > /usr/local/bin/lefthook \
 && chmod +x /usr/local/bin/lefthook \
 && curl -fsSL "https://github.com/crate-ci/typos/releases/download/v${TYPOS_VERSION}/typos-v${TYPOS_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
        | tar -xz -C /usr/local/bin ./typos \
 && curl -fsSL -o /usr/local/bin/hadolint \
        "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT_VERSION}/hadolint-Linux-x86_64" \
 && chmod +x /usr/local/bin/hadolint

# Passwordless sudo for the dev user — devcontainer convenience only.
RUN echo 'app ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/app \
 && chmod 0440 /etc/sudoers.d/app

# Override runtime defaults: re-enable dev group on `uv sync` and point
# UV_PROJECT_ENVIRONMENT at the workspace mount so the venv inherits host
# UID ownership. Prepend the workspace .venv to PATH so post-sync tools
# (ruff/pyright/pytest/yt-dlp) win over /opt/venv's runtime-only copy.
ENV UV_NO_DEV=0 \
    UV_PROJECT_ENVIRONMENT=/workspaces/patreon-archiver/.venv \
    PATH="/workspaces/patreon-archiver/.venv/bin:${PATH}"

# Devcontainer keeps the container alive itself; the runtime ENTRYPOINT
# (tini -- just) would refuse to exec `sleep infinity` cleanly.
ENTRYPOINT []
CMD ["sleep", "infinity"]

USER app
WORKDIR /workspaces/patreon-archiver
