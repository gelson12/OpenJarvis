"""Google Calendar REST API v3 client.

Auth: OAuth2 refresh-token flow. The user runs a one-time browser OAuth
once on a machine they trust (Google Cloud Console → OAuth client →
"Web application" or "Desktop app", scope ``calendar``), captures the
refresh_token, and sets it on the server alongside client id/secret:

  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  GOOGLE_REFRESH_TOKEN          (the long-lived token from the OAuth flow)

This module exchanges the refresh_token for short-lived access tokens
on demand (cached in process memory until 60s before expiry — same
shape as the PayPal client).

Why a thin httpx wrapper instead of google-api-python-client
------------------------------------------------------------
The Calendar surface we use (events list/get/create/update/delete,
freebusy, calendarList) is a small, stable subset. The official SDK
adds googleapiclient + oauth2client + httplib2 + protobuf — ~30 MB of
deps for endpoints we hand-roll in 200 lines. Same justification as
the n8n / stripe / paypal connectors.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_GCAL_API_BASE = "https://www.googleapis.com/calendar/v3"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleCalendarUnavailableError(RuntimeError):
    """Raised when credentials are missing or an API call fails."""


class GoogleCalendarClient:
    """Synchronous httpx-based client for the Calendar REST API v3."""

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self._client_id = client_id or os.environ.get("GOOGLE_CLIENT_ID", "")
        self._client_secret = (
            client_secret or os.environ.get("GOOGLE_CLIENT_SECRET", "")
        )
        self._refresh_token = (
            refresh_token or os.environ.get("GOOGLE_REFRESH_TOKEN", "")
        )
        self._timeout = timeout
        self._access_token: Optional[str] = None
        self._access_expires_at: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(
            self._client_id and self._client_secret and self._refresh_token
        )

    # ------------------------------------------------------------------
    # OAuth refresh + token caching
    # ------------------------------------------------------------------

    def _fetch_access_token(self) -> str:
        if not self.configured:
            raise GoogleCalendarUnavailableError(
                "Google Calendar not configured — set GOOGLE_CLIENT_ID, "
                "GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN"
            )
        try:
            with httpx.Client(timeout=self._timeout) as c:
                resp = c.post(
                    _GOOGLE_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise GoogleCalendarUnavailableError(
                f"Google token refresh failed: {exc}"
            ) from exc

        token = payload.get("access_token", "")
        if not token:
            raise GoogleCalendarUnavailableError(
                f"Google token response missing access_token: {payload}"
            )
        ttl = int(payload.get("expires_in", 3600))
        self._access_token = token
        self._access_expires_at = time.time() + max(0, ttl - 60)
        return token

    def _bearer(self) -> str:
        if self._access_token and time.time() < self._access_expires_at:
            return self._access_token
        return self._fetch_access_token()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        url = f"{_GCAL_API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self._bearer()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self._timeout) as c:
                resp = c.request(
                    method, url, headers=headers, params=params, json=json_body,
                )
                resp.raise_for_status()
                return resp.json() if resp.content else None
        except httpx.HTTPError as exc:
            raise GoogleCalendarUnavailableError(
                f"Google Calendar {method} {path} failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Calendars
    # ------------------------------------------------------------------

    def list_calendars(self) -> Any:
        """List calendars on the authorized account."""
        return self._request("GET", "/users/me/calendarList")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def list_events(
        self,
        *,
        calendar_id: str = "primary",
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        max_results: int = 50,
        single_events: bool = True,
        order_by: str = "startTime",
        q: Optional[str] = None,
    ) -> Any:
        """List events in ``calendar_id`` with optional time bounds.

        ``time_min`` / ``time_max`` are RFC3339 strings (e.g.
        ``"2026-05-08T00:00:00Z"``). When omitted, defaults to "now"
        for time_min so we don't dump the full event archive.
        """
        if not time_min:
            time_min = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        params: dict[str, Any] = {
            "timeMin": time_min,
            "maxResults": min(max_results, 250),
            "singleEvents": "true" if single_events else "false",
            "orderBy": order_by if single_events else "updated",
        }
        if time_max:
            params["timeMax"] = time_max
        if q:
            params["q"] = q
        return self._request(
            "GET", f"/calendars/{calendar_id}/events", params=params,
        )

    def get_event(
        self, event_id: str, *, calendar_id: str = "primary",
    ) -> Any:
        return self._request(
            "GET", f"/calendars/{calendar_id}/events/{event_id}",
        )

    def create_event(
        self,
        *,
        summary: str,
        start: dict[str, Any],
        end: dict[str, Any],
        calendar_id: str = "primary",
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[list[dict[str, Any]]] = None,
        send_updates: str = "none",
    ) -> Any:
        """Create an event. ``start`` / ``end`` are dicts of the shape
        ``{"dateTime": "2026-05-08T10:00:00-04:00", "timeZone": "America/New_York"}``
        for timed events, or ``{"date": "2026-05-08"}`` for all-day events."""
        body: dict[str, Any] = {"summary": summary, "start": start, "end": end}
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = attendees
        return self._request(
            "POST",
            f"/calendars/{calendar_id}/events",
            params={"sendUpdates": send_updates},
            json_body=body,
        )

    def update_event(
        self,
        event_id: str,
        patch: dict[str, Any],
        *,
        calendar_id: str = "primary",
        send_updates: str = "none",
    ) -> Any:
        """PATCH an event. ``patch`` is a partial event resource."""
        return self._request(
            "PATCH",
            f"/calendars/{calendar_id}/events/{event_id}",
            params={"sendUpdates": send_updates},
            json_body=patch,
        )

    def delete_event(
        self,
        event_id: str,
        *,
        calendar_id: str = "primary",
        send_updates: str = "none",
    ) -> Any:
        return self._request(
            "DELETE",
            f"/calendars/{calendar_id}/events/{event_id}",
            params={"sendUpdates": send_updates},
        )

    # ------------------------------------------------------------------
    # Free/busy
    # ------------------------------------------------------------------

    def freebusy_query(
        self,
        *,
        time_min: str,
        time_max: str,
        calendar_ids: Optional[list[str]] = None,
    ) -> Any:
        """Free/busy lookup across one or more calendars.

        Pass full RFC3339 ``time_min`` / ``time_max``. ``calendar_ids``
        defaults to ``["primary"]``.
        """
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": cid} for cid in (calendar_ids or ["primary"])],
        }
        return self._request("POST", "/freeBusy", json_body=body)


_default: Optional[GoogleCalendarClient] = None


def get_default_client() -> GoogleCalendarClient:
    global _default
    if _default is None:
        _default = GoogleCalendarClient()
    return _default


__all__ = [
    "GoogleCalendarClient",
    "GoogleCalendarUnavailableError",
    "get_default_client",
]
