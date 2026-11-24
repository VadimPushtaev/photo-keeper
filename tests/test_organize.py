from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from photo_keeper import backup, organize
from photo_keeper.backup import run_chain
from photo_keeper.config import DeletionConfig
from photo_keeper.errors import KeeperError
from photo_keeper.media import MediaInfo
from photo_keeper.organize import clean_raw, process_local
from photo_keeper.rclone import RemoteFile


@pytest.fixture
def monthly(chain, monkeypatch):
    monkeypatch.setattr(organize, "read_media_info", lambda _: MediaInfo(None, False))
    return replace(chain, sort_by_month=True)


def phone(chain, year=2025):
    return chain.destination / f"{year:04d}/phone"


def raw_file(chain, name="picture.heic", data=b"original photo", *, year=2025):
    path = phone(chain, year) / "__raw__" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    organize.os.utime(path, (1752170400, 1752170400))  # July 2025 in UTC.
    return path


@pytest.mark.parametrize(
    "name,camera,expected",
    [
        ("photo.HEIC", False, "photo.HEIC"),
        ("photo.jpg", True, "photo.jpg"),
        ("download.JPEG", False, "pics/download.JPEG"),
        ("nested/download.jpg", False, "pics/nested/download.jpg"),
        ("screen.PNG", True, "screenshot/screen.PNG"),
        ("clip.MOV", False, "video/clip.MOV"),
        ("clip.mp4", False, "video/clip.mp4"),
        ("sidecar.xmp", False, "sidecar.xmp"),
        ("download.webp", False, "download.webp"),
    ],
)
def test_sort_classifies_and_preserves_original_bytes_and_mtime(
    remote, monthly, monkeypatch, name, camera, expected
):
    raw = raw_file(monthly, name, b"unchanged bytes including motion video if present")
    monkeypatch.setattr(organize, "read_media_info", lambda _: MediaInfo(date(2025, 8, 31), camera))
    result = run_chain(remote, monthly)
    dest = phone(monthly) / "08" / expected
    assert result.sorted == 1
    assert dest.read_bytes() == raw.read_bytes()
    assert dest.stat().st_mtime_ns == raw.stat().st_mtime_ns
    assert dest.stat().st_ino != raw.stat().st_ino
    repeat = run_chain(remote, monthly)
    assert (repeat.sorted, repeat.sort_unchanged) == (0, 1)


@pytest.mark.parametrize("source", ["capture", "modified"])
def test_month_falls_back_to_utc_modified_date(remote, monthly, monkeypatch, source):
    raw_file(monthly, year=2024)
    metadata = date(2024, 3, 1) if source == "modified" else None
    reader = Mock(return_value=MediaInfo(metadata, False))
    monkeypatch.setattr(organize, "read_media_info", reader)
    result = run_chain(remote, replace(monthly, date_source=source))
    assert result.sorted == 1
    assert (phone(monthly, 2024) / "07/picture.heic").is_file()
    if source == "modified":
        reader.assert_not_called()


def test_jpeg_camera_flag_is_checked_in_modified_mode(remote, monthly, monkeypatch):
    raw_file(monthly, "camera.jpg")
    monkeypatch.setattr(organize, "read_media_info", lambda _: MediaInfo(date(2019, 1, 1), True))
    run_chain(remote, replace(monthly, date_source="modified"))
    assert (phone(monthly) / "07/camera.jpg").is_file()


def test_month_conflict_keeps_both_distinct_files(remote, monthly):
    raw = raw_file(monthly)
    existing = phone(monthly) / "07/picture.heic"
    existing.parent.mkdir()
    existing.write_bytes(b"other")
    assert run_chain(remote, monthly).sorted == 1
    (alternate,) = existing.parent.glob("picture--*.heic")
    assert alternate.read_bytes() == raw.read_bytes()
    assert existing.read_bytes() == b"other"
    assert run_chain(remote, monthly).sorted == 0


def test_archive_symlink_is_not_followed(remote, monthly, tmp_path):
    raw = raw_file(monthly)
    outside = tmp_path / "outside"
    outside.mkdir()
    (phone(monthly) / "07").symlink_to(outside)
    result = run_chain(remote, replace(monthly, delete=DeletionConfig(raw=True)))
    assert result.failed == 1
    assert raw.exists()
    assert list(outside.iterdir()) == []


def test_raw_symlinks_are_skipped(remote, monthly, tmp_path):
    raw = raw_file(monthly)
    (raw.parent / "another.heic").symlink_to(raw)
    folder = tmp_path / "private"
    folder.mkdir()
    (folder / "outside.heic").write_bytes(b"elsewhere")
    (raw.parent / "folder").symlink_to(folder)
    assert run_chain(remote, monthly).sorted == 1
    assert list((phone(monthly) / "07").iterdir()) == [phone(monthly) / "07/picture.heic"]


