"""Sort verified rclone downloads into the local archive."""

import fcntl
import hashlib
import logging
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC
from itertools import batched
from pathlib import Path

from photo_keeper.config import ChainConfig
from photo_keeper.errors import KeeperError
from photo_keeper.hashing import hash_file as dropbox_hash
from photo_keeper.media import capture_year
from photo_keeper.rclone import Rclone, RemoteFile

logger = logging.getLogger(__name__)
BATCH_SIZE = 16


@dataclass
class RunResult:
    copied: int = 0
    unchanged: int = 0
    planned: int = 0
    failed: int = 0


def _format_size(size: int) -> str:
    if size < 0:
        return "unknown size"
    if size < 1024:
        return f"{size} B"
    scaled = size / 1024
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        if scaled < 1024:
            return f"{scaled:.1f} {unit}"
        scaled /= 1024
    return f"{scaled:.1f} EiB"


@contextmanager
def destination_lock(root: Path) -> Iterator[None]:
    """Use a persistent lock inode, released by the OS even if a process crashes."""
    fd = os.open(root / ".photo-keeper.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise KeeperError(f"Another copy run is using {root}.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


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


def _year(entry: RemoteFile, date_source: str, path: Path) -> int:
    if date_source == "capture" and (year := capture_year(path)) is not None:
        return year
    if entry.modified is None:
        raise KeeperError(f"No usable date for {entry.path}.")
    return entry.modified.year


def _target(root: Path, chain: ChainConfig, entry: RemoteFile, year: int) -> Path:
    return root / f"{year:04d}" / chain.subdirectory / entry.path


class ArchiveLookup:
    """Cache each relevant archive directory's names for the planning pass."""

    def __init__(self, root: Path):
        self.root = root
        with os.scandir(root) as folders:
            self.years = sorted(
                int(folder.name)
                for folder in folders
                if len(folder.name) == 4
                and folder.name.isascii()
                and folder.name.isdecimal()
                and folder.is_dir(follow_symlinks=False)
            )
        self._names: dict[Path, set[str]] = {}

    def contains(self, path: Path) -> bool:
        parent = path.parent
        if parent not in self._names:
            try:
                safe_path(self.root, parent)
                with os.scandir(parent) as entries:
                    names = {entry.name.casefold() for entry in entries}
            except (KeeperError, FileNotFoundError, NotADirectoryError):
                names = set()
            self._names[parent] = names
        # The final stat and hash use the actual path, so this also works on NTFS.
        return path.name.casefold() in self._names[parent]


def _find_existing(
    client: Rclone, root: Path, chain: ChainConfig, entry: RemoteFile, archive: ArchiveLookup
) -> Path | None:
    if entry.checksum is None or entry.size < 0:
        return None
    if chain.date_source == "modified":
        if entry.modified is None:
            raise KeeperError(f"No usable date for {entry.path}.")
        target = _target(root, chain, entry, entry.modified.year)
        target, unchanged = _choose_target(client, root, target, entry.size, entry.checksum)
        return target if unchanged else None

    kind, digest = entry.checksum
    for year in archive.years:
        target = _target(root, chain, entry, year)
        for candidate in (target, _alternate(target, digest)):
            if not archive.contains(candidate):
                continue
            # Unrelated years can have an unrelated directory or symlink at this name.
            try:
                safe_path(root, candidate)
                size = _existing_size(candidate)
            except (KeeperError, NotADirectoryError):
                continue
            if size == entry.size and _digest(client, candidate, kind) == digest:
                if _year(entry, chain.date_source, candidate) == year:
                    return candidate
    return None


def _publish_file(
    client: Rclone, root: Path, chain: ChainConfig, entry: RemoteFile, staged: Path
) -> tuple[Path, bool, int]:
    size = _existing_size(staged)
    if size is None:
        raise KeeperError("rclone did not download the listed file.")
    if entry.size >= 0 and size != entry.size:
        raise KeeperError("Download failed size verification.")
    checksum = entry.checksum
    if checksum is None:
        checksum = ("sha256", _digest(client, staged, "sha256"))
    elif _digest(client, staged, checksum[0]) != checksum[1]:
        raise KeeperError("Download failed checksum verification.")

    year = _year(entry, chain.date_source, staged)
    target = _target(root, chain, entry, year)
    target, unchanged = _choose_target(client, root, target, size, checksum)
    if not unchanged:
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_path(root, target)
        if entry.modified is not None:
            modified = entry.modified
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=UTC)
            os.utime(staged, (modified.timestamp(), modified.timestamp()))
        with staged.open("rb") as file:
            os.fsync(file.fileno())
        os.link(staged, target)  # Fails if the target appeared concurrently.
    return target, unchanged, size


