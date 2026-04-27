set shell := ["bash", "-cu"]
set positional-arguments := true
set dotenv-load := true

compose := "docker compose"
uv_image := "ghcr.io/astral-sh/uv:python3.13-bookworm-slim"
hadolint_image := "hadolint/hadolint:v2.14.0"
shellcheck_image := "koalaman/shellcheck:v0.11.0"
yamllint_image := "cytopia/yamllint:1"
gitleaks_image := "zricethezav/gitleaks:v8.30.1"

# Show available recipes.
default:
    @just --list

# ---------- build / lifecycle ----------

# Build the image. Re-run after Dockerfile or pyproject.toml changes.
build:
    {{compose}} build

# Bump yt-dlp / base layers without using any cache.
upgrade:
    {{compose}} build --no-cache --pull

# Re-resolve dependencies into uv.lock. Run after editing pyproject.toml.
relock:
    docker run --rm \
      -v "$(pwd)":/work -w /work \
      -u "$(id -u):$(id -g)" \
      -e UV_CACHE_DIR=/tmp/uv-cache --tmpfs /tmp \
      {{uv_image}} uv lock

# ---------- run ----------

# Download a single URL. Cookies-free path; works for presigned/token URLs.
download *URL:
    {{compose}} run --rm archiver "$@"

# Download a single URL with the host cookies.txt mounted read-only.
download-cookies *URL:
    {{compose}} --profile cookies run --rm archiver-with-cookies "$@"

# Batch-download every URL in urls/urls.txt. Each URL may be preceded by
# `# key: value` metadata lines (see urls/urls.txt.example). The wrapper
# sleeps a random duration in [YTDLP_BATCH_SLEEP_MIN, YTDLP_BATCH_SLEEP_MAX]
# seconds between URLs (env vars come from .env via compose).
batch:
    {{compose}} run --rm archiver --batch-file /work/urls/urls.txt

# Drop into an interactive shell inside the image.
shell:
    {{compose}} --profile dev run --rm shell

# ---------- ops ----------

# Print the bundled yt-dlp version.
version:
    {{compose}} run --rm --entrypoint yt-dlp archiver --version

# Resolve a publisher page URL to the underlying Cloudflare Stream iframe.
# Useful for sanity-checking a new URL before letting `just download` run.
resolve URL:
    {{compose}} run --rm --entrypoint /work/scripts/resolve.sh archiver "{{URL}}"

# Convert a saved Patreon page MHTML into a curation-friendly markdown
# inventory. Pipe to a file or your editor.
#   just inventory ~/Downloads/foo.mhtml > urls/posts.md
inventory PATH:
    #!/usr/bin/env bash
    set -euo pipefail
    in_path=$(realpath "{{PATH}}")
    {{compose}} run --rm -T \
      -v "${in_path}":/in.mhtml:ro \
      --entrypoint python3 archiver \
      /work/scripts/inventory.py /in.mhtml

