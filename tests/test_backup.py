import hashlib
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from photo_keeper import backup
from photo_keeper.backup import destination_lock, run_chain
from photo_keeper.errors import KeeperError

from .conftest import reference_hash


def target_path(chain, year=2025, name="photo.jpg"):
    return chain.destination / str(year) / chain.subdirectory / name


def assert_no_staging(chain):
    assert not list(chain.destination.glob(".photo-keeper-*"))


def test_capture_year_takes_priority_and_rerun_reads_local_metadata(remote, chain, monkeypatch):
    capture = Mock(return_value=2022)
    monkeypatch.setattr(backup, "capture_year", capture)
    entry = remote.add()
    result = run_chain(remote, chain)
    target = target_path(chain, 2022)
    assert result.copied == 1
    assert target.read_bytes() == b"photo contents"
    assert target.stat().st_mtime == entry.modified.timestamp()
    assert capture.call_args.args[0].name == "photo.jpg"
    assert run_chain(remote, chain).unchanged == 1
    assert capture.call_args.args[0] == target
    assert remote.downloads == [("cloud:Camera Uploads", Path("photo.jpg"))]
    assert_no_staging(chain)


def test_modified_mode_does_not_read_capture_metadata(remote, chain, monkeypatch):
    remote.add(modified=datetime(2025, 1, 1))
    capture = Mock(side_effect=AssertionError("unexpected metadata read"))
    monkeypatch.setattr(backup, "capture_year", capture)
    chain = replace(chain, date_source="modified", recursive=False)
    assert run_chain(remote, chain).copied == 1
    assert run_chain(remote, chain).unchanged == 1
    assert target_path(chain).is_file()
    assert remote.list_calls == [("cloud:Camera Uploads", False)] * 2


def test_missing_date_does_not_write_into_a_guessed_year(remote, chain):
    remote.add(modified=None)
    assert run_chain(remote, chain).failed == 1
    assert not list(chain.destination.glob("[0-9]*"))
    assert_no_staging(chain)
    assert run_chain(remote, replace(chain, date_source="modified"), dry_run=True).failed == 1


def test_capture_date_can_be_used_without_modification_date(remote, chain, monkeypatch):
    remote.add(modified=None)
    monkeypatch.setattr(backup, "capture_year", lambda path: 2024)
    assert run_chain(remote, chain).copied == 1
    assert target_path(chain, 2024).is_file()


@pytest.mark.parametrize("kind", ["sha256", "sha1", "md5", "dropbox", "quickxor", None])
def test_hashes_across_backends_and_hashless_comparison(remote, chain, kind):
    data = b"photo contents"
    if kind is None:
        checksum = None
    elif kind == "dropbox":
        checksum = (kind, reference_hash(data))
    elif kind == "quickxor":
        checksum = (kind, "1" * 40)
    else:
        checksum = (kind, hashlib.new(kind, data).hexdigest())
    remote.add(checksum=checksum)
    assert run_chain(remote, chain).copied == 1
    assert run_chain(remote, chain).unchanged == 1
    assert len(remote.downloads) == (2 if kind is None else 1)


def test_unknown_size_can_be_copied_and_compared(remote, chain):
    remote.add(size=-1)
    assert run_chain(remote, chain).copied == 1
    assert run_chain(remote, chain).unchanged == 1


def test_same_size_different_content_preserves_old_file_and_is_repeatable(remote, chain):
    entry = remote.add()
    original = target_path(chain)
    original.parent.mkdir(parents=True)
    old_bytes = b"x" * entry.size
    original.write_bytes(old_bytes)
    assert run_chain(remote, chain).copied == 1
    alternate = original.with_name(f"photo--{entry.checksum[1]}.jpg")
    assert alternate.read_bytes() == b"photo contents"
    assert original.read_bytes() == old_bytes
    assert run_chain(remote, chain).unchanged == 1
    original.unlink()
    assert run_chain(remote, chain).unchanged == 1
    assert len(remote.downloads) == 1


def test_conflicting_alternate_is_preserved(remote, chain):
    entry = remote.add()
    original = target_path(chain)
    original.parent.mkdir(parents=True)
    original.write_bytes(b"old")
    alternate = original.with_name(f"photo--{entry.checksum[1]}.jpg")
    alternate.write_bytes(b"conflict")
    assert run_chain(remote, chain).failed == 1
    assert alternate.read_bytes() == b"conflict"
    assert_no_staging(chain)


def test_rechecks_local_file_after_metadata_places_it_in_another_year(remote, chain, monkeypatch):
    remote.add()
    original = target_path(chain)
    original.parent.mkdir(parents=True)
    original.write_bytes(b"photo contents")
    monkeypatch.setattr(backup, "capture_year", lambda path: 2023)
    assert run_chain(remote, chain).copied == 1
    assert target_path(chain, 2023).exists()
    assert original.exists()


