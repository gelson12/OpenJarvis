"""Tests for GmailClient + gmail tools.

Mocks the shared _google_oauth helper so no real Google calls happen,
plus httpx for the per-endpoint requests. Verifies request shape,
mutating-tool confirmation gates, and the send-message MIME assembly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from openjarvis.integrations import _google_oauth
from openjarvis.integrations.gmail import (
    GmailClient,
    GmailUnavailableError,
)
from openjarvis.tools.gmail_tools import (
    GmailGetProfileTool,
    GmailListMessagesTool,
    GmailModifyMessageTool,
    GmailSendMessageTool,
    GmailTrashMessageTool,
)


@pytest.fixture(autouse=True)
def _reset_oauth_cache():
    _google_oauth.reset_cache()
    yield
    _google_oauth.reset_cache()


def _resp(json_payload, status_code: int = 200) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.content = b"{}"
    r.json.return_value = json_payload
    r.raise_for_status = MagicMock()
    return r


def _patch_token(monkeypatch, token: str = "ACCESS"):
    """Stub get_access_token() so we don't need to mock Google's token endpoint."""
    monkeypatch.setattr(
        "openjarvis.integrations.gmail.get_access_token",
        lambda **_: token,
    )
    # configured() reads is_configured(); make it report True.
    monkeypatch.setattr(
        "openjarvis.integrations.gmail.is_configured",
        lambda: True,
    )


# ---------------------------------------------------------------------------
# Configured-ness
# ---------------------------------------------------------------------------


def test_unconfigured_when_creds_missing(monkeypatch):
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert GmailClient().configured is False


def test_configured_when_all_creds_set(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "rt")
    assert GmailClient().configured is True


def test_request_raises_when_unconfigured(monkeypatch):
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(GmailUnavailableError):
        GmailClient().get_profile()


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_list_messages_passes_q_and_label_filters(monkeypatch):
    _patch_token(monkeypatch)
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.request.return_value = _resp({"messages": []})
    with patch("httpx.Client", return_value=fake):
        GmailClient().list_messages(
            q="is:unread newer_than:1d",
            label_ids=["INBOX"],
            max_results=10,
        )
    params = fake.request.call_args.kwargs["params"]
    assert params["q"] == "is:unread newer_than:1d"
    assert params["labelIds"] == ["INBOX"]
    assert params["maxResults"] == 10
    assert params["includeSpamTrash"] == "false"


def test_get_message_passes_format(monkeypatch):
    _patch_token(monkeypatch)
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.request.return_value = _resp({"id": "m1"})
    with patch("httpx.Client", return_value=fake):
        GmailClient().get_message("m1", format="metadata")
    assert fake.request.call_args.kwargs["params"]["format"] == "metadata"


def test_modify_message_builds_label_diff(monkeypatch):
    _patch_token(monkeypatch)
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.request.return_value = _resp({"id": "m1"})
    with patch("httpx.Client", return_value=fake):
        GmailClient().modify_message(
            "m1", add_label_ids=["STARRED"], remove_label_ids=["UNREAD"],
        )
    body = fake.request.call_args.kwargs["json"]
    assert body == {"addLabelIds": ["STARRED"], "removeLabelIds": ["UNREAD"]}


def test_send_message_builds_rfc822_with_required_headers(monkeypatch):
    _patch_token(monkeypatch)
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.request.return_value = _resp({"id": "sent_1"})
    with patch("httpx.Client", return_value=fake):
        GmailClient().send_message(
            to="alice@example.com",
            subject="Lunch?",
            body="Tomorrow at 1?",
        )
    body = fake.request.call_args.kwargs["json"]
    # raw is base64url-encoded RFC 822
    import base64

    raw_decoded = base64.urlsafe_b64decode(body["raw"]).decode()
    assert "To: alice@example.com" in raw_decoded
    assert "Subject: Lunch?" in raw_decoded
    assert "Tomorrow at 1?" in raw_decoded


def test_send_message_threads_when_thread_id_given(monkeypatch):
    _patch_token(monkeypatch)
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.request.return_value = _resp({"id": "sent_2"})
    with patch("httpx.Client", return_value=fake):
        GmailClient().send_message(
            to="b@x.com", subject="re", body="ok", thread_id="t-1",
        )
    body = fake.request.call_args.kwargs["json"]
    assert body["threadId"] == "t-1"


def test_send_message_html_sets_content_type(monkeypatch):
    _patch_token(monkeypatch)
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.request.return_value = _resp({"id": "sent_3"})
    with patch("httpx.Client", return_value=fake):
        GmailClient().send_message(
            to="c@x.com",
            subject="Newsletter",
            body="<p>Hello!</p>",
            html=True,
        )
    body = fake.request.call_args.kwargs["json"]
    import base64

    raw = base64.urlsafe_b64decode(body["raw"]).decode()
    assert "Content-Type: text/html" in raw


# ---------------------------------------------------------------------------
# Tools — surface
# ---------------------------------------------------------------------------


def test_get_profile_tool_returns_success():
    fake_client = MagicMock(spec=GmailClient)
    fake_client.get_profile.return_value = {
        "emailAddress": "user@example.com", "messagesTotal": 1234,
    }
    out = GmailGetProfileTool(client=fake_client).execute()
    assert out.success is True
    assert "user@example.com" in out.content


def test_list_messages_tool_passes_query():
    fake_client = MagicMock(spec=GmailClient)
    fake_client.list_messages.return_value = {"messages": []}
    GmailListMessagesTool(client=fake_client).execute(
        q="is:unread", max_results=5,
    )
    fake_client.list_messages.assert_called_once_with(
        q="is:unread",
        label_ids=None,
        max_results=5,
        include_spam_trash=False,
    )


def test_mutating_tools_require_confirmation():
    fake_client = MagicMock(spec=GmailClient)
    for tool_cls in (
        GmailModifyMessageTool,
        GmailTrashMessageTool,
        GmailSendMessageTool,
    ):
        spec = tool_cls(client=fake_client).spec
        assert spec.requires_confirmation is True, (
            f"{tool_cls.__name__} must require confirmation"
        )


def test_tool_surfaces_unavailable_as_error():
    fake_client = MagicMock(spec=GmailClient)
    fake_client.get_profile.side_effect = GmailUnavailableError("no creds")
    out = GmailGetProfileTool(client=fake_client).execute()
    assert out.success is False
    assert "gmail error" in out.content
