from datetime import date
from pathlib import Path

import pytest

from photo_keeper import backup, organize
from photo_keeper.backup import run_chain
from photo_keeper.config import ConfigError, load_config
from photo_keeper.layout import Layout
from photo_keeper.media import MediaInfo

CHAIN = '[[chains]]\nname = "example"\nenabled = true\n'
COPY = '[[chains.steps]]\ntype = "copy"\n'
SORT = '[[chains.steps]]\ntype = "sort"\n'


def configure(root: Path, settings: str):
    path = root / "settings.toml"
    path.write_text(CHAIN + settings.replace("ROOT", str(root)), encoding="utf-8")
    return load_config(path).chains[0]


def fixture_file(directory: Path, name: str, payload: bytes):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_remote_then_local_routes_pics_and_screenshots_at_year_level(tmp_path, remote, monkeypatch):
    chain = configure(
        tmp_path,
        COPY
        + 'from = { rclone = "cloud:inbox" }\nto = "ROOT/{year}/Phone/__raw__"\n'
        + SORT
        + 'from = "previous"\nto = "ROOT/{year}/Phone/{month}"\n'
        '[chains.steps.routes]\nvideo = "video"\npng = "ROOT/{year}/Phone/screenshot"\n'
        'jpeg_without_camera = "ROOT/{year}/Phone/pics"\n',
    )
    files = {
        "screen.PNG": ("screenshot/screen.PNG", False),
        "nested/download.jpeg": ("pics/nested/download.jpeg", False),
        "shot.jpg": ("08/shot.jpg", True),
        "trip.mov": ("08/video/trip.mov", False),
        "motion.heic": ("08/motion.heic", True),
    }
    for name in files:
        remote.add(name, name.encode())
    old = fixture_file(tmp_path / "2025/Phone/08/screenshot", "screen.PNG", b"previous archive")
    monkeypatch.setattr(backup, "capture_year", lambda _: 2025)

    def metadata(path):
        camera = next(camera for name, (_, camera) in files.items() if Path(name).name == path.name)
        return MediaInfo(date(2025, 8, 30), camera)

    monkeypatch.setattr(organize, "read_media_info", metadata)
    result = run_chain(remote, chain)
    assert (result.copied, result.sorted, result.failed) == (5, 5, 0)
    for name, (where, _) in files.items():
        assert (tmp_path / "2025/Phone" / where).read_bytes() == name.encode()
    assert old.read_bytes() == b"previous archive"
    assert not (tmp_path / "2025/Phone/08/pics").exists()
    again = run_chain(remote, chain)
    assert (again.unchanged, again.sort_unchanged, again.sorted) == (5, 5, 0)
    assert len(remote.batches) == 1


@pytest.mark.parametrize("delete_origin", [True, False])
def test_three_local_steps_use_checked_output_and_cleanup_in_order(tmp_path, remote, delete_origin):
    original = fixture_file(tmp_path / "inbox", "trip.mov", b"video")
    chain = configure(
        tmp_path,
        COPY
        + 'from = { local = "ROOT/inbox" }\nto = "ROOT/staging"\n'
        + f"delete_source = {str(delete_origin).lower()}\n"
        + SORT
        + 'from = "previous"\nto = "ROOT/review"\ndelete_source = true\n'
        '[chains.steps.routes]\nvideo = "video"\n'
        + COPY
        + 'from = "previous"\nto = "ROOT/final"\ndelete_source = true\n',
    )
    result = run_chain(remote, chain)
    assert (result.copied, result.sorted, result.failed) == (2, 1, 0)
    assert result.raw_deleted == (3 if delete_origin else 0)
    assert original.exists() is not delete_origin
    assert (tmp_path / "staging/trip.mov").exists() is not delete_origin
    assert (tmp_path / "review/video/trip.mov").exists() is not delete_origin
    assert (tmp_path / "final/trip.mov").read_bytes() == b"video"
    assert not remote.list_calls
    repeat = run_chain(remote, chain)
    assert (repeat.copied, repeat.sorted, repeat.raw_deleted, repeat.failed) == (0, 0, 0, 0)
    assert (tmp_path / "final/trip.mov").read_bytes() == b"video"


def test_later_remote_step_cannot_delete_while_earlier_local_copy_exists(tmp_path, remote):
    original = fixture_file(tmp_path / "inbox", "first-name.heic", b"content")
    remote.add("other-name.heic", b"content")
    chain = configure(
        tmp_path,
        COPY
        + 'from = { local = "ROOT/inbox" }\nto = "ROOT/first"\n'
        + COPY
        + 'from = { rclone = "cloud:inbox" }\nto = "ROOT/second"\ndelete_source = true\n',
    )
    result = run_chain(remote, chain)
    assert (result.copied, result.source_deleted, result.failed) == (2, 0, 0)
    assert original.exists()
    assert remote.entries


