#!/usr/bin/env bash
# Self-contained environment smoke test. No network access; safe to run in
# air-gapped CI. Verifies the image can run, ffmpeg/yt-dlp are present, the
# yt-dlp config parses, and /downloads is writable.
set -euo pipefail

echo "[1/4] yt-dlp version"
yt-dlp --version

echo "[2/4] ffmpeg present"
ffmpeg -version | head -n1

echo "[3/4] yt-dlp config parses"
yt-dlp --config-location /work/config/yt-dlp.conf --help >/dev/null

echo "[4/4] /downloads is writable"
probe="/downloads/.smoke.$$"
touch "${probe}"
rm -f "${probe}"

echo "smoke OK"
