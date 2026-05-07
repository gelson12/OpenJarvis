"""Tests for GoogleCalendarClient + calendar tools.

Mocks httpx so no real Google calls are made; exercises OAuth refresh
caching, request shape, and the tool surface.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from openjarvis.integrations.google_calendar import (
    GoogleCalendarClient,
    GoogleCalendarUnavailableError,
)
from openjarvis.tools.calendar_tools import (
    CalendarCreateEventTool,
    CalendarDeleteEventTool,
    CalendarFreeBusyTool,
    CalendarListEventsTool,
    CalendarUpdateEventTool,
)


def _resp(json_payload, status_code: int = 200) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.content = b"{}"
    r.json.return_value = json_payload
    r.raise_for_status = MagicMock()
    return r


def _patch_client(post_payload=None, request_payload=None):
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    if post_payload is not None:
        fake.post.return_value = _resp(post_payload)
    if request_payload is not None:
        fake.request.return_value = _resp(request_payload)
    return patch("httpx.Client", return_value=fake), fake


# ---------------------------------------------------------------------------
# Configured-ness
# ---------------------------------------------------------------------------


def test_unconfigured_when_creds_missing(monkeypatch):
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert GoogleCalendarClient().configured is False


def test_configured_when_all_creds_set(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "rt")
    assert GoogleCalendarClient().configured is True


def test_request_raises_when_unconfigured(monkeypatch):
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(GoogleCalendarUnavailableError):
        GoogleCalendarClient().list_calendars()


# ---------------------------------------------------------------------------
# OAuth refresh caching
# ---------------------------------------------------------------------------


def test_token_cached_within_ttl():
    client = GoogleCalendarClient(
        client_id="id", client_secret="secret", refresh_token="rt",
    )
    p, fake = _patch_client(post_payload={"access_token": "AAA", "expires_in": 3600})
    with p:
        a = client._bearer()
        b = client._bearer()
    assert a == b == "AAA"
    assert fake.post.call_count == 1


def test_token_refetched_after_expiry():
    client = GoogleCalendarClient(
        client_id="id", client_secret="secret", refresh_token="rt",
    )
    p, fake = _patch_client(post_payload={"access_token": "AAA", "expires_in": 1})
    with p:
        client._bearer()
        client._access_expires_at = time.time() - 10
        client._bearer()
    assert fake.post.call_count == 2


def test_token_response_missing_access_token_raises():
    client = GoogleCalendarClient(
        client_id="id", client_secret="secret", refresh_token="rt",
    )
    p, fake = _patch_client(post_payload={"oops": "no token"})
    with p:
        with pytest.raises(GoogleCalendarUnavailableError):
            client._fetch_access_token()


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def _client_with_token():
    c = GoogleCalendarClient(
        client_id="id", client_secret="secret", refresh_token="rt",
    )
    c._access_token = "T"
    c._access_expires_at = time.time() + 3600
    return c


def test_list_events_default_time_min_is_now():
    client = _client_with_token()
    p, fake = _patch_client(request_payload={"items": []})
    with p:
        client.list_events()
    params = fake.request.call_args.kwargs["params"]
    # timeMin should be set automatically.
    assert "timeMin" in params
    assert params["singleEvents"] == "true"
    assert params["orderBy"] == "startTime"


def test_list_events_passes_q_when_given():
    client = _client_with_token()
    p, fake = _patch_client(request_payload={"items": []})
    with p:
        client.list_events(q="standup", max_results=10)
    params = fake.request.call_args.kwargs["params"]
    assert params["q"] == "standup"
    assert params["maxResults"] == 10


def test_create_event_includes_optional_fields():
    client = _client_with_token()
    p, fake = _patch_client(request_payload={"id": "evt_1"})
    with p:
        client.create_event(
            summary="Lunch",
            start={"dateTime": "2026-05-08T12:00:00-04:00"},
            end={"dateTime": "2026-05-08T13:00:00-04:00"},
            location="Le Bernardin",
            attendees=[{"email": "sarah@example.com"}],
        )
    body = fake.request.call_args.kwargs["json"]
    assert body["summary"] == "Lunch"
    assert body["location"] == "Le Bernardin"
    assert body["attendees"] == [{"email": "sarah@example.com"}]
    # send_updates default = none, should appear in params
    assert fake.request.call_args.kwargs["params"]["sendUpdates"] == "none"


def test_freebusy_query_default_calendars_is_primary():
    client = _client_with_token()
    p, fake = _patch_client(request_payload={"calendars": {}})
    with p:
        client.freebusy_query(
            time_min="2026-05-08T00:00:00Z",
            time_max="2026-05-08T23:59:59Z",
        )
    body = fake.request.call_args.kwargs["json"]
    assert body["items"] == [{"id": "primary"}]


def test_delete_event_returns_none():
    client = _client_with_token()
    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.status_code = 204
    fake_resp.content = b""
    fake_resp.json.return_value = None
    fake_resp.raise_for_status = MagicMock()

    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.request.return_value = fake_resp

    with patch("httpx.Client", return_value=fake):
        result = client.delete_event("evt_1")
    assert result is None


# ---------------------------------------------------------------------------
# Tools — surface
# ---------------------------------------------------------------------------


def test_list_events_tool_returns_success(monkeypatch):
    fake_client = MagicMock(spec=GoogleCalendarClient)
    fake_client.list_events.return_value = {"items": [{"summary": "lunch"}]}
    out = CalendarListEventsTool(client=fake_client).execute(time_min="2026-05-08T00:00:00Z")
    assert out.success is True
    assert "lunch" in out.content


def test_freebusy_tool_passes_required_args(monkeypatch):
    fake_client = MagicMock(spec=GoogleCalendarClient)
    fake_client.freebusy_query.return_value = {"calendars": {}}
    out = CalendarFreeBusyTool(client=fake_client).execute(
        time_min="2026-05-08T00:00:00Z",
        time_max="2026-05-08T23:59:59Z",
    )
    assert out.success is True
    fake_client.freebusy_query.assert_called_once_with(
        time_min="2026-05-08T00:00:00Z",
        time_max="2026-05-08T23:59:59Z",
        calendar_ids=None,
    )


def test_mutating_tools_require_confirmation():
    """create / update / delete must carry requires_confirmation=True so
    the agent's confirmation gate fires before they touch real schedules."""
    fake_client = MagicMock(spec=GoogleCalendarClient)
    for tool_cls in (
        CalendarCreateEventTool,
        CalendarUpdateEventTool,
        CalendarDeleteEventTool,
    ):
        spec = tool_cls(client=fake_client).spec
        assert spec.requires_confirmation is True, (
            f"{tool_cls.__name__} must require confirmation"
        )


def test_tool_surfaces_unavailable_as_error():
    fake_client = MagicMock(spec=GoogleCalendarClient)
    fake_client.list_events.side_effect = GoogleCalendarUnavailableError("no key")
    out = CalendarListEventsTool(client=fake_client).execute()
    assert out.success is False
    assert "calendar error" in out.content
