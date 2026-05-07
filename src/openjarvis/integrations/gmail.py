"""Gmail REST API v1 client (read + send + label + modify).

Auth: same OAuth refresh-token flow as Calendar — shares
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN. The
user does the OAuth dance once with both ``calendar`` and
``gmail.modify`` scopes; the resulting refresh token works for both
connectors.

Why not the official google-api-python-client SDK
-------------------------------------------------
Same justification as Calendar — the SDK adds ~30 MB of transitive
deps (google-auth, googleapiclient, oauth2client, httplib2, protobuf)
for endpoints we hand-roll in ~250 lines. Mirrors the n8n / stripe /
paypal / calendar pattern.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from typing import Any, Optional

import httpx

from openjarvis.integrations._google_oauth import (
    GoogleOAuthError,
    get_access_token,
    is_configured,
)

logger = logging.getLogger(__name__)

_GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"


class GmailUnavailableError(RuntimeError):
    """Raised when credentials are missing or an API call fails."""


class GmailClient:
    """Synchronous httpx-based client for the Gmail REST API v1.

    All endpoints scoped to ``users/me`` — i.e. the OAuth-authorized
    account. Multi-account support would need a per-call user override,
    which we don't need yet.
    """

    def __init__(self, *, timeout: float = 15.0) -> None:
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return is_configured()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        try:
            token = get_access_token(timeout=self._timeout)
        except GoogleOAuthError as exc:
            raise GmailUnavailableError(str(exc)) from exc
        url = f"{_GMAIL_API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
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
            raise GmailUnavailableError(
                f"Gmail {method} {path} failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Profile + labels
    # ------------------------------------------------------------------

    def get_profile(self) -> Any:
        """Return the authorized user's email + thread/message totals."""
        return self._request("GET", "/users/me/profile")

    def list_labels(self) -> Any:
        return self._request("GET", "/users/me/labels")

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def list_messages(
        self,
        *,
        q: Optional[str] = None,
        label_ids: Optional[list[str]] = None,
        max_results: int = 25,
        page_token: Optional[str] = None,
        include_spam_trash: bool = False,
    ) -> Any:
        """List message ids matching ``q`` (Gmail search syntax — same as
        the search bar: ``is:unread``, ``from:foo@bar.com``, ``newer_than:1d``).
        Use list_messages then get_message for actual content."""
        params: dict[str, Any] = {
            "maxResults": min(max_results, 100),
            "includeSpamTrash": "true" if include_spam_trash else "false",
        }
        if q:
            params["q"] = q
        if label_ids:
            params["labelIds"] = label_ids
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", "/users/me/messages", params=params)

    def get_message(
        self,
        message_id: str,
        *,
        format: str = "full",
    ) -> Any:
        """Fetch a single message. ``format`` = full / metadata /
        minimal / raw. Default 'full' includes body + headers."""
        return self._request(
            "GET",
            f"/users/me/messages/{message_id}",
            params={"format": format},
        )

    def modify_message(
        self,
        message_id: str,
        *,
        add_label_ids: Optional[list[str]] = None,
        remove_label_ids: Optional[list[str]] = None,
    ) -> Any:
        """Add / remove labels (e.g. mark read by removing UNREAD,
        archive by removing INBOX, star by adding STARRED)."""
        body: dict[str, Any] = {}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids
        return self._request(
            "POST",
            f"/users/me/messages/{message_id}/modify",
            json_body=body or None,
        )

    def trash_message(self, message_id: str) -> Any:
        """Move a message to trash (recoverable for 30 days)."""
        return self._request(
            "POST", f"/users/me/messages/{message_id}/trash",
        )

    def send_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        html: bool = False,
    ) -> Any:
        """Send an email via Gmail. ``body`` is plain text by default;
        set ``html=True`` to send an HTML body. ``thread_id`` keeps the
        message in an existing conversation."""
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        if html:
            msg.set_content(body, subtype="html")
        else:
            msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        return self._request(
            "POST", "/users/me/messages/send", json_body=payload,
        )

    # ------------------------------------------------------------------
    # Threads (read-only — useful for "show me the conversation")
    # ------------------------------------------------------------------

    def get_thread(self, thread_id: str) -> Any:
        return self._request("GET", f"/users/me/threads/{thread_id}")


_default: Optional[GmailClient] = None


def get_default_client() -> GmailClient:
    global _default
    if _default is None:
        _default = GmailClient()
    return _default


__all__ = [
    "GmailClient",
    "GmailUnavailableError",
    "get_default_client",
]
