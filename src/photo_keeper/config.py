"""Validated configuration for Dropbox copy chains."""

import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class ConfigError(ValueError):
    """The configuration could not be read or contains invalid values."""


@dataclass(frozen=True)
class ChainConfig:
    name: str
    enabled: bool = False
    source: str | None = None
    destination: Path | None = None
    subdirectory: str = "__raw__"
    recursive: bool = True
    date_source: str = "capture"


@dataclass(frozen=True)
class AppConfig:
    chains: tuple[ChainConfig, ...] = ()
    credentials_file: Path = Path(".secrets/dropbox.json")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ConfigError(
            f"{label} must be a nonempty name or path without surrounding whitespace."
        )
    if any(ord(char) < 32 for char in value):
        raise ConfigError(f"{label} must not contain control characters.")
    return value


def _relative_path(value: object, label: str) -> str:
    value = _text(value, label)
    if (
        PurePosixPath(value).is_absolute()
        or "\\" in value
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ConfigError(f"{label} must be a relative path without empty, '.' or '..' components.")
    return value


def load_config(path: Path) -> AppConfig:
    """Read and validate a chain inventory; reject typos and duplicate names."""
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot load {path}: {exc}") from exc

    if unknown := data.keys() - {"chains", "dropbox"}:
        raise ConfigError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")

    dropbox = data.get("dropbox", {})
    if not isinstance(dropbox, dict):
        raise ConfigError("'dropbox' must be a table.")
    if unknown := dropbox.keys() - {"credentials_file"}:
        raise ConfigError(f"Unknown Dropbox keys: {', '.join(sorted(unknown))}")
    credentials_file = Path(
        _text(dropbox.get("credentials_file", ".secrets/dropbox.json"), "credentials_file")
    ).expanduser()
    if not credentials_file.is_absolute():
        credentials_file = path.resolve().parent / credentials_file

    raw_chains = data.get("chains", [])
    if not isinstance(raw_chains, list):
        raise ConfigError("'chains' must be an array of tables ([[chains]]).")

    chains = []
    names: set[str] = set()
    for index, raw_chain in enumerate(raw_chains, start=1):
        label = f"Chain #{index}"
        if not isinstance(raw_chain, dict):
            raise ConfigError(f"{label} must be a table.")
        if unknown := raw_chain.keys() - {
            "name",
            "enabled",
            "source",
            "destination",
            "subdirectory",
            "recursive",
            "date_source",
        }:
            raise ConfigError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")

        name = _text(raw_chain.get("name"), f"{label} name")
        if name in names:
            raise ConfigError(f"Duplicate chain name: {name!r}.")

        enabled = raw_chain.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{label} 'enabled' must be a boolean.")

        source = raw_chain.get("source")
        if source is not None:
            source = _text(source, f"{label} source")
            if not source.startswith("/") or "\\" in source:
                raise ConfigError(f"{label} source must be an absolute Dropbox path (or '/').")
            if source != "/":
                _relative_path(source[1:], f"{label} source")

        destination = raw_chain.get("destination")
        if destination is not None:
            destination = Path(_text(destination, f"{label} destination")).expanduser()
            if not destination.is_absolute():
                raise ConfigError(f"{label} destination must be an absolute local path.")
        if enabled and (source is None or destination is None):
            raise ConfigError(f"{label}: enabled chains require source and destination.")

        subdirectory = _relative_path(raw_chain.get("subdirectory", "__raw__"), "subdirectory")
        recursive = raw_chain.get("recursive", True)
        if not isinstance(recursive, bool):
            raise ConfigError(f"{label} recursive must be a boolean.")
        date_source = raw_chain.get("date_source", "capture")
        if date_source not in ("capture", "modified"):
            raise ConfigError(f"{label} date_source must be 'capture' or 'modified'.")

        chains.append(
            ChainConfig(
                name=name,
                enabled=enabled,
                source=source,
                destination=destination,
                subdirectory=subdirectory,
                recursive=recursive,
                date_source=date_source,
            )
        )
        names.add(name)

    return AppConfig(chains=tuple(chains), credentials_file=credentials_file)
