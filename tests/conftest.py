"""Offline fixtures with real Dropbox SDK metadata and in-memory remote content."""

import hashlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from dropbox.files import FileMetadata, MediaInfo, PhotoMetadata

from photo_keeper.config import ChainConfig


def reference_hash(data: bytes) -> str:
    blocks = [hashlib.sha256(data[i : i + 4194304]).digest() for i in range(0, len(data), 4194304)]
    return hashlib.sha256(b"".join(blocks)).hexdigest()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("Tests must not contact Dropbox or any other network service")

    monkeypatch.setattr(requests.Session, "send", fail)


@pytest.fixture
def make_entry():
    def make(
        path="/Camera Uploads/photo.jpg",
        data=b"photo contents",
        *,
        taken=None,
        modified=datetime(2025, 6, 15, 12),
        **overrides,
    ):
        fingerprint = hashlib.sha256(path.encode() + data).hexdigest()[:12]
        fields = dict(
            name=path.rsplit("/", 1)[-1],
            id=f"id:{fingerprint}",
            rev=fingerprint,
            path_display=path,
            path_lower=path.lower(),
            size=len(data),
            client_modified=modified,
            server_modified=datetime(2026, 1, 1),
            content_hash=reference_hash(data),
            is_downloadable=True,
        )
        if taken is not None:
            fields["media_info"] = MediaInfo.metadata(PhotoMetadata(time_taken=taken))
        fields.update(overrides)
        return FileMetadata(**fields)

    return make


class FakeResponse:
    def __init__(self, data: bytes):
        self.data = data
        self.closed = False
        self.error = None

    def iter_content(self, chunk_size):
        # Empty keepalive chunks and short network reads are both valid.
        yield b""
        for index in range(0, len(self.data), 3):
            yield self.data[index : index + 3]
            if self.error:
                raise self.error

    def close(self):
        self.closed = True


class FakeDropbox:
    def __init__(self):
        self.entries = []
        self.payloads = {}
        self.metadata = {}
        self.download_metadata = {}
        self.pages = None
        self.responses = []
        self.downloads = []
        self.metadata_calls = []
        self.list_calls = []
        self.continue_calls = []
        self.stream_error = None

    def add(self, entry, data=b"photo contents"):
        self.entries.append(entry)
        self.payloads[entry.rev] = data
        self.metadata[entry.rev] = entry
        self.download_metadata[entry.rev] = entry
        return entry

    def files_list_folder(self, path, *, recursive, include_deleted):
        self.list_calls.append((path, recursive, include_deleted))
        pages = self.pages if self.pages is not None else [self.entries]
        return SimpleNamespace(entries=pages[0], has_more=len(pages) > 1, cursor="1")

    def files_list_folder_continue(self, cursor):
        self.continue_calls.append(cursor)
        index = int(cursor)
        return SimpleNamespace(
            entries=self.pages[index], has_more=index < len(self.pages) - 1, cursor=str(index + 1)
        )

    def files_get_metadata(self, path, *, include_media_info):
        self.metadata_calls.append((path, include_media_info))
        result = self.metadata[path.removeprefix("rev:")]
        if isinstance(result, Exception):
            raise result
        return result

    def files_download(self, path):
        self.downloads.append(path)
        rev = path.removeprefix("rev:")
        response = FakeResponse(self.payloads[rev])
        response.error = self.stream_error
        self.responses.append(response)
        return self.download_metadata[rev], response


@pytest.fixture
def remote():
    return FakeDropbox()


@pytest.fixture
def chain(tmp_path: Path) -> ChainConfig:
    root = tmp_path / "photos"
    root.mkdir()
    return ChainConfig(
        name="test-copy",
        enabled=True,
        source="/Camera Uploads",
        destination=root,
        subdirectory="phone/__raw__",
    )
