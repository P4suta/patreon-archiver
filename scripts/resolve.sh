#!/usr/bin/env bash
# Resolve a publisher page URL into the underlying Cloudflare Stream iframe
# URL that yt-dlp's CloudflareStream extractor understands.
#
# Pass-through cases (already on Cloudflare Stream):
#   * https://iframe.videodelivery.net/<JWT-or-uid>
#   * https://watch.videodelivery.net/<uid>
#   * https://customer-<id>.cloudflarestream.com/<uid>/...
#
# Resolution case: any other http(s) URL is treated as an HTML page that
# embeds the Cloudflare Stream iframe; the first iframe.videodelivery.net
# src found in the page is returned.
set -euo pipefail

if (( $# != 1 )); then
    printf 'usage: resolve.sh <URL>\n' >&2
    exit 64
fi

input="$1"

case "$input" in
    https://iframe.videodelivery.net/*|\
    https://watch.videodelivery.net/*|\
    https://customer-*.cloudflarestream.com/*)
        printf '%s\n' "$input"
        exit 0
        ;;
esac

iframe=$(curl --silent --location --max-time 30 \
              --user-agent "Mozilla/5.0" \
              "$input" \
        | grep --only-matching --extended-regexp \
               'https://iframe\.videodelivery\.net/[A-Za-z0-9._-]+' \
        | head -n1) || true

if [[ -z "$iframe" ]]; then
    printf 'resolve: no Cloudflare Stream iframe found at %s\n' "$input" >&2
    exit 1
fi

printf '%s\n' "$iframe"
