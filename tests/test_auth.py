import stat
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from dropbox.exceptions import AuthError
from dropbox.oauth import BadInputException
from requests.exceptions import ConnectionError

from photo_keeper import auth
from photo_keeper.auth import Credentials, create_client, load_credentials, login, save_credentials
from photo_keeper.errors import KeeperError


def test_credentials_round_trip_and_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "private" / "dropbox.json"
    credentials = Credentials("app-key", "a-secret-refresh-token")
    save_credentials(path, credentials)
    assert load_credentials(path) == credentials
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.iterdir()) == [path]
    assert credentials.refresh_token not in repr(credentials)


def test_relogin_replaces_credentials_and_fixes_permissions(tmp_path: Path) -> None:
    path = tmp_path / "dropbox.json"
    path.write_text("old")
    path.chmod(0o644)
    save_credentials(path, Credentials("new-app", "new-token"))
    assert load_credentials(path).refresh_token == "new-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_failed_save_preserves_old_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "dropbox.json"
    old = Credentials("old-app", "old-token")
    save_credentials(path, old)
    monkeypatch.setattr(auth.os, "replace", MagicMock(side_effect=OSError("disk failure")))
    with pytest.raises(OSError):
        save_credentials(path, Credentials("new", "new"))
    assert load_credentials(path) == old
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "{broken",
        "null",
        "[]",
        "42",
        "{}",
        '{"app_key": "key"}',
        '{"app_key": 3, "refresh_token": "secret"}',
        '{"app_key": "key", "refresh_token": " "}',
        '{"app_key": "key", "refresh_token": false}',
        '{"app_key": "", "refresh_token": "secret"}',
    ],
)
def test_invalid_credentials_are_actionable_and_do_not_leak(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "dropbox.json"
    path.write_text(contents)
    with pytest.raises(KeeperError, match="login dropbox") as caught:
        load_credentials(path)
    assert "secret" not in str(caught.value)


def test_missing_credentials(tmp_path: Path) -> None:
    with pytest.raises(KeeperError, match="login dropbox"):
        load_credentials(tmp_path / "missing")


def test_invalid_utf8_credentials(tmp_path: Path) -> None:
    path = tmp_path / "creds"
    path.write_bytes(b"\xff")
    with pytest.raises(KeeperError, match="login dropbox"):
        load_credentials(path)


@pytest.fixture
def oauth(monkeypatch: pytest.MonkeyPatch):
    flow = MagicMock()
    flow.start.return_value = "https://www.dropbox.com/oauth2/authorize?test"
    flow.finish.return_value = SimpleNamespace(refresh_token="private-refresh-token")
    factory = MagicMock(return_value=flow)
    client = MagicMock()
    client.__enter__.return_value = client
    client.users_get_current_account.return_value = SimpleNamespace(email="test@example.org")
    monkeypatch.setattr(auth.dropbox, "DropboxOAuth2FlowNoRedirect", factory)
    monkeypatch.setattr(auth, "create_client", MagicMock(return_value=client))
    monkeypatch.setattr(auth.getpass, "getpass", MagicMock(return_value=" authorization-code "))
    browser = MagicMock(return_value=True)
    monkeypatch.setattr(auth.webbrowser, "open", browser)
    return SimpleNamespace(flow=flow, factory=factory, client=client, browser=browser)


def test_login_uses_pkce_offline_read_scopes_and_saves_token(oauth, tmp_path, capsys) -> None:
    path = tmp_path / "private" / "dropbox.json"
    assert login(" app-key ", path) == "test@example.org"
    oauth.factory.assert_called_once_with(
        "app-key", use_pkce=True, token_access_type="offline", scope=auth.SCOPES, timeout=60
    )
    assert all(scope.endswith(".read") for scope in auth.SCOPES)
    oauth.flow.finish.assert_called_once_with("authorization-code")
    oauth.browser.assert_called_once()
    assert load_credentials(path) == Credentials("app-key", "private-refresh-token")
    assert "private-refresh-token" not in capsys.readouterr().out


def test_no_browser_mode(oauth, tmp_path) -> None:
    login("app", tmp_path / "creds", open_browser=False)
    oauth.browser.assert_not_called()


def test_browser_failure_still_allows_manual_login(oauth, tmp_path) -> None:
    oauth.browser.side_effect = webbrowser.Error("no browser")
    login("app", tmp_path / "creds")
    assert (tmp_path / "creds").exists()


@pytest.mark.parametrize(
    "error",
    [BadInputException("secret-code"), ConnectionError("secret-token"), AuthError("req", "secret")],
)
def test_login_errors_do_not_expose_secrets_or_replace_credentials(oauth, tmp_path, error) -> None:
    path = tmp_path / "creds"
    save_credentials(path, Credentials("old", "old"))
    oauth.flow.finish.side_effect = error
    with pytest.raises(KeeperError, match="login failed") as caught:
        login("new", path)
    assert "secret" not in str(caught.value)
    assert load_credentials(path) == Credentials("old", "old")


def test_missing_refresh_token_does_not_save(oauth, tmp_path) -> None:
    oauth.flow.finish.return_value.refresh_token = None
    with pytest.raises(KeeperError, match="refresh token"):
        login("app", tmp_path / "creds")
    assert not (tmp_path / "creds").exists()


def test_account_verification_failure_does_not_save(oauth, tmp_path) -> None:
    oauth.client.users_get_current_account.side_effect = ConnectionError("private response")
    with pytest.raises(KeeperError, match="login failed"):
        login("app", tmp_path / "creds")
    assert not (tmp_path / "creds").exists()


@pytest.mark.parametrize("code", ["", "   ", EOFError()])
def test_missing_authorization_code(oauth, tmp_path, monkeypatch, code) -> None:
    reader = (
        MagicMock(side_effect=code) if isinstance(code, Exception) else MagicMock(return_value=code)
    )
    monkeypatch.setattr(auth.getpass, "getpass", reader)
    with pytest.raises(KeeperError, match="No authorization code"):
        login("app", tmp_path / "creds")
    oauth.flow.finish.assert_not_called()


def test_empty_app_key(oauth, tmp_path) -> None:
    with pytest.raises(KeeperError, match="app key"):
        login(" ", tmp_path / "creds")
    oauth.factory.assert_not_called()


def test_sdk_client_uses_refresh_token_and_bounded_retries(monkeypatch) -> None:
    factory = MagicMock()
    monkeypatch.setattr(auth.dropbox, "Dropbox", factory)
    create_client(Credentials("app", "token"))
    factory.assert_called_once_with(
        app_key="app",
        oauth2_refresh_token="token",
        timeout=60,
        max_retries_on_error=3,
        max_retries_on_rate_limit=3,
    )
