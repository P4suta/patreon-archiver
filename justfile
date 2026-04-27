set shell := ["bash", "-cu"]
set positional-arguments := true

# All recipes here are designed to run *inside* the container — the host
# wrapper `pa` just sets up bind mounts and execs `docker run image <recipe>`.
# Container path conventions:
#
#   /downloads            DOWNLOAD_DIR (final mp4 + archive.txt)
#   /state                seen_posts.txt + coverage.txt + urls.txt scratch
#   /in/mhtml             input MHTML (mounted by `pa` for sync/inventory)
#   /secrets/cookies.txt  optional cookies (read-only)
#   /work/scripts/*.sh    bundled helpers (download.sh, resolve.sh, smoke.sh)
#   /work/scripts/inventory.py
#   /work/config/yt-dlp.conf

default:
    @just --list --unsorted

# ---------- runtime ----------

# Diff the MHTML against /state/seen_posts.txt, download new posts, advance
# the coverage anchor in /state/coverage.txt. Idempotent.
sync MHTML:
    #!/usr/bin/env bash
    set -euo pipefail
    seen=/state/seen_posts.txt
    coverage=/state/coverage.txt
    urls=/state/urls.txt
    inv_err=$(mktemp)
    trap 'rm -f "$inv_err"' EXIT
    mkdir -p /state
    touch "$seen"
    python3 /work/scripts/inventory.py "{{MHTML}}" --seen-file "$seen" --minimal \
        > "$urls" 2> >(tee "$inv_err" >&2)
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
    if ! grep -qE '^https?://' "$urls"; then
        echo "[sync] no new posts since last run."
        rm -f "$urls"
    else
        new=$(grep -cE '^# post: ' "$urls" || true)
        echo "[sync] $new new post(s) queued; running batch..."
        /work/scripts/download.sh --batch-file "$urls"
        grep -E '^# post: ' "$urls" | sed 's/^# post: //' >> "$seen"
        sort -u -o "$seen" "$seen"
        echo "[sync] $new post(s) marked as seen ($(wc -l < "$seen") total)."
    fi
    [[ -n "$new_anchor" ]] && echo "$new_anchor" > "$coverage"
    if [[ -n "$gap_msg" ]]; then
        echo
        echo "[sync] $gap_msg"
    fi

# Markdown inventory of an MHTML. Pipe to a file for human review.
#   pa inventory ~/Downloads/foo.mhtml > posts.md
inventory MHTML *EXTRA:
    python3 /work/scripts/inventory.py "{{MHTML}}" {{EXTRA}}

# Batch-download every URL in /state/urls.txt. Each URL may be preceded by
# `# key: value` metadata lines (see urls/urls.txt.example).
batch *EXTRA:
    /work/scripts/download.sh --batch-file /state/urls.txt {{EXTRA}}

# Single-URL download. Cookies-free path; works for presigned/token URLs.
#   pa download "https://stream.example.com/<date>_<slug>_<token>/"
download URL *EXTRA:
    /work/scripts/download.sh "{{URL}}" {{EXTRA}}

# Resolve a publisher page URL to the underlying Cloudflare Stream iframe.
resolve URL:
    /work/scripts/resolve.sh "{{URL}}"

# Offline self-test (no network).
smoke:
    /work/scripts/smoke.sh

# Print the bundled yt-dlp version.
version:
    yt-dlp --version

# Drop into an interactive bash inside the container.
shell:
    bash
