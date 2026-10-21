# patreon-archiver

Personal offline archiver for authorized Patreon videos.

Save supported videos and metadata through a Docker-based yt-dlp workflow.
An exported MHTML page provides an inventory and input for incremental downloads.
Use only content you are authorized to archive; see the [usage restrictions](docs/disclaimer.md).

## Install

Install Docker with Compose, clone this repository, then build the Linux x86-64 image:

```powershell
docker compose build pa
.\pa.cmd smoke
```

The smoke test runs offline.
Linux hosts must make `data/` writable by the container user, UID/GID 10001.

## Use

Save an authorized page as `data/mhtml/snapshot.mhtml`, then preview the pending downloads:

```powershell
.\pa.cmd sync-dry
```

Run `.\pa.cmd` to list commands.
See [usage](docs/usage.md) for downloads, cookies, state, and profiles, or [development](docs/development.md) for checks.

## License

[MIT](LICENSE).
The software license grants no rights to downloaded content.
