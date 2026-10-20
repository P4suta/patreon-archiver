# Usage

## Build and run

Run commands from the cloned repository.
Docker with Compose and Linux x86-64 container support is required.
Build or rebuild with `docker compose build pa`.

On Windows, `pa.cmd` forwards its arguments to the container's `just` recipes.
For example, `.\pa.cmd smoke` runs the offline environment check.
On other hosts, use `docker compose run --rm pa smoke`.
Omit `smoke` to list the available recipes.

Compose mounts only the checkout's `data/` directory at `/data`.
Paths passed to recipes refer to the container, so `mhtml/snapshot.mhtml` means `data/mhtml/snapshot.mhtml` on the host.
On Linux, provide a writable data directory for UID/GID 10001 before running the container.

## Preview and sync

Save an authorized Patreon page as MHTML under `data/mhtml/`.
Include the older posts needed to overlap the previous snapshot.

```powershell
.\pa.cmd inventory
.\pa.cmd sync-dry
.\pa.cmd sync
```

Without a path, `inventory` and `sync` choose the newest MHTML by modification time.
Extension matching ignores case; ties use the filename.
Pass a container-relative path to choose a snapshot explicitly.

`inventory` prints a Markdown report.
Use `.\pa.cmd inventory mhtml/snapshot.mhtml --minimal` for metadata and URL blocks.
`sync-dry` reports pending work without invoking the downloader or updating state.
`sync` writes a batch, downloads new posts, and records successful progress.

The snapshot contains only posts the browser loaded.
A `gap pending` message means it does not overlap the saved coverage anchor.
Load older posts, export another snapshot, and retry.
The anchor is a date-based continuity check, not proof that every post was captured.
Keep `coverage.txt` and `seen_posts.txt` with their matching archive.

## Download commands

Use only authorized URLs and follow the [usage restrictions](disclaimer.md).

| Recipe | Behavior |
| --- | --- |
| `download URL` | Download one supported video |
| `batch` | Read `data/urls.txt` |
| `simulate URL` | Fetch metadata without writing a video or archive state |
| `retest URL` | Download into `data/.retest/` without updating the normal archive |
| `resolve URL` | Resolve a supported publisher page to its stream URL |
| `version` | Print the bundled yt-dlp version |
| `shell` | Open a shell in `/data` |

Copy the format in [urls.txt.example](../urls.txt.example) into `data/urls.txt`.
A URL may have preceding `# title:`, `# uploader:`, `# date:`, and `# post:` metadata lines.
Dates use `YYYY-MM-DD`.
Inventory output supplies the same blocks.

`simulate` still makes network requests.
`retest` performs a real download and consumes disk space.
Only `smoke`, `inventory`, and `sync-dry` are offline operations.

## Files and cookies

| Host path | Purpose |
| --- | --- |
| `data/mhtml/` | Exported page snapshots |
| `data/<uploader>/` | Completed MP4 files |
| `data/urls.txt` | Batch input |
| `data/archive.txt` | yt-dlp archive identifiers |
| `data/seen_posts.txt` | Canonical post URLs recorded by sync |
| `data/coverage.txt` | Sync coverage anchor |
| `data/cookies.txt` | Optional Netscape-format cookies |
| `data/.retest/` | Isolated test downloads |

Compose supplies cookies automatically when `data/cookies.txt` exists.
Treat cookies, signed URLs, snapshots, and downloaded files as private data.
Do not commit them.
An expired URL or denied request may require a fresh authorized snapshot or cookies.

The container stages downloads under `/var/lib/pa/staging` before publishing outputs to `/data`.
Keep backups of videos and state files.
Do not run concurrent writers against one data directory.

Each checkout has its own data directory and state.
Use a separate checkout for a separate archive; the local image can be shared.

## Profiles

The default profile uses four concurrent fragments and a one-second request delay.
Batch delays default to 5–15 seconds.
Set `YTDLP_BATCH_SLEEP_MIN`, `YTDLP_BATCH_SLEEP_MAX`, and `TZ` in the shell or a local `.env` file beside Compose.

`fast URL`, `fast-batch`, and `fast-sync` select the faster configuration: eight fragments, no request delay, and 0–2 seconds between batch items.
These settings change request pacing, not access permissions.
Prefer the default and respect the service's limits.

The runtime supports unencrypted streams handled by the bundled extractor.
It does not bypass DRM or guarantee that a changed publisher page will still work.
