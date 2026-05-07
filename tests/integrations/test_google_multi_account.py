"""Multi-account Google OAuth tests.

Verifies that 'primary' and 'bridge' accounts:
- Read independent credentials from env (with the BRIDGE_GMAIL_* aliases)
- Maintain independent token caches
- Are discoverable via list_configured_accounts
- Get routed to the correct credentials by the gmail/calendar tools
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from openjarvis.core.env import apply_aliases
from openjarvis.integrations import _google_oauth
from openjarvis.integrations._google_oauth import (
    DEFAULT_ACCOUNT,
    GoogleOAuthError,
    get_access_token,
    get_google_credentials,
    is_configured,
    list_configured_accounts,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Each test starts with no Google env and an empty token cache."""
    for k in list(__import__("os").environ.keys()):
        if "GOOGLE" in k or k.startswith("GMAIL") or k.startswith("BRIDGE_"):
            monkeypatch.delenv(k, raising=False)
    _google_oauth.reset_cache()
    yield
    _google_oauth.reset_cache()


def _patched_post(payload):
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    fake.post.return_value = resp
    return patch("httpx.Client", return_value=fake), fake


# ---------------------------------------------------------------------------
# Credential reading
# ---------------------------------------------------------------------------


def test_primary_reads_canonical_google_envs(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "primary-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "primary-secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "primary-rt")
    cid, cs, rt = get_google_credentials("primary")
    assert (cid, cs, rt) == ("primary-id", "primary-secret", "primary-rt")


def test_bridge_reads_prefixed_envs(monkeypatch):
    monkeypatch.setenv("BRIDGE_GOOGLE_CLIENT_ID", "bridge-id")
    monkeypatch.setenv("BRIDGE_GOOGLE_CLIENT_SECRET", "bridge-secret")
    monkeypatch.setenv("BRIDGE_GOOGLE_REFRESH_TOKEN", "bridge-rt")
    cid, cs, rt = get_google_credentials("bridge")
    assert (cid, cs, rt) == ("bridge-id", "bridge-secret", "bridge-rt")


def test_bridge_alias_resolves_via_apply_aliases(monkeypatch):
    """User's existing Railway naming (BRIDGE_GMAIL_*) flows through to
    the canonical BRIDGE_GOOGLE_* via the env-alias pass."""
    monkeypatch.setenv("BRIDGE_GMAIL_Client_ID", "bridge-id-from-railway")
    monkeypatch.setenv("BRIDGE_GMAIL_Client_secret", "bridge-secret-from-railway")
    monkeypatch.setenv("BRIDGE_GMAIL_Refresh_Token", "bridge-rt-from-railway")
    apply_aliases()
    cid, cs, rt = get_google_credentials("bridge")
    assert cid == "bridge-id-from-railway"
    assert cs == "bridge-secret-from-railway"
    assert rt == "bridge-rt-from-railway"


def test_bridge_unconfigured_raises_helpful_error(monkeypatch):
    with pytest.raises(GoogleOAuthError) as ei:
        get_google_credentials("bridge")
    assert "BRIDGE_" in str(ei.value)


# ---------------------------------------------------------------------------
# Configured / discovery
# ---------------------------------------------------------------------------


def test_is_configured_per_account(monkeypatch):
    assert is_configured("primary") is False
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "x")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "x")
    assert is_configured("primary") is True
    assert is_configured("bridge") is False


def test_list_configured_accounts(monkeypatch):
    assert list_configured_accounts() == []
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "x")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "x")
    monkeypatch.setenv("BRIDGE_GOOGLE_CLIENT_ID", "y")
    monkeypatch.setenv("BRIDGE_GOOGLE_CLIENT_SECRET", "y")
    monkeypatch.setenv("BRIDGE_GOOGLE_REFRESH_TOKEN", "y")
    accounts = list_configured_accounts()
    assert "primary" in accounts
    assert "bridge" in accounts
    # Primary should come first for log readability.
    assert accounts[0] == "primary"


