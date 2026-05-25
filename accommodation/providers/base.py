"""
Provider interface every accommodation source implements.

Implementations: liteapi.py (Phase 1), booking_com.py (Phase 3),
homestay.py (Phase 3), expedia_rapid.py (Phase 3), apify_airbnb.py
(Phase 2, search-only — `book()` raises).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from accommodation.models import (
    BookingRequest,
    BookingResult,
    Property,
    Quote,
    SearchQuery,
)


class ProviderError(Exception):
    """Provider call failed — network, auth, validation, sold-out, etc."""


class Provider(ABC):
    """Abstract base. Concrete providers are stateless and accept an httpx
    client + config at __init__ so tests can inject mocks.

    `id` is the short stable identifier used in `Property.provider_id` and
    surfaces in env-var names (`{ID}_KEY`, `{ID}_AFFILIATE_ID`).
    """

    id: str

    @property
    @abstractmethod
    def can_book(self) -> bool:
        """False for read-only providers like the Apify Airbnb scraper."""

    @abstractmethod
    async def search(self, query: SearchQuery, limit: int = 20) -> list[Property]:
        """Return up to `limit` properties matching the query, sorted by the
        provider's relevance/price blend. Raises ProviderError on failure."""

    @abstractmethod
    async def quote(self, prop: Property) -> Quote:
        """Lock in a price + cancellation policy for `prop`. Quotes expire —
        never cache the result."""

    @abstractmethod
    async def book(self, request: BookingRequest) -> BookingResult:
        """Finalize a booking. For LiteAPI this returns either a
        `checkout_url` for hosted-payment-page handoff or a `confirmation_id`
        if the provider doesn't gate on payment. Raises ProviderError on
        anything we can't recover from."""

    async def aclose(self) -> None:
        """Optional cleanup. Default no-op; concrete providers close httpx
        clients here."""
