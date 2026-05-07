"""Tests for OutlookClient + outlook tools.

Mocks httpx so no real Microsoft Graph calls happen; verifies OAuth
refresh flow, env-var alias resolution (the user's Railway entries
have spaces), request shape, and the mutating-tool confirmation gates.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from openjarvis.integrations.outlook import (
    OutlookClient,
    OutlookUnavailableError,
)
from openjarvis.tools.outlook_tools import (
    OutlookDeleteMessageTool,
    OutlookGetProfileTool,
    OutlookListMessagesTool,
    OutlookMoveMessageTool,
    OutlookReplyToMessageTool,
    OutlookSendMessageTool,
    OutlookUpdateMessageTool,
)


def _resp(json_payload, status_code: int = 200, content: bytes = b"{}") -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.content = content
    r.json.return_value = json_payload
    r.raise_for_status = MagicMock()
    return r


def _patch_client(post_payload=None, request_payload=None, request_status=200, request_content=b"{}"):
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    if post_payload is not None:
        fake.post.return_value = _resp(post_payload)
    if request_payload is not None:
        fake.request.return_value = _resp(
            request_payload, status_code=request_status, content=request_content,
        )
    return patch("httpx.Client", return_value=fake), fake


# ---------------------------------------------------------------------------
# Configured-ness with env aliases
# ---------------------------------------------------------------------------


def test_unconfigured_when_creds_missing(monkeypatch):
    for k in (
        "OUTLOOK_CLIENT_ID", "OUTLOOK_Client_ID", "OUTLOOK_Client ID",
        "OUTLOOK_CLIENT_SECRET", "OUTLOOK_Client_Secret",
        "OUTLOOK_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)
    assert OutlookClient().configured is False


def test_configured_via_canonical_names(monkeypatch):
    monkeypatch.setenv("OUTLOOK_CLIENT_ID", "id")
    monkeypatch.setenv("OUTLOOK_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OUTLOOK_REFRESH_TOKEN", "rt")
    assert OutlookClient().configured is True


def test_configured_via_space_separated_aliases(monkeypatch):
    """The user's Railway entries are 'OUTLOOK_Client ID' etc. — read
    via aliases since canonical names aren't set."""
    for k in ("OUTLOOK_CLIENT_ID", "OUTLOOK_CLIENT_SECRET", "OUTLOOK_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OUTLOOK_Client ID", "id-from-railway")
    monkeypatch.setenv("OUTLOOK_Client_Secret", "secret-from-railway")
    monkeypatch.setenv("OUTLOOK_REFRESH_TOKEN", "rt")  # this one's already canonical
    assert OutlookClient().configured is True


def test_token_url_uses_microsoft_default_when_unset(monkeypatch):
    for k in ("OUTLOOK_TOKEN_URL", "OUTLOOK_Access_Token_URL", "OUTLOOK_Access Token URL"):
        monkeypatch.delenv(k, raising=False)
    client = OutlookClient(
        client_id="id", client_secret="s", refresh_token="rt",
    )
    assert "login.microsoftonline.com" in client._token_url


def test_token_url_picks_up_railway_alias(monkeypatch):
    for k in ("OUTLOOK_TOKEN_URL", "OUTLOOK_Access_Token_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OUTLOOK_Access Token URL", "https://example.com/token")
    client = OutlookClient(
        client_id="id", client_secret="s", refresh_token="rt",
    )
    assert client._token_url == "https://example.com/token"


