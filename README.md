# Photo Keeper

A Poetry-based Python application for configurable backup chains for photos and other media.
The intended model is a command that runs a named chain once and exits, with cron managing the
schedule. Chains will eventually describe copying, organizing, and optional cleanup of media.

This repository currently contains the scaffold: packaging, a CLI, configuration validation,
logging, and development checks. Chain execution, storage providers, sorting rules, and deletion
are not implemented. No cron jobs are installed.

## Setup

Requires Python 3.12+ and Poetry 2.x.

```bash
poetry install
cp config.example.toml config.toml
poetry run photo-keeper --config config.toml check-config
poetry run photo-keeper --config config.toml list-chains
```

Poetry creates the virtual environment in `.venv/`. Local `config.toml`, `.env` files, `data/`,
and `logs/` are ignored by Git. Commit `poetry.lock` to keep dependency versions reproducible.

## Configuration

Configuration uses TOML and currently supports only an inventory of named chains:

```toml
[[chains]]
name = "example"
enabled = false
```

Add another `[[chains]]` table for each chain. Names must be unique and nonempty; `enabled`
defaults to `false`. Unknown keys are rejected so spelling mistakes are visible. An empty
inventory (`chains = []`) is valid. Provider settings and chain steps will be added alongside
their implementations.

## CLI and scheduling

```bash
poetry run photo-keeper --help
poetry run photo-keeper --config /absolute/path/to/config.toml check-config
poetry run photo-keeper --config /absolute/path/to/config.toml list-chains
poetry run photo-keeper --config /absolute/path/to/config.toml run example
```

The `run` command is a placeholder. It rejects unknown or disabled chains and reports that
execution is not implemented for an enabled chain. It never reports a successful backup.
Exit codes are `0` for successful inspection, `1` for unavailable execution, and `2` for
configuration or usage errors. Command results go to stdout; timestamped logs go to stderr.
The same CLI is available via `poetry run python -m photo_keeper`.

When execution is implemented, cron can invoke the installed `.venv/bin/photo-keeper` directly
with absolute executable and configuration paths. Each invocation will be responsible for
one run; the application will not need an internal scheduler. Do not schedule the placeholder.

## Development

```bash
make check   # Ruff, formatting, tests, and Poetry metadata/lock validation
make format
make build
```

The package lives in `src/photo_keeper/`; `cli.py` handles commands and `config.py` owns the
configuration model. Tests live in `tests/`. The runtime currently uses only the Python standard
library; pytest and Ruff are development dependencies.

The first planned chain will pull new Dropbox media to local storage, route it by type, state,
and size, and eventually remove eligible originals from Dropbox. Its provider integration,
classification rules, progress tracking, and cleanup policy are deliberately left for the
implementation phase.