# ---------------------------------------------------------------------------
# Independent token caches
# ---------------------------------------------------------------------------


def test_independent_token_caches(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "primary-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "primary-secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "primary-rt")
    monkeypatch.setenv("BRIDGE_GOOGLE_CLIENT_ID", "bridge-id")
    monkeypatch.setenv("BRIDGE_GOOGLE_CLIENT_SECRET", "bridge-secret")
    monkeypatch.setenv("BRIDGE_GOOGLE_REFRESH_TOKEN", "bridge-rt")

    primary_called: list[str] = []

    def fake_post(url, data=None, **kw):
        # Capture which refresh_token Google was asked about — proves
        # we're not cross-using credentials between accounts.
        rt = data.get("refresh_token") if data else None
        primary_called.append(rt)
        access_token = "primary-AAA" if rt == "primary-rt" else "bridge-BBB"
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {"access_token": access_token, "expires_in": 3600}
        resp.raise_for_status = MagicMock()
        return resp

    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.post.side_effect = fake_post
    with patch("httpx.Client", return_value=fake):
        primary_token = get_access_token("primary")
        bridge_token = get_access_token("bridge")
        # Second call should be cache hits (no extra POST).
        assert get_access_token("primary") == primary_token
        assert get_access_token("bridge") == bridge_token

    assert primary_token == "primary-AAA"
    assert bridge_token == "bridge-BBB"
    # Exactly two refresh round-trips: one per account.
    assert primary_called == ["primary-rt", "bridge-rt"]


def test_reset_cache_per_account(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "x")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "x")
    p, _ = _patched_post({"access_token": "T", "expires_in": 3600})
    with p:
        get_access_token("primary")
    assert "primary" in _google_oauth._token_cache
    _google_oauth.reset_cache("primary")
    assert "primary" not in _google_oauth._token_cache


# ---------------------------------------------------------------------------
# Tool routing — gmail tools route to the right account
# ---------------------------------------------------------------------------


def test_gmail_tool_routes_to_account_param(monkeypatch):
    """When the agent calls gmail_get_profile with account='bridge',
    the tool resolves a GmailClient bound to the bridge credentials."""
    from openjarvis.tools.gmail_tools import GmailGetProfileTool

    captured_accounts: list[str] = []

    class _FakeClient:
        def get_profile(self):
            return {"emailAddress": "fake@example.com"}

    def fake_get_default(account="primary"):
        captured_accounts.append(account)
        return _FakeClient()

    monkeypatch.setattr(
        "openjarvis.tools.gmail_tools.get_default_client",
        fake_get_default,
    )

    tool = GmailGetProfileTool()  # injected_client=None -> production path
    out = tool.execute(account="bridge")
    assert out.success is True
    assert captured_accounts == ["bridge"]

    out2 = tool.execute(account="primary")
    assert out2.success is True
    assert captured_accounts == ["bridge", "primary"]


def test_gmail_tool_account_param_in_spec():
    """The agent learns about the account param via the tool spec."""
    from openjarvis.tools.gmail_tools import (
        GmailGetProfileTool,
        GmailListMessagesTool,
        GmailSendMessageTool,
    )

    for tool_cls in (
        GmailGetProfileTool,
        GmailListMessagesTool,
        GmailSendMessageTool,
    ):
        spec = tool_cls().spec
        assert "account" in spec.parameters["properties"], (
            f"{tool_cls.__name__} missing account parameter"
        )


def test_calendar_tool_account_param_in_spec():
    from openjarvis.tools.calendar_tools import (
        CalendarListEventsTool,
        CalendarFreeBusyTool,
        CalendarCreateEventTool,
    )

    for tool_cls in (
        CalendarListEventsTool,
        CalendarFreeBusyTool,
        CalendarCreateEventTool,
    ):
        spec = tool_cls().spec
        assert "account" in spec.parameters["properties"], (
            f"{tool_cls.__name__} missing account parameter"
        )


def test_default_account_constant():
    """Sanity — the canonical default account name is 'primary'."""
    assert DEFAULT_ACCOUNT == "primary"
