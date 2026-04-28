set shell := ["bash", "-cu"]
set positional-arguments := true

# All recipes here are designed to run *inside* the container — the
# Compose-driven wrapper (`pa.cmd` on Windows, `alias pa='docker compose
# -f .../compose.yaml run --rm pa'` on WSL) bind-mounts the host's
# `<repo>/data/` directory into `/data` and execs `just <recipe>`.
# Container path conventions:
#
#   /data                 = host's `<repo>/data/`. Holds every user-
#                         facing file: mp4 outputs, MHTML snapshots,
#                         state files, cookies, .retest sandboxes.
#                         The repo source code stays under
#                         `<repo>/scripts/`, `<repo>/config/`, etc.
#                         and is *not* visible in /data.
#   /data/mhtml/          MHTML snapshot input dir. `sync` / `inventory`
#                         with no positional arg picks the newest *.mhtml.
#   /data/cookies.txt     optional cookies (auto-detected if present)
#   /work/scripts/*       bundled helpers (Python + smoke.sh)
#   /work/config/yt-dlp.conf

default:
    @just --list --unsorted

# ---------- runtime ----------

# Diff the MHTML against /data/seen_posts.txt, download new posts, advance
# the coverage anchor in /data/coverage.txt. Idempotent.
# If MHTML is omitted, picks the newest *.mhtml under /data/mhtml/.
sync MHTML="":
    python3 /work/scripts/sync.py "{{MHTML}}"

# Read-only "what would happen" report for a sync. Lists every post that
# would be downloaded, shows coverage-anchor changes, and exits without
# touching any state file (urls.txt / seen_posts.txt / coverage.txt all
# untouched, yt-dlp not invoked). Re-runnable any number of times.
sync-dry MHTML="":
    python3 /work/scripts/sync.py "{{MHTML}}" --dry-run

# Test-download a single URL: yt-dlp metadata fetch only (--simulate),
# no actual mp4 written, no archive/seen state mutated. Useful to confirm
# that a URL resolves end-to-end (publisher → CF Stream → format pick) and
# that cookies are accepted, before committing real downloads to /data.
simulate URL *EXTRA:
    python3 /work/scripts/download.py "{{URL}}" --simulate {{EXTRA}}

# Real-download a URL but bypass the archive cache and quarantine the
# output: yt-dlp gets an empty download-archive seed (so already-archived
# URLs re-download), the published mp4 lands under /data/.retest/<ts>/
# instead of /data/<uploader>/, and archive.txt is *not* updated. Wipe
# the sandbox afterward with `rm -rf .retest/`.
retest URL *EXTRA:
    python3 /work/scripts/download.py --retest "{{URL}}" {{EXTRA}}

# Markdown inventory of an MHTML. Pipe to a file for human review.
#   pa inventory mhtml/foo.mhtml > posts.md
# If MHTML is omitted, picks the newest *.mhtml under /data/mhtml/.
inventory MHTML="" *EXTRA:
    python3 /work/scripts/inventory.py "{{MHTML}}" {{EXTRA}}

# Batch-download every URL in /data/urls.txt. Each URL may be preceded by
# `# key: value` metadata lines (see urls.txt.example at the repo root).
batch *EXTRA:
    python3 /work/scripts/download.py --batch-file /data/urls.txt {{EXTRA}}

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

# Drop into an interactive bash inside the container, in /data (user CWD).
shell:
    cd /data && exec bash
