"""Stripe REST API client (read-only revenue + customer queries, refunds).

Talks to Stripe's REST API at ``https://api.stripe.com/v1`` over HTTPS.
Auth is HTTP Basic with the secret key as the username and an empty
password — Stripe's standard scheme. Key comes from
``STRIPE_SECRET_KEY`` (canonical name; aliases handled by the
env-alias pass at startup).

Why a thin httpx wrapper instead of the official ``stripe`` SDK
---------------------------------------------------------------
The Stripe surface we need (balance, charges, subscriptions, refunds)
is small and stable. The SDK would add ~80MB of optional deps (urllib3
patches, pluggy, pytz transitively) and an extra import-time cost on
every server startup, while we'd still be hand-rolling our briefing
aggregation logic on top. Mirroring the n8n / github pattern keeps
the dependency tree slim and the auth + pagination shape consistent
across connectors.

Pagination uses Stripe's ``starting_after`` cursor; for the briefing
use case a single page (default ``limit=100``) is sufficient because
we cap the time window at 7-30 days. Multi-page aggregation can be
added later if a busy account needs it.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

_STRIPE_API_BASE = "https://api.stripe.com/v1"


class StripeUnavailableError(RuntimeError):
    """Raised when the Stripe key is missing or an API call fails."""


class StripeClient:
    """Synchronous httpx-based client for the Stripe REST API."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        timeout: float = 15.0,
        api_base: str = _STRIPE_API_BASE,
    ) -> None:
        self._key = api_key or os.environ.get("STRIPE_SECRET_KEY", "")
        self._timeout = timeout
        self._api_base = api_base.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _headers(self) -> dict[str, str]:
        # Stripe uses HTTP Basic with the secret key as the username and
        # no password. We construct the Authorization header manually so
        # we don't need a separate basic-auth helper.
        import base64

        token = base64.b64encode(f"{self._key}:".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        form: Optional[dict[str, Any]] = None,
    ) -> Any:
        if not self.configured:
            raise StripeUnavailableError(
                "Stripe not configured — set STRIPE_SECRET_KEY"
            )
        url = f"{self._api_base}{path}"
        try:
            with httpx.Client(timeout=self._timeout) as c:
                resp = c.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    data=form,  # Stripe wants application/x-www-form-urlencoded
                )
                resp.raise_for_status()
                return resp.json() if resp.content else None
        except httpx.HTTPError as exc:
            raise StripeUnavailableError(
                f"Stripe {method} {path} failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_balance(self) -> Any:
        """Current account balance — available + pending across currencies."""
        return self._request("GET", "/balance")

    # ------------------------------------------------------------------
    # Charges
    # ------------------------------------------------------------------

    def list_charges(
        self,
        *,
        created_gte: Optional[int] = None,
        limit: int = 100,
        starting_after: Optional[str] = None,
    ) -> Any:
        """List charges, optionally filtered to ``created >= created_gte``
        (Unix timestamp seconds). ``limit`` capped at 100 by Stripe."""
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if created_gte is not None:
            params["created[gte]"] = created_gte
        if starting_after:
            params["starting_after"] = starting_after
        return self._request("GET", "/charges", params=params)

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def list_subscriptions(
        self,
        *,
        status: str = "active",
        limit: int = 100,
        starting_after: Optional[str] = None,
    ) -> Any:
        """List subscriptions filtered by ``status`` (active/canceled/all)."""
        params: dict[str, Any] = {"status": status, "limit": min(limit, 100)}
        if starting_after:
            params["starting_after"] = starting_after
        return self._request("GET", "/subscriptions", params=params)

    # ------------------------------------------------------------------
    # Refunds
    # ------------------------------------------------------------------

    def list_refunds(
        self,
        *,
        charge: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if charge:
            params["charge"] = charge
        return self._request("GET", "/refunds", params=params)

    def create_refund(
        self,
        *,
        charge: str,
        amount: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> Any:
        """Refund a charge. ``amount`` is in the smallest currency unit
        (cents for USD). Omit to refund the full amount."""
        form: dict[str, Any] = {"charge": charge}
        if amount is not None:
            form["amount"] = amount
        if reason:
            form["reason"] = reason
        return self._request("POST", "/refunds", form=form)

    # ------------------------------------------------------------------
    # Convenience: revenue summary for the briefing
    # ------------------------------------------------------------------

    def revenue_summary(self, *, days: int = 7) -> dict[str, Any]:
        """Aggregate succeeded-charge revenue over the last N days.

        Returns gross revenue (sum of succeeded charges), refunded
        amount, charge count, and currency breakdown. Single-page only —
        an account with >100 charges/day will under-report.
        """
        cutoff = int(time.time()) - days * 86400
        page = self.list_charges(created_gte=cutoff, limit=100)
        charges: Iterable[dict[str, Any]] = page.get("data", []) if page else []

        gross_by_currency: dict[str, int] = {}
        refunded_by_currency: dict[str, int] = {}
        succeeded = 0
        for ch in charges:
            if ch.get("status") != "succeeded":
                continue
            currency = ch.get("currency", "usd")
            gross_by_currency[currency] = (
                gross_by_currency.get(currency, 0) + (ch.get("amount") or 0)
            )
            refunded_by_currency[currency] = (
                refunded_by_currency.get(currency, 0)
                + (ch.get("amount_refunded") or 0)
            )
            succeeded += 1

        return {
            "window_days": days,
            "succeeded_charges": succeeded,
            "gross_by_currency": gross_by_currency,
            "refunded_by_currency": refunded_by_currency,
            "net_by_currency": {
                cur: gross_by_currency[cur] - refunded_by_currency.get(cur, 0)
                for cur in gross_by_currency
            },
            "has_more": bool(page.get("has_more")) if page else False,
        }


_default: Optional[StripeClient] = None


def get_default_client() -> StripeClient:
    global _default
    if _default is None:
        _default = StripeClient()
    return _default


__all__ = [
    "StripeClient",
    "StripeUnavailableError",
    "get_default_client",
]