def test_unicode_and_source_subfolders(remote, chain):
    chain = replace(chain, subdirectory="Камера/__raw__")
    remote.add("Поездка/снимок.jpg")
    remote.add("Дом/снимок.jpg", b"other")
    assert run_chain(remote, chain).copied == 2
    assert target_path(chain, name="Поездка/снимок.jpg").read_bytes() == b"photo contents"
    assert target_path(chain, name="Дом/снимок.jpg").read_bytes() == b"other"


@pytest.mark.parametrize(
    "name", ["video.MOV", "image.HEIC", "image.RAW", "notes.xmp", ".hidden", "-option"]
)
def test_raw_copy_accepts_all_file_types(remote, chain, name):
    remote.add(name, b"")
    assert run_chain(remote, chain).copied == 1
    assert target_path(chain, name=name).read_bytes() == b""


@pytest.mark.parametrize("date_source", ["capture", "modified"])
def test_dry_run_does_not_download_or_write(remote, chain, date_source, caplog):
    remote.add()
    chain = replace(chain, date_source=date_source)
    with caplog.at_level("INFO"):
        assert run_chain(remote, chain, dry_run=True).planned == 1
    assert not list(chain.destination.iterdir())
    assert remote.downloads == []
    assert (
        "capture year" if date_source == "capture" else str(target_path(chain).parent)
    ) in caplog.text


def test_dry_run_verifies_existing_file(remote, chain):
    remote.add()
    run_chain(remote, chain)
    assert run_chain(remote, chain, dry_run=True).unchanged == 1
    assert len(remote.downloads) == 1


@pytest.mark.parametrize(
    "payload", [b"short", b"x" * 14, OSError("disk full"), KeeperError("network failed")]
)
def test_bad_download_is_cleaned_up_and_other_files_still_copy(remote, chain, payload):
    bad = remote.add("bad.jpg")
    remote.payloads[bad.path] = payload
    remote.add("good.jpg")
    result = run_chain(remote, chain)
    assert (result.failed, result.copied) == (1, 1)
    assert not target_path(chain, name="bad.jpg").exists()
    assert target_path(chain, name="good.jpg").exists()
    assert_no_staging(chain)
    remote.payloads[bad.path] = b"photo contents"
    assert run_chain(remote, chain).copied == 1


def test_success_without_a_downloaded_file_is_an_error(remote, chain, monkeypatch):
    remote.add()
    monkeypatch.setattr(remote, "download_batch", Mock())
    assert run_chain(remote, chain).failed == 1
    assert not target_path(chain).exists()


def test_interruption_cleans_up_and_releases_lock(remote, chain):
    entry = remote.add()
    remote.payloads[entry.path] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_chain(remote, chain)
    assert_no_staging(chain)
    with destination_lock(chain.destination):
        pass


def test_publish_never_overwrites_a_concurrently_created_file(remote, chain, monkeypatch):
    remote.add()
    real_link = os.link

    def competing_link(source, destination):
        Path(destination).write_bytes(b"someone else's file")
        real_link(source, destination)

    monkeypatch.setattr(backup.os, "link", competing_link)
    assert run_chain(remote, chain).failed == 1
    assert target_path(chain).read_bytes() == b"someone else's file"
    assert_no_staging(chain)


def test_fsync_failure_does_not_publish(remote, chain, monkeypatch):
    remote.add()
    monkeypatch.setattr(backup.os, "fsync", Mock(side_effect=OSError("disk error")))
    assert run_chain(remote, chain).failed == 1
    assert not target_path(chain).exists()
    assert_no_staging(chain)


@pytest.mark.parametrize(
    "component", ["2025", "2025/phone", "2025/phone/__raw__", "2025/phone/__raw__/photo.jpg"]
)
def test_symlinks_in_destination_are_rejected(remote, chain, tmp_path, component):
    remote.add()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = chain.destination / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    assert run_chain(remote, chain).failed == 1
    assert list(outside.iterdir()) == []
    assert link.is_symlink()


def test_unrelated_blocked_year_does_not_prevent_a_valid_copy(remote, chain):
    remote.add()
    (chain.destination / "1999").mkdir()
    (chain.destination / "1999" / "phone").write_bytes(b"not a directory")
    assert run_chain(remote, chain).copied == 1


def test_destination_root_must_exist(remote, chain, tmp_path):
    path = tmp_path / "missing-mount" / "photos"
    with pytest.raises(KeeperError, match="must already exist"):
        run_chain(remote, replace(chain, destination=path))
    assert not path.parent.exists()
    assert not remote.list_calls


