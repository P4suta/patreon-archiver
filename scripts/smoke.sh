#!/usr/bin/env bash
# Self-contained environment smoke test. No network access; safe to run in
# air-gapped CI. Verifies the image can run, ffmpeg/yt-dlp are present, the
# yt-dlp config parses, and both the user-visible /data mount and the
# private /var/lib/pa/staging area are writable by the runtime user.
set -euo pipefail

echo "[1/6] yt-dlp version"
yt-dlp --version

echo "[2/6] ffmpeg present"
ffmpeg -version | head -n1

echo "[3/6] yt-dlp polite config parses"
yt-dlp --config-location /work/config/yt-dlp.conf --help >/dev/null

echo "[4/6] yt-dlp fast config parses"
yt-dlp --config-location /work/config/yt-dlp-fast.conf --help >/dev/null

echo "[5/6] /data is writable"
probe="/data/.smoke.$$"
touch "${probe}"
rm -f "${probe}"

echo "[6/6] /var/lib/pa/staging is writable"
probe="/var/lib/pa/staging/.smoke.$$"
touch "${probe}"
rm -f "${probe}"

echo "smoke OK"
