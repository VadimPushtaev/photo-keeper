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
def test_real_local_rclone_remote_and_capture_year(tmp_path, capsys, caplog):
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
    )
    args = ["--config", str(conf), "run", "camera"]
    assert main([*args, "--dry-run"]) == 0, caplog.text
    assert "planned=2" in capsys.readouterr().out
    assert not list(archive.iterdir())
    assert main(args) == 0, caplog.text
    assert "copied=2" in capsys.readouterr().out
    local_photo = archive / "2021/Камера/__raw__/Поездка/Снимок.png"
    assert local_photo.read_bytes() == remote_photo.read_bytes()
    assert local_photo.stat().st_mtime == timestamp
    assert (archive / "2025/Камера/__raw__/# note [file].txt").read_bytes() == b"sidecar"
    assert main(args) == 0, caplog.text
    assert "unchanged=2" in capsys.readouterr().out
