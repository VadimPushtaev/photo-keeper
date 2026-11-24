import hashlib
from pathlib import Path

import pytest

from photo_keeper.hashing import BLOCK_SIZE, DropboxHasher, hash_file

from .conftest import reference_hash


@pytest.mark.parametrize(
    "length", [0, 1, 31, BLOCK_SIZE - 1, BLOCK_SIZE, BLOCK_SIZE + 1, 2 * BLOCK_SIZE + 9]
)
@pytest.mark.parametrize("chunk_size", [77777, BLOCK_SIZE, BLOCK_SIZE + 3])
def test_network_chunk_boundaries_do_not_affect_hash(length: int, chunk_size: int) -> None:
    data = (b"abcdefgh01234567" * (length // 16 + 1))[:length]
    hasher = DropboxHasher()
    for index in range(0, length, chunk_size):
        hasher.update(data[index : index + chunk_size])
    assert hasher.hexdigest() == reference_hash(data)


def test_empty_file_vector() -> None:
    assert (
        DropboxHasher().hexdigest()
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_digest_does_not_consume_or_reset_state() -> None:
    hasher = DropboxHasher()
    hasher.update(b"abc")
    expected = hashlib.sha256(hashlib.sha256(b"abc").digest()).hexdigest()
    assert hasher.hexdigest() == hasher.hexdigest() == expected
    hasher.update(b"def")
    assert hasher.hexdigest() == reference_hash(b"abcdef")


def test_hash_local_file(tmp_path: Path) -> None:
    path = tmp_path / "photo.jpg"
    data = b"abcdef" * 900_000
    path.write_bytes(data)
    assert hash_file(path) == reference_hash(data)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        hash_file(tmp_path / "missing")