def test_changed_input_does_not_publish_or_delete(remote, monthly, monkeypatch):
    raw = raw_file(monthly)
    original = organize.shutil.copyfileobj

    def change_before_copy(src, dest, length):
        raw.write_bytes(b"new raw content")
        original(src, dest, length)

    monkeypatch.setattr(organize.shutil, "copyfileobj", change_before_copy)
    result = run_chain(remote, replace(monthly, delete=DeletionConfig(raw=True)))
    assert result.failed == 1
    assert raw.read_bytes() == b"new raw content"
    assert not list((phone(monthly) / "07").iterdir())


@pytest.mark.parametrize(
    "delete,source_left,raw_left",
    [
        (DeletionConfig(), True, True),
        (DeletionConfig(source=True), False, True),
        (DeletionConfig(raw=True), True, True),
        (DeletionConfig(source=True, raw=True, archive=True), False, False),
    ],
)
def test_cleanup_flags_are_independent_and_archive_is_terminal(
    remote, monthly, delete, source_left, raw_left
):
    remote.add("image.heic", b"media")
    monthly = replace(monthly, delete=delete)
    result = run_chain(remote, monthly)
    phone_dir = phone(monthly)
    assert result.failed == 0
    assert (result.copied, result.sorted) == (1, 1)
    assert result.source_deleted == int(not source_left)
    assert result.raw_deleted == int(not raw_left)
    assert (phone_dir / "06/__raw__/image.heic").exists() is False
    assert (phone_dir / "__raw__/image.heic").exists() == raw_left
    assert bool(remote.entries) is source_left
    archive = phone_dir / "06/image.heic"
    assert archive.read_bytes() == b"media"
    again = run_chain(remote, monthly)
    assert again.sorted == 0
    assert archive.read_bytes() == b"media"


def test_raw_is_terminal_if_month_sort_is_disabled(remote, chain):
    remote.add("picture.heic", b"photo")
    chain = replace(chain, delete=DeletionConfig(source=True, raw=True, archive=True))
    result = run_chain(remote, chain)
    assert result.source_deleted == 1
    assert result.raw_deleted == 0
    assert (phone(chain) / "__raw__/picture.heic").read_bytes() == b"photo"


def test_dry_run_only_plans_actions_whose_prerequisites_currently_exist(remote, monthly):
    original = raw_file(monthly, "image.heic", b"media")
    copy = raw_file(monthly, "source-absent.heic", b"other")
    run_chain(remote, monthly)
    remote.add("image.heic", b"media")
    pending = raw_file(monthly, "month-absent.heic", b"new")
    remote.add("raw-absent.heic", b"download")
    monthly = replace(monthly, delete=DeletionConfig(source=True, raw=True, archive=True))
    preview = run_chain(remote, monthly, dry_run=True)
    assert (preview.planned, preview.sort_planned) == (1, 1)
    assert (preview.source_delete_planned, preview.raw_delete_planned) == (1, 1)
    assert (preview.source_deleted, preview.raw_deleted, preview.sorted) == (0, 0, 0)
    assert all(path.exists() for path in (original, copy, pending))
    assert not (phone(monthly) / "07/month-absent.heic").exists()
    assert not remote.downloads and not remote.removed


@pytest.mark.parametrize("reason", ["same name", "same bytes", "unknown checksum", "unknown size"])
def test_raw_cleanup_retains_uncertain_or_present_upstream_files(remote, monthly, reason):
    raw = raw_file(monthly, data=b"media")
    (sorted_copy,) = process_local(
        remote, monthly.resolved_steps()[1], monthly.resolved_steps()[0].outputs, dry_run=False
    ).copies
    options = {
        "same name": ("picture.heic", b"different", {}),
        "same bytes": ("other.heic", b"media", {}),
        "unknown checksum": ("other.heic", b"maybe", {"checksum": None}),
        "unknown size": ("other.heic", b"maybe", {"size": -1}),
    }
    name, content, kwargs = options[reason]
    entry = remote.add(name, content, **kwargs)
    assert clean_raw(remote, [sorted_copy], [entry], dry_run=False) == (
        0,
        0,
        0,
    )
    assert raw.exists()


