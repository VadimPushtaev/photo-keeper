import errno
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import DeletedMetadata, FolderMetadata, MediaInfo, PhotoMetadata, VideoMetadata
from requests.exceptions import ConnectionError

from photo_keeper import backup
from photo_keeper.backup import (
    destination_lock,
    destination_year,
    relative_path,
    run_chain,
)
from photo_keeper.errors import KeeperError


def target_path(chain, year=2025, name="photo.jpg"):
    return chain.destination / str(year) / chain.subdirectory / name


def assert_no_partial_files(root):
    assert list(root.rglob("*.part")) == []


def test_copy_verifies_bytes_and_preserves_modified_time(remote, make_entry, chain) -> None:
    entry = remote.add(make_entry(taken=datetime(2022, 7, 4)))
    result = run_chain(remote, chain)
    target = target_path(chain, 2022)
    assert result.copied == 1
    assert result.failed == result.unchanged == 0
    assert target.read_bytes() == b"photo contents"
    assert target.stat().st_mtime == entry.client_modified.replace(tzinfo=UTC).timestamp()
    assert remote.downloads == [f"rev:{entry.rev}"]
    assert remote.metadata_calls == [(f"rev:{entry.rev}", True)]
    assert remote.responses[0].closed
    assert_no_partial_files(chain.destination)


def test_repeat_run_does_not_download_again(remote, make_entry, chain) -> None:
    remote.add(make_entry())
    assert run_chain(remote, chain).copied == 1
    assert run_chain(remote, chain).unchanged == 1
    assert len(remote.downloads) == 1


def test_same_size_different_content_is_preserved(remote, make_entry, chain) -> None:
    entry = remote.add(make_entry())
    original = target_path(chain)
    original.parent.mkdir(parents=True)
    old_bytes = b"x" * entry.size
    original.write_bytes(old_bytes)
    assert run_chain(remote, chain).copied == 1
    alternate = original.with_name(f"photo--{entry.content_hash}.jpg")
    assert alternate.read_bytes() == b"photo contents"
    assert original.read_bytes() == old_bytes
    assert run_chain(remote, chain).unchanged == 1
    assert len(remote.downloads) == 1


def test_changed_source_revision_is_copied_without_overwriting(remote, make_entry, chain) -> None:
    remote.add(make_entry())
    run_chain(remote, chain)
    remote.entries.clear()
    revised = remote.add(make_entry(data=b"new version"), b"new version")
    assert run_chain(remote, chain).copied == 1
    original = target_path(chain)
    assert original.read_bytes() == b"photo contents"
    assert original.with_name(f"photo--{revised.content_hash}.jpg").read_bytes() == b"new version"


def test_conflicting_alternate_is_never_overwritten(remote, make_entry, chain) -> None:
    entry = remote.add(make_entry())
    original = target_path(chain)
    original.parent.mkdir(parents=True)
    original.write_bytes(b"old")
    alternate = original.with_name(f"photo--{entry.content_hash}.jpg")
    alternate.write_bytes(b"conflict")
    assert run_chain(remote, chain).failed == 1
    assert alternate.read_bytes() == b"conflict"
    assert remote.downloads == []


def test_missing_local_copy_is_downloaded_again(remote, make_entry, chain) -> None:
    remote.add(make_entry())
    run_chain(remote, chain)
    target_path(chain).unlink()
    assert run_chain(remote, chain).copied == 1
    assert len(remote.downloads) == 2


def test_existing_exact_file_is_verified_without_download(remote, make_entry, chain) -> None:
    remote.add(make_entry())
    path = target_path(chain)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"photo contents")
    assert run_chain(remote, chain).unchanged == 1
    assert not remote.downloads


def test_unicode_and_subfolders_with_duplicate_basenames(remote, make_entry, chain) -> None:
    chain = replace(chain, subdirectory="Камера/__raw__")
    remote.add(make_entry("/Camera Uploads/Поездка/снимок.jpg"))
    remote.add(make_entry("/Camera Uploads/Дом/снимок.jpg", b"other"), b"other")
    assert run_chain(remote, chain).copied == 2
    assert target_path(chain, name="Поездка/снимок.jpg").read_bytes() == b"photo contents"
    assert target_path(chain, name="Дом/снимок.jpg").read_bytes() == b"other"


