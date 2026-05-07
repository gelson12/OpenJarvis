"""Microsoft Graph (Outlook / Microsoft 365 mail) REST API client.

Auth: OAuth2 refresh-token flow against Microsoft identity platform.
Same shape as the Gmail / Google connectors but pointed at the Microsoft
endpoints. Env vars (canonical names; aliases handled in env.py for the
space-separated Railway variants the user already created):

  OUTLOOK_CLIENT_ID
  OUTLOOK_CLIENT_SECRET
  OUTLOOK_REFRESH_TOKEN          (long-lived; from a one-time OAuth flow)
  OUTLOOK_TOKEN_URL              (optional — defaults to Microsoft's common endpoint)
  OUTLOOK_AUTH_URL               (optional — only used by the helper /auth-url
                                  endpoint that prints a URL to start the OAuth flow)

Why a thin httpx wrapper instead of msgraph-sdk-python
------------------------------------------------------
Same reason as Gmail / Calendar: the official SDK adds ~25 MB of deps
and complex async-only request shapes for endpoints we hand-roll in
~250 lines. Mirrors the n8n / stripe / paypal / gmail pattern.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from email.message import EmailMessage
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
_DEFAULT_TOKEN_URL = (
    "https://login.microsoftonline.com/common/oauth2/v2.0/token"
)


class OutlookUnavailableError(RuntimeError):
    """Raised when credentials are missing or an API call fails."""


def _env(*aliases: str, default: str = "") -> str:
    """Read the first non-empty env var across alias names. Mirrors the
    pattern used elsewhere in the codebase for tolerating Railway's
    inconsistent env-var casing / spacing."""
    for name in aliases:
        v = os.environ.get(name)
        if v:
            return v
    return default


class OutlookClient:
    """Synchronous httpx-based client for Microsoft Graph mail endpoints."""

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
        token_url: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self._client_id = client_id or _env(
            "OUTLOOK_CLIENT_ID",
            "OUTLOOK_Client_ID",
            "OUTLOOK_Client ID",
        )
        self._client_secret = client_secret or _env(
            "OUTLOOK_CLIENT_SECRET",
            "OUTLOOK_Client_Secret",
        )
        self._refresh_token = refresh_token or _env(
            "OUTLOOK_REFRESH_TOKEN",
            "OUTLOOK_Refresh_Token",
            "OUTLOOK_Refresh Token",
        )
        self._token_url = (
            token_url
            or _env(
                "OUTLOOK_TOKEN_URL",
                "OUTLOOK_Access_Token_URL",
                "OUTLOOK_Access Token URL",
            )
            or _DEFAULT_TOKEN_URL
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
            raise OutlookUnavailableError(
                "Outlook not configured — set OUTLOOK_CLIENT_ID, "
                "OUTLOOK_CLIENT_SECRET, OUTLOOK_REFRESH_TOKEN"
            )
        try:
            with httpx.Client(timeout=self._timeout) as c:
                resp = c.post(
                    self._token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise OutlookUnavailableError(
                f"Outlook token refresh failed: {exc}"
            ) from exc

        token = payload.get("access_token", "")
        if not token:
            raise OutlookUnavailableError(
                f"Outlook token response missing access_token: {payload}"
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
        url = f"{_GRAPH_API_BASE}{path}"
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
                # 204 No Content for some mutations — return None for those.
                return resp.json() if resp.content else None
        except httpx.HTTPError as exc:
            raise OutlookUnavailableError(
                f"Outlook {method} {path} failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Profile + folders
    # ------------------------------------------------------------------

    def get_profile(self) -> Any:
        """Authorized user — email, display name, id."""
        return self._request("GET", "/me")

    def list_folders(self) -> Any:
        return self._request("GET", "/me/mailFolders")

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def list_messages(
        self,
        *,
        folder_id: Optional[str] = None,
        search: Optional[str] = None,
        filter_: Optional[str] = None,
        top: int = 25,
        select: Optional[list[str]] = None,
    ) -> Any:
        """List messages. Use Microsoft Graph's $search (KQL: 'subject:invoice')
        OR $filter (OData: "isRead eq false and receivedDateTime ge 2026-05-01T00:00:00Z").
        ``folder_id`` defaults to the inbox if omitted."""
        path = (
            f"/me/mailFolders/{folder_id}/messages"
            if folder_id
            else "/me/messages"
        )
        params: dict[str, Any] = {"$top": min(top, 100)}
        if search:
            params["$search"] = f'"{search}"'
        if filter_:
            params["$filter"] = filter_
        if select:
            params["$select"] = ",".join(select)
        return self._request("GET", path, params=params)

    def get_message(
        self, message_id: str, *, include_attachments: bool = False,
    ) -> Any:
        if include_attachments:
            return self._request(
                "GET", f"/me/messages/{message_id}",
                params={"$expand": "attachments"},
            )
        return self._request("GET", f"/me/messages/{message_id}")

    def update_message(
        self,
        message_id: str,
        *,
        is_read: Optional[bool] = None,
        flag: Optional[str] = None,
    ) -> Any:
        """PATCH a message. Set is_read=True to mark read.
        flag='flagged' / 'complete' / 'notFlagged' to flag/unflag."""
        body: dict[str, Any] = {}
        if is_read is not None:
            body["isRead"] = is_read
        if flag is not None:
            body["flag"] = {"flagStatus": flag}
        return self._request(
            "PATCH", f"/me/messages/{message_id}", json_body=body or None,
        )

    def move_message(self, message_id: str, *, destination_id: str) -> Any:
        """Move a message to another folder (e.g. archive, trash)."""
        return self._request(
            "POST",
            f"/me/messages/{message_id}/move",
            json_body={"destinationId": destination_id},
        )

    def delete_message(self, message_id: str) -> Any:
        """Permanently delete a message (use move to 'deleteditems' for soft-delete)."""
        return self._request("DELETE", f"/me/messages/{message_id}")

    def send_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        html: bool = False,
        save_to_sent_items: bool = True,
    ) -> Any:
        """Send an email. Microsoft Graph's POST /me/sendMail returns 202
        Accepted on success (no JSON body)."""
        def _recip(addr: str) -> dict[str, Any]:
            return {"emailAddress": {"address": addr.strip()}}

        def _split(s: str) -> list[dict[str, Any]]:
            return [_recip(a) for a in s.split(",") if a.strip()]

        message: dict[str, Any] = {
            "subject": subject,
            "body": {
                "contentType": "HTML" if html else "Text",
                "content": body,
            },
            "toRecipients": _split(to),
        }
        if cc:
            message["ccRecipients"] = _split(cc)
        if bcc:
            message["bccRecipients"] = _split(bcc)
        return self._request(
            "POST",
            "/me/sendMail",
            json_body={
                "message": message,
                "saveToSentItems": save_to_sent_items,
            },
        )

    def reply_to_message(
        self,
        message_id: str,
        *,
        body: str,
        reply_all: bool = False,
        html: bool = False,
    ) -> Any:
        """Reply to a message in-thread."""
        path = (
            f"/me/messages/{message_id}/replyAll"
            if reply_all
            else f"/me/messages/{message_id}/reply"
        )
        return self._request(
            "POST",
            path,
            json_body={
                "comment": body
                if not html
                else f"<p>{body}</p>",  # Graph's reply.comment is plain text
            },
        )


_default: Optional[OutlookClient] = None


def get_default_client() -> OutlookClient:
    global _default
    if _default is None:
        _default = OutlookClient()
    return _default


__all__ = [
    "OutlookClient",
    "OutlookUnavailableError",
    "get_default_client",
]