@pytest.mark.parametrize("changes", [{"enabled": False}, {"source": None}, {"destination": None}])
def test_invalid_chain_never_lists_remote_files(remote, chain, changes):
    with pytest.raises(KeeperError):
        run_chain(remote, replace(chain, **changes))
    assert not remote.list_calls


def test_concurrent_runs_are_rejected_before_listing(remote, chain):
    with destination_lock(chain.destination), pytest.raises(KeeperError, match="Another copy run"):
        run_chain(remote, chain)
    assert not remote.list_calls
    assert run_chain(remote, chain).failed == 0


def test_lock_symlink_is_not_followed(remote, chain, tmp_path):
    outside = tmp_path / "file"
    outside.write_text("unchanged")
    (chain.destination / ".photo-keeper.lock").symlink_to(outside)
    with pytest.raises(OSError):
        run_chain(remote, chain)
    assert outside.read_text() == "unchanged"


def test_listing_failure_releases_lock(remote, chain, monkeypatch):
    monkeypatch.setattr(remote, "list_files", Mock(side_effect=KeeperError("listing failed")))
    with pytest.raises(KeeperError, match="listing failed"):
        run_chain(remote, chain)
    with destination_lock(chain.destination):
        pass


def test_long_unicode_filename_and_collision_are_handled(remote, chain):
    name = "ф" * 118 + ".jpg"
    remote.add(name)
    target = target_path(chain, name=name)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing contents")
    assert run_chain(remote, chain).copied == 1
    saved = [path for path in target.parent.iterdir() if path != target]
    assert len(saved) == 1
    assert saved[0].suffix == ".jpg"
    assert saved[0].read_bytes() == b"photo contents"
    assert target.read_bytes() == b"existing contents"
    assert run_chain(remote, chain).unchanged == 1
    assert len(remote.downloads) == 1


def test_downloads_only_pending_files_in_bounded_batches(remote, chain, monkeypatch):
    monkeypatch.setattr(backup, "BATCH_SIZE", 2)
    remote.add("existing.jpg")
    target = target_path(chain, name="existing.jpg")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"photo contents")
    new = [remote.add(f"file-{i}.jpg").path for i in range(5)]
    result = run_chain(remote, chain)
    assert (result.copied, result.unchanged, result.failed) == (5, 1, 0)
    assert remote.batches == [tuple(new[:2]), tuple(new[2:4]), tuple(new[4:])]
    assert_no_staging(chain)


def test_dry_run_scans_each_relevant_folder_once(remote, chain, monkeypatch):
    for name in ("one.jpg", "two.jpg", "three.jpg"):
        remote.add(name)
    archive = target_path(chain).parent
    archive.mkdir(parents=True)
    (archive / "one.jpg").write_bytes(b"photo contents")
    real_scandir = os.scandir
    scan = Mock(side_effect=real_scandir)
    real_stat = backup._existing_size
    check_size = Mock(side_effect=real_stat)
    monkeypatch.setattr(backup.os, "scandir", scan)
    monkeypatch.setattr(backup, "_existing_size", check_size)
    result = run_chain(remote, chain, dry_run=True)
    assert (result.unchanged, result.planned) == (1, 2)
    assert [call.args[0] for call in scan.call_args_list] == [chain.destination, archive]
    check_size.assert_called_once_with(archive / "one.jpg")
    assert not remote.batches


def test_failed_batch_only_publishes_matching_checksums(remote, chain, monkeypatch):
    monkeypatch.setattr(backup, "BATCH_SIZE", 3)
    bad = remote.add("broken.jpg")
    remote.payloads[bad.path] = KeeperError("transfer failed")
    remote.add("no-checksum.jpg", checksum=None)
    remote.add("verified.jpg")
    remote.add("next-batch.jpg", checksum=None)
    result = run_chain(remote, chain)
    assert (result.copied, result.failed) == (2, 2)
    assert not target_path(chain, name="broken.jpg").exists()
    assert not target_path(chain, name="no-checksum.jpg").exists()
    assert target_path(chain, name="verified.jpg").is_file()
    assert target_path(chain, name="next-batch.jpg").is_file()
    assert_no_staging(chain)


def test_interrupt_keeps_published_batches(remote, chain, monkeypatch):
    monkeypatch.setattr(backup, "BATCH_SIZE", 1)
    remote.add("first.jpg")
    second = remote.add("second.jpg")
    remote.payloads[second.path] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_chain(remote, chain)
    assert target_path(chain, name="first.jpg").is_file()
    assert not target_path(chain, name="second.jpg").exists()
    assert_no_staging(chain)
