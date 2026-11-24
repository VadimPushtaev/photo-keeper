# Photo Keeper

Photo Keeper runs ordered copy and sort steps for photos and other files. Sources can be
named rclone remotes, local folders, or the output of the preceding step. Each invocation
runs one chain, so cron can manage scheduling.

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

[[chains.steps]]
type = "copy"
from = { rclone = "dropbox:Camera Uploads" }
to = "/absolute/path/to/photos/{year}/phone/__raw__"
delete_source = false

[[chains.steps]]
type = "sort"
from = "previous"
to = "/absolute/path/to/photos/{year}/phone/{month}"
delete_source = false

[chains.steps.routes]
video = "video"
png = "/absolute/path/to/photos/{year}/phone/screenshot"
jpeg_without_camera = "/absolute/path/to/photos/{year}/phone/pics"
```

Steps run in the order listed. A copy step accepts a named rclone remote, a local folder
(`from = { local = "/absolute/path/to/inbox" }`), or `from = "previous"`. A sort step takes
local input and can send file types to separate folders. `from = "previous"` reads every
output folder of the preceding step, including files from earlier runs.

Destinations are absolute local folders. Use `{year}` and `{month}` as whole path
components. The directory before the first date component must exist. A fixed destination
uses its existing parent directory. A relative route such as `video = "video"` adds a
subfolder to `to`, while an absolute route defines its own layout. In the example, videos
go into `year/phone/MM/video/`, PNGs into `year/phone/screenshot/`, and JPEGs without an
EXIF camera make or model into `year/phone/pics/`. Everything else goes into `year/phone/MM/`.

Each step preserves paths relative to its input folder. Date values already present in a
local input layout carry forward. For the remaining values, steps use ExifTool's capture
date or fall back to the file's modification date. Set `date_source = "modified"` on a
step to skip capture date lookup. The JPEG route still checks EXIF when that rule is set.
`recursive` defaults to true. `enabled` and `delete_source` default to false.

Copy and sort steps preserve the original bytes, including embedded motion in HEIC photos,
and the modification time. Existing files with different contents are kept and new copies
receive a checksum suffix. Legacy chain settings are still accepted with their original
layout. To use the new layout, replace them with explicit step tables.

## Optional cleanup

Set `delete_source = true` on the step that reads the input you want to clean up. Cleanup
runs in step order after the copy and sort phases. An input is eligible only if its
output copy is verified and the file is absent from every earlier input in the chain.
The first step has no earlier input to check. The last step's output is preserved
because cleanup only applies to step inputs. Configuration rejects overlapping inputs
and outputs that would turn an earlier source into a later destination.

Cleanup refreshes earlier file listings before deleting and retains files when upstream
presence is uncertain. Dry runs preview cleanup only when the verified output already exists.

Remote cleanup requires a named folder and permission to delete files. It checks a fresh
listing against the local output by size and checksum, then deletes the verified paths
directly from the source. This assumes files stay unchanged at those paths during cleanup.
Files without a comparable checksum are retained. Existing recovery files from older
versions in `.photo-keeper-cleanup/` remain outside normal chain processing.

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

The summary reports counts for downloads, sorting, cleanup, and their previews.
`sorted` counts new sort outputs. `sort_unchanged` counts existing sort outputs verified
against their inputs. Exit codes are `0` for
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
