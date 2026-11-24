"""Dropbox content hashes: SHA-256 over the SHA-256 digests of 4 MiB blocks."""

import hashlib
from pathlib import Path

BLOCK_SIZE = 4 * 1024 * 1024


class DropboxHasher:
    def __init__(self) -> None:
        self._blocks = hashlib.sha256()
        self._block = hashlib.sha256()
        self._size = 0

    def update(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            length = min(len(view), BLOCK_SIZE - self._size)
            self._block.update(view[:length])
            self._size += length
            view = view[length:]
            if self._size == BLOCK_SIZE:
                self._blocks.update(self._block.digest())
                self._block = hashlib.sha256()
                self._size = 0

    def hexdigest(self) -> str:
        blocks = self._blocks.copy()
        if self._size:
            blocks.update(self._block.digest())
        return blocks.hexdigest()


def hash_file(path: Path) -> str:
    hasher = DropboxHasher()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()
