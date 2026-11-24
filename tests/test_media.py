import json
import subprocess
from unittest.mock import Mock

import pytest

from photo_keeper import media
from photo_keeper.errors import KeeperError
from photo_keeper.media import capture_year


@pytest.mark.parametrize(
    "tags,year",
    [
        ({"DateTimeOriginal": "2016:02:29 16:00:00", "CreateDate": "2025:01:01 12:01:00"}, 2016),
        (
            {"CreationDate": "2017:12:31 23:59:00-08:00", "MediaCreateDate": "2018:01:01 01:10:00"},
            2017,
        ),
        (
            {"DateTimeOriginal": "0000:00:00 00:00:00", "MediaCreateDate": "2020:02:02 01:10:00"},
            2020,
        ),
        ({"CreateDate": "2018:01:01 01:02:03"}, 2018),
        ({"DateTimeOriginal": "2025:02:29 01:02:03", "CreateDate": 2020}, None),
        ({}, None),
    ],
)
def test_capture_year_prioritizes_valid_photo_and_video_tags(tmp_path, monkeypatch, tags, year):
    process = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([tags])))
    monkeypatch.setattr(media.subprocess, "run", process)
    path = tmp_path / "Файл with spaces.mov"
    assert capture_year(path) == year
    assert process.call_args.args[0][-1] == str(path)


@pytest.mark.parametrize(
    "output,code", [("", 1), ("[]", 0), ("{}", 0), ('{"x":true}', 0), ("[1]", 0), ("[{}]", 2)]
)
def test_unexpected_exiftool_responses_fail(tmp_path, monkeypatch, output, code):
    monkeypatch.setattr(
        media.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], code, output))
    )
    with pytest.raises(KeeperError, match="could not inspect"):
        capture_year(tmp_path / "photo.jpg")


def test_unsupported_metadata_response_can_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(
        media.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 1, "[{}]"))
    )
    assert capture_year(tmp_path / "unknown.bin") is None


def test_missing_exiftool_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(media.subprocess, "run", Mock(side_effect=FileNotFoundError()))
    with pytest.raises(KeeperError, match="ExifTool is required"):
        capture_year(tmp_path / "photo.jpg")
