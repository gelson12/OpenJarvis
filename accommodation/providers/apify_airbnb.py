"""
Apify Airbnb provider — read-only discovery, no booking.

Airbnb has no public booking API for third parties (the Preferred Software
Partner program is invite-only and not accepting unsolicited applicants).
This provider scrapes public Airbnb search results via an Apify actor so
voice agents can SHOW Airbnb listings, then hand the user off to airbnb.com
to complete the booking themselves.

Trade-offs to keep in mind:
- Apify actors are SLOW (typical 30-90s for a search). We use the
  `run-sync-get-dataset-items` endpoint so results arrive in one HTTP call,
  but the voice flow needs a longer timeout (default 60s here, tuned by
  `APIFY_AIRBNB_TIMEOUT_S`).
- Scraping public data is generally legal post-hiQ-vs-LinkedIn but may
  violate Airbnb's ToS. Treat this provider as disposable Phase 2 — don't
  build affiliate UX assuming it survives a C&D.
- `book()` and `quote()` are NOT supported — they return a BookingResult
  whose `checkout_url` points at the Airbnb listing URL. The user completes
  the booking on airbnb.com; we never see card data.

Default actor: `tri_angle/airbnb-scraper`. Override with
`APIFY_AIRBNB_ACTOR=<owner~name>` env var (note: API path uses `~`, not `/`).
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

_log = logging.getLogger("accommodation.apify_airbnb")

_DEFAULT_ACTOR = "tri_angle~airbnb-scraper"
_DEFAULT_TIMEOUT_S = 60.0


class ApifyAirbnbProvider(Provider):
    id = "apify_airbnb"

    def __init__(
        self,
        token: str | None = None,
        actor: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.token = token or os.environ.get("APIFY_TOKEN", "").strip()
        if not self.token:
            raise ProviderError("APIFY_TOKEN not configured")
        self.actor = (actor or os.environ.get("APIFY_AIRBNB_ACTOR") or _DEFAULT_ACTOR).strip()
        try:
            self.timeout = float(
                timeout
                if timeout is not None
                else os.environ.get("APIFY_AIRBNB_TIMEOUT_S", _DEFAULT_TIMEOUT_S)
            )
        except ValueError:
            self.timeout = _DEFAULT_TIMEOUT_S
        self._client = client or httpx.AsyncClient(timeout=self.timeout)
        self._owns_client = client is None

    @property
    def can_book(self) -> bool:
        # Airbnb doesn't expose a third-party booking API; we redirect.
        return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(self, query: SearchQuery, limit: int = 20) -> list[Property]:
        url = (
            f"https://api.apify.com/v2/acts/{self.actor}"
            f"/run-sync-get-dataset-items?token={self.token}"
        )
        payload = {
            "locationQueries": [query.location],
            "checkIn": query.check_in.isoformat(),
            "checkOut": query.check_out.isoformat(),
            "adults": max(1, query.guests),
            "currency": query.currency,
            "maxItems": limit,
        }
        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            _log.warning(
                "apify-airbnb %s -> %s: %s",
                self.actor, e.response.status_code, e.response.text[:300],
            )
            raise ProviderError(
                f"apify-airbnb {self.actor} returned {e.response.status_code}"
            ) from e
        except httpx.HTTPError as e:
            _log.warning("apify-airbnb network error: %s", e)
            raise ProviderError("apify-airbnb network error") from e
        try:
            items = resp.json()
        except ValueError as e:
            raise ProviderError("apify-airbnb returned non-JSON body") from e
        if not isinstance(items, list):
            # Some actor versions wrap results — handle both shapes.
            items = items.get("data") if isinstance(items, dict) else []
            if not isinstance(items, list):
                items = []
        return [p for p in (self._to_property(query, item) for item in items[:limit]) if p is not None]

    def _to_property(self, query: SearchQuery, item: dict[str, Any]) -> Property | None:
        """Convert one Apify-Airbnb dataset entry into our normalized Property.
        Defensive on field names — scrapers rename fields between versions.
        """
        try:
            external_id = str(
                item.get("id")
                or item.get("listingId")
                or item.get("roomId")
                or ""
            )
            name = str(
                item.get("name")
                or item.get("title")
                or item.get("listingName")
                or "Airbnb listing"
            )
            # Coordinates — try several common shapes.
            lat = float(
                item.get("latitude")
                or (item.get("location") or {}).get("lat")
                or (item.get("coordinates") or {}).get("latitude")
                or 0.0
            )
            lng = float(
                item.get("longitude")
                or (item.get("location") or {}).get("lng")
                or (item.get("coordinates") or {}).get("longitude")
                or 0.0
            )
            address = str(
                item.get("address")
                or item.get("location_name")
                or (item.get("location") or {}).get("name")
                or query.location
            )
            # Price — totals come back in many shapes; prefer a per-night
            # number multiplied by nights, else any "total" field.
            price_total = 0.0
            currency = query.currency
            pricing = item.get("pricing") or item.get("price") or {}
            if isinstance(pricing, dict):
                price_total = float(
                    pricing.get("total")
                    or pricing.get("totalPrice")
                    or pricing.get("amount")
                    or 0.0
                )
                currency = str(pricing.get("currency") or query.currency)
                if price_total <= 0:
                    rate = float(pricing.get("rate") or pricing.get("nightlyRate") or 0.0)
                    if rate > 0:
                        price_total = rate * max(1, query.nights)
            elif isinstance(pricing, (int, float)):
                price_total = float(pricing) * max(1, query.nights)
            if price_total <= 0:
                # Without a price we can't display it usefully — skip.
                return None
            rating_raw = item.get("rating") or item.get("starRating") or item.get("avgRating")
            rating = float(rating_raw) if rating_raw is not None else None
            review_count = item.get("reviewsCount") or item.get("reviewCount") or item.get("numberOfReviews")
            review_count = int(review_count) if review_count is not None else None
            images_raw = item.get("images") or item.get("photos") or item.get("pictures") or []
            images: list[str] = []
            for img in images_raw:
                if isinstance(img, str):
                    images.append(img)
                elif isinstance(img, dict):
                    src = img.get("url") or img.get("src") or img.get("picture")
                    if isinstance(src, str):
                        images.append(src)
                if len(images) >= 6:
                    break
            # The "book_token" for Airbnb is the listing URL itself — when
            # the user picks this property we redirect them to Airbnb.
            book_url = (
                item.get("url")
                or item.get("listingUrl")
                or item.get("link")
                or (f"https://www.airbnb.com/rooms/{external_id}" if external_id else "")
            )
            if not book_url:
                return None
            return Property(
                provider_id=self.id,
                external_id=external_id or book_url,
                name=name,
                lat=lat,
                lng=lng,
                address=address,
                price_total=round(price_total, 2),
                price_currency=currency,
                rating=rating,
                review_count=review_count,
                images=images,
                book_token=str(book_url),
                extras={"is_redirect_provider": True},
            )
        except (KeyError, ValueError, TypeError) as e:
            _log.debug("apify-airbnb: skipping malformed item: %s", e)
            return None

    async def quote(self, prop: Property) -> Quote:
        # No API quote — return the search-time price as the quote so the
        # voice agent can still speak a number. The user will see the real
        # price on Airbnb itself when they tap the redirect link.
        return Quote(
            provider_id=self.id,
            property_external_id=prop.external_id,
            quote_id=prop.book_token,  # the Airbnb URL doubles as the quote id
            price_total=prop.price_total,
            price_currency=prop.price_currency,
            cancellation_policy=(
                "Cancellation policy varies by host — see the listing on Airbnb "
                "for full details."
            ),
            expires_at_iso="",
            book_token=prop.book_token,
        )

    async def book(self, request: BookingRequest) -> BookingResult:
        # We don't actually book — we redirect. `book_token` is the Airbnb
        # listing URL set during search; we surface it back as the checkout
        # URL so the voice handler's existing Telegram-magic-link flow sends
        # the user to airbnb.com to finish.
        return BookingResult(
            provider_id=self.id,
            success=True,
            checkout_url=request.book_token,
            confirmation_id=None,
            price_total=0.0,  # unknown until user finishes on Airbnb
            price_currency="GBP",
            commission_estimate=0.0,
            error=None,
        )
