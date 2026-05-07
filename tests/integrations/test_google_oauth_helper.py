"""Tests for the shared Google OAuth refresh-token helper.

Both Calendar and Gmail use these helpers (well, Gmail does — Calendar
keeps its own token refresh as a stable baseline). The helper caches the
access token across the whole process so multiple Google connectors
share one refresh round-trip per hour.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from openjarvis.integrations import _google_oauth


@pytest.fixture(autouse=True)
def _clear_env_and_cache(monkeypatch):
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    _google_oauth.reset_cache()
    yield
    _google_oauth.reset_cache()


def _patched_post(json_payload):
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_payload
    resp.raise_for_status = MagicMock()
    fake.post.return_value = resp
    return patch("httpx.Client", return_value=fake), fake


def test_get_credentials_raises_when_missing():
    with pytest.raises(_google_oauth.GoogleOAuthError):
        _google_oauth.get_google_credentials()


def test_is_configured_reflects_env(monkeypatch):
    assert _google_oauth.is_configured() is False
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "rt")
    assert _google_oauth.is_configured() is True


def test_token_cached_within_ttl(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "rt")
    p, fake = _patched_post({"access_token": "AAA", "expires_in": 3600})
    with p:
        a = _google_oauth.get_access_token()
        b = _google_oauth.get_access_token()
    assert a == b == "AAA"
    assert fake.post.call_count == 1  # cache hit on second call


def test_token_refetched_after_expiry(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "rt")
    p, fake = _patched_post({"access_token": "AAA", "expires_in": 1})
    with p:
        _google_oauth.get_access_token()
        # Force expiry by rewinding the cache deadline
        _google_oauth._cached_expires_at = time.time() - 10
        _google_oauth.get_access_token()
    assert fake.post.call_count == 2


def test_missing_access_token_in_response_raises(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "rt")
    p, _ = _patched_post({"oops": "no token"})
    with p:
        with pytest.raises(_google_oauth.GoogleOAuthError):
            _google_oauth.get_access_token()


def test_http_error_wraps_to_oauth_error(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "rt")
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.post.side_effect = httpx.HTTPError("boom")
    with patch("httpx.Client", return_value=fake):
        with pytest.raises(_google_oauth.GoogleOAuthError) as ei:
            _google_oauth.get_access_token()
    assert "boom" in str(ei.value)
