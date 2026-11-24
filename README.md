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
sort_by_month = true

[chains.delete]
source = false
raw = false
archive = false
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

When `sort_by_month = true`, the chain checks existing raw files and files downloaded in
the current run. It copies each one from `year/phone/__raw__/` into `year/phone/MM/`
using capture month, with modification month in UTC as the fallback. The year comes
from the raw folder. Nested source paths are kept inside their category.

- Videos go in `MM/video/`.
- PNGs go in `MM/screenshot/`.
- JPEGs without an EXIF camera make or model go in `MM/pics/`. Camera JPEGs and other
  files go directly in the month folder.

The monthly copy keeps the original bytes, including embedded motion in HEIC photos, and
preserves the file's modification time. An existing file with different contents is kept
and the new copy receives a checksum suffix. Sorting requires the configured subdirectory
to end in `__raw__`.

## Optional cleanup

Each step's deletion flag defaults to false. The first step has no predecessor and the last
step has no successor. Photo Keeper applies these rules:

| Step | When a file may be removed |
| --- | --- |
| Cloud source | `delete.source = true` and the raw copy matches the file's size and remote checksum. |
| `__raw__` | `delete.raw = true`, the file is absent from the cloud source, and its monthly copy matches byte for byte. |
| Monthly archive | The chain always preserves the final files, even with `delete.archive = true`. |

Raw cleanup refreshes the source listing after source cleanup and retains files when
upstream presence is uncertain. With monthly sorting disabled, the raw directory is the
last step and is also preserved.

Source cleanup requires a folder on the remote and permission to move and delete files.
It moves eligible files to `.photo-keeper-cleanup/<id>` on the same remote, then compares
their checksums with the raw copies before deleting them from the holding folder. If the
operation is interrupted or verification fails, any remaining held files stay recoverable
at the remote path printed in the log. Runs starting at the remote root skip this holding
area. Source files without a comparable checksum are retained.

## Run

```bash
poetry run photo-keeper check-config
poetry run photo-keeper list-chains
poetry run photo-keeper run example --dry-run
poetry run photo-keeper run example
```

Dry runs list the remote and check existing files without downloading, writing, or deleting
files. Cleanup previews use copies that already exist, so a dry run may show a sorting action
whose result would make a later cleanup eligible during a real run.
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

With cleanup off, the operation only reads the cloud source. Local files survive deletion
on the remote. If a raw copy is removed and the cloud file remains, a later run can download
it again. Each run holds a lock in the destination root to prevent overlapping copies.
Failed downloads are cleaned up on exit.
An abrupt kill or power loss can leave a hidden `.photo-keeper-*` staging directory.

The summary reports counts for downloads, sorting, cleanup, and their previews. Exit codes are `0` for
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
month sorting, per-step cleanup, repeat runs, conflicts, interrupted downloads, and CLI errors.
Integration tests use actual rclone and ExifTool with local fixtures.
