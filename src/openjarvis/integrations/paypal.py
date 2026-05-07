"""PayPal REST API client (transactions + subscriptions + refunds).

Talks to PayPal's REST API at ``https://api-m.paypal.com`` (live) or
``https://api-m.sandbox.paypal.com`` (sandbox). Auth is OAuth2 client
credentials: POST /v1/oauth2/token with
``Authorization: Basic <client_id:client_secret>`` exchanges for a
short-lived bearer token (typically ~9 hours). Token is cached in
memory and refreshed on demand.

Env vars (canonical names):
  PAYPAL_CLIENT_ID
  PAYPAL_CLIENT_SECRET
  PAYPAL_API_BASE  (optional — defaults to live; set to sandbox URL for testing)

Why mirror Stripe's thin httpx wrapper instead of paypalcheckoutsdk
-------------------------------------------------------------------
The official SDK is heavy, partially deprecated by PayPal in favour of
direct REST, and still doesn't cover all the endpoints we want
(transactions reporting, subscriptions). Hand-rolled httpx with cached
OAuth tokens stays consistent with the n8n / github / stripe pattern
used elsewhere in this codebase.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

_PAYPAL_LIVE_BASE = "https://api-m.paypal.com"


class PayPalUnavailableError(RuntimeError):
    """Raised when PayPal credentials are missing or an API call fails."""


class PayPalClient:
    """Synchronous httpx-based client for the PayPal REST API."""

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        api_base: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self._client_id = client_id or os.environ.get("PAYPAL_CLIENT_ID", "")
        self._client_secret = (
            client_secret or os.environ.get("PAYPAL_CLIENT_SECRET", "")
        )
        self._api_base = (
            api_base
            or os.environ.get("PAYPAL_API_BASE", _PAYPAL_LIVE_BASE)
        ).rstrip("/")
        self._timeout = timeout
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # ------------------------------------------------------------------
    # OAuth2 token caching
    # ------------------------------------------------------------------

    def _fetch_token(self) -> str:
        if not self.configured:
            raise PayPalUnavailableError(
                "PayPal not configured — set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET"
            )
        creds = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        try:
            with httpx.Client(timeout=self._timeout) as c:
                resp = c.post(
                    f"{self._api_base}/v1/oauth2/token",
                    headers={
                        "Authorization": f"Basic {creds}",
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={"grant_type": "client_credentials"},
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise PayPalUnavailableError(
                f"PayPal OAuth token fetch failed: {exc}"
            ) from exc

        token = payload.get("access_token", "")
        if not token:
            raise PayPalUnavailableError(
                f"PayPal OAuth response missing access_token: {payload}"
            )
        # PayPal returns expires_in seconds; cache 60s short of expiry to
        # avoid using a token that expires mid-request.
        ttl = int(payload.get("expires_in", 3600))
        self._token = token
        self._token_expires_at = time.time() + max(0, ttl - 60)
        return token

    def _bearer(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        return self._fetch_token()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        url = f"{self._api_base}{path}"
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
            raise PayPalUnavailableError(
                f"PayPal {method} {path} failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Transactions (Reporting API)
    # ------------------------------------------------------------------

    def list_transactions(
        self,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page_size: int = 100,
        page: int = 1,
    ) -> Any:
        """List transactions in an ISO8601 date range.

        ``start_date`` / ``end_date`` are RFC 3339 strings, e.g.
        ``"2026-05-01T00:00:00Z"``. Defaults to the last 7 days.
        """
        if not start_date:
            start_date = time.strftime(
                "%Y-%m-%dT00:00:00Z",
                time.gmtime(time.time() - 7 * 86400),
            )
        if not end_date:
            end_date = time.strftime("%Y-%m-%dT23:59:59Z", time.gmtime())
        params = {
            "start_date": start_date,
            "end_date": end_date,
            "page_size": min(page_size, 500),
            "page": page,
            "fields": "transaction_info,payer_info",
        }
        return self._request("GET", "/v1/reporting/transactions", params=params)

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def get_subscription(self, subscription_id: str) -> Any:
        return self._request("GET", f"/v1/billing/subscriptions/{subscription_id}")

    # ------------------------------------------------------------------
    # Refunds (Payments v2)
    # ------------------------------------------------------------------

    def get_refund(self, refund_id: str) -> Any:
        return self._request("GET", f"/v2/payments/refunds/{refund_id}")

    def create_refund(
        self,
        *,
        capture_id: str,
        amount_value: Optional[str] = None,
        currency: str = "USD",
        note_to_payer: Optional[str] = None,
    ) -> Any:
        """Refund a capture. ``amount_value`` is a decimal string
        (e.g. ``"19.99"``); omit to refund the full capture."""
        body: dict[str, Any] = {}
        if amount_value is not None:
            body["amount"] = {"value": amount_value, "currency_code": currency}
        if note_to_payer:
            body["note_to_payer"] = note_to_payer
        return self._request(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            json_body=body or None,
        )

    # ------------------------------------------------------------------
    # Convenience: revenue summary
    # ------------------------------------------------------------------

    def revenue_summary(self, *, days: int = 7) -> dict[str, Any]:
        """Aggregate transaction revenue over the last N days.

        Sums the gross amounts of completed (status ``S``) transactions,
        grouped by currency. Single-page only — accounts with >500
        transactions in the window will under-report.
        """
        start = time.strftime(
            "%Y-%m-%dT00:00:00Z",
            time.gmtime(time.time() - days * 86400),
        )
        page = self.list_transactions(start_date=start, page_size=500)
        details: Iterable[dict[str, Any]] = (
            page.get("transaction_details", []) if page else []
        )

        gross_by_currency: dict[str, float] = {}
        completed = 0
        for entry in details:
            info = entry.get("transaction_info", {}) or {}
            if info.get("transaction_status") != "S":
                continue
            amount = info.get("transaction_amount") or {}
            try:
                value = float(amount.get("value", "0") or 0)
            except (TypeError, ValueError):
                value = 0.0
            currency = amount.get("currency_code", "USD")
            gross_by_currency[currency] = (
                gross_by_currency.get(currency, 0.0) + value
            )
            completed += 1

        return {
            "window_days": days,
            "completed_transactions": completed,
            "gross_by_currency": gross_by_currency,
            "page_size": 500,
            "has_more": (
                int(page.get("total_items", 0)) > 500 if page else False
            ),
        }


_default: Optional[PayPalClient] = None


def get_default_client() -> PayPalClient:
    global _default
    if _default is None:
        _default = PayPalClient()
    return _default


__all__ = [
    "PayPalClient",
    "PayPalUnavailableError",
    "get_default_client",
]