@pytest.mark.parametrize(
    "name", ["video.MOV", "image.HEIC", "image.RAW", "notes.xmp", "no-extension", ".hidden"]
)
def test_raw_copy_does_not_discard_files_based_on_extension(
    remote, make_entry, chain, name
) -> None:
    remote.add(make_entry("/Camera Uploads/" + name))
    assert run_chain(remote, chain).copied == 1
    assert target_path(chain, name=name).is_file()


def test_empty_file_is_verified_and_repeatable(remote, make_entry, chain) -> None:
    remote.add(make_entry(data=b""), b"")
    assert run_chain(remote, chain).copied == 1
    assert target_path(chain).read_bytes() == b""
    assert run_chain(remote, chain).unchanged == 1


def test_dry_run_performs_no_local_writes_or_downloads(remote, make_entry, chain) -> None:
    remote.add(make_entry())
    result = run_chain(remote, chain, dry_run=True)
    assert result.planned == 1
    assert result.copied == result.failed == 0
    assert list(chain.destination.iterdir()) == []
    assert remote.downloads == []


def test_dry_run_checks_existing_files(remote, make_entry, chain) -> None:
    remote.add(make_entry())
    run_chain(remote, chain)
    assert run_chain(remote, chain, dry_run=True).unchanged == 1
    assert len(remote.downloads) == 1


@pytest.mark.parametrize("media", [None, MediaInfo.pending, MediaInfo.metadata(PhotoMetadata())])
def test_capture_date_fallback_uses_client_not_server_modified(remote, make_entry, media) -> None:
    entry = remote.add(make_entry(media_info=media, modified=datetime(2017, 12, 31, 23, 59)))
    assert destination_year(remote, entry, "capture") == 2017


@pytest.mark.parametrize("kind", [PhotoMetadata, VideoMetadata])
def test_photo_and_video_capture_dates(remote, make_entry, kind) -> None:
    entry = remote.add(
        make_entry(media_info=MediaInfo.metadata(kind(time_taken=datetime(2019, 1, 1))))
    )
    assert destination_year(remote, entry, "capture") == 2019


def test_modified_policy_avoids_media_metadata_request(remote, make_entry, chain) -> None:
    remote.add(make_entry(taken=datetime(2001, 1, 1)))
    assert run_chain(remote, replace(chain, date_source="modified")).copied == 1
    assert target_path(chain).exists()
    assert remote.metadata_calls == []


def test_metadata_failure_is_not_silently_misfiled(remote, make_entry, chain) -> None:
    entry = remote.add(make_entry())
    remote.metadata[entry.rev] = ConnectionError("secret response")
    assert run_chain(remote, chain).failed == 1
    assert not remote.downloads
    assert not target_path(chain).exists()


@pytest.mark.parametrize("change", ["rev", "id", "folder"])
def test_metadata_for_wrong_revision_is_rejected(remote, make_entry, chain, change) -> None:
    entry = remote.add(make_entry())
    remote.metadata[entry.rev] = (
        FolderMetadata(name="folder", id="id:folder")
        if change == "folder"
        else make_entry(**{change: "abcdefabcdef" if change == "rev" else "id:other"})
    )
    assert run_chain(remote, chain).failed == 1
    assert not remote.downloads


def test_pagination_and_folder_filtering(remote, make_entry, chain) -> None:
    first = remote.add(make_entry("/Camera Uploads/first.jpg"))
    second = remote.add(make_entry("/Camera Uploads/second.jpg"))
    remote.pages = [
        [FolderMetadata(name="folder", id="id:folder"), first],
        [],
        [DeletedMetadata(name="gone"), second],
    ]
    assert run_chain(remote, chain).copied == 2
    assert remote.continue_calls == ["1", "2"]
    assert remote.list_calls == [("/Camera Uploads", True, False)]


def test_empty_source_succeeds(remote, chain) -> None:
    result = run_chain(remote, chain)
    assert result.copied == result.failed == result.ignored == 0


def test_root_source_is_translated_to_dropbox_empty_path(remote, make_entry, chain) -> None:
    remote.add(make_entry("/top.jpg"))
    assert run_chain(remote, replace(chain, source="/", recursive=False)).copied == 1
    assert remote.list_calls == [("", False, False)]
    assert target_path(chain, name="top.jpg").exists()


