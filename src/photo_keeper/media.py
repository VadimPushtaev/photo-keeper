"""Read capture dates from downloaded photos and videos."""

import json
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from photo_keeper.errors import KeeperError

_CAPTURE_TAGS = ("DateTimeOriginal", "CreationDate", "MediaCreateDate", "CreateDate")


@dataclass(frozen=True)
class MediaInfo:
    capture_date: date | None
    has_camera: bool


def read_media_info(path: Path) -> MediaInfo:
    try:
        result = subprocess.run(
            [
                "exiftool",
                "-j",
                *("-" + tag for tag in _CAPTURE_TAGS),
                "-EXIF:Make",
                "-EXIF:Model",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise KeeperError(
            "ExifTool is required for capture dates. Install libimage-exiftool-perl."
        ) from exc
    # ExifTool can exit 1 for an empty file or an unsupported format and still emit JSON.
    try:
        records = json.loads(result.stdout)
        if (
            not isinstance(records, list)
            or len(records) != 1
            or not isinstance(records[0], dict)
            or result.returncode not in (0, 1)
        ):
            raise ValueError("Unexpected ExifTool response")
        tags = records[0]
    except ValueError as exc:
        raise KeeperError(f"ExifTool could not inspect {path.name}.") from exc

    has_camera = any(str(tags.get(key) or "").strip() for key in ("Make", "Model"))
    for tag in _CAPTURE_TAGS:
        value = tags.get(tag)
        if isinstance(value, str):
            try:
                return MediaInfo(date.fromisoformat(value[:10].replace(":", "-")), has_camera)
            except ValueError:
                continue
    return MediaInfo(None, has_camera)


def capture_year(path: Path) -> int | None:
    captured = read_media_info(path).capture_date
    return captured.year if captured is not None else None
