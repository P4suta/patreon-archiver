#!/usr/bin/env bash
# yt-dlp wrapper that injects per-URL metadata so Cloudflare Stream
# downloads end up with human-readable filenames and mp4 tags.
#
# Input shapes:
#   * Positional URLs from the CLI (`just download "<URL>"`).
#   * --batch-file <path> where each URL may be preceded by `# key: value`
#     comment lines that supply the metadata yt-dlp's CloudflareStream
#     extractor never returns. Recognized keys:
#       title    — used for the filename %(title)s and the mp4 title tag
#       uploader — folder name and mp4 artist tag
#       date     — YYYY-MM-DD; becomes %(upload_date)s (yt-dlp wants YYYYMMDD)
#       post     — canonical Patreon post URL; embedded as the mp4 comment
#                  in place of the JWT iframe URL yt-dlp would otherwise use
#     Unknown comment keys are silently ignored.
#
# Each URL is downloaded with its own yt-dlp invocation so the metadata
# injection is per-URL. In batch mode we sleep a random duration in
# [YTDLP_BATCH_SLEEP_MIN, YTDLP_BATCH_SLEEP_MAX] seconds between videos.
set -euo pipefail

resolve_one() {
    /work/scripts/resolve.sh "$1"
}

# Derive baseline metadata from the publisher URL. Used when a single-shot
# `just download <URL>` call gives us no curated metadata block. Yields
# tab-separated `key<TAB>value` lines on stdout.
derive_defaults() {
    local url="$1"
    if [[ "$url" =~ ^https?://(stream\.[^/]+)/([0-9]{8})_([^_/]+)_[^/]+/?$ ]]; then
        local host="${BASH_REMATCH[1]}"
        local date_compact="${BASH_REMATCH[2]}"
        local slug="${BASH_REMATCH[3]}"
        local handle="${host#stream.}"
        handle="${handle%%.*}"
        printf 'uploader\t%s\n' "$handle"
        printf 'date\t%s-%s-%s\n' "${date_compact:0:4}" "${date_compact:4:2}" "${date_compact:6:2}"
        printf 'title\t%s\n' "$slug"
        printf 'post\thttps://%s/%s_%s/\n' "$host" "$date_compact" "$slug"
    fi
}

# yt-dlp's --parse-metadata splits on the first unescaped colon, so any
# colon inside the literal source value has to be escaped.
escape_colons() {
    printf '%s' "${1//:/\\:}"
}

emit_meta_flags() {
    local key="$1" val="$2"
    [[ -z "$val" ]] && return
    local literal
    literal="$(escape_colons "$val")"
    # Why the "= " prefix on FROM and TO:
    # yt-dlp's MetadataParserPP.field_to_template auto-wraps any pure-
    # alphabetic FROM string ([a-zA-Z_]+) into %(<from>)s — i.e. it treats
    # an unspaced word as a field name, not a literal. For values like a
    # bare lowercase handle (no digits, no spaces) the injected metadata
    # then resolves to the info-dict's NA default. Adding a non-alphabetic
    # sentinel ("= ") to both sides defeats the auto-wrap and the matching
    # prefix in the TO regex strips the sentinel before the named-group
    # capture, so the captured value equals the original `val`.
    case "$key" in
        title)
            # %(title)s drives the output template; meta_title becomes the
            # mp4 \xa9nam tag via --embed-metadata.
            printf -- '--parse-metadata\n= %s:= %%(title)s\n' "$literal"
            printf -- '--parse-metadata\n= %s:= %%(meta_title)s\n' "$literal"
            ;;
        uploader)
            printf -- '--parse-metadata\n= %s:= %%(uploader)s\n' "$literal"
            ;;
        date)
            local compact="${val//-/}"
            literal="$(escape_colons "$compact")"
            printf -- '--parse-metadata\n= %s:= %%(upload_date)s\n' "$literal"
            ;;
        post)
            # ffmpeg "comment" + "purl" tags. Replaces the JWT iframe URL
            # yt-dlp would otherwise embed as the comment.
            printf -- '--parse-metadata\n= %s:= %%(meta_comment)s\n' "$literal"
            printf -- '--parse-metadata\n= %s:= %%(meta_purl)s\n' "$literal"
            ;;
    esac
}

run_one() {
    local url="$1"; shift
    declare -A meta=()
    while (( $# )); do
        meta["$1"]="$2"
        shift 2
    done

    while IFS=$'\t' read -r k v; do
        [[ -z "$k" ]] && continue
        : "${meta[$k]:=$v}"
    done < <(derive_defaults "$url")

    local resolved
    resolved="$(resolve_one "$url")"

    local args=(--config-location /work/config/yt-dlp.conf)
    if [[ -n "${YTDLP_COOKIES:-}" && -r "${YTDLP_COOKIES}" ]]; then
        args+=(--cookies "${YTDLP_COOKIES}")
    fi
    args+=("${EXTRA_FLAGS[@]}")

    local k
    for k in title uploader date post; do
        if [[ -n "${meta[$k]:-}" ]]; then
            while IFS= read -r line; do
                args+=("$line")
            done < <(emit_meta_flags "$k" "${meta[$k]}")
        fi
    done

    yt-dlp "${args[@]}" "$resolved"
}

run_batch() {
    local file="$1"
    local sleep_min="${YTDLP_BATCH_SLEEP_MIN:-5}"
    local sleep_max="${YTDLP_BATCH_SLEEP_MAX:-15}"
    declare -a meta_pairs=()
    local first=1 line rest key val span rnd

    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        case "$line" in
            '')
                ;;
            '#'*)
                rest="${line#'#'}"
                rest="${rest# }"
                if [[ "$rest" == *:* ]]; then
                    key="${rest%%:*}"
                    val="${rest#*:}"
                    val="${val# }"
                    meta_pairs+=("$key" "$val")
                fi
                ;;
            http://*|https://*)
                if (( first )); then
                    first=0
                else
                    span=$(( sleep_max - sleep_min ))
                    rnd=$sleep_min
                    (( span > 0 )) && rnd=$(( sleep_min + RANDOM % (span + 1) ))
                    printf '[batch] sleeping %ds before next URL\n' "$rnd"
                    sleep "$rnd"
                fi
                run_one "$line" "${meta_pairs[@]}"
                meta_pairs=()
                ;;
        esac
    done < "$file"
}

EXTRA_FLAGS=()
URLS=()
BATCH_FILE=""
while (( $# )); do
    case "$1" in
        --batch-file)
            shift
            BATCH_FILE="$1"
            ;;
        --batch-file=*)
            BATCH_FILE="${1#*=}"
            ;;
        http://*|https://*)
            URLS+=("$1")
            ;;
        *)
            EXTRA_FLAGS+=("$1")
            ;;
    esac
    shift
done

if [[ -n "$BATCH_FILE" ]]; then
    run_batch "$BATCH_FILE"
elif (( ${#URLS[@]} )); then
    for url in "${URLS[@]}"; do
        run_one "$url"
    done
else
    printf 'usage: download.sh [yt-dlp flags] <URL>... | --batch-file <path>\n' >&2
    exit 64
fi
