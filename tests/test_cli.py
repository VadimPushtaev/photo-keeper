import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dropbox.exceptions import AuthError
from requests.exceptions import ConnectionError

from photo_keeper import cli
from photo_keeper.auth import Credentials, save_credentials
from photo_keeper.backup import RunResult
from photo_keeper.cli import main
from photo_keeper.errors import KeeperError


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '[[chains]]\nname = "first"\nenabled = true\nsource = "/Camera Uploads"\n'
        f'destination = "{tmp_path.as_posix()}"\n[[chains]]\nname = "second"\n',
        encoding="utf-8",
    )
    return path


def test_check_config(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(config_path), "check-config"]) == 0
    assert "Configuration valid: 2 chain(s)." in capsys.readouterr().out


def test_list_chains(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(config_path), "list-chains"]) == 0
    assert capsys.readouterr().out == "first\tenabled\nsecond\tdisabled\n"


@pytest.mark.parametrize(
    ("chain", "status", "message"),
    [
        ("first", 1, "Cannot read credentials"),
        ("second", 2, "disabled"),
        ("missing", 2, "Unknown chain"),
    ],
)
def test_run_reports_failure(
    config_path: Path,
    caplog: pytest.LogCaptureFixture,
    chain: str,
    status: int,
    message: str,
) -> None:
    with caplog.at_level(logging.ERROR):
        assert main(["--config", str(config_path), "run", chain]) == status
    assert message in caplog.text


def test_bad_config_returns_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        assert main(["--config", str(tmp_path / "missing.toml"), "check-config"]) == 2
    assert "Cannot load" in caplog.text


@pytest.fixture
def authenticated(config_path, monkeypatch):
    save_credentials(config_path.parent / ".secrets/dropbox.json", Credentials("key", "secret"))
    client = MagicMock()
    client.__enter__.return_value = client
    monkeypatch.setattr(cli, "create_client", MagicMock(return_value=client))
    return client


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (RunResult(copied=2, unchanged=3), 0),
        (RunResult(copied=2, failed=1), 1),
        (RunResult(planned=4), 0),
    ],
)
def test_run_summary_and_exit_status(
    config_path, authenticated, monkeypatch, capsys, result, code
) -> None:
    run = MagicMock(return_value=result)
    monkeypatch.setattr(cli, "run_chain", run)
    assert main(["--config", str(config_path), "run", "first"]) == code
    output = capsys.readouterr().out
    assert f"copied={result.copied}" in output
    assert f"failed={result.failed}" in output
    assert run.call_args.args[0] is authenticated
    assert run.call_args.kwargs == {"dry_run": False}
    authenticated.__exit__.assert_called_once()


def test_dry_run_is_forwarded(config_path, authenticated, monkeypatch, capsys) -> None:
    run = MagicMock(return_value=RunResult(planned=1))
    monkeypatch.setattr(cli, "run_chain", run)
    assert main(["--config", str(config_path), "run", "first", "--dry-run"]) == 0
    assert run.call_args.kwargs == {"dry_run": True}
    assert "Dry run first" in capsys.readouterr().out


@pytest.mark.parametrize(
    "error",
    [
        KeeperError("cannot copy"),
        PermissionError("access denied"),
        ConnectionError("private-token"),
        AuthError("request", "private-token"),
    ],
)
def test_run_errors_are_nonzero_without_traceback_or_secrets(
    config_path, authenticated, monkeypatch, caplog, error
) -> None:
    monkeypatch.setattr(cli, "run_chain", MagicMock(side_effect=error))
    assert main(["--config", str(config_path), "run", "first"]) == 1
    assert "private-token" not in caplog.text
    assert "Traceback" not in caplog.text
    authenticated.__exit__.assert_called_once()


def test_interrupt_returns_130(config_path, authenticated, monkeypatch) -> None:
    monkeypatch.setattr(cli, "run_chain", MagicMock(side_effect=KeyboardInterrupt()))
    assert main(["--config", str(config_path), "run", "first"]) == 130
    authenticated.__exit__.assert_called_once()


def test_login_does_not_require_config(tmp_path, monkeypatch, capsys) -> None:
    login = MagicMock(return_value="test@example.org")
    monkeypatch.setattr(cli, "login", login)
    config = tmp_path / "absent.toml"
    assert main(["--config", str(config), "login", "dropbox", "--app-key", "app"]) == 0
    login.assert_called_once_with("app", tmp_path / ".secrets/dropbox.json", open_browser=True)
    assert "test@example.org" in capsys.readouterr().out


def test_login_no_browser_and_credentials_override(tmp_path, monkeypatch) -> None:
    login = MagicMock(return_value="test@example.org")
    monkeypatch.setattr(cli, "login", login)
    path = tmp_path / "token.json"
    assert (
        main(
            [
                "login",
                "dropbox",
                "--app-key",
                "app",
                "--no-browser",
                "--credentials-file",
                str(path),
            ]
        )
        == 0
    )
    login.assert_called_once_with("app", path, open_browser=False)


def test_login_respects_config_credentials_path(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[dropbox]\ncredentials_file = "custom/creds.json"')
    login = MagicMock(return_value="test@example.org")
    monkeypatch.setattr(cli, "login", login)
    assert main(["--config", str(config), "login", "dropbox", "--app-key", "app"]) == 0
    login.assert_called_once_with("app", tmp_path / "custom/creds.json", open_browser=True)


def test_login_key_from_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DROPBOX_APP_KEY", "env-key")
    login = MagicMock(return_value="test@example.org")
    monkeypatch.setattr(cli, "login", login)
    assert main(["--config", str(tmp_path / "config.toml"), "login", "dropbox"]) == 0
    assert login.call_args.args[0] == "env-key"


def test_login_requires_app_key(monkeypatch, caplog) -> None:
    monkeypatch.delenv("DROPBOX_APP_KEY", raising=False)
    assert main(["login", "dropbox"]) == 2
    assert "--app-key" in caplog.text


def test_login_error_is_reported(monkeypatch, caplog) -> None:
    monkeypatch.setattr(cli, "login", MagicMock(side_effect=KeeperError("login failed")))
    assert main(["login", "dropbox", "--app-key", "app"]) == 1
    assert "login failed" in caplog.text


def test_cli_copy_and_rerun_with_fake_remote(
    config_path, authenticated, remote, make_entry, monkeypatch, capsys
) -> None:
    remote.add(make_entry())
    authenticated.__enter__.return_value = remote
    assert main(["--config", str(config_path), "run", "first"]) == 0
    target = config_path.parent / "2025/__raw__/photo.jpg"
    assert target.read_bytes() == b"photo contents"
    assert "copied=1" in capsys.readouterr().out
    assert main(["--config", str(config_path), "run", "first"]) == 0
    assert "unchanged=1" in capsys.readouterr().out
    assert len(remote.downloads) == 1


def test_unknown_and_disabled_chains_do_not_load_credentials(config_path, monkeypatch) -> None:
    credentials = MagicMock(side_effect=AssertionError("must not access credentials"))
    monkeypatch.setattr(cli, "load_credentials", credentials)
    assert main(["--config", str(config_path), "run", "second"]) == 2
    assert main(["--config", str(config_path), "run", "missing"]) == 2
    credentials.assert_not_called()


@pytest.mark.parametrize(
    "args", [[], ["run"], ["login"], ["login", "other"], ["run", "first", "--delete"]]
)
def test_invalid_cli_usage(args) -> None:
    with pytest.raises(SystemExit) as caught:
        main(args)
    assert caught.value.code == 2
