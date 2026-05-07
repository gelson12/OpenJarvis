"""Multi-account Google OAuth2 refresh-token flow.

Both Calendar and Gmail can act on multiple accounts (e.g. a personal
Gmail + a BRIDGE work account) by registering each account with its
own credential set under a name. Each account has independent env vars,
independent token caches, and independent refresh cycles.

Account naming convention
-------------------------
The account "primary" is the default for backwards compatibility. Its
env vars are the canonical names used elsewhere in the codebase:

    GOOGLE_CLIENT_ID              — primary client id
    GOOGLE_CLIENT_SECRET          — primary client secret
    GOOGLE_REFRESH_TOKEN          — primary long-lived refresh token

For any other account "<name>", env vars are prefixed:

    <NAME>_GOOGLE_CLIENT_ID        e.g. BRIDGE_GOOGLE_CLIENT_ID
    <NAME>_GOOGLE_CLIENT_SECRET    e.g. BRIDGE_GOOGLE_CLIENT_SECRET
    <NAME>_GOOGLE_REFRESH_TOKEN    e.g. BRIDGE_GOOGLE_REFRESH_TOKEN

For the user's existing Railway naming (BRIDGE_GMAIL_*), aliases live in
:mod:`openjarvis.core.env` so BRIDGE_GMAIL_Client_ID resolves to the
canonical BRIDGE_GOOGLE_CLIENT_ID at startup.

Tools take an ``account="primary"`` parameter. The agent picks which
account to act on per call ("send from BRIDGE", "what's on my primary
calendar?").
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

DEFAULT_ACCOUNT = "primary"


class GoogleOAuthError(RuntimeError):
    """Raised when credentials are missing or token refresh fails."""


# Per-account access-token cache: {account_name: (token, expires_at)}
_token_cache: dict[str, tuple[str, float]] = {}


def _env_prefix(account: str) -> str:
    """The env-var prefix for ``account``. 'primary' uses no prefix
    (canonical GOOGLE_*); every other account uses <NAME>_GOOGLE_*."""
    if account == DEFAULT_ACCOUNT:
        return ""
    return f"{account.upper()}_"


def _read_env(account: str, suffix: str) -> str:
    """Read GOOGLE_<suffix> for primary, or <ACCOUNT>_GOOGLE_<suffix>."""
    prefix = _env_prefix(account)
    return os.environ.get(f"{prefix}GOOGLE_{suffix}", "").strip()


def get_google_credentials(account: str = DEFAULT_ACCOUNT) -> tuple[str, str, str]:
    """Read CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN for ``account``.

    Raises GoogleOAuthError if any are missing.
    """
    cid = _read_env(account, "CLIENT_ID")
    csec = _read_env(account, "CLIENT_SECRET")
    rt = _read_env(account, "REFRESH_TOKEN")
    if not (cid and csec and rt):
        prefix = _env_prefix(account) or "(no prefix)"
        raise GoogleOAuthError(
            f"Google OAuth not configured for account {account!r} — set "
            f"{prefix}GOOGLE_CLIENT_ID, {prefix}GOOGLE_CLIENT_SECRET, "
            f"{prefix}GOOGLE_REFRESH_TOKEN in env"
        )
    return cid, csec, rt


def is_configured(account: str = DEFAULT_ACCOUNT) -> bool:
    """True if all three env vars for ``account`` are set."""
    try:
        get_google_credentials(account)
        return True
    except GoogleOAuthError:
        return False


def list_configured_accounts() -> list[str]:
    """Discover which Google accounts have credentials in env.

    Always checks the primary slot, plus scans os.environ for any
    ``<NAME>_GOOGLE_REFRESH_TOKEN`` to find named accounts. Returns
    sorted account names; "primary" comes first if present.
    """
    accounts: set[str] = set()
    if is_configured(DEFAULT_ACCOUNT):
        accounts.add(DEFAULT_ACCOUNT)
    for key in os.environ:
        # Match <NAME>_GOOGLE_REFRESH_TOKEN — extract NAME, lowercase.
        if key.endswith("_GOOGLE_REFRESH_TOKEN"):
            name = key[: -len("_GOOGLE_REFRESH_TOKEN")].lower()
            if name and is_configured(name):
                accounts.add(name)
    out = sorted(accounts)
    # Ensure primary is first if present (cosmetic, helps log readability).
    if DEFAULT_ACCOUNT in out:
        out.remove(DEFAULT_ACCOUNT)
        out.insert(0, DEFAULT_ACCOUNT)
    return out


def get_access_token(
    account: str = DEFAULT_ACCOUNT, *, timeout: float = 15.0,
) -> str:
    """Return a valid access token for ``account``, refreshing via
    Google's token endpoint when the cached one is expired.

    Per-account cache so multiple accounts coexist without clobbering
    each other's tokens.
    """
    cached = _token_cache.get(account)
    if cached and time.time() < cached[1]:
        return cached[0]

    client_id, client_secret, refresh_token = get_google_credentials(account)
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
        raise GoogleOAuthError(
            f"Google token refresh failed for account {account!r}: {exc}"
        ) from exc

    token = payload.get("access_token", "")
    if not token:
        raise GoogleOAuthError(
            f"Google token response missing access_token for "
            f"account {account!r}: {payload}"
        )
    ttl = int(payload.get("expires_in", 3600))
    _token_cache[account] = (token, time.time() + max(0, ttl - 60))
    return token


def reset_cache(account: Optional[str] = None) -> None:
    """Drop cached access token(s). Without ``account``, drops every
    cache entry; with ``account``, drops only that one. Useful in tests."""
    if account is None:
        _token_cache.clear()
    else:
        _token_cache.pop(account, None)


__all__ = [
    "DEFAULT_ACCOUNT",
    "GoogleOAuthError",
    "get_access_token",
    "get_google_credentials",
    "is_configured",
    "list_configured_accounts",
    "reset_cache",
]
