"""Run local copy and sort steps, and verify their inputs before cleanup."""

import logging
import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from photo_keeper.config import StepConfig
from photo_keeper.errors import KeeperError
from photo_keeper.layout import Folder, Layout
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
    raw_root: Path
    archive_root: Path


@dataclass
class SortResult:
    copies: list[SortedCopy]
    sorted: int = 0
    unchanged: int = 0
    planned: int = 0
    failed: int = 0


@dataclass(frozen=True)
class LocalFile:
    layout: Layout
    folder: Folder
    path: Path

    @property
    def relative(self) -> Path:
        return self.path.relative_to(self.folder.path)


def local_files(layouts: Sequence[Layout], *, recursive: bool) -> Iterator[LocalFile]:
    locations = {folder.path: (layout, folder) for layout in layouts for folder in layout.folders()}
    for layout, base in locations.values():
        safe_path(layout.root, base.path)
        for folder, directories, filenames in base.path.walk(on_error=_ignore_missing):
            # A route such as {month}/video belongs to the route, not also to {month}.
            directories[:] = [name for name in directories if folder / name not in locations]
            for filename in filenames:
                if filename.startswith(".photo-keeper"):
                    continue
                file = folder / filename
                if not file.is_symlink() and file.is_file():
                    yield LocalFile(layout, base, file)
            if not recursive:
                break


def _ignore_missing(exc: OSError) -> None:
    if not isinstance(exc, FileNotFoundError):
        raise exc


def _destination(step: StepConfig, item: LocalFile, modified: float) -> tuple[Layout, Path]:
    extension = item.path.suffix.lower()
    routes = dict(step.routes)
    inspect_camera = extension in {".jpg", ".jpeg"} and "jpeg_without_camera" in routes
    inspect_date = step.date_source == "capture" and any(layout.fields for layout in step.outputs)
    media = read_media_info(item.path) if inspect_camera or inspect_date else None
    captured = media.capture_date if media and inspect_date else None
    stamp = captured or datetime.fromtimestamp(modified, UTC)
    output = step.destination
    if extension in _VIDEO_EXTENSIONS:
        output = routes.get("video", output)
    elif extension == ".png":
        output = routes.get("png", output)
    elif inspect_camera and media is not None and not media.has_camera:
        output = routes["jpeg_without_camera"]
    year = item.folder.year if item.folder.year is not None else stamp.year
    month = item.folder.month if item.folder.month is not None else stamp.month
    return output, output.render(year, month) / item.relative


def _copy_original(client: Rclone, copy: SortedCopy, mtime_ns: int) -> None:
    copy.archive.parent.mkdir(parents=True, exist_ok=True)
    safe_path(copy.archive_root, copy.archive)
    with tempfile.NamedTemporaryFile(prefix=".photo-keeper-", dir=copy.archive.parent) as staged:
        tmp = Path(staged.name)
        with copy.raw.open("rb") as source:
            shutil.copyfileobj(source, staged, length=1024 * 1024)
        staged.flush()
        if not _matches(client, copy.archive_root, tmp, copy.size, copy.digest):
            raise KeeperError("Local input changed or the output failed verification.")
        os.utime(tmp, ns=(mtime_ns, mtime_ns))
        os.fsync(staged.fileno())
        os.link(tmp, copy.archive)


def _matches(client: Rclone, root: Path, path: Path, size: int, digest: str) -> bool:
    safe_path(root, path)
    return _existing_size(path) == size and _digest(client, path, "sha256") == digest


def process_local(
    client: Rclone, step: StepConfig, sources: Sequence[Layout], *, dry_run: bool
) -> SortResult:
    result = SortResult(copies=[])
    for item in local_files(sources, recursive=step.recursive):
        try:
            raw = item.path
            safe_path(item.layout.root, raw)
            info = raw.stat()
            output, target = _destination(step, item, info.st_mtime)
            digest = _digest(client, raw, "sha256")
            target, unchanged = _choose_target(
                client, output.root, target, info.st_size, ("sha256", digest)
            )
            copy = SortedCopy(
                raw, item.relative, target, info.st_size, digest, item.layout.root, output.root
            )
            if unchanged:
                result.copies.append(copy)
                result.unchanged += 1
            elif dry_run:
                result.planned += 1
                logger.info("Would %s %s to %s", step.kind, raw, target)
            else:
                _copy_original(client, copy, info.st_mtime_ns)
                result.copies.append(copy)
                result.sorted += 1
                logger.info(
                    "%s %s to %s", "Sorted" if step.kind == "sort" else "Copied", raw, target
                )
        except (KeeperError, OSError) as exc:
            result.failed += 1
            logger.error("Cannot %s %s: %s", step.kind, item.path, exc)
    return result


def local_snapshot(
    client: Rclone, layouts: Sequence[Layout], *, recursive: bool
) -> Iterator[RemoteFile]:
    for item in local_files(layouts, recursive=recursive):
        safe_path(item.layout.root, item.path)
        yield RemoteFile(
            item.relative,
            item.path.stat().st_size,
            None,
            ("sha256", _digest(client, item.path, "sha256")),
        )


class UpstreamFiles:
    """A fresh listing of earlier inputs, including uncertain matches."""

    def __init__(self, entries: Sequence[RemoteFile]):
        self.names = {entry.path.as_posix().casefold() for entry in entries}
        self.by_size: dict[int, list[RemoteFile]] = defaultdict(list)
        for entry in entries:
            self.by_size[entry.size].append(entry)

    def contains(
        self, client: Rclone, path: Path, relative: Path, size: int, checksum: tuple[str, str]
    ) -> bool:
        if relative.as_posix().casefold() in self.names or self.by_size[-1]:
            return True
        original_kind, original_digest = checksum
        hashes = {original_kind: original_digest}
        for entry in self.by_size[size]:
            if entry.checksum is None:
                return True
            kind, expected = entry.checksum
            if kind not in hashes:
                hashes[kind] = _digest(client, path, kind)
            if hashes[kind] == expected:
                return True
        return False


def clean_raw(
    client: Rclone,
    copies: Sequence[SortedCopy],
    upstream: Sequence[RemoteFile],
    *,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Remove raw originals only when upstream is absent and the monthly copy matches."""
    earlier = UpstreamFiles(upstream)
    removed = planned = failures = 0
    for copy in copies:
        try:
            safe_path(copy.raw_root, copy.raw)
            if earlier.contains(
                client, copy.raw, copy.relative, copy.size, ("sha256", copy.digest)
            ):
                continue
            if not (
                _matches(client, copy.raw_root, copy.raw, copy.size, copy.digest)
                and _matches(client, copy.archive_root, copy.archive, copy.size, copy.digest)
            ):
                raise KeeperError("Local input and output files no longer match.")
            if dry_run:
                planned += 1
                logger.info("Would delete local input %s", copy.raw)
            else:
                copy.raw.unlink()
                removed += 1
                logger.info("Deleted local input %s", copy.raw)
        except (KeeperError, OSError) as exc:
            failures += 1
            logger.error("Cannot delete raw file %s: %s", copy.raw, exc)
    return removed, planned, failures
