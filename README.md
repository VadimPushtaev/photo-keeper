# Photo Keeper

A Poetry-based Python application for copying photos and other media through named chains.
Each invocation runs one chain and exits, so cron can manage scheduling. The first supported
chain copies Dropbox files into local folders grouped by year. It does not delete or modify
anything in Dropbox.

## Setup

Requires Python 3.12+, Poetry 2.x, and Linux or WSL. The destination filesystem must support
file locks and hard links, which prevent overlapping runs and publish complete files without
overwriting existing data.

```bash
poetry install
cp config.example.toml config.toml
```

Edit `config.toml` with your source, destination, and folder name, then enable the chain.
The destination root must already exist. Poetry creates the virtual environment in `.venv/`.
Personal chains belong in the ignored `config.toml`; credentials belong in the ignored
`.secrets/` directory. Commit application code, tests, and `poetry.lock`.

## Dropbox login

First create your own app in the [Dropbox App Console](https://www.dropbox.com/developers/apps).
Choose **Full Dropbox** access so it can read an existing folder such as Camera Uploads;
an App Folder app cannot read folders elsewhere in your Dropbox. Enable these permissions:

- `account_info.read`
- `files.metadata.read`
- `files.content.read`

Copy the **App key** from the app's settings, then run:

```bash
poetry run photo-keeper login dropbox --app-key YOUR_APP_KEY
```

Open the printed authorization URL, approve access, and paste the authorization code into
the terminal. The command also tries to open your browser. Use `--no-browser` on a headless
machine; the printed URL can be opened in another browser. The code prompt hides your input.
If your app settings expose an option for public clients/PKCE, allow it.

Login uses [Dropbox's PKCE flow with offline access](https://docs.dropboxapi.com/dropbox-api/docs/oauth).
It needs no app secret or redirect URL. The command verifies the account and saves the app key
and refresh token in `.secrets/dropbox.json`, with owner-only file permissions. The SDK then
refreshes short-lived access tokens automatically for cron runs. Tokens are never printed.
Log in again if access is revoked. `DROPBOX_APP_KEY` can supply the app key instead of the flag.

Use `--config /path/to/config.toml` before `login` to select another configuration. A custom
`[dropbox].credentials_file` is respected. Login also works before a configuration exists.
`login --credentials-file /path/to/token.json` overrides the save location; configure the same
path for subsequent runs and keep it out of version control.

## Chain configuration

```toml
[dropbox]
credentials_file = ".secrets/dropbox.json"

[[chains]]
name = "example"
enabled = true
source = "/Camera Uploads"
destination = "/absolute/path/to/photos"
subdirectory = "phone/__raw__"
recursive = true
date_source = "capture"
```

A file `/Camera Uploads/trip/image.jpg` captured in 2024 is copied to
`/absolute/path/to/photos/2024/phone/__raw__/trip/image.jpg`. Source subdirectories are preserved.
All downloadable files are copied, including videos, RAW files, and sidecars, regardless of
extension. Dropbox-native documents that cannot be downloaded are reported as ignored.

`date_source = "capture"` uses the photo/video capture timestamp supplied by Dropbox. If that
metadata is missing or still pending, the year comes from Dropbox's `client_modified` timestamp
(the file's modification date). This fallback is logged. Years use Dropbox's timestamps without
converting to the machine's timezone. The Dropbox upload date and current date are not used.
Capture metadata is requested separately using
[`files/get_metadata`](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/get-metadata);
folder listings no longer supply it. Set `date_source = "modified"` to use modification dates
directly. Embedded metadata is not parsed locally.

Add another `[[chains]]` table for another chain. Names must be unique; `enabled` defaults to
`false`. Enabled chains require explicit `source` and `destination` values. `source = "/"`
means all of Dropbox. `recursive` defaults to `true`, and `subdirectory` defaults to `__raw__`.
Unknown configuration keys and unsafe paths are rejected. Relative credential paths are
resolved against the configuration file, so cron's working directory does not affect them.

## Run and inspect

```bash
poetry run photo-keeper --config config.toml check-config
poetry run photo-keeper --config config.toml list-chains
poetry run photo-keeper --config config.toml run example --dry-run
poetry run photo-keeper --config config.toml run example
```

Dry runs read Dropbox metadata and check existing local files, but do not download content or
write local files. They require login. Each real run enumerates the configured source again;
there is no persisted Dropbox cursor or local database yet.

Downloads are streamed to temporary sibling files, checked for size and
[Dropbox content hash](https://docs.dropboxapi.com/dropbox-api/docs/technical-reference/content-hash),
flushed to disk, and published only after verification. The requested revision is pinned for
both capture metadata and download. Local modification times are preserved.

Existing local files are hashed before being skipped. If a filename contains different content,
the existing file is preserved and the new copy receives a deterministic content-hash suffix.
Subsequent runs recognize either verified filename. This is a copy operation: remote deletions
do not remove local files, and a local file removed or moved elsewhere will be downloaded again.
Later organization or cleanup steps will need persistent tracking before they are introduced.

Failed downloads are removed without publishing a partial destination. After a forced process
kill or power failure, a hidden `.photo-keeper-*.part` file may remain; it is never treated as a
completed copy. The next run retries the download. A persistent `.photo-keeper.lock` in the
destination root prevents concurrent writers; the OS releases the lock when the process exits.
Do not delete that lock file during a run. Symlinks below the destination root are rejected.

Individual copy failures are logged and other files are still attempted. The final summary
reports copied, unchanged, planned, ignored, and failed counts. Exit codes are `0` for success,
`1` for authentication/I/O/copy failures, `2` for configuration or usage errors, and `130` for
an interrupted run. Results go to stdout; timestamped logs go to stderr.

For cron, use absolute executable and configuration paths, for example:

```cron
*/30 * * * * /path/to/photo-keeper/.venv/bin/photo-keeper --config /path/to/photo-keeper/config.toml run example >> /path/to/photo-keeper/logs/backup.log 2>&1
```

Create the log directory first. Scheduling remains external; the app does not install cron jobs.

## Development

```bash
make check   # Lint, formatting, tests with coverage, and Poetry metadata/lock validation
make format
make build
```

Code lives in `src/photo_keeper/`: `auth.py` handles login and credential storage, `config.py`
validates settings, `backup.py` runs verified copies, and `hashing.py` implements Dropbox's hash.
The CLI is also available through `poetry run python -m photo_keeper`.

Tests run offline and block unexpected network access. They cover configuration, PKCE login,
refresh tokens, pagination, photo/video date selection, content verification across block
boundaries, Unicode paths, repeat runs, conflicts, symlinks, interrupted transfers, full disks,
concurrent runs, and CLI exit codes. Integration tests exercise the real Dropbox SDK against
simulated HTTP responses. No tests require an account or touch a personal media directory.
