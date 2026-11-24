"""Exercise the actual rclone and ExifTool commands without a cloud account."""

import json
import os
import shutil
import struct
import subprocess
import zlib

import pytest

from photo_keeper.cli import main


def png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))


@pytest.mark.skipif(
    not shutil.which("rclone") or not shutil.which("exiftool"),
    reason="Requires rclone and ExifTool",
)
def test_real_local_rclone_remote_and_capture_year(tmp_path, capfd, caplog):
    inbox = tmp_path / "Camera Uploads"
    nested = inbox / "Поездка"
    nested.mkdir(parents=True)
    remote_photo = nested / "Снимок.png"
    remote_photo.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(b"\0\xff"))
        + png_chunk(b"IEND", b"")
    )
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-EXIF:DateTimeOriginal=2021:12:31 23:59:59",
            str(remote_photo),
        ],
        check=True,
        capture_output=True,
    )
    timestamp = 1748779200  # 2025-06-01, a different year from the capture date.
    os.utime(remote_photo, (timestamp, timestamp))
    note = inbox / "# note [file].txt"
    note.write_bytes(b"sidecar")
    os.utime(note, (timestamp, timestamp))
    archive = tmp_path / "Архив"
    archive.mkdir()
    (tmp_path / "rclone.conf").write_text(f"[fixture]\ntype = alias\nremote = {tmp_path}\n")
    conf = tmp_path / "chain.toml"
    conf.write_text(
        '[rclone]\nconfig_file = "rclone.conf"\n'
        '[[chains]]\nname = "camera"\nenabled = true\nsource = "fixture:Camera Uploads"\n'
        f"destination = {json.dumps(str(archive), ensure_ascii=False)}\n"
        'subdirectory = "Камера/__raw__"\n'
        "sort_by_month = true\n"
    )
    args = ["--config", str(conf), "run", "camera"]
    assert main([*args, "--dry-run"]) == 0, caplog.text
    preview = capfd.readouterr()
    assert "planned=2" in preview.out
    assert "ETA" not in preview.err
    assert not list(archive.iterdir())
    assert main(args) == 0, caplog.text
    copied = capfd.readouterr()
    assert "copied=2" in copied.out
    assert "sorted=2, sort_unchanged=0" in copied.out
    assert "100%" in copied.err and "ETA" in copied.err
    local_photo = archive / "2021/Камера/__raw__/Поездка/Снимок.png"
    assert local_photo.read_bytes() == remote_photo.read_bytes()
    assert local_photo.stat().st_mtime == timestamp
    assert (archive / "2025/Камера/__raw__/# note [file].txt").read_bytes() == b"sidecar"
    assert main(args) == 0, caplog.text
    repeat = capfd.readouterr().out
    assert "unchanged=2" in repeat
    assert "sorted=0, sort_unchanged=2" in repeat


@pytest.mark.skipif(not shutil.which("rclone"), reason="Requires rclone")
def test_chain_sort_and_cleanup_with_a_real_local_rclone_remote(tmp_path, capfd):
    remote = tmp_path / "remote"
    nested = remote / "Camera Uploads" / "trip"
    nested.mkdir(parents=True)
    remote_video = nested / "# clip.mov"
    remote_video.write_bytes(b"original video fixture")
    timestamp = 1752170400  # July 2025.
    os.utime(remote_video, (timestamp, timestamp))
    outside_source = remote / "leave-alone.mov"
    outside_source.write_bytes(b"outside the scoped source")
    recovery = remote / ".photo-keeper-cleanup/previous-run/keep.mov"
    recovery.parent.mkdir(parents=True)
    recovery.write_bytes(b"older recovery copy")
    archive = tmp_path / "archive"
    archive.mkdir()
    (tmp_path / "rclone.conf").write_text(f"[fixture]\ntype = alias\nremote = {remote}\n")
    config = tmp_path / "chain.toml"
    config.write_text(
        '[rclone]\nconfig_file = "rclone.conf"\n'
        '[[chains]]\nname = "camera"\nenabled = true\n'
        '[[chains.steps]]\ntype = "copy"\nfrom = { rclone = "fixture:Camera Uploads" }\n'
        f"to = {json.dumps(str(archive / '{year}/phone/__raw__'))}\n"
        'date_source = "modified"\ndelete_source = true\n'
        '[[chains.steps]]\ntype = "sort"\nfrom = "previous"\n'
        f"to = {json.dumps(str(archive / '{year}/phone/{month}'))}\n"
        'date_source = "modified"\ndelete_source = true\n'
        '[chains.steps.routes]\nvideo = "video"\n',
    )
    args = ["--config", str(config), "run", "camera"]
    assert main([*args, "--dry-run"]) == 0
    assert remote_video.exists()
    assert not list(archive.iterdir())
    assert main(args) == 0
    assert "source_deleted=1, raw_deleted=1" in capfd.readouterr().out
    result = archive / "2025/phone/07/video/trip/# clip.mov"
    assert result.read_bytes() == b"original video fixture"
    assert not remote_video.exists()
    assert not (archive / "2025/phone/__raw__/trip/# clip.mov").exists()
    assert outside_source.read_bytes() == b"outside the scoped source"
    assert list(recovery.parent.parent.iterdir()) == [recovery.parent]
    assert recovery.read_bytes() == b"older recovery copy"
    assert main(args) == 0
    assert "source_deleted=0, raw_deleted=0" in capfd.readouterr().out
    assert result.is_file()