@pytest.mark.parametrize("side", ["raw", "archive"])
def test_raw_cleanup_requires_fresh_matching_contents(remote, monthly, side):
    raw_file(monthly, data=b"backup")
    (sorted_copy,) = process_local(
        remote, monthly.resolved_steps()[1], monthly.resolved_steps()[0].outputs, dry_run=False
    ).copies
    getattr(sorted_copy, side).write_bytes(b"edited")
    assert clean_raw(remote, [sorted_copy], [], dry_run=False) == (0, 0, 1)
    assert sorted_copy.raw.exists()
    assert sorted_copy.archive.exists()


@pytest.mark.parametrize("changed", ["local", "remote"])
def test_cloud_cleanup_checks_fresh_listing_against_output(remote, monthly, monkeypatch, changed):
    remote.add("photo.heic", b"media")
    list_files = remote.list_files
    raw = phone(monthly) / "__raw__/photo.heic"

    def fresh_list(source, recursive):
        if raw.is_file():
            if changed == "local":
                raw.write_bytes(b"other")
            else:
                remote.entries.clear()
                remote.add("photo.heic", b"newer")
        return list_files(source, recursive)

    monkeypatch.setattr(remote, "list_files", fresh_list)
    result = run_chain(remote, replace(monthly, delete=DeletionConfig(source=True)))
    assert result.failed == 0
    assert result.source_deleted == 0
    assert remote.entries and not remote.removed
    assert raw.read_bytes() == (b"other" if changed == "local" else b"media")


def test_cloud_cleanup_does_not_act_on_hashless_source(remote, monthly):
    remote.add("photo.heic", b"media", checksum=None)
    result = run_chain(remote, replace(monthly, delete=DeletionConfig(source=True, raw=True)))
    assert result.failed == 0
    assert result.source_deleted == result.raw_deleted == 0
    assert remote.entries


def test_cloud_cleanup_batches_only_checksum_matches(remote, monthly, monkeypatch):
    monkeypatch.setattr(backup, "BATCH_SIZE", 2)
    for index in range(3):
        remote.add(f"{index}.heic", f"media {index}".encode())
    unverified = remote.add("unverified.heic", b"other", checksum=None)
    delete = Mock(wraps=remote.delete_files)
    monkeypatch.setattr(remote, "delete_files", delete)
    result = run_chain(remote, replace(monthly, delete=DeletionConfig(source=True)))
    assert result.source_deleted == 3
    assert [call.args[1] for call in delete.call_args_list] == [
        [Path("0.heic"), Path("1.heic")],
        [Path("2.heic")],
    ]
    assert remote.entries == [unverified]


def test_cloud_cleanup_failure_stops_further_deletes(remote, monthly, monkeypatch):
    monkeypatch.setattr(backup, "BATCH_SIZE", 1)
    remote.add("photo.heic", b"media")
    remote.add("next.heic", b"other")
    delete = Mock(side_effect=KeeperError("rclone error"))
    monkeypatch.setattr(remote, "delete_files", delete)
    result = run_chain(remote, replace(monthly, delete=DeletionConfig(source=True, raw=True)))
    assert result.failed == 1
    assert result.source_deleted == result.raw_deleted == 0
    delete.assert_called_once_with(monthly.source, [Path("photo.heic")])
    assert (phone(monthly) / "__raw__/photo.heic").is_file()
    assert (phone(monthly) / "__raw__/next.heic").is_file()


def test_cleanup_uses_fresh_listing_before_removing_from_raw(remote, monthly, monkeypatch):
    raw = raw_file(monthly, data=b"media")
    entry = RemoteFile(Path("picture.heic"), 5, datetime(2025, 7, 10, tzinfo=UTC), None)
    listings = Mock(side_effect=[[], [entry]])
    monkeypatch.setattr(remote, "list_files", listings)
    result = run_chain(remote, replace(monthly, delete=DeletionConfig(raw=True)))
    assert result.sorted == 1
    assert result.raw_deleted == 0
    assert raw.exists()


def test_raw_cleanup_accepts_same_size_when_checksums_prove_it_is_a_different_object(
    remote, monthly
):
    raw_file(monthly, data=b"media")
    (sorted_copy,) = process_local(
        remote, monthly.resolved_steps()[1], monthly.resolved_steps()[0].outputs, dry_run=False
    ).copies
    other = remote.add("other.heic", b"other", checksum=("md5", "1" * 32))
    assert clean_raw(remote, [sorted_copy], [other], dry_run=False) == (
        1,
        0,
        0,
    )


def test_cleanup_rechecks_raw_even_when_the_original_copy_phase_fails(remote, monthly, monkeypatch):
    raw_file(monthly)
    remote.add("different.heic")
    monkeypatch.setattr(remote, "download_batch", Mock(side_effect=KeeperError("download error")))
    result = run_chain(remote, replace(monthly, delete=DeletionConfig(source=True)))
    assert result.failed == 1
    assert not remote.removed
