"""Read capture dates from downloaded photos and videos."""

import json
import subprocess
from datetime import date
from pathlib import Path

from photo_keeper.errors import KeeperError

_CAPTURE_TAGS = ("DateTimeOriginal", "CreationDate", "MediaCreateDate", "CreateDate")


def capture_year(path: Path) -> int | None:
    try:
        result = subprocess.run(
            ["exiftool", "-j", *("-" + tag for tag in _CAPTURE_TAGS), str(path)],
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

    for tag in _CAPTURE_TAGS:
        value = tags.get(tag)
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10].replace(":", "-")).year
            except ValueError:
                continue
    return None
