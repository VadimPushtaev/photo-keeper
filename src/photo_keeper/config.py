"""Validated configuration for rclone copy chains."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from photo_keeper.layout import FIELDS, Layout
from photo_keeper.rclone import HOLD_FOLDER


class ConfigError(ValueError):
    """The configuration could not be read or contains invalid values."""


@dataclass(frozen=True)
class DeletionConfig:
    source: bool = False
    raw: bool = False
    archive: bool = False


@dataclass(frozen=True)
class StepConfig:
    kind: str
    source: str | Layout  # An rclone remote, a local layout, or "previous".
    destination: Layout
    routes: tuple[tuple[str, Layout], ...] = ()
    recursive: bool = True
    date_source: str = "capture"
    delete_source: bool = False

    @property
    def outputs(self) -> tuple[Layout, ...]:
        return (self.destination, *(layout for _, layout in self.routes))


@dataclass(frozen=True)
class ChainConfig:
    name: str
    enabled: bool = False
    source: str | None = None
    destination: Path | None = None
    subdirectory: str = "__raw__"
    recursive: bool = True
    date_source: str = "capture"
    sort_by_month: bool = False
    delete: DeletionConfig = DeletionConfig()
    steps: tuple[StepConfig, ...] = ()

    def resolved_steps(self) -> tuple[StepConfig, ...]:
        if self.steps:
            return self.steps
        if self.source is None or self.destination is None:
            return ()
        raw = Layout(self.destination / "{year}" / self.subdirectory)
        copy = StepConfig(
            "copy",
            self.source,
            raw,
            recursive=self.recursive,
            date_source=self.date_source,
            delete_source=self.delete.source,
        )
        if not self.sort_by_month:
            return (copy,)
        month = raw.path.parent / "{month}"
        sort = StepConfig(
            "sort",
            "previous",
            Layout(month),
            routes=tuple(
                (key, Layout(month / folder))
                for key, folder in (
                    ("video", "video"),
                    ("png", "screenshot"),
                    ("jpeg_without_camera", "pics"),
                )
            ),
            date_source=self.date_source,
            delete_source=self.delete.raw,
        )
        return (copy, sort)


@dataclass(frozen=True)
class AppConfig:
    chains: tuple[ChainConfig, ...] = ()
    rclone_config: Path | None = None


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


def _deletion_settings(value: object, label: str) -> DeletionConfig:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} delete must be a table.")
    for step, enabled in value.items():
        if step not in {"source", "raw", "archive"} or not isinstance(enabled, bool):
            raise ConfigError(
                f"{label} delete.{step} must be a boolean for source, raw, or archive."
            )
    return DeletionConfig(**value)


def _remote_source(value: object, label: str) -> str:
    source = _text(value, label)
    remote, separator, folder = source.partition(":")
    if not separator or not re.fullmatch(r"[\w][\w .-]*", remote) or remote.endswith(" "):
        raise ConfigError(f"{label} must be a named rclone remote (remote:path).")
    if folder not in ("", "/"):
        _relative_path(folder.removeprefix("/"), label)
    if folder.strip("/").split("/")[0].casefold() == HOLD_FOLDER:
        raise ConfigError(f"{label} is inside the reserved cleanup holding area.")
    return source


def _layout(value: object, label: str) -> Layout:
    text = _text(value, label)
    path = Path(text).expanduser()
    if not path.is_absolute() or path == Path("/") or "\\" in text or ":" in text:
        raise ConfigError(f"{label} must be an absolute local folder.")
    seen = set()
    for part in path.parts:
        if part == ".." or (("{" in part or "}" in part) and part not in FIELDS) or part in seen:
            raise ConfigError(f"{label} uses an invalid folder or date placeholder.")
        if part in FIELDS:
            seen.add(part)
    return Layout(path)


def _steps(value: object, chain_label: str) -> tuple[StepConfig, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{chain_label} steps must be a nonempty array of tables.")
    steps = []
    protected: list[Layout] = []
    for number, table in enumerate(value, start=1):
        label = f"{chain_label} step {number}"
        if not isinstance(table, dict):
            raise ConfigError(f"{label} must be a table.")
        if unknown := table.keys() - {
            "type",
            "from",
            "to",
            "routes",
            "recursive",
            "date_source",
            "delete_source",
        }:
            raise ConfigError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")
        kind = table.get("type")
        if kind not in ("copy", "sort"):
            raise ConfigError(f"{label} type must be 'copy' or 'sort'.")
        source_value = table.get("from")
        if source_value == "previous" and steps:
            source = "previous"
        elif isinstance(source_value, dict) and source_value.keys() == {"local"}:
            source = _layout(source_value["local"], f"{label} from.local")
            protected.append(source)
        elif (
            kind == "copy" and isinstance(source_value, dict) and source_value.keys() == {"rclone"}
        ):
            source = _remote_source(source_value["rclone"], f"{label} from.rclone")
        else:
            raise ConfigError(
                f"{label} needs from.local, from.rclone for copy, or 'previous' after step 1."
            )
        destination = _layout(table.get("to"), f"{label} to")
        routes_value = table.get("routes", {})
        if not isinstance(routes_value, dict) or (kind == "copy" and routes_value):
            raise ConfigError(f"{label} routes must be a table on a sort step.")
        routes = []
        for key, value in routes_value.items():
            if key not in {"video", "png", "jpeg_without_camera"}:
                raise ConfigError(f"{label} has an unknown route {key!r}.")
            route = _text(value, f"{label} routes.{key}")
            path = (
                route
                if Path(route).expanduser().is_absolute()
                else str(destination.path / _relative_path(route, "route"))
            )
            layout = _layout(path, f"{label} routes.{key}")
            if layout.path.is_relative_to(destination.path):
                layout = Layout(layout.path, base=destination.root)
            routes.append((key, layout))
        recursive, delete = table.get("recursive", True), table.get("delete_source", False)
        if not isinstance(recursive, bool) or not isinstance(delete, bool):
            raise ConfigError(f"{label} recursive and delete_source must be booleans.")
        if (
            delete
            and isinstance(source, str)
            and source != "previous"
            and not source.partition(":")[2].strip("/")
        ):
            raise ConfigError(
                f"{label} source cleanup requires a named folder, not the remote root."
            )
        date_source = table.get("date_source", "capture")
        if date_source not in ("capture", "modified"):
            raise ConfigError(f"{label} date_source must be 'capture' or 'modified'.")
        step = StepConfig(kind, source, destination, tuple(routes), recursive, date_source, delete)
        if any(output.overlaps(other) for output in step.outputs for other in protected):
            raise ConfigError(f"{label} output overlaps an input or another step's output.")
        protected.extend(step.outputs)
        steps.append(step)
    return tuple(steps)


def load_config(path: Path) -> AppConfig:
    """Read and validate a chain inventory; reject typos and duplicate names."""
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot load {path}: {exc}") from exc

    if "dropbox" in data:
        raise ConfigError("Replace [dropbox] with rclone configuration. See the README.")
    if unknown := data.keys() - {"chains", "rclone"}:
        raise ConfigError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")

    rclone = data.get("rclone", {})
    if not isinstance(rclone, dict):
        raise ConfigError("'rclone' must be a table.")
    if unknown := rclone.keys() - {"config_file"}:
        raise ConfigError(f"Unknown rclone keys: {', '.join(sorted(unknown))}")
    rclone_config = None
    if "config_file" in rclone:
        rclone_config = Path(_text(rclone["config_file"], "config_file")).expanduser()
        if not rclone_config.is_absolute():
            rclone_config = path.resolve().parent / rclone_config

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
            "sort_by_month",
            "delete",
            "steps",
        }:
            raise ConfigError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")

        name = _text(raw_chain.get("name"), f"{label} name")
        if name in names:
            raise ConfigError(f"Duplicate chain name: {name!r}.")

        enabled = raw_chain.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{label} 'enabled' must be a boolean.")

        if "steps" in raw_chain:
            if raw_chain.keys() - {"name", "enabled", "steps"}:
                raise ConfigError(f"{label} cannot mix explicit steps with legacy settings.")
            chains.append(
                ChainConfig(name=name, enabled=enabled, steps=_steps(raw_chain["steps"], label))
            )
            names.add(name)
            continue

        source = raw_chain.get("source")
        if source is not None:
            source = _remote_source(source, f"{label} source")

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
        sort_by_month = raw_chain.get("sort_by_month", False)
        if not isinstance(sort_by_month, bool):
            raise ConfigError(f"{label} sort_by_month must be a boolean.")
        if sort_by_month and PurePosixPath(subdirectory).name != "__raw__":
            raise ConfigError(f"{label} month sorting requires subdirectory to end in '__raw__'.")
        deletion = _deletion_settings(raw_chain.get("delete", {}), label)
        if deletion.source and source is not None and not source.partition(":")[2].strip("/"):
            raise ConfigError(
                f"{label} source cleanup requires a named folder, not the remote root."
            )

        chains.append(
            ChainConfig(
                name=name,
                enabled=enabled,
                source=source,
                destination=destination,
                subdirectory=subdirectory,
                recursive=recursive,
                date_source=date_source,
                sort_by_month=sort_by_month,
                delete=deletion,
            )
        )
        names.add(name)

    return AppConfig(chains=tuple(chains), rclone_config=rclone_config)
