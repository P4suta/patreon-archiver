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
#   /work/scripts/*       bundled helpers (Python + smoke.sh)
#   /work/config/yt-dlp.conf

default:
    @just --list --unsorted

# ---------- runtime ----------

# Diff the MHTML against /state/seen_posts.txt, download new posts, advance
# the coverage anchor in /state/coverage.txt. Idempotent.
sync MHTML:
    python3 /work/scripts/sync.py "{{MHTML}}"

# Markdown inventory of an MHTML. Pipe to a file for human review.
#   pa inventory ~/Downloads/foo.mhtml > posts.md
inventory MHTML *EXTRA:
    python3 /work/scripts/inventory.py "{{MHTML}}" {{EXTRA}}

# Batch-download every URL in /state/urls.txt. Each URL may be preceded by
# `# key: value` metadata lines (see urls/urls.txt.example).
batch *EXTRA:
    python3 /work/scripts/download.py --batch-file /state/urls.txt {{EXTRA}}

# Single-URL download. Cookies-free path; works for presigned/token URLs.
#   pa download "https://stream.example.com/<date>_<slug>_<token>/"
download URL *EXTRA:
    python3 /work/scripts/download.py "{{URL}}" {{EXTRA}}

# Resolve a publisher page URL to the underlying Cloudflare Stream iframe.
resolve URL:
    python3 /work/scripts/resolve.py "{{URL}}"

# Offline self-test (no network).
smoke:
    /work/scripts/smoke.sh

# Print the bundled yt-dlp version.
version:
    yt-dlp --version

# Drop into an interactive bash inside the container.
shell:
    bash
