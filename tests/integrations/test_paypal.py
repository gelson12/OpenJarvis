"""Tests for the PayPal connector + tools.

Mocks httpx so no real PayPal calls are made; exercises OAuth token
caching, request shape, the revenue_summary aggregator, and the tool
surface.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from openjarvis.integrations.paypal import (
    PayPalClient,
    PayPalUnavailableError,
)
from openjarvis.tools.paypal_tools import (
    PayPalCreateRefundTool,
    PayPalRevenueSummaryTool,
)


def _resp(json_payload, status_code: int = 200) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.content = b"{}"
    r.json.return_value = json_payload
    r.raise_for_status = MagicMock()
    return r


# ---------------------------------------------------------------------------
# Configured-ness
# ---------------------------------------------------------------------------


def test_unconfigured_when_no_creds(monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    assert PayPalClient().configured is False


def test_configured_when_both_creds_set(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "secret")
    assert PayPalClient().configured is True


def test_token_fetch_raises_when_unconfigured(monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    with pytest.raises(PayPalUnavailableError):
        PayPalClient()._fetch_token()


# ---------------------------------------------------------------------------
# OAuth token caching
# ---------------------------------------------------------------------------


def test_token_cached_within_ttl():
    client = PayPalClient(client_id="id", client_secret="secret")
    fake_client = MagicMock()
    fake_client.post.return_value = _resp({"access_token": "AAA", "expires_in": 3600})
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("httpx.Client", return_value=fake_client):
        a = client._bearer()
        b = client._bearer()
    assert a == b == "AAA"
    # Only one POST to /oauth2/token despite two _bearer() calls
    assert fake_client.post.call_count == 1


def test_token_refetched_after_expiry():
    client = PayPalClient(client_id="id", client_secret="secret")
    fake_client = MagicMock()
    fake_client.post.return_value = _resp({"access_token": "AAA", "expires_in": 1})
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("httpx.Client", return_value=fake_client):
        client._bearer()
        # Force "expiry" by rewinding the cached deadline.
        client._token_expires_at = time.time() - 10
        client._bearer()
    assert fake_client.post.call_count == 2


def test_token_response_missing_access_token_raises():
    client = PayPalClient(client_id="id", client_secret="secret")
    fake_client = MagicMock()
    fake_client.post.return_value = _resp({"oops": "no token"})
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("httpx.Client", return_value=fake_client):
        with pytest.raises(PayPalUnavailableError):
            client._fetch_token()


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_list_transactions_includes_default_window():
    client = PayPalClient(client_id="id", client_secret="secret")
    client._token = "T"
    client._token_expires_at = time.time() + 3600
    fake_client = MagicMock()
    fake_client.request.return_value = _resp({"transaction_details": []})
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("httpx.Client", return_value=fake_client):
        client.list_transactions(page_size=200)
    params = fake_client.request.call_args.kwargs["params"]
    # Should set start_date / end_date even without explicit args.
    assert "start_date" in params
    assert "end_date" in params
    assert params["page_size"] == 200


def test_create_refund_partial_amount():
    client = PayPalClient(client_id="id", client_secret="secret")
    client._token = "T"
    client._token_expires_at = time.time() + 3600
    fake_client = MagicMock()
    fake_client.request.return_value = _resp({"id": "RE-1"})
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("httpx.Client", return_value=fake_client):
        client.create_refund(capture_id="CAP1", amount_value="9.99", currency="EUR")
    call = fake_client.request.call_args
    assert call.args[0] == "POST"
    assert call.args[1].endswith("/v2/payments/captures/CAP1/refund")
    assert call.kwargs["json"] == {
        "amount": {"value": "9.99", "currency_code": "EUR"},
    }


def test_create_refund_full_amount_omits_body():
    client = PayPalClient(client_id="id", client_secret="secret")
    client._token = "T"
    client._token_expires_at = time.time() + 3600
    fake_client = MagicMock()
    fake_client.request.return_value = _resp({"id": "RE-1"})
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("httpx.Client", return_value=fake_client):
        client.create_refund(capture_id="CAP1")
    # No body when full refund (PayPal default = full amount).
    assert fake_client.request.call_args.kwargs["json"] is None


# ---------------------------------------------------------------------------
# revenue_summary aggregator
# ---------------------------------------------------------------------------


def test_revenue_summary_sums_completed_only_per_currency():
    client = PayPalClient(client_id="id", client_secret="secret")
    client._token = "T"
    client._token_expires_at = time.time() + 3600
    payload = {
        "transaction_details": [
            {"transaction_info": {
                "transaction_status": "S",
                "transaction_amount": {"value": "20.00", "currency_code": "USD"},
            }},
            {"transaction_info": {
                "transaction_status": "S",
                "transaction_amount": {"value": "5.50", "currency_code": "USD"},
            }},
            {"transaction_info": {
                "transaction_status": "S",
                "transaction_amount": {"value": "10.00", "currency_code": "EUR"},
            }},
            # Pending transaction — should be ignored.
            {"transaction_info": {
                "transaction_status": "P",
                "transaction_amount": {"value": "9999.00", "currency_code": "USD"},
            }},
        ],
        "total_items": 4,
    }
    fake_client = MagicMock()
    fake_client.request.return_value = _resp(payload)
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("httpx.Client", return_value=fake_client):
        summary = client.revenue_summary(days=7)
    assert summary["completed_transactions"] == 3
    assert summary["gross_by_currency"]["USD"] == pytest.approx(25.50)
    assert summary["gross_by_currency"]["EUR"] == pytest.approx(10.00)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def test_revenue_summary_tool_clamps_days_to_31():
    fake_client = MagicMock(spec=PayPalClient)
    fake_client.revenue_summary.return_value = {"window_days": 31}
    PayPalRevenueSummaryTool(client=fake_client).execute(days=999)
    fake_client.revenue_summary.assert_called_once_with(days=31)


def test_create_refund_tool_requires_confirmation():
    spec = PayPalCreateRefundTool(client=MagicMock(spec=PayPalClient)).spec
    assert spec.requires_confirmation is True


def test_tool_surfaces_unavailable_as_error():
    fake_client = MagicMock(spec=PayPalClient)
    fake_client.revenue_summary.side_effect = PayPalUnavailableError("no creds")
    out = PayPalRevenueSummaryTool(client=fake_client).execute(days=7)
    assert out.success is False
    assert "paypal error" in out.content
