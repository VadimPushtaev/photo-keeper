from pathlib import Path
from unittest.mock import Mock

import pytest

from photo_keeper import cli
from photo_keeper.cli import main
from photo_keeper.errors import KeeperError


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '[[chains]]\nname = "first"\nenabled = true\nsource = "dropbox:Camera Uploads"\n'
        f'destination = "{tmp_path.as_posix()}"\ndate_source = "modified"\n'
        '[[chains]]\nname = "second"\n',
        encoding="utf-8",
    )
    return path


def test_check_config(config_path, capsys):
    assert main(["--config", str(config_path), "check-config"]) == 0
    assert "Configuration valid: 2 chain(s)." in capsys.readouterr().out


def test_list_chains(config_path, capsys):
    assert main(["--config", str(config_path), "list-chains"]) == 0
    assert capsys.readouterr().out == "first\tenabled\nsecond\tdisabled\n"


@pytest.mark.parametrize("name", ["second", "missing"])
def test_unknown_and_disabled_chains_do_not_create_a_cloud_client(config_path, monkeypatch, name):
    rclone = Mock()
    monkeypatch.setattr(cli, "Rclone", rclone)
    assert main(["--config", str(config_path), "run", name]) == 2
    rclone.assert_not_called()


def test_bad_config_returns_error(tmp_path, caplog):
    assert main(["--config", str(tmp_path / "absent.toml"), "check-config"]) == 2
    assert "Cannot load" in caplog.text


@pytest.mark.parametrize(
    "error,code",
    [
        (KeeperError("cloud error"), 1),
        (PermissionError("access denied"), 1),
        (KeyboardInterrupt(), 130),
    ],
)
def test_runtime_failures_return_exit_codes(config_path, monkeypatch, caplog, error, code):
    monkeypatch.setattr(cli, "run_chain", Mock(side_effect=error))
    assert main(["--config", str(config_path), "run", "first"]) == code
    assert "Traceback" not in caplog.text


def test_login_uses_default_rclone_config_if_photo_keeper_config_is_missing(monkeypatch, tmp_path):
    rclone = Mock()
    monkeypatch.setattr(cli, "Rclone", rclone)
    assert main(["--config", str(tmp_path / "absent.toml"), "login"]) == 0
    rclone.assert_called_once_with(None)
    rclone.return_value.configure.assert_called_once_with()


def test_login_uses_custom_rclone_config(monkeypatch, tmp_path):
    rclone = Mock()
    monkeypatch.setattr(cli, "Rclone", rclone)
    config = tmp_path / "config.toml"
    config.write_text('[rclone]\nconfig_file = "secrets/rclone.conf"')
    assert main(["--config", str(config), "login"]) == 0
    rclone.assert_called_once_with(tmp_path / "secrets/rclone.conf")
    rclone.return_value.configure.assert_called_once_with()


def test_cli_dry_run_copy_and_rerun(config_path, remote, monkeypatch, capsys):
    remote.add()
    monkeypatch.setattr(cli, "Rclone", Mock(return_value=remote))
    args = ["--config", str(config_path), "run", "first"]
    assert main([*args, "--dry-run"]) == 0
    assert "planned=1" in capsys.readouterr().out
    target = config_path.parent / "2025/__raw__/photo.jpg"
    assert not target.exists()
    assert main(args) == 0
    assert target.read_bytes() == b"photo contents"
    assert "copied=1" in capsys.readouterr().out
    assert main(args) == 0
    assert "unchanged=1" in capsys.readouterr().out
    assert len(remote.downloads) == 1


def test_cli_reports_failed_file_count(config_path, remote, monkeypatch, capsys):
    remote.add(size=1)
    monkeypatch.setattr(cli, "Rclone", Mock(return_value=remote))
    assert main(["--config", str(config_path), "run", "first"]) == 1
    assert "failed=1" in capsys.readouterr().out
