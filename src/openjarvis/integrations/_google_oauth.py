"""Shared Google OAuth2 refresh-token flow for Calendar + Gmail (+ future
Drive, Sheets, etc.).

Both Calendar and Gmail use the same env vars (GOOGLE_CLIENT_ID,
GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN) — they're scopes on a
single OAuth client. Centralising the token-fetch + cache here means
the user only does the OAuth dance once, and both clients share a
single in-memory access-token cache (one refresh round-trip per hour
instead of two).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleOAuthError(RuntimeError):
    """Raised when Google credentials are missing or token refresh fails."""


_cached_token: Optional[str] = None
_cached_expires_at: float = 0.0


def get_google_credentials() -> tuple[str, str, str]:
    """Read GOOGLE_CLIENT_ID / SECRET / REFRESH_TOKEN from env.

    Raises GoogleOAuthError if any are missing.
    """
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    csec = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    rt = os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip()
    if not (cid and csec and rt):
        raise GoogleOAuthError(
            "Google OAuth not configured — set GOOGLE_CLIENT_ID, "
            "GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN in env"
        )
    return cid, csec, rt


def is_configured() -> bool:
    """True if all three Google OAuth env vars are set."""
    try:
        get_google_credentials()
        return True
    except GoogleOAuthError:
        return False


def get_access_token(*, timeout: float = 15.0) -> str:
    """Return a valid access token, refreshing via Google's token
    endpoint when the cached one is expired.

    Cached for ~expires_in seconds (60s safety margin) across the
    entire process — Calendar + Gmail share the same token because
    they share the same OAuth client.
    """
    global _cached_token, _cached_expires_at
    if _cached_token and time.time() < _cached_expires_at:
        return _cached_token

    client_id, client_secret, refresh_token = get_google_credentials()
    try:
        with httpx.Client(timeout=timeout) as c:
            resp = c.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise GoogleOAuthError(f"Google token refresh failed: {exc}") from exc

    token = payload.get("access_token", "")
    if not token:
        raise GoogleOAuthError(
            f"Google token response missing access_token: {payload}"
        )
    ttl = int(payload.get("expires_in", 3600))
    _cached_token = token
    _cached_expires_at = time.time() + max(0, ttl - 60)
    return token


def reset_cache() -> None:
    """Drop the cached access token. Useful in tests; production code
    rarely needs to call this."""
    global _cached_token, _cached_expires_at
    _cached_token = None
    _cached_expires_at = 0.0


__all__ = [
    "GoogleOAuthError",
    "get_access_token",
    "get_google_credentials",
    "is_configured",
    "reset_cache",
]
