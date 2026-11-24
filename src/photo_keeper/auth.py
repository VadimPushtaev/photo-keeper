"""Interactive PKCE login and private, persistent credentials for cron."""

import getpass
import json
import os
import tempfile
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import dropbox
from dropbox.exceptions import DropboxException
from dropbox.oauth import BadInputException
from requests.exceptions import RequestException

from photo_keeper.errors import KeeperError

SCOPES = ["account_info.read", "files.metadata.read", "files.content.read"]


@dataclass(frozen=True)
class Credentials:
    app_key: str
    refresh_token: str = field(repr=False)


def load_credentials(path: Path) -> Credentials:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KeeperError(
            f"Cannot read credentials at {path}. Run 'photo-keeper login dropbox'."
        ) from exc
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), str) or not data[key].strip()
        for key in ("app_key", "refresh_token")
    ):
        raise KeeperError("Invalid Dropbox credentials. Run 'photo-keeper login dropbox'.")
    return Credentials(app_key=data["app_key"], refresh_token=data["refresh_token"])


def save_credentials(path: Path, credentials: Credentials) -> None:
    """Atomically replace credentials; the temporary and final file have mode 0600."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(
                {"app_key": credentials.app_key, "refresh_token": credentials.refresh_token}, file
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_client(credentials: Credentials) -> dropbox.Dropbox:
    return dropbox.Dropbox(
        app_key=credentials.app_key,
        oauth2_refresh_token=credentials.refresh_token,
        timeout=60,
        max_retries_on_error=3,
        max_retries_on_rate_limit=3,
    )


def login(app_key: str, path: Path, *, open_browser: bool = True) -> str:
    if not app_key.strip():
        raise KeeperError("A Dropbox app key is required.")
    flow = dropbox.DropboxOAuth2FlowNoRedirect(
        app_key.strip(), use_pkce=True, token_access_type="offline", scope=SCOPES, timeout=60
    )
    url = flow.start()
    print(f"Open this URL, allow access, and copy the authorization code:\n{url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass  # The printed URL also works in another browser or from an SSH session.
    try:
        code = getpass.getpass("Dropbox authorization code: ").strip()
    except EOFError as exc:
        raise KeeperError(
            "No authorization code received. Run login in an interactive terminal."
        ) from exc
    if not code:
        raise KeeperError("No authorization code received.")
    try:
        result = flow.finish(code)
        if not result.refresh_token:
            raise KeeperError("Dropbox did not return a refresh token. Please log in again.")
        credentials = Credentials(app_key.strip(), result.refresh_token)
        with create_client(credentials) as client:
            account = client.users_get_current_account()
    except (DropboxException, RequestException, BadInputException) as exc:
        # OAuth errors can contain token response bodies. Never print those bodies.
        raise KeeperError(
            f"Dropbox login failed ({type(exc).__name__}). Please try again."
        ) from exc
    save_credentials(path, credentials)
    return account.email
