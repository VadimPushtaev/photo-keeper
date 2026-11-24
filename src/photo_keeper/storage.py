"""Validate local paths and compare copies without overwriting them."""

import hashlib
import stat
from pathlib import Path

from photo_keeper.errors import KeeperError
from photo_keeper.hashing import hash_file as dropbox_hash
from photo_keeper.rclone import Rclone


def safe_path(root: Path, path: Path) -> None:
    """Refuse symlinks below the configured root, including broken symlinks."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise KeeperError("Destination path escapes the configured root.") from exc
    current = root
    for part in relative.parts:
        if part in {".", ".."}:
            raise KeeperError("Destination path escapes the configured root.")
        current /= part
        if current.is_symlink():
            raise KeeperError(f"Refusing destination symlink: {current}")


def _digest(client: Rclone, path: Path, kind: str) -> str:
    if kind == "dropbox":
        return dropbox_hash(path)
    if kind == "quickxor":
        return client.quickxor(path)
    with path.open("rb") as file:
        return hashlib.file_digest(file, kind).hexdigest()


def _existing_size(path: Path) -> int | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise KeeperError(f"Destination is not a regular file: {path}")
    return info.st_size


def _alternate(target: Path, digest: str) -> Path:
    name = f"{target.stem}--{digest}{target.suffix}"
    if len(name.encode()) > 255:
        # Preserve a normal extension and distinguish long names sharing a prefix.
        suffix = target.suffix.encode()[:32].decode(errors="ignore")
        name_hash = hashlib.sha256(target.name.encode()).hexdigest()[:12]
        ending = f"--{name_hash}--{digest}{suffix}"
        stem = target.stem.encode()[: 255 - len(ending.encode())].decode(errors="ignore")
        name = stem + ending
    return target.with_name(name)


def _choose_target(
    client: Rclone, root: Path, target: Path, size: int, checksum: tuple[str, str]
) -> tuple[Path, bool]:
    kind, digest = checksum
    alternate = _alternate(target, digest)
    safe_path(root, target)
    original_size = _existing_size(target)
    if original_size == size and _digest(client, target, kind) == digest:
        return target, True
    # Check the alternate too, since the original could have been moved out of the archive.
    safe_path(root, alternate)
    alternate_size = _existing_size(alternate)
    if alternate_size == size and _digest(client, alternate, kind) == digest:
        return alternate, True
    if original_size is None:
        return target, False
    if alternate_size is not None:
        raise KeeperError(f"Conflicting content already exists at {alternate}")
    return alternate, False