def _log_batch(entries: tuple[RemoteFile, ...], number: int, count: int) -> None:
    known = [entry for entry in entries if entry.size >= 0]
    file_count = f"{len(entries)} {'file' if len(entries) == 1 else 'files'}"
    if not known:
        logger.info("Downloading batch %d/%d (%s, size unknown).", number, count, file_count)
        return
    total = _format_size(sum(entry.size for entry in known))
    unknown = len(entries) - len(known)
    if unknown:
        total += f" listed, {unknown} with unknown size"
    largest = max(known, key=lambda entry: entry.size)
    logger.info(
        "Downloading batch %d/%d (%s, %s). Largest listed file is %s (%s).",
        number,
        count,
        file_count,
        total,
        largest.path,
        _format_size(largest.size),
    )


def _copy_batch(
    client: Rclone,
    root: Path,
    chain: ChainConfig,
    source: str,
    entries: tuple[RemoteFile, ...],
    result: RunResult,
) -> None:
    with tempfile.TemporaryDirectory(prefix=".photo-keeper-", dir=root) as folder:
        staging = Path(folder)
        batch_failed = False
        try:
            client.download_batch(source, entries, staging)
        except KeeperError as exc:
            batch_failed = True
            logger.warning("rclone batch reported an error: %s", exc)

        logger.info("Verifying and filing the downloaded batch.")
        for entry in entries:
            try:
                # A failed transfer can leave a staged file behind. Match the listed
                # checksum before publishing from a failed batch.
                if batch_failed and entry.checksum is None:
                    raise KeeperError(
                        "Cannot verify this file after a failed batch without a remote checksum."
                    )
                staged = staging / entry.path
                safe_path(staging, staged)
                target, unchanged, size = _publish_file(client, root, chain, entry, staged)
                if unchanged:
                    result.unchanged += 1
                    logger.info("Already verified: %s (%s)", target, _format_size(size))
                else:
                    result.copied += 1
                    logger.info("Copied %s (%s) to %s", entry.path, _format_size(size), target)
            except (KeeperError, OSError) as exc:
                result.failed += 1
                logger.error("Cannot copy %s: %s", entry.path, exc)


def run_chain(client: Rclone, chain: ChainConfig, *, dry_run: bool = False) -> RunResult:
    if not chain.enabled:
        raise KeeperError(f"Chain {chain.name!r} is disabled.")
    if chain.source is None or chain.destination is None:
        raise KeeperError("Chain requires a source and destination.")
    if not chain.destination.is_dir():
        raise KeeperError(f"Destination root must already exist: {chain.destination}")
    root = chain.destination.resolve()
    result = RunResult()

    with nullcontext() if dry_run else destination_lock(root):
        archive = ArchiveLookup(root)
        pending = []
        for entry in client.list_files(chain.source, chain.recursive):
            try:
                existing = _find_existing(client, root, chain, entry, archive)
                if existing is not None:
                    result.unchanged += 1
                    logger.info("Already verified: %s (%s)", existing, _format_size(entry.size))
                elif dry_run:
                    if chain.date_source == "modified":
                        year = _year(entry, chain.date_source, root)
                        target = _target(root, chain, entry, year)
                        logger.info(
                            "Would check and copy %s (%s) into %s",
                            entry.path,
                            _format_size(entry.size),
                            target.parent,
                        )
                    else:
                        logger.info(
                            "Would download %s (%s) to check its capture year.",
                            entry.path,
                            _format_size(entry.size),
                        )
                    result.planned += 1
                else:
                    pending.append(entry)
            except (KeeperError, OSError) as exc:
                result.failed += 1
                logger.error("Cannot copy %s: %s", entry.path, exc)

        batch_count = (len(pending) + BATCH_SIZE - 1) // BATCH_SIZE
        for number, batch in enumerate(batched(pending, BATCH_SIZE), start=1):
            _log_batch(batch, number, batch_count)
            _copy_batch(client, root, chain, chain.source, batch, result)
    return result
