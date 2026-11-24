"""Run ordered copy and sort steps and verify their cleanup conditions."""

import fcntl
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC
from itertools import batched
from pathlib import Path

from photo_keeper.config import ChainConfig, StepConfig
from photo_keeper.errors import KeeperError
from photo_keeper.layout import Layout
from photo_keeper.media import capture_year, read_media_info
from photo_keeper.organize import (
    SortedCopy,
    UpstreamFiles,
    clean_raw,
    local_snapshot,
    process_local,
)
from photo_keeper.rclone import Rclone, RemoteFile
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
    sort_unchanged: int = 0
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


def _target(step: StepConfig, entry: RemoteFile, path: Path) -> Path:
    output = step.destination
    if not output.fields:
        return output.path / entry.path
    year = month = None
    if step.date_source == "capture":
        if "{month}" in output.fields:
            captured = read_media_info(path).capture_date
            if captured is not None:
                year, month = captured.year, captured.month
        else:
            year = capture_year(path)
    if year is None:
        if entry.modified is None:
            raise KeeperError(f"No usable date for {entry.path}.")
        year, month = entry.modified.year, entry.modified.month
    return output.render(year, month or 1) / entry.path


class ArchiveLookup:
    """Cache each relevant archive directory's names for the planning pass."""

    def __init__(self, layout: Layout):
        self.root = layout.root
        self.folders = list(layout.folders())
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
    client: Rclone, root: Path, step: StepConfig, entry: RemoteFile, archive: ArchiveLookup
) -> Path | None:
    if entry.checksum is None or entry.size < 0:
        return None
    if step.date_source == "modified" or not step.destination.fields:
        target = _target(step, entry, root)
        target, unchanged = _choose_target(client, root, target, entry.size, entry.checksum)
        return target if unchanged else None

    kind, digest = entry.checksum
    for folder in archive.folders:
        target = folder.path / entry.path
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
                if _target(step, entry, candidate) == target:
                    return candidate
    return None