# Diff the MHTML against urls/seen_posts.txt, write the new posts (only)
# into urls/urls.txt, run batch, and append the freshly handled post URLs
# back to seen_posts.txt on success. Idempotent — re-running with the same
# MHTML is a no-op once everything is seen.
#   just sync ~/Downloads/foo.mhtml
sync PATH:
    #!/usr/bin/env bash
    set -euo pipefail
    seen=urls/seen_posts.txt
    coverage=urls/coverage.txt
    touch "$seen"
    in_path=$(realpath "{{PATH}}")
    inv_err=$(mktemp)
    trap 'rm -f "$inv_err"' EXIT
    {{compose}} run --rm -T \
      -v "${in_path}":/in.mhtml:ro \
      -v "$(pwd)/$seen":/seen.txt:ro \
      --entrypoint python3 archiver \
      /work/scripts/inventory.py /in.mhtml --seen-file /seen.txt --minimal \
      > urls/urls.txt 2> >(tee "$inv_err" >&2)
    # coverage.txt holds the "anchor" date — the newest post date that we
    # know lives within continuous coverage back through previously-seen
    # history. A new sync is gap-free iff its MHTML reaches back to the
    # anchor (mhtml_oldest <= anchor); in that case the anchor is advanced
    # forward to the new MHTML's newest date. If mhtml_oldest > anchor,
    # there's an unsampled window (anchor, mhtml_oldest); the anchor is
    # held in place and a future MHTML that does reach back will close it.
    range_line=$(grep -F '[inventory] mhtml_date_range:' "$inv_err" | head -n1)
    mhtml_oldest=$(echo "$range_line" | sed -E 's/.*: ([0-9-]+) \.\..*/\1/')
    mhtml_newest=$(echo "$range_line" | sed -E 's/.*\.\. ([0-9-]+) \(.*/\1/')
    prev_anchor=""
    [[ -f "$coverage" ]] && prev_anchor=$(head -n1 "$coverage" | tr -d '[:space:]')
    gap_msg=""
    new_anchor="$prev_anchor"
    if [[ -n "$mhtml_oldest" && -n "$mhtml_newest" ]]; then
        if [[ -z "$prev_anchor" ]]; then
            new_anchor="$mhtml_newest"
            echo "[sync] coverage anchor initialized at $new_anchor (first sync)."
        elif [[ "$mhtml_oldest" < "$prev_anchor" || "$mhtml_oldest" == "$prev_anchor" ]]; then
            if [[ "$mhtml_newest" > "$prev_anchor" ]]; then
                echo "[sync] coverage anchor advanced: $prev_anchor -> $mhtml_newest."
                new_anchor="$mhtml_newest"
            fi
        else
            gap_msg="gap pending — dates ($prev_anchor, $mhtml_oldest) may have un-handled posts. Visible-page diff is being downloaded; the system keeps the gap pending until a future MHTML reaches back to $prev_anchor or earlier."
        fi
    fi
    if ! grep -qE '^https?://' urls/urls.txt; then
        echo "[sync] no new posts since last run."
        rm -f urls/urls.txt
    else
        new=$(grep -cE '^# post: ' urls/urls.txt || true)
        echo "[sync] $new new post(s) queued; running batch..."
        just batch
        grep -E '^# post: ' urls/urls.txt | sed 's/^# post: //' >> "$seen"
        sort -u -o "$seen" "$seen"
        echo "[sync] $new post(s) marked as seen ($(wc -l < "$seen") total)."
    fi
    [[ -n "$new_anchor" ]] && echo "$new_anchor" > "$coverage"
    if [[ -n "$gap_msg" ]]; then
        echo
        echo "[sync] $gap_msg"
    fi

# Local environment smoke test (does not touch the network).
smoke:
    {{compose}} run --rm --entrypoint /work/scripts/smoke.sh archiver

# ---------- quality ----------

lint: lint-docker lint-shell lint-yaml lint-secrets lint-grep

lint-docker:
    docker run --rm -i {{hadolint_image}} < Dockerfile

lint-shell:
    docker run --rm -v "$(pwd)":/mnt -w /mnt {{shellcheck_image}} \
      scripts/download.sh scripts/resolve.sh scripts/smoke.sh

lint-yaml:
    docker run --rm -v "$(pwd)":/data {{yamllint_image}} \
      -c /data/.yamllint.yaml --strict /data

lint-secrets:
    docker run --rm -v "$(pwd)":/repo {{gitleaks_image}} \
      detect --source=/repo --verbose --redact --no-git

# Defensive grep gate: forbid tracker tags and unsafe shell builtins.
# The justfile defines the very pattern, so it is excluded from the search.
lint-grep:
    @bash -eu -c '\
      if grep -RInE "(TODO|FIXME|XXX)" scripts/ config/ Dockerfile compose.yaml compose.override.yaml; then \
        echo "tracker tag found"; exit 1; \
      fi; \
      if grep -RIn "eval " scripts/; then \
        echo "unsafe eval"; exit 1; \
      fi; \
      echo "lint-grep OK"\
    '