def test_non_downloadable_document_is_reported_as_ignored(remote, make_entry, chain) -> None:
    remote.add(make_entry(is_downloadable=False))
    result = run_chain(remote, chain)
    assert result.ignored == 1
    assert result.copied == 0
    assert remote.metadata_calls == remote.downloads == []


@pytest.mark.parametrize("content_hash", [None, "g" * 64, "../" + "x" * 61])
def test_missing_or_malformed_hash_fails_before_download(
    remote, make_entry, chain, content_hash
) -> None:
    remote.add(make_entry(content_hash=content_hash))
    assert run_chain(remote, chain).failed == 1
    assert remote.downloads == []


@pytest.mark.parametrize("payload", [b"short", b"x" * 14, b"x" * 100])
def test_corrupt_or_truncated_download_is_not_published(remote, make_entry, chain, payload) -> None:
    entry = remote.add(make_entry())
    remote.payloads[entry.rev] = payload
    assert run_chain(remote, chain).failed == 1
    assert not target_path(chain).exists()
    assert remote.responses[0].closed
    assert_no_partial_files(chain.destination)


def test_failed_file_does_not_prevent_other_copies(remote, make_entry, chain) -> None:
    bad = remote.add(make_entry("/Camera Uploads/bad.jpg"))
    remote.payloads[bad.rev] = b"wrong"
    remote.add(make_entry("/Camera Uploads/good.jpg"))
    result = run_chain(remote, chain)
    assert result.failed == result.copied == 1
    assert not target_path(chain, name="bad.jpg").exists()
    assert target_path(chain, name="good.jpg").exists()


@pytest.mark.parametrize(
    "error", [ConnectionError("secret-token"), OSError(errno.ENOSPC, "Disk full")]
)
def test_interrupted_download_is_cleaned_and_can_be_retried(
    remote, make_entry, chain, error, caplog
) -> None:
    remote.add(make_entry())
    remote.stream_error = error
    with caplog.at_level(logging.ERROR):
        assert run_chain(remote, chain).failed == 1
    assert "secret-token" not in caplog.text
    assert_no_partial_files(chain.destination)
    assert not target_path(chain).exists()
    assert remote.responses[0].closed
    remote.stream_error = None
    assert run_chain(remote, chain).copied == 1


@pytest.mark.parametrize("field", ["id", "rev", "size", "content_hash"])
def test_download_metadata_must_match_enumerated_revision(remote, make_entry, chain, field) -> None:
    entry = remote.add(make_entry())
    values = {"id": "id:other", "rev": "bbbbbbbbbbbb", "size": 100, "content_hash": "f" * 64}
    remote.download_metadata[entry.rev] = make_entry(**{field: values[field]})
    assert run_chain(remote, chain).failed == 1
    assert remote.responses[0].closed
    assert not target_path(chain).exists()
    assert_no_partial_files(chain.destination)


def test_keyboard_interrupt_cleans_up_and_releases_lock(remote, make_entry, chain) -> None:
    remote.add(make_entry())
    remote.stream_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_chain(remote, chain)
    assert_no_partial_files(chain.destination)
    assert remote.responses[0].closed
    with destination_lock(chain.destination):
        pass


def test_publish_does_not_overwrite_file_created_during_download(
    remote, make_entry, chain, monkeypatch
) -> None:
    remote.add(make_entry())
    real_link = os.link

    def competing_link(source, destination):
        Path(destination).write_bytes(b"someone else's file")
        real_link(source, destination)

    monkeypatch.setattr(backup.os, "link", competing_link)
    assert run_chain(remote, chain).failed == 1
    assert target_path(chain).read_bytes() == b"someone else's file"
    assert_no_partial_files(chain.destination)


def test_fsync_failure_does_not_publish(remote, make_entry, chain, monkeypatch) -> None:
    remote.add(make_entry())
    monkeypatch.setattr(backup.os, "fsync", MagicMock(side_effect=OSError("disk error")))
    assert run_chain(remote, chain).failed == 1
    assert not target_path(chain).exists()
    assert_no_partial_files(chain.destination)


@pytest.mark.parametrize(
    "path",
    [
        "/Other/photo.jpg",
        "/Camera Uploads Other/photo.jpg",
        "/Camera Uploads",
        "/Camera Uploads/../escape.jpg",
        "/Camera Uploads/./photo.jpg",
        "/Camera Uploads//photo.jpg",
        "/Camera Uploads/a\\b.jpg",
        "/Camera Uploads/C:photo.jpg",
        "/Camera Uploads/line\nfeed.jpg",
    ],
)
def test_unsafe_remote_paths_cannot_write_outside_destination(
    remote, make_entry, chain, path
) -> None:
    remote.add(make_entry(path))
    assert run_chain(remote, chain).failed == 1
    assert remote.downloads == []


