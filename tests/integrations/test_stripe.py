"""Tests for the Stripe connector + tools.

Mocks httpx so no real Stripe calls are made; verifies the client builds
the right requests and the revenue_summary aggregator does the math
correctly across multiple currencies.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from openjarvis.integrations.stripe import (
    StripeClient,
    StripeUnavailableError,
)
from openjarvis.tools.stripe_tools import (
    StripeCreateRefundTool,
    StripeGetBalanceTool,
    StripeRevenueSummaryTool,
)


# ---------------------------------------------------------------------------
# Client construction + configured-ness
# ---------------------------------------------------------------------------


def test_client_unconfigured_when_no_key(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    assert StripeClient().configured is False


def test_client_configured_with_key(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_xxx")
    assert StripeClient().configured is True


def test_request_raises_when_unconfigured(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    with pytest.raises(StripeUnavailableError):
        StripeClient().get_balance()


# ---------------------------------------------------------------------------
# HTTP layer (mocked httpx)
# ---------------------------------------------------------------------------


def _mock_httpx_response(json_payload: dict | list, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.content = b"{}"
    resp.json.return_value = json_payload
    resp.raise_for_status = MagicMock()
    return resp


def _patch_httpx_client(json_payload):
    """Returns a context manager patch for httpx.Client whose request()
    returns the given json. Use as ``with _patch_httpx_client(payload) as m:``"""
    fake_resp = _mock_httpx_response(json_payload)
    fake_client = MagicMock()
    fake_client.request.return_value = fake_resp
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    return patch("httpx.Client", return_value=fake_client), fake_client


def test_get_balance_uses_basic_auth_with_secret_key():
    client = StripeClient(api_key="sk_test_AB")
    p, fake = _patch_httpx_client({"available": [{"amount": 100}]})
    with p:
        out = client.get_balance()
    assert out == {"available": [{"amount": 100}]}
    # Inspect the request the client built
    call = fake.request.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == "https://api.stripe.com/v1/balance"
    headers = call.kwargs["headers"]
    # Basic auth header is base64("sk_test_AB:")
    assert headers["Authorization"].startswith("Basic ")


def test_list_charges_passes_created_filter():
    client = StripeClient(api_key="sk_test_x")
    p, fake = _patch_httpx_client({"data": [], "has_more": False})
    with p:
        client.list_charges(created_gte=1700000000, limit=50)
    params = fake.request.call_args.kwargs["params"]
    assert params == {"limit": 50, "created[gte]": 1700000000}


def test_create_refund_uses_form_encoding():
    client = StripeClient(api_key="sk_test_x")
    p, fake = _patch_httpx_client({"id": "re_123"})
    with p:
        client.create_refund(charge="ch_abc", amount=500, reason="duplicate")
    call = fake.request.call_args
    assert call.args[0] == "POST"
    assert call.kwargs["data"] == {
        "charge": "ch_abc",
        "amount": 500,
        "reason": "duplicate",
    }


def test_http_error_wrapped_as_stripe_unavailable():
    client = StripeClient(api_key="sk_test_x")
    fake_client = MagicMock()
    fake_client.request.side_effect = httpx.HTTPError("boom")
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("httpx.Client", return_value=fake_client):
        with pytest.raises(StripeUnavailableError) as ei:
            client.get_balance()
    assert "boom" in str(ei.value)


# ---------------------------------------------------------------------------
# revenue_summary aggregator
# ---------------------------------------------------------------------------


def test_revenue_summary_aggregates_succeeded_only_per_currency():
    client = StripeClient(api_key="sk_test_x")
    payload = {
        "data": [
            {"status": "succeeded", "currency": "usd", "amount": 2000, "amount_refunded": 0},
            {"status": "succeeded", "currency": "usd", "amount": 1500, "amount_refunded": 500},
            {"status": "succeeded", "currency": "eur", "amount": 1000, "amount_refunded": 0},
            # Failed charge — should be ignored.
            {"status": "failed", "currency": "usd", "amount": 9999, "amount_refunded": 0},
        ],
        "has_more": False,
    }
    p, _ = _patch_httpx_client(payload)
    with p:
        summary = client.revenue_summary(days=7)
    assert summary["window_days"] == 7
    assert summary["succeeded_charges"] == 3
    assert summary["gross_by_currency"] == {"usd": 3500, "eur": 1000}
    assert summary["refunded_by_currency"] == {"usd": 500, "eur": 0}
    assert summary["net_by_currency"] == {"usd": 3000, "eur": 1000}


def test_revenue_summary_handles_empty_account():
    client = StripeClient(api_key="sk_test_x")
    p, _ = _patch_httpx_client({"data": [], "has_more": False})
    with p:
        summary = client.revenue_summary(days=30)
    assert summary["succeeded_charges"] == 0
    assert summary["gross_by_currency"] == {}
    assert summary["net_by_currency"] == {}


# ---------------------------------------------------------------------------
# Tools — the model-callable surface
# ---------------------------------------------------------------------------


def test_get_balance_tool_returns_success(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    fake_client = MagicMock(spec=StripeClient)
    fake_client.get_balance.return_value = {"available": [{"amount": 1234}]}
    out = StripeGetBalanceTool(client=fake_client).execute()
    assert out.success is True
    assert "1234" in out.content


def test_revenue_summary_tool_clamps_days_to_90(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    fake_client = MagicMock(spec=StripeClient)
    fake_client.revenue_summary.return_value = {"window_days": 90}
    out = StripeRevenueSummaryTool(client=fake_client).execute(days=999)
    assert out.success is True
    fake_client.revenue_summary.assert_called_once_with(days=90)


def test_revenue_summary_tool_clamps_days_to_minimum_1(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    fake_client = MagicMock(spec=StripeClient)
    fake_client.revenue_summary.return_value = {}
    StripeRevenueSummaryTool(client=fake_client).execute(days=-5)
    fake_client.revenue_summary.assert_called_once_with(days=1)


def test_create_refund_tool_requires_confirmation():
    """Refund tool spec must carry requires_confirmation=True."""
    spec = StripeCreateRefundTool(client=MagicMock(spec=StripeClient)).spec
    assert spec.requires_confirmation is True


def test_tool_surfaces_stripe_unavailable_as_error():
    """A StripeUnavailableError from the client becomes a failed ToolResult."""
    fake_client = MagicMock(spec=StripeClient)
    fake_client.get_balance.side_effect = StripeUnavailableError("no key")
    out = StripeGetBalanceTool(client=fake_client).execute()
    assert out.success is False
    assert "stripe error" in out.content
