"""One-shot, verified Dropbox copies. No remote write or deletion operations."""

import fcntl
import logging
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

import dropbox
from dropbox.exceptions import DropboxException
from dropbox.files import FileMetadata
from requests.exceptions import RequestException

from photo_keeper.config import ChainConfig
from photo_keeper.errors import KeeperError
from photo_keeper.hashing import DropboxHasher, hash_file

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    copied: int = 0
    unchanged: int = 0
    planned: int = 0
    ignored: int = 0
    failed: int = 0


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


def iter_files(client: dropbox.Dropbox, source: str, recursive: bool) -> Iterator[FileMetadata]:
    page = client.files_list_folder(
        "" if source == "/" else source, recursive=recursive, include_deleted=False
    )
    while True:
        for entry in page.entries:
            if isinstance(entry, FileMetadata):
                yield entry
        if not page.has_more:
            break
        page = client.files_list_folder_continue(page.cursor)


def relative_path(entry: FileMetadata, source: str) -> Path:
    """Preserve source subdirectories and reject paths that can escape the destination."""
    remote = entry.path_display or entry.path_lower
    if not remote or not remote.startswith("/"):
        raise KeeperError("Dropbox returned a file without an absolute path.")
    parts = remote[1:].split("/")
    if any(
        part in {"", ".", ".."}
        or "\\" in part
        or ":" in part
        or any(ord(char) < 32 for char in part)
        for part in parts
    ):
        raise KeeperError("Dropbox returned an unsafe file path.")
    source_parts = source.strip("/").split("/") if source != "/" else []
    if len(parts) <= len(source_parts) or [part.lower() for part in parts[: len(source_parts)]] != [
        part.lower() for part in source_parts
    ]:
        raise KeeperError("Dropbox returned a file outside the configured source.")
    return Path(*parts[len(source_parts) :])


def destination_year(client: dropbox.Dropbox, entry: FileMetadata, date_source: str) -> int:
    if date_source == "capture":
        metadata = client.files_get_metadata(f"rev:{entry.rev}", include_media_info=True)
        if (
            not isinstance(metadata, FileMetadata)
            or metadata.rev != entry.rev
            or metadata.id != entry.id
        ):
            raise KeeperError("Dropbox returned metadata for a different file revision.")
        media = metadata.media_info
        if media is not None and media.is_metadata():
            taken = media.get_metadata().time_taken
            if taken is not None:
                return taken.year
        logger.info("No capture date for %r; using client modification date.", entry.path_display)
    return entry.client_modified.year


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


def same_content(path: Path, entry: FileMetadata) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise KeeperError(f"Destination is not a regular file: {path}")
    return info.st_size == entry.size and hash_file(path) == entry.content_hash


def choose_target(root: Path, target: Path, entry: FileMetadata) -> tuple[Path, bool]:
    safe_path(root, target)
    if same_content(target, entry):
        return target, True
    if not target.exists():
        return target, False
    # Preserve both versions; a stable suffix also makes subsequent runs idempotent.
    alternate = target.with_name(f"{target.stem}--{entry.content_hash}{target.suffix}")
    safe_path(root, alternate)
    if same_content(alternate, entry):
        return alternate, True
    if alternate.exists():
        raise KeeperError(f"Conflicting content already exists at {alternate}")
    return alternate, False


def download_verified(client: dropbox.Dropbox, entry: FileMetadata, target: Path) -> None:
    """Stream into a sibling temp file, verify, and atomically publish without overwriting."""
    fd, name = tempfile.mkstemp(prefix=".photo-keeper-", suffix=".part", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as file:
            metadata, response = client.files_download(f"rev:{entry.rev}")
            with closing(response):
                if (
                    metadata.id != entry.id
                    or metadata.rev != entry.rev
                    or metadata.size != entry.size
                    or metadata.content_hash != entry.content_hash
                ):
                    raise KeeperError("Dropbox returned a different file revision during download.")
                hasher = DropboxHasher()
                size = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > entry.size:
                        raise KeeperError("Download exceeds the expected file size.")
                    file.write(chunk)
                    hasher.update(chunk)
                if size != entry.size or hasher.hexdigest() != entry.content_hash:
                    raise KeeperError("Download failed size or content-hash verification.")
            file.flush()
            os.fsync(file.fileno())
        modified = entry.client_modified
        if modified.tzinfo is None:
            modified = modified.replace(tzinfo=UTC)
        os.utime(temporary, (modified.timestamp(), modified.timestamp()))
        # link() fails if target appeared concurrently; rename() could overwrite it on Unix.
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def run_chain(client: dropbox.Dropbox, chain: ChainConfig, *, dry_run: bool = False) -> RunResult:
    if not chain.enabled:
        raise KeeperError(f"Chain {chain.name!r} is disabled.")
    if chain.source is None or chain.destination is None:
        raise KeeperError("Chain requires a source and destination.")
    # Do not create the root: a missing mounted drive must not become a local directory.
    if not chain.destination.is_dir():
        raise KeeperError(f"Destination root must already exist: {chain.destination}")
    root = chain.destination.resolve()
    result = RunResult()
    with nullcontext() if dry_run else destination_lock(root):
        for entry in iter_files(client, chain.source, chain.recursive):
            if not entry.is_downloadable:
                logger.warning("Ignoring non-downloadable Dropbox document: %r", entry.path_display)
                result.ignored += 1
                continue
            try:
                if not entry.content_hash or not re.fullmatch(r"[0-9a-f]{64}", entry.content_hash):
                    raise KeeperError("Dropbox did not provide a valid content hash.")
                relative = relative_path(entry, chain.source)
                year = destination_year(client, entry, chain.date_source)
                target = root / f"{year:04d}" / chain.subdirectory / relative
                target, unchanged = choose_target(root, target, entry)
                if unchanged:
                    result.unchanged += 1
                    logger.info("Already verified: %s", target)
                elif dry_run:
                    result.planned += 1
                    logger.info("Would copy %r to %s", entry.path_display, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    safe_path(root, target)
                    download_verified(client, entry, target)
                    result.copied += 1
                    logger.info("Copied %r to %s", entry.path_display, target)
            except (KeeperError, OSError, DropboxException, RequestException) as exc:
                result.failed += 1
                detail = (
                    type(exc).__name__
                    if isinstance(exc, (DropboxException, RequestException))
                    else str(exc)
                )
                logger.error("Cannot copy %r: %s", entry.path_display, detail)
    return result