def test_remote_paths_match_source_case_insensitively(make_entry) -> None:
    assert relative_path(make_entry("/camera uploads/Photo.JPG"), "/Camera Uploads") == Path(
        "Photo.JPG"
    )


def test_missing_display_path_uses_path_lower(make_entry) -> None:
    entry = make_entry(path_display=None)
    assert relative_path(entry, "/Camera Uploads") == Path("photo.jpg")


def test_missing_both_paths_is_rejected(make_entry) -> None:
    with pytest.raises(KeeperError, match="absolute path"):
        relative_path(make_entry(path_display=None, path_lower=None), "/Camera Uploads")


@pytest.mark.parametrize(
    "component", ["2025", "2025/phone", "2025/phone/__raw__", "2025/phone/__raw__/photo.jpg"]
)
def test_symlink_destinations_are_rejected(remote, make_entry, chain, tmp_path, component) -> None:
    remote.add(make_entry())
    outside = tmp_path / "outside"
    outside.mkdir()
    link = chain.destination / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    assert run_chain(remote, chain).failed == 1
    assert list(outside.iterdir()) == []
    assert not remote.downloads


def test_broken_destination_symlink_is_not_replaced(remote, make_entry, chain, tmp_path) -> None:
    remote.add(make_entry())
    target = target_path(chain)
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "missing")
    assert run_chain(remote, chain).failed == 1
    assert target.is_symlink()


def test_directory_at_filename_is_not_overwritten(remote, make_entry, chain) -> None:
    remote.add(make_entry())
    target_path(chain).mkdir(parents=True)
    assert run_chain(remote, chain).failed == 1
    assert target_path(chain).is_dir()


def test_missing_destination_root_is_not_created(remote, chain, tmp_path) -> None:
    path = tmp_path / "missing-mount" / "photos"
    with pytest.raises(KeeperError, match="must already exist"):
        run_chain(remote, replace(chain, destination=path))
    assert not path.parent.exists()
    assert not remote.list_calls


def test_destination_file_is_rejected(remote, chain, tmp_path) -> None:
    path = tmp_path / "file"
    path.write_text("existing")
    with pytest.raises(KeeperError, match="must already exist"):
        run_chain(remote, replace(chain, destination=path))
    assert path.read_text() == "existing"


def test_disabled_chain_never_contacts_dropbox(remote, chain) -> None:
    with pytest.raises(KeeperError, match="disabled"):
        run_chain(remote, replace(chain, enabled=False))
    assert remote.list_calls == []


def test_concurrent_run_is_rejected_before_listing(remote, chain) -> None:
    with destination_lock(chain.destination):
        with pytest.raises(KeeperError, match="Another copy run"):
            run_chain(remote, chain)
    assert not remote.list_calls
    assert run_chain(remote, chain).failed == 0


def test_lock_symlink_is_not_followed(remote, chain, tmp_path) -> None:
    outside = tmp_path / "file"
    outside.write_text("unchanged")
    (chain.destination / ".photo-keeper.lock").symlink_to(outside)
    with pytest.raises(OSError):
        run_chain(remote, chain)
    assert outside.read_text() == "unchanged"


def test_pagination_failure_propagates_and_releases_lock(remote, chain, monkeypatch) -> None:
    remote.pages = [[], []]
    monkeypatch.setattr(
        remote, "files_list_folder_continue", MagicMock(side_effect=ConnectionError())
    )
    with pytest.raises(ConnectionError):
        run_chain(remote, chain)
    with destination_lock(chain.destination):
        pass


@pytest.mark.parametrize(
    "error", [AuthError("req", "secret-token"), ApiError("req", "secret-token", None, None)]
)
def test_api_errors_do_not_leak_token_response(remote, make_entry, chain, error, caplog) -> None:
    entry = remote.add(make_entry())
    remote.metadata[entry.rev] = error
    with caplog.at_level(logging.ERROR):
        assert run_chain(remote, chain).failed == 1
    assert "secret-token" not in caplog.text
