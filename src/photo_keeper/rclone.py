"""Read files through named rclone remotes."""

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from photo_keeper.errors import KeeperError

_HASH_LENGTHS = {"sha256": 64, "dropbox": 64, "sha1": 40, "md5": 32, "quickxor": 40}


@dataclass(frozen=True)
class RemoteFile:
    path: Path
    size: int
    modified: datetime | None
    checksum: tuple[str, str] | None


def _parse_file(item: object) -> RemoteFile:
    if not isinstance(item, dict):
        raise KeeperError("rclone returned an invalid file listing.")
    name = item.get("Path")
    if not isinstance(name, str) or any(
        part in {"", ".", ".."}
        or "\\" in part
        or ":" in part
        or any(ord(char) < 32 for char in part)
        for part in name.split("/")
    ):
        raise KeeperError(f"rclone returned an unsafe file path: {name!r}")
    size = item.get("Size")
    if type(size) is not int or size < -1:
        raise KeeperError(f"rclone returned an invalid size for {name!r}.")

    timestamp = item.get("ModTime")
    try:
        modified = datetime.fromisoformat(timestamp) if timestamp else None
    except (ValueError, TypeError) as exc:
        raise KeeperError(f"rclone returned an invalid modification date for {name!r}.") from exc
    if modified is not None and modified.year == 1:
        modified = None  # rclone uses Go's zero time for unavailable dates.

    hashes = item.get("Hashes") or {}
    if not isinstance(hashes, dict):
        raise KeeperError(f"rclone returned invalid hashes for {name!r}.")
    for kind, length in _HASH_LENGTHS.items():
        digest = hashes.get(kind)
        if digest and isinstance(digest, str) and re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", digest):
            return RemoteFile(Path(name), size, modified, (kind, digest.lower()))
    return RemoteFile(Path(name), size, modified, None)


class Rclone:
    def __init__(self, config_file: Path | None = None) -> None:
        self.config_file = config_file

    def _run(
        self, *arguments: str, inherit_output: bool = False, input_text: str | None = None
    ) -> str:
        command = ["rclone"]
        if self.config_file is not None:
            command.extend(("--config", str(self.config_file)))
        try:
            result = subprocess.run(
                [*command, *arguments],
                check=True,
                text=True,
                capture_output=not inherit_output,
                input=input_text,
            )
        except FileNotFoundError as exc:
            raise KeeperError("rclone is required and must be available on PATH.") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip()
            message = f"rclone {arguments[0]} failed (exit {exc.returncode})"
            raise KeeperError(f"{message}: {detail}" if detail else message) from exc
        return result.stdout or ""

    def configure(self) -> None:
        self._run("config", inherit_output=True)

    def list_files(self, source: str, recursive: bool) -> list[RemoteFile]:
        flags = ("--recursive",) if recursive else ()
        output = self._run("lsjson", "--files-only", "--hash", *flags, "--", source)
        try:
            items = json.loads(output)
        except ValueError as exc:
            raise KeeperError("rclone returned invalid listing JSON.") from exc
        if not isinstance(items, list):
            raise KeeperError("rclone returned an invalid file listing.")
        files = [_parse_file(item) for item in items]
        seen: set[Path] = set()
        for entry in files:
            if entry.path in seen:
                raise KeeperError(
                    f"rclone listed multiple files at {entry.path}. Resolve the duplicate."
                )
            seen.add(entry.path)
        return files

    def download_batch(self, source: str, entries: Sequence[RemoteFile], target: Path) -> None:
        manifest = "".join(f"{entry.path.as_posix()}\n" for entry in entries)
        self._run(
            "copy",
            "--files-from-raw",
            "-",
            "--no-traverse",
            "--no-unicode-normalization",
            "--transfers",
            "4",
            "--stats",
            "5s",
            "--stats-one-line",
            "--stats-log-level",
            "NOTICE",
            "--",
            source,
            str(target),
            inherit_output=True,
            input_text=manifest,
        )

    def quickxor(self, path: Path) -> str:
        output = self._run("hashsum", "quickxor", "--", str(path))
        digest = output.split(maxsplit=1)[0] if output.strip() else ""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", digest):
            raise KeeperError(f"rclone returned an invalid QuickXor checksum for {path}.")
        return digest.lower()
