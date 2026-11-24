import json
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from photo_keeper import rclone
from photo_keeper.errors import KeeperError
from photo_keeper.rclone import Rclone


@pytest.fixture
def process(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "[]"))
    monkeypatch.setattr(rclone.subprocess, "run", run)
    return run


def mock_listing(process, *entries):
    process.return_value.stdout = json.dumps(entries)


def file_entry(**changes):
    return {
        "Path": "Москва/Фото.jpg",
        "Size": 123,
        "ModTime": "2026-12-31T23:59:59.123456789+02:00",
        "Hashes": {"dropbox": "A" * 64},
    } | changes


@pytest.mark.parametrize("recursive", [False, True])
def test_listing_parses_rclone_output_and_uses_named_remote(process, tmp_path, recursive):
    config = tmp_path / "config with spaces"
    mock_listing(process, file_entry())
    files = Rclone(config).list_files("dropbox:Camera Uploads", recursive)
    assert len(files) == 1
    assert files[0].path == Path("Москва/Фото.jpg")
    assert files[0].size == 123
    assert files[0].modified == datetime.fromisoformat("2026-12-31T23:59:59.123456+02:00")
    assert files[0].checksum == ("dropbox", "a" * 64)
    flags = ["--recursive"] if recursive else []
    process.assert_called_once_with(
        [
            "rclone",
            "--config",
            str(config),
            "lsjson",
            "--files-only",
            "--hash",
            *flags,
            "--",
            "dropbox:Camera Uploads",
        ],
        check=True,
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    "hashes,expected",
    [
        ({"md5": "a" * 32}, ("md5", "a" * 32)),
        ({"sha1": "a" * 40}, ("sha1", "a" * 40)),
        ({"quickxor": "a" * 40}, ("quickxor", "a" * 40)),
        ({"sha256": "b" * 64, "sha1": "a" * 40}, ("sha256", "b" * 64)),
        ({"dropbox": "bad", "md5": "a" * 32}, ("md5", "a" * 32)),
        ({"dropbox": "x" * 64}, None),
        ({"md5": True}, None),
        ({"unsupported": "a" * 32}, None),
        (None, None),
    ],
)
def test_listing_uses_available_supported_checksum(process, hashes, expected):
    mock_listing(process, file_entry(Hashes=hashes))
    assert Rclone().list_files("cloud:", True)[0].checksum == expected


@pytest.mark.parametrize("timestamp", [None, "", "0001-01-01T00:00:00Z"])
def test_listing_can_represent_missing_modification_date(process, timestamp):
    mock_listing(process, file_entry(ModTime=timestamp, Size=-1))
    entry = Rclone().list_files("cloud:", True)[0]
    assert entry.modified is None
    assert entry.size == -1


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "dir/../photo",
        "dir//photo",
        "/root/file",
        "C:photo",
        "a\\b",
        "line\nfeed",
        None,
    ],
)
def test_unsafe_listed_paths_are_rejected(process, name):
    mock_listing(process, file_entry(Path=name))
    with pytest.raises(KeeperError, match="unsafe file path"):
        Rclone().list_files("cloud:", True)


@pytest.mark.parametrize(
    "entry,message",
    [
        (None, "invalid file listing"),
        (file_entry(Size=True), "invalid size"),
        (file_entry(Size="12"), "invalid size"),
        (file_entry(Size=-2), "invalid size"),
        (file_entry(ModTime="wrong"), "invalid modification date"),
        (file_entry(ModTime=123), "invalid modification date"),
        (file_entry(Hashes=["bad"]), "invalid hashes"),
    ],
)
def test_malformed_listing_fails_with_context(process, entry, message):
    mock_listing(process, entry)
    with pytest.raises(KeeperError, match=message):
        Rclone().list_files("cloud:", True)


@pytest.mark.parametrize(
    "output,message", [("plain text", "invalid listing JSON"), ("{}", "invalid file listing")]
)
def test_invalid_listing_response_is_reported(process, output, message):
    process.return_value.stdout = output
    with pytest.raises(KeeperError, match=message):
        Rclone().list_files("cloud:", True)


def test_duplicate_remote_paths_are_ambiguous(process):
    mock_listing(process, file_entry(), file_entry())
    with pytest.raises(KeeperError, match="multiple files"):
        Rclone().list_files("cloud:", True)


@pytest.mark.parametrize("source", ["cloud:", "cloud:/", "cloud:Camera Uploads"])
def test_copy_passes_literal_file_paths_and_uses_copyto(process, tmp_path, source):
    mock_listing(process, file_entry(Path="sub/a [file].jpg"))
    client = Rclone()
    entry = client.list_files(source, True)[0]
    path = tmp_path / "Файл.jpg"
    client.download(source, entry, path)
    prefix = source if source.endswith((":", "/")) else source + "/"
    assert process.call_args.args[0] == [
        "rclone",
        "copyto",
        "--max-depth",
        "1",
        "--",
        prefix + "sub/a [file].jpg",
        str(path),
    ]


def test_interactive_configuration_inherits_the_terminal(process):
    process.return_value.stdout = None
    Rclone().configure()
    process.assert_called_once_with(
        ["rclone", "config"], check=True, text=True, capture_output=False
    )


def test_missing_rclone_has_clear_error(process):
    process.side_effect = FileNotFoundError()
    with pytest.raises(KeeperError, match="on PATH"):
        Rclone().configure()


@pytest.mark.parametrize(
    "stderr,expected", [(None, "exit 1"), ("remote missing\n", "remote missing")]
)
def test_subprocess_errors_are_reported(process, stderr, expected):
    process.side_effect = subprocess.CalledProcessError(1, [], stderr=stderr)
    with pytest.raises(KeeperError, match=expected):
        Rclone().list_files("cloud:", True)


def test_quickxor_uses_rclone_for_local_hashing(process, tmp_path):
    path = tmp_path / "a b.jpg"
    process.return_value.stdout = "A" * 40 + "  a b.jpg\n"
    assert Rclone().quickxor(path) == "a" * 40
    assert process.call_args.args[0] == ["rclone", "hashsum", "quickxor", "--", str(path)]


@pytest.mark.parametrize("output", ["", "UNSUPPORTED  file", "x" * 40 + "  file"])
def test_quickxor_never_accepts_missing_or_invalid_results(process, tmp_path, output):
    process.return_value.stdout = output
    with pytest.raises(KeeperError, match="invalid QuickXor"):
        Rclone().quickxor(tmp_path / "file")