def _publish_file(
    client: Rclone, root: Path, step: StepConfig, entry: RemoteFile, staged: Path
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

    target = _target(step, entry, staged)
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
    step: StepConfig,
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
                target, unchanged, size = _publish_file(client, root, step, entry, staged)
                if unchanged:
                    result.unchanged += 1
                    logger.info("Already verified: %s (%s)", target, _format_size(size))
                else:
                    result.copied += 1
                    logger.info("Copied %s (%s) to %s", entry.path, _format_size(size), target)
            except (KeeperError, OSError) as exc:
                result.failed += 1
                logger.error("Cannot copy %s: %s", entry.path, exc)


def _delete_source_batch(client: Rclone, source: str, paths: list[Path], result: RunResult) -> bool:
    logger.info("Deleting %d verified files from %s.", len(paths), source)
    try:
        client.delete_files(source, paths)
    except (KeeperError, OSError) as exc:
        result.failed += 1
        logger.error("Source cleanup failed for %s: %s", source, exc)
        return False
    result.source_deleted += len(paths)
    logger.info("Deleted %d verified source files.", len(paths))
    return True


def _clean_source(
    client: Rclone,
    root: Path,
    step: StepConfig,
    source: str,
    entries: list[RemoteFile],
    result: RunResult,
    *,
    dry_run: bool,
    upstream: list[RemoteFile],
) -> None:
    archive = ArchiveLookup(step.destination)
    earlier = UpstreamFiles(upstream)
    candidates: list[Path] = []
    for entry in entries:
        try:
            raw = _find_existing(client, root, step, entry, archive)
            if raw is None:
                continue
            assert entry.checksum is not None  # A verified match requires a remote checksum.
            if earlier.contains(client, raw, entry.path, entry.size, entry.checksum):
                continue
            if dry_run:
                result.source_delete_planned += 1
                logger.info(
                    "Would delete source file %s after verifying its output %s", entry.path, raw
                )
            else:
                candidates.append(entry.path)
        except (KeeperError, OSError) as exc:
            result.failed += 1
            logger.error("Cannot verify output copy for source cleanup of %s: %s", entry.path, exc)
        if len(candidates) == BATCH_SIZE:
            if not _delete_source_batch(client, source, candidates, result):
                return
            candidates = []
    if candidates:
        _delete_source_batch(client, source, candidates, result)


def _copy_remote(
    client: Rclone, step: StepConfig, source: str, result: RunResult, *, dry_run: bool
) -> list[RemoteFile]:
    root = step.destination.root
    archive = ArchiveLookup(step.destination)
    pending = []
    upstream = client.list_files(source, step.recursive)
    for entry in upstream:
        try:
            existing = _find_existing(client, root, step, entry, archive)
            if existing is not None:
                result.unchanged += 1
                logger.info("Already verified: %s (%s)", existing, _format_size(entry.size))
            elif dry_run:
                if step.date_source == "capture" and step.destination.fields:
                    precision = "date" if "{month}" in step.destination.fields else "year"
                    logger.info(
                        "Would download %s (%s) to check its capture %s.",
                        entry.path,
                        _format_size(entry.size),
                        precision,
                    )
                else:
                    target = _target(step, entry, root)
                    logger.info(
                        "Would check and copy %s (%s) into %s",
                        entry.path,
                        _format_size(entry.size),
                        target.parent,
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
        _copy_batch(client, root, step, source, batch, result)
    return upstream


@dataclass
class ExecutedStep:
    step: StepConfig
    source: str | tuple[Layout, ...]
    remote_files: list[RemoteFile] | None = None
    local_copies: list[SortedCopy] | None = None

    def snapshot(self, client: Rclone, *, dry_run: bool) -> list[RemoteFile]:
        if isinstance(self.source, str):
            if dry_run and self.remote_files is not None:
                return self.remote_files
            return client.list_files(self.source, self.step.recursive)
        return list(local_snapshot(client, self.source, recursive=self.step.recursive))


def _clean_steps(
    client: Rclone, runs: list[ExecutedStep], result: RunResult, *, dry_run: bool
) -> None:
    preceding: list[ExecutedStep] = []
    for run in runs:
        if run.step.delete_source:
            before = result.failed
            upstream = [
                entry
                for earlier in preceding
                for entry in earlier.snapshot(client, dry_run=dry_run)
            ]
            if isinstance(run.source, str):
                entries = run.snapshot(client, dry_run=dry_run)
                _clean_source(
                    client,
                    run.step.destination.root,
                    run.step,
                    run.source,
                    entries,
                    result,
                    dry_run=dry_run,
                    upstream=upstream,
                )
            elif run.local_copies:
                deleted, planned, failed = clean_raw(
                    client,
                    run.local_copies,
                    upstream,
                    dry_run=dry_run,
                )
                result.raw_deleted += deleted
                result.raw_delete_planned += planned
                result.failed += failed
            if result.failed > before:
                break
        preceding.append(run)


def run_chain(client: Rclone, chain: ChainConfig, *, dry_run: bool = False) -> RunResult:
    if not chain.enabled:
        raise KeeperError(f"Chain {chain.name!r} is disabled.")
    steps = chain.resolved_steps()
    if not steps:
        raise KeeperError("Chain requires at least one step with a source and destination.")

    result = RunResult()
    roots = {layout.root for step in steps for layout in step.outputs}
    roots.update(
        step.source.root for step in steps if step.delete_source and isinstance(step.source, Layout)
    )
    for root in roots:
        if not root.is_dir():
            raise KeeperError(f"Destination root must already exist: {root}")
        safe_path(Path(root.anchor), root)

    with ExitStack() as locks:
        if not dry_run:
            for root in sorted(roots):
                locks.enter_context(destination_lock(root))

        runs = []
        previous: tuple[Layout, ...] = ()
        for number, step in enumerate(steps, start=1):
            logger.info(
                "Step %d/%d: %s to %s", number, len(steps), step.kind, step.destination.path
            )
            source = step.source
            if source == "previous":
                source = previous
            elif isinstance(source, Layout):
                source = (source,)

            run = ExecutedStep(step, source)
            if isinstance(source, str):
                run.remote_files = _copy_remote(client, step, source, result, dry_run=dry_run)
            else:
                copies = process_local(client, step, source, dry_run=dry_run)
                run.local_copies = copies.copies
                result.failed += copies.failed
                if step.kind == "sort":
                    result.sorted += copies.sorted
                    result.sort_unchanged += copies.unchanged
                    result.sort_planned += copies.planned
                else:
                    result.copied += copies.sorted
                    result.unchanged += copies.unchanged
                    result.planned += copies.planned
            runs.append(run)
            previous = step.outputs

        _clean_steps(client, runs, result, dry_run=dry_run)
    return result
