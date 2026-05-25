"""
LiteAPI provider — Phase 1's only live supply source.

LiteAPI aggregates 100+ hotel/short-let suppliers into a single REST API.
Sandbox keys are self-serve, no credit card. Production rates flow through
the same endpoints once the user upgrades.

Wholesale rates are exposed verbatim; we apply our own markup before
surfacing prices to the user. The markup is configured via
`LITEAPI_AFFILIATE_MARKUP_PCT` env (e.g. `10.0` = 10%).

API reference: https://docs.liteapi.travel/

Endpoint shape (verified against docs as of 2026):
- POST /v3.0/hotels/rates       — search by location + dates → rates list
- POST /v3.0/rates/prebook      — lock in a rate, get a prebookId
- POST /v3.0/rates/book         — finalize, returns confirmation + checkout_url
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from accommodation.models import (
    BookingRequest,
    BookingResult,
    Property,
    Quote,
    SearchQuery,
)
from accommodation.providers.base import Provider, ProviderError

_log = logging.getLogger("accommodation.liteapi")

_SANDBOX_BASE = "https://api.sandbox.liteapi.travel"
_PRODUCTION_BASE = "https://api.liteapi.travel"


class LiteApiProvider(Provider):
    id = "liteapi"

    def __init__(
        self,
        api_key: str | None = None,
        env: str | None = None,
        markup_pct: float | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ):
        self.api_key = api_key or os.environ.get("LITEAPI_KEY", "").strip()
        if not self.api_key:
            raise ProviderError("LITEAPI_KEY not configured")
        environment = (env or os.environ.get("LITEAPI_ENV", "sandbox")).strip().lower()
        self.base_url = _PRODUCTION_BASE if environment == "production" else _SANDBOX_BASE
        try:
            self.markup_pct = float(
                markup_pct
                if markup_pct is not None
                else os.environ.get("LITEAPI_AFFILIATE_MARKUP_PCT", "10.0")
            )
        except ValueError:
            self.markup_pct = 10.0
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"X-API-Key": self.api_key, "Accept": "application/json"},
        )
        self._owns_client = client is None

    @property
    def can_book(self) -> bool:
        return True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _apply_markup(self, wholesale: float) -> float:
        return round(wholesale * (1 + self.markup_pct / 100.0), 2)

    def _commission_estimate(self, retail_total: float, wholesale_total: float) -> float:
        return round(retail_total - wholesale_total, 2)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            _log.warning("liteapi %s -> %s: %s", path, e.response.status_code, e.response.text[:300])
            raise ProviderError(f"liteapi {path} returned {e.response.status_code}") from e
        except httpx.HTTPError as e:
            _log.warning("liteapi %s network error: %s", path, e)
            raise ProviderError(f"liteapi {path} network error") from e
        return resp.json()

    async def search(self, query: SearchQuery, limit: int = 20) -> list[Property]:
        payload = {
            "checkin": query.check_in.isoformat(),
            "checkout": query.check_out.isoformat(),
            "currency": query.currency,
            "guestNationality": "GB",
            "occupancies": [{"adults": max(1, query.guests), "children": []}],
            "cityName": query.location,
            "limit": limit,
        }
        data = await self._post("/v3.0/hotels/rates", payload)
        hotels = data.get("data") or []
        properties: list[Property] = []
        for hotel in hotels[:limit]:
            try:
                hotel_info = hotel.get("hotel", {}) or {}
                room_types = hotel.get("roomTypes") or []
                if not room_types:
                    continue
                cheapest = min(
                    room_types,
                    key=lambda r: float((r.get("rates") or [{}])[0].get("retailRate", {}).get("total", [{}])[0].get("amount", float("inf"))),
                )
                rate = (cheapest.get("rates") or [{}])[0]
                retail = rate.get("retailRate", {})
                amount_blocks = retail.get("total") or [{}]
                wholesale_total = float(amount_blocks[0].get("amount", 0.0))
                currency = amount_blocks[0].get("currency", query.currency)
                if wholesale_total <= 0:
                    continue
                retail_total = self._apply_markup(wholesale_total)
                book_token = cheapest.get("offerId") or rate.get("rateId") or ""
                if not book_token:
                    continue
                properties.append(
                    Property(
                        provider_id=self.id,
                        external_id=str(hotel_info.get("id", "")),
                        name=str(hotel_info.get("name", "Unknown property")),
                        lat=float(hotel_info.get("latitude", 0.0)),
                        lng=float(hotel_info.get("longitude", 0.0)),
                        address=str(hotel_info.get("address", "")),
                        price_total=retail_total,
                        price_currency=currency,
                        rating=float(hotel_info["rating"]) if hotel_info.get("rating") else None,
                        review_count=hotel_info.get("reviewCount"),
                        images=[img for img in (hotel_info.get("hotelImages") or []) if isinstance(img, str)][:6],
                        book_token=book_token,
                        extras={
                            "wholesale_total": wholesale_total,
                            "cancellation": rate.get("cancellationPolicies", {}),
                        },
                    )
                )
            except (KeyError, ValueError, TypeError) as e:
                _log.debug("liteapi: skipping malformed hotel entry: %s", e)
                continue
        return properties

    async def quote(self, prop: Property) -> Quote:
        data = await self._post(
            "/v3.0/rates/prebook",
            {"offerId": prop.book_token, "usePaymentSdk": False},
        )
        body = data.get("data") or {}
        retail = body.get("price") or body.get("retailRate") or {}
        amount = float(retail.get("amount", prop.price_total))
        # Apply our markup on top of the prebook wholesale price (re-quote
        # because prices may have shifted between search and prebook).
        wholesale = float(body.get("wholesalePrice", {}).get("amount", amount))
        marked_up = self._apply_markup(wholesale) if wholesale != amount else amount
        return Quote(
            provider_id=self.id,
            property_external_id=prop.external_id,
            quote_id=str(body.get("prebookId", "")),
            price_total=marked_up,
            price_currency=str(retail.get("currency", prop.price_currency)),
            cancellation_policy=str(
                body.get("cancellationPolicies", {}).get("description", "Standard cancellation policy applies.")
            ),
            expires_at_iso=str(body.get("expiresAt", "")),
            book_token=str(body.get("prebookId", "")),
        )

    async def book(self, request: BookingRequest) -> BookingResult:
        payload = {
            "prebookId": request.book_token,
            "holder": {
                "firstName": request.guest_first_name,
                "lastName": request.guest_last_name,
                "email": request.guest_email,
            },
            "guests": [
                {
                    "occupancyNumber": 1,
                    "firstName": request.guest_first_name,
                    "lastName": request.guest_last_name,
                    "email": request.guest_email,
                }
            ],
            "specialRequests": request.special_requests or "",
            "payment": {"method": "ACC_CREDIT_CARD", "useExternalCheckout": True},
        }
        data = await self._post("/v3.0/rates/book", payload)
        body = data.get("data") or {}
        checkout_url = body.get("paymentUrl") or body.get("checkoutUrl")
        confirmation_id = body.get("bookingId") or body.get("confirmationNumber")
        price = body.get("price") or {}
        amount = float(price.get("amount", 0.0))
        currency = str(price.get("currency", "GBP"))
        wholesale = float((body.get("wholesalePrice") or {}).get("amount", amount))
        return BookingResult(
            provider_id=self.id,
            success=bool(checkout_url or confirmation_id),
            checkout_url=checkout_url,
            confirmation_id=str(confirmation_id) if confirmation_id else None,
            price_total=amount,
            price_currency=currency,
            commission_estimate=self._commission_estimate(amount, wholesale),
            error=None,
        )
