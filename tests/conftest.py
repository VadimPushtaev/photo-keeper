import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from photo_keeper import backup
from photo_keeper.config import ChainConfig
from photo_keeper.rclone import RemoteFile


def reference_hash(data: bytes) -> str:
    blocks = [hashlib.sha256(data[i : i + 4194304]).digest() for i in range(0, len(data), 4194304)]
    return hashlib.sha256(b"".join(blocks)).hexdigest()


class FakeRclone:
    def __init__(self):
        self.entries = []
        self.payloads = {}
        self.downloads = []
        self.list_calls = []

    def add(self, path="photo.jpg", data=b"photo contents", **overrides):
        path = Path(path)
        metadata = {
            "path": path,
            "size": len(data),
            "modified": datetime(2025, 6, 15, 12, tzinfo=UTC),
            "checksum": ("sha256", hashlib.sha256(data).hexdigest()),
        }
        self.entries.append(RemoteFile(**(metadata | overrides)))
        self.payloads[path] = data
        return self.entries[-1]

    def list_files(self, source, recursive):
        self.list_calls.append((source, recursive))
        return self.entries

    def download(self, source, entry, target):
        self.downloads.append((source, entry.path))
        data = self.payloads[entry.path]
        if isinstance(data, BaseException):
            target.write_bytes(b"partial")
            raise data
        target.write_bytes(data)

    def quickxor(self, path):
        assert path.is_file()
        return "1" * 40


@pytest.fixture
def remote():
    return FakeRclone()


@pytest.fixture
def chain(tmp_path: Path, monkeypatch) -> ChainConfig:
    root = tmp_path / "photos"
    root.mkdir()
    monkeypatch.setattr(backup, "capture_year", lambda path: None)
    return ChainConfig(
        name="test-copy",
        enabled=True,
        source="cloud:Camera Uploads",
        destination=root,
        subdirectory="phone/__raw__",
    )
