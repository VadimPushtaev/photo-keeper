"""Copy raw files into month folders and verify both copies before local cleanup."""

import logging
import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from photo_keeper.config import ChainConfig
from photo_keeper.errors import KeeperError
from photo_keeper.media import read_media_info
from photo_keeper.rclone import Rclone, RemoteFile
from photo_keeper.storage import _choose_target, _digest, _existing_size, safe_path

logger = logging.getLogger(__name__)
_VIDEO_EXTENSIONS = frozenset(
    {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm", ".mts", ".m2ts", ".3gp", ".mpg", ".mpeg"}
)


@dataclass(frozen=True)
class SortedCopy:
    raw: Path
    relative: Path
    archive: Path
    size: int
    digest: str


@dataclass
class SortResult:
    copies: list[SortedCopy]
    sorted: int = 0
    unchanged: int = 0
    planned: int = 0
    failed: int = 0


def _raw_files(root: Path, subdirectory: str, years: Sequence[int]) -> Iterator[tuple[Path, Path]]:
    for year in years:
        raw_root = root / f"{year:04d}" / subdirectory
        safe_path(root, raw_root)
        for folder, _, filenames in raw_root.walk(on_error=_ignore_missing):
            for filename in filenames:
                file = folder / filename
                if not file.is_symlink() and file.is_file():
                    yield raw_root, file


def _ignore_missing(exc: OSError) -> None:
    if not isinstance(exc, FileNotFoundError):
        raise exc


def _month_target(chain: ChainConfig, raw_root: Path, path: Path, modified: float) -> Path:
    extension = path.suffix.lower()
    jpeg = extension in {".jpg", ".jpeg"}
    metadata = read_media_info(path) if jpeg or chain.date_source == "capture" else None
    captured = metadata.capture_date if metadata and chain.date_source == "capture" else None
    month = captured.month if captured else datetime.fromtimestamp(modified, UTC).month
    destination = raw_root.parent / f"{month:02d}"
    if extension in _VIDEO_EXTENSIONS:
        destination /= "video"
    elif extension == ".png":
        destination /= "screenshot"
    elif jpeg and metadata and not metadata.has_camera:
        destination /= "pics"
    return destination / path.relative_to(raw_root)


def _copy_original(client: Rclone, root: Path, copy: SortedCopy, mtime_ns: int) -> None:
    copy.archive.parent.mkdir(parents=True, exist_ok=True)
    safe_path(root, copy.archive)
    with tempfile.NamedTemporaryFile(prefix=".photo-keeper-", dir=copy.archive.parent) as staged:
        tmp = Path(staged.name)
        with copy.raw.open("rb") as source:
            shutil.copyfileobj(source, staged, length=1024 * 1024)
        staged.flush()
        if not _matches(client, root, tmp, copy.size, copy.digest):
            raise KeeperError("Raw file changed or the monthly copy failed verification.")
        os.utime(tmp, ns=(mtime_ns, mtime_ns))
        os.fsync(staged.fileno())
        os.link(tmp, copy.archive)


def _matches(client: Rclone, root: Path, path: Path, size: int, digest: str) -> bool:
    safe_path(root, path)
    return _existing_size(path) == size and _digest(client, path, "sha256") == digest


def sort_raw(
    client: Rclone, root: Path, chain: ChainConfig, years: Sequence[int], *, dry_run: bool
) -> SortResult:
    result = SortResult(copies=[])
    for raw_root, raw in _raw_files(root, chain.subdirectory, years):
        try:
            safe_path(root, raw)
            info = raw.stat()
            target = _month_target(chain, raw_root, raw, info.st_mtime)
            digest = _digest(client, raw, "sha256")
            target, unchanged = _choose_target(
                client, root, target, info.st_size, ("sha256", digest)
            )
            copy = SortedCopy(raw, raw.relative_to(raw_root), target, info.st_size, digest)
            if unchanged:
                result.copies.append(copy)
                result.unchanged += 1
            elif dry_run:
                result.planned += 1
                logger.info("Would sort %s to %s", raw, target)
            else:
                _copy_original(client, root, copy, info.st_mtime_ns)
                result.copies.append(copy)
                result.sorted += 1
                logger.info("Sorted %s to %s", raw, target)
        except (KeeperError, OSError) as exc:
            result.failed += 1
            logger.error("Cannot sort %s: %s", raw, exc)
    return result


def clean_raw(
    client: Rclone,
    root: Path,
    copies: Sequence[SortedCopy],
    upstream: Sequence[RemoteFile],
    *,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Remove raw originals only when upstream is absent and the monthly copy matches."""
    names = {entry.path.as_posix().casefold() for entry in upstream}
    by_size: dict[int, list[RemoteFile]] = defaultdict(list)
    for entry in upstream:
        by_size[entry.size].append(entry)

    removed = planned = failures = 0
    for copy in copies:
        if copy.relative.as_posix().casefold() in names or by_size[-1]:
            continue
        try:
            safe_path(root, copy.raw)
            raw_checksums = {"sha256": copy.digest}
            for entry in by_size[copy.size]:
                if entry.checksum is None:
                    break  # Can't prove this same-size upstream file is a different file.
                kind, expected = entry.checksum
                if kind not in raw_checksums:
                    raw_checksums[kind] = _digest(client, copy.raw, kind)
                if raw_checksums[kind] == expected:
                    break
            else:
                if not (
                    _matches(client, root, copy.raw, copy.size, copy.digest)
                    and _matches(client, root, copy.archive, copy.size, copy.digest)
                ):
                    raise KeeperError("Raw and monthly files no longer match.")
                if dry_run:
                    planned += 1
                    logger.info("Would delete raw file %s", copy.raw)
                else:
                    copy.raw.unlink()
                    removed += 1
                    logger.info("Deleted raw file %s", copy.raw)
        except (KeeperError, OSError) as exc:
            failures += 1
            logger.error("Cannot delete raw file %s: %s", copy.raw, exc)
    return removed, planned, failures
