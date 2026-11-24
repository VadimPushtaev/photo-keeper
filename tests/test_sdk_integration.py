"""Exercise the real SDK's OAuth, wire formats, streaming and response decoding offline."""

import base64
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from dropbox import Dropbox

from photo_keeper import auth
from photo_keeper.auth import Credentials, create_client, load_credentials
from photo_keeper.backup import run_chain

from .conftest import reference_hash


def response(request, data, *, headers=None):
    result = requests.Response()
    result.request = request
    result.status_code = 200
    result.headers["content-type"] = "application/json"
    result.headers.update(headers or {})
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    result.raw = io.BytesIO(body)
    return result


def test_real_sdk_pkce_exchange_without_an_app_secret(tmp_path, monkeypatch, capsys) -> None:
    requests_seen = []

    def send(session, request, **kwargs):
        assert request.url == "https://api.dropboxapi.com/oauth2/token"
        requests_seen.append(parse_qs(request.body))
        return response(
            request,
            {
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "account_id": "dbid:test",
                "uid": "123",
                "expires_in": 14400,
                "scope": " ".join(auth.SCOPES),
            },
        )

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "user-code")
    client = MagicMock()
    client.__enter__.return_value = client
    client.users_get_current_account.return_value = SimpleNamespace(email="test@example.org")
    monkeypatch.setattr(auth, "create_client", MagicMock(return_value=client))

    path = tmp_path / "creds.json"
    auth.login("app-key", path, open_browser=False)
    url = capsys.readouterr().out.splitlines()[1]
    query = parse_qs(urlparse(url).query)
    assert query["token_access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    assert set(query["scope"][0].split()) == set(auth.SCOPES)
    body = requests_seen[0]
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["user-code"]
    assert "client_secret" not in body
    challenge = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"][0].encode()).digest())
    assert query["code_challenge"] == [challenge.rstrip(b"=").decode()]
    assert load_credentials(path) == Credentials("app-key", "private-refresh")


def test_real_sdk_refreshes_saved_credentials_without_interaction(monkeypatch) -> None:
    requests_seen = []

    def send(session, request, **kwargs):
        requests_seen.append(request)
        if request.url.endswith("/oauth2/token"):
            form = parse_qs(request.body)
            assert form["grant_type"] == ["refresh_token"]
            assert form["refresh_token"] == ["refresh-secret"]
            assert form["client_id"] == ["app-key"]
            assert "client_secret" not in form
            return response(request, {"access_token": "refreshed", "expires_in": 14400})
        assert request.url.endswith("/2/files/list_folder")
        assert request.headers["Authorization"] == "Bearer refreshed"
        return response(request, {"entries": [], "cursor": "cursor", "has_more": False})

    monkeypatch.setattr(requests.Session, "send", send)
    with create_client(Credentials("app-key", "refresh-secret")) as client:
        assert client.files_list_folder("").entries == []
        assert client.files_list_folder("").entries == []
    assert len([req for req in requests_seen if req.url.endswith("/oauth2/token")]) == 1


@pytest.mark.parametrize("media_type", ["photo", "video"])
def test_real_sdk_lists_metadata_and_streams_verified_revision(
    chain, monkeypatch, media_type
) -> None:
    data = b"01234567" * 600_000  # Crosses Dropbox's 4 MiB hash block boundary.
    metadata = {
        ".tag": "file",
        "name": "photo.jpg",
        "id": "id:photo",
        "rev": "abcdefabcdef",
        "path_lower": "/camera uploads/photo.jpg",
        "path_display": "/Camera Uploads/photo.jpg",
        "client_modified": "2025-05-01T11:20:30Z",
        "server_modified": "2026-01-01T00:00:00Z",
        "size": len(data),
        "content_hash": reference_hash(data),
        "is_downloadable": True,
    }
    endpoints = []
    responses = []

    def send(session, request, **kwargs):
        endpoint = urlparse(request.url).path
        endpoints.append(endpoint)
        if endpoint == "/2/files/list_folder":
            body = json.loads(request.body)
            assert body["path"] == "/Camera Uploads"
            assert body["recursive"] is True
            return response(request, {"entries": [metadata], "cursor": "cursor", "has_more": False})
        if endpoint == "/2/files/get_metadata":
            body = json.loads(request.body)
            assert body["path"] == "rev:abcdefabcdef"
            assert body["include_media_info"] is True
            media = {".tag": media_type, "time_taken": "2020-12-31T23:59:59Z"}
            if media_type == "video":
                media["duration"] = 1000
            return response(
                request, {**metadata, "media_info": {".tag": "metadata", "metadata": media}}
            )
        assert endpoint == "/2/files/download"
        assert kwargs["stream"] is True
        assert json.loads(request.headers["Dropbox-API-Arg"])["path"] == "rev:abcdefabcdef"
        result = response(request, data, headers={"dropbox-api-result": json.dumps(metadata)})
        result.close = MagicMock(wraps=result.close)
        responses.append(result)
        return result

    monkeypatch.setattr(requests.Session, "send", send)
    with Dropbox("offline-test-access-token") as client:
        result = run_chain(client, chain)
        assert result.copied == 1
        assert result.failed == 0
        assert (chain.destination / "2020" / chain.subdirectory / "photo.jpg").read_bytes() == data
        responses[0].close.assert_called_once()
        assert run_chain(client, chain).unchanged == 1
    assert endpoints.count("/2/files/download") == 1
    assert all("delete" not in endpoint and "upload" not in endpoint for endpoint in endpoints)