def test_request_raises_when_unconfigured(monkeypatch):
    for k in ("OUTLOOK_CLIENT_ID", "OUTLOOK_Client_ID", "OUTLOOK_Client ID",
              "OUTLOOK_CLIENT_SECRET", "OUTLOOK_Client_Secret",
              "OUTLOOK_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(OutlookUnavailableError):
        OutlookClient().get_profile()


# ---------------------------------------------------------------------------
# OAuth refresh caching
# ---------------------------------------------------------------------------


def test_token_cached_within_ttl():
    client = OutlookClient(client_id="id", client_secret="s", refresh_token="rt")
    p, fake = _patch_client(post_payload={"access_token": "AAA", "expires_in": 3600})
    with p:
        a = client._bearer()
        b = client._bearer()
    assert a == b == "AAA"
    assert fake.post.call_count == 1


def test_token_refetched_after_expiry():
    client = OutlookClient(client_id="id", client_secret="s", refresh_token="rt")
    p, fake = _patch_client(post_payload={"access_token": "AAA", "expires_in": 1})
    with p:
        client._bearer()
        client._access_expires_at = time.time() - 10
        client._bearer()
    assert fake.post.call_count == 2


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def _client_with_token():
    c = OutlookClient(client_id="id", client_secret="s", refresh_token="rt")
    c._access_token = "T"
    c._access_expires_at = time.time() + 3600
    return c


def test_list_messages_passes_search_and_filter():
    client = _client_with_token()
    p, fake = _patch_client(request_payload={"value": []})
    with p:
        client.list_messages(
            search="invoice",
            filter_="isRead eq false",
            top=10,
            select=["subject", "from"],
        )
    params = fake.request.call_args.kwargs["params"]
    assert params["$search"] == '"invoice"'
    assert params["$filter"] == "isRead eq false"
    assert params["$top"] == 10
    assert params["$select"] == "subject,from"


def test_send_message_builds_graph_payload():
    client = _client_with_token()
    p, fake = _patch_client(
        request_payload=None, request_status=202, request_content=b"",
    )
    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.status_code = 202
    fake_resp.content = b""
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = None
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.request.return_value = fake_resp
    with patch("httpx.Client", return_value=fake):
        client.send_message(
            to="alice@example.com,bob@example.com",
            subject="Hello",
            body="Hi there",
            cc="cc@example.com",
            html=False,
        )
    body = fake.request.call_args.kwargs["json"]
    assert body["message"]["subject"] == "Hello"
    assert body["message"]["body"]["contentType"] == "Text"
    assert body["message"]["toRecipients"] == [
        {"emailAddress": {"address": "alice@example.com"}},
        {"emailAddress": {"address": "bob@example.com"}},
    ]
    assert body["message"]["ccRecipients"] == [
        {"emailAddress": {"address": "cc@example.com"}},
    ]
    assert body["saveToSentItems"] is True


def test_update_message_builds_patch_body():
    client = _client_with_token()
    p, fake = _patch_client(request_payload={"id": "m1"})
    with p:
        client.update_message("m1", is_read=True, flag="flagged")
    body = fake.request.call_args.kwargs["json"]
    assert body == {"isRead": True, "flag": {"flagStatus": "flagged"}}


def test_move_message_passes_destination():
    client = _client_with_token()
    p, fake = _patch_client(request_payload={"id": "m1"})
    with p:
        client.move_message("m1", destination_id="archive")
    body = fake.request.call_args.kwargs["json"]
    assert body == {"destinationId": "archive"}


# ---------------------------------------------------------------------------
# Tools — surface
# ---------------------------------------------------------------------------


def test_get_profile_tool_returns_success():
    fake = MagicMock(spec=OutlookClient)
    fake.get_profile.return_value = {"mail": "user@example.com"}
    out = OutlookGetProfileTool(client=fake).execute()
    assert out.success is True
    assert "user@example.com" in out.content


def test_list_messages_tool_passthrough():
    fake = MagicMock(spec=OutlookClient)
    fake.list_messages.return_value = {"value": []}
    OutlookListMessagesTool(client=fake).execute(
        search="invoice", top=5,
    )
    fake.list_messages.assert_called_once_with(
        folder_id=None,
        search="invoice",
        filter_=None,
        top=5,
        select=None,
    )


def test_mutating_tools_require_confirmation():
    fake = MagicMock(spec=OutlookClient)
    for tool_cls in (
        OutlookUpdateMessageTool,
        OutlookMoveMessageTool,
        OutlookDeleteMessageTool,
        OutlookSendMessageTool,
        OutlookReplyToMessageTool,
    ):
        spec = tool_cls(client=fake).spec
        assert spec.requires_confirmation is True, (
            f"{tool_cls.__name__} must require confirmation"
        )


def test_tool_surfaces_unavailable_as_error():
    fake = MagicMock(spec=OutlookClient)
    fake.get_profile.side_effect = OutlookUnavailableError("no creds")
    out = OutlookGetProfileTool(client=fake).execute()
    assert out.success is False
    assert "outlook error" in out.content
