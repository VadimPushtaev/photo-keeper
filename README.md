# Photo Keeper

Photo Keeper copies photos and other files from named rclone remotes into a local archive
organized by year. Each invocation runs one chain, so cron can manage scheduling.

## Setup

Use Python 3.12+, Poetry 2.x, and rclone on Linux or WSL. Capture date mode also needs
[ExifTool](https://exiftool.org/) on PATH (`libimage-exiftool-perl` on Ubuntu).
The destination root must exist on a filesystem that supports file locks and hard links.

```bash
poetry install
cp -n config.example.toml config.toml
poetry run photo-keeper login
```

The login command opens the interactive `rclone config` menu. Create a remote named
`dropbox`, choose Dropbox storage, leave the app key and secret blank, and authorize it in
your browser. If you already configured a remote, use its name in the chain. Other clouds
use the same setup flow with their respective storage type. [Dropbox setup](https://rclone.org/dropbox/)
and [headless login](https://rclone.org/remote_setup/).

Rclone stores and refreshes its credentials. It uses its standard config location by default.
To use a specific file, add `[rclone]` with `config_file = ".secrets/rclone.conf"` to
`config.toml`. Both `login` and `run` use that file. Relative paths are resolved from the
Photo Keeper config. Keep personal chains and credentials in the ignored `config.toml` and
`.secrets/` directory.

If upgrading from direct Dropbox access, remove the old `[dropbox]` table, change
`source = "/Camera Uploads"` to `source = "dropbox:Camera Uploads"`, and authorize the remote
in rclone. The previous app credentials are separate from rclone's login.

## Chain configuration

```toml
[[chains]]
name = "example"
enabled = true
source = "dropbox:Camera Uploads"
destination = "/absolute/path/to/photos"
subdirectory = "phone/__raw__"
recursive = true
date_source = "capture"
```

A file `Camera Uploads/trip/image.jpg` captured in 2024 is copied to
`/absolute/path/to/photos/2024/phone/__raw__/trip/image.jpg`. The chain copies files of any
type and preserves source subfolders. Use `remote:` to start at a remote's root. Each named
chain needs its own table. `enabled` defaults to false, `recursive` to true, and
`subdirectory` to `__raw__`.

ExifTool reads capture dates from the file's photo or video metadata. The year falls back to
the remote modification date when no capture date is available. A file with neither date is
reported as a failure. Set `date_source = "modified"` to use modification dates directly and
skip ExifTool.

## Run

```bash
poetry run photo-keeper check-config
poetry run photo-keeper list-chains
poetry run photo-keeper run example --dry-run
poetry run photo-keeper run example
```

Dry runs list the remote and check existing files without downloading or writing files.
Capture date mode can confirm the year from an existing verified copy. For new files, the
exact year is determined after download. Planning reads the filenames in each relevant
archive folder once and hashes only potential matches.

Rclone downloads pending files in batches of up to 16, with four transfers in parallel.
The log shows the batch number, total size, and largest listed file. Rclone reports bytes
transferred, transfer rate, and estimated time remaining every five seconds during a download.
Dry runs and copy logs also show individual file sizes.

Each batch downloads into a temporary directory on the destination drive. Photo Keeper
checks the expected size and supported remote checksum before publishing each copy. Backends
without a supported checksum may need to download the file again on subsequent runs so its
contents can be compared locally. A matching file is skipped. If a destination filename
contains different content, the new copy receives a deterministic checksum suffix.

The operation only reads the cloud source. Local files survive deletion on the remote.
Files removed or moved out of the archive may be downloaded again. Each run holds a lock
in the destination root to prevent overlapping copies. Failed downloads are cleaned up on exit.
An abrupt kill or power loss can leave a hidden `.photo-keeper-*` staging directory.

The summary reports copied, unchanged, planned, and failed counts. Exit codes are `0` for
success, `1` for runtime or copy failures, `2` for configuration or usage errors, and `130`
for an interrupted run. Results go to stdout and timestamped logs go to stderr.

For cron, use absolute executable and configuration paths and make sure both rclone and
ExifTool are on cron's PATH. Run as the user who authorized the rclone remote.

```cron
*/30 * * * * /path/to/photo-keeper/.venv/bin/photo-keeper --config /path/to/photo-keeper/config.toml run example
```

## Development

```bash
make check
make build
```

The tests cover rclone arguments and output parsing, capture dates, checksum verification,
repeat runs, conflicts, interrupted downloads, and CLI errors. The integration test uses
actual rclone and ExifTool with a local fixture and skips if either program is unavailable.