def test_remote_copy_can_use_both_year_and_month(tmp_path, remote, monkeypatch):
    remote.add("image.heic")
    chain = configure(
        tmp_path,
        COPY + 'from = { rclone = "cloud:inbox" }\nto = "ROOT/{year}/{month}"\n',
    )
    monkeypatch.setattr(backup, "read_media_info", lambda _: MediaInfo(date(2024, 2, 29), True))
    assert run_chain(remote, chain).copied == 1
    assert (tmp_path / "2024/02/image.heic").is_file()
    assert run_chain(remote, chain, dry_run=True).unchanged == 1


def test_standalone_local_sort_preview_preserves_source_when_output_does_not_exist(
    tmp_path, remote
):
    origin = fixture_file(tmp_path / "inbox", "top.mov", b"video")
    fixture_file(tmp_path / "inbox", "nested/hidden.mov", b"nested video")
    chain = configure(
        tmp_path,
        SORT + 'from = { local = "ROOT/inbox" }\nto = "ROOT/output"\nrecursive = false\n'
        'delete_source = true\n[chains.steps.routes]\nvideo = "video"\n',
    )
    result = run_chain(remote, chain, dry_run=True)
    assert (result.sort_planned, result.raw_delete_planned, result.failed) == (1, 0, 0)
    assert origin.exists() and not (tmp_path / "output").exists()
    assert run_chain(remote, chain).raw_deleted == 1
    assert (tmp_path / "inbox/nested/hidden.mov").is_file()
    assert (tmp_path / "output/video/top.mov").is_file()


@pytest.mark.parametrize(
    "settings,match",
    [
        ("steps = []", "nonempty array"),
        ("steps = [1]", "must be a table"),
        (COPY + 'from = "previous"\nto = "ROOT/out"', "previous"),
        (SORT + 'from = { rclone = "cloud:inbox" }\nto = "ROOT/out"', "needs"),
        (
            COPY + 'from = { rclone = "cloud:" }\nto = "ROOT/out"\ndelete_source = true',
            "named folder",
        ),
        (COPY + 'from = { local = "ROOT/in" }\nto = "relative"', "absolute local folder"),
        (COPY + 'from = { local = "ROOT/in" }\nto = "ROOT/../out"', "placeholder"),
        (COPY + 'from = { local = "ROOT/in" }\nto = "ROOT/{week}"', "placeholder"),
        (COPY + 'from = { local = "ROOT/in" }\nto = "ROOT/{year}/{year}"', "placeholder"),
        (COPY + 'from = { local = "ROOT/in" }\nto = "ROOT/out"\ndelete_source = 1', "booleans"),
        (
            COPY + 'from = { local = "ROOT/in" }\nto = "ROOT/out"\ndate_source = "today"',
            "date_source",
        ),
        (COPY + 'from = { local = "ROOT/in" }\nto = "ROOT/in/nested"', "overlaps"),
        (COPY + 'from = { local = "ROOT/{year}/in" }\nto = "ROOT/2025/in"', "overlaps"),
        (
            COPY + 'from = { local = "ROOT/in" }\nto = "ROOT/out"\nroutes = { video = "sub" }',
            "sort step",
        ),
        (
            SORT + 'from = { local = "ROOT/in" }\nto = "ROOT/out"\nroutes = { raw = "sub" }',
            "unknown route",
        ),
        (
            SORT + 'from = { local = "ROOT/in" }\nto = "ROOT/out"\nroutes = { video = "../in" }',
            "relative path",
        ),
        (COPY + 'from = { local = "ROOT/in" }\nto = "ROOT/out"\nextras = true', "unknown keys"),
        (
            '[[chains.steps]]\ntype = "move"\nfrom = { local = "ROOT/in" }\nto = "ROOT/out"',
            "copy.*sort",
        ),
        (
            'source = "cloud:folder"\n' + COPY + 'from = { local = "ROOT/in" }\nto = "ROOT/out"',
            "cannot mix",
        ),
        (
            COPY
            + 'from = { local = "ROOT/in" }\nto = "ROOT/out"\n'
            + COPY
            + 'from = "previous"\nto = "ROOT/in"\ndelete_source = true',
            "overlaps",
        ),
    ],
)
def test_step_configuration_rejects_invalid_or_unsafe_layout(tmp_path, settings, match):
    with pytest.raises(ConfigError, match=match):
        configure(tmp_path, settings)


def test_path_template_ignores_noncalendar_folders_and_symlinks(tmp_path):
    for relative in ("2024/01", "2025/12", "2025/13", "2025/3", "2026/10"):
        (tmp_path / relative).mkdir(parents=True)
    (tmp_path / "2020").symlink_to(tmp_path / "2024")
    layout = Layout(tmp_path / "{year}/{month}")
    assert {(folder.year, folder.month) for folder in layout.folders()} == {
        (2024, 1),
        (2025, 12),
        (2026, 10),
    }
    assert not layout.overlaps(Layout(tmp_path / "{month}/01"))
    assert layout.overlaps(Layout(tmp_path / "2025/12/descendant"))
