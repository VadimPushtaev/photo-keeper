"""Load the chain inventory from TOML without performing media operations."""

import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """The configuration could not be read or contains invalid values."""


@dataclass(frozen=True)
class ChainConfig:
    name: str
    enabled: bool = False


@dataclass(frozen=True)
class AppConfig:
    chains: tuple[ChainConfig, ...] = ()


def load_config(path: Path) -> AppConfig:
    """Read and validate a chain inventory; reject typos and duplicate names."""
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot load {path}: {exc}") from exc

    if unknown := data.keys() - {"chains"}:
        raise ConfigError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")

    raw_chains = data.get("chains", [])
    if not isinstance(raw_chains, list):
        raise ConfigError("'chains' must be an array of tables ([[chains]]).")

    chains = []
    names: set[str] = set()
    for index, raw_chain in enumerate(raw_chains, start=1):
        label = f"Chain #{index}"
        if not isinstance(raw_chain, dict):
            raise ConfigError(f"{label} must be a table.")
        if unknown := raw_chain.keys() - {"name", "enabled"}:
            raise ConfigError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")

        name = raw_chain.get("name")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ConfigError(f"{label} needs a nonempty name without surrounding whitespace.")
        if name in names:
            raise ConfigError(f"Duplicate chain name: {name!r}.")

        enabled = raw_chain.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{label} 'enabled' must be a boolean.")

        chains.append(ChainConfig(name=name, enabled=enabled))
        names.add(name)

    return AppConfig(chains=tuple(chains))
