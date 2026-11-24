"""Sort verified rclone downloads into the local archive."""

import fcntl
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC
from itertools import batched
from pathlib import Path

from photo_keeper.config import ChainConfig
from photo_keeper.errors import KeeperError
from photo_keeper.media import capture_year
from photo_keeper.organize import clean_raw, sort_raw
from photo_keeper.rclone import Rclone, RemoteFile, holding_area
from photo_keeper.storage import _alternate, _choose_target, _digest, _existing_size, safe_path

logger = logging.getLogger(__name__)
BATCH_SIZE = 16


@dataclass
class RunResult:
    copied: int = 0
    unchanged: int = 0
    planned: int = 0
    failed: int = 0
    sorted: int = 0
    sort_planned: int = 0
    source_deleted: int = 0
    raw_deleted: int = 0
    source_delete_planned: int = 0
    raw_delete_planned: int = 0


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


def _delete_source_batch(
    client: Rclone,
    root: Path,
    source: str,
    batch: tuple[tuple[RemoteFile, Path], ...],
    result: RunResult,
) -> None:
    holding = holding_area(source)
    logger.info("Holding up to %d source files at %s until verified.", len(batch), holding)
    try:
        client.move_to_holding(source, holding, [entry.path for entry, _ in batch])
        held = {entry.path: entry for entry in client.list_files(holding, recursive=True)}
        confirmed: list[Path] = []
        for original, raw in batch:
            moved = held.get(original.path)
            checksum = original.checksum
            safe_path(root, raw)
            if (
                moved is None
                or checksum is None
                or moved.checksum != checksum
                or moved.size != original.size
                or _existing_size(raw) != original.size
                or _digest(client, raw, checksum[0]) != checksum[1]
            ):
                result.failed += 1
                logger.error(
                    "Could not verify %s. Any moved copy stays at %s.", original.path, holding
                )
            else:
                confirmed.append(original.path)
        if confirmed:
            client.remove_held(holding, confirmed)
            result.source_deleted += len(confirmed)
            logger.info("Deleted %d verified source files.", len(confirmed))
            if len(confirmed) == len(held):
                try:
                    client.prune_holding(holding)
                except KeeperError as exc:
                    logger.warning(
                        "Could not remove empty holding directories at %s: %s", holding, exc
                    )
    except (KeeperError, OSError) as exc:
        result.failed += 1
        logger.error("Source cleanup failed. Check %s for held files. %s", holding, exc)


def _clean_source(
    client: Rclone,
    root: Path,
    chain: ChainConfig,
    source: str,
    upstream: list[RemoteFile],
    result: RunResult,
    *,
    dry_run: bool,
) -> None:
    archive = ArchiveLookup(root)
    candidates = []
    for entry in upstream:
        try:
            raw = _find_existing(client, root, chain, entry, archive)
            if raw is None:
                continue
            if dry_run:
                result.source_delete_planned += 1
                logger.info(
                    "Would delete source file %s after verifying raw copy %s", entry.path, raw
                )
            else:
                candidates.append((entry, raw))
        except (KeeperError, OSError) as exc:
            result.failed += 1
            logger.error("Cannot verify raw copy for source cleanup of %s: %s", entry.path, exc)
    for batch in batched(candidates, BATCH_SIZE):
        _delete_source_batch(client, root, source, batch, result)


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
        upstream = client.list_files(chain.source, chain.recursive)
        for entry in upstream:
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

        if chain.sort_by_month:
            logger.info("Checking raw files for month sorting.")
            sorted_files = sort_raw(
                client,
                root,
                chain,
                ArchiveLookup(root).years,
                dry_run=dry_run,
            )
            result.sorted = sorted_files.sorted
            result.sort_planned = sorted_files.planned
            result.failed += sorted_files.failed

        before_source_cleanup = result.failed
        if chain.delete.source:
            if not dry_run:
                upstream = client.list_files(chain.source, chain.recursive)
            _clean_source(client, root, chain, chain.source, upstream, result, dry_run=dry_run)
        if chain.sort_by_month and chain.delete.raw and before_source_cleanup == result.failed:
            if not dry_run:
                upstream = client.list_files(chain.source, chain.recursive)
            result.raw_deleted, result.raw_delete_planned, failures = clean_raw(
                client,
                root,
                sorted_files.copies,
                upstream,
                dry_run=dry_run,
            )
            result.failed += failures
    return result
