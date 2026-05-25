"""
Data models for accommodation search, quoting, and booking.

Uses stdlib dataclasses so the module has no Pydantic dependency. Conversion
to/from JSON is explicit at provider boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class SearchQuery:
    """User intent for an accommodation search.

    `location` is a free-text city/region/landmark string (e.g. "Lisbon",
    "Camden Town", "near Heathrow"). Provider implementations resolve it via
    their own geocoding.
    """

    location: str
    check_in: date
    check_out: date
    guests: int = 2
    rooms: int = 1
    currency: str = "GBP"
    preferred_providers: list[str] = field(default_factory=list)

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days


@dataclass
class Property:
    """A normalized property across all providers.

    `provider_id` + `external_id` form the unique key. `book_token` is an
    opaque blob the provider needs to lock in a rate; treat as a write-only
    cookie.
    """

    provider_id: str
    external_id: str
    name: str
    lat: float
    lng: float
    address: str
    price_total: float
    price_currency: str
    rating: float | None
    review_count: int | None
    images: list[str]
    book_token: str
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> tuple[str, float, float, str]:
        return (
            self.provider_id,
            round(self.lat, 4),
            round(self.lng, 4),
            self.name.strip().lower(),
        )


@dataclass
class Quote:
    """A locked-in price quote from the provider. Has a TTL — never cache.

    `cancellation_policy` is provider-formatted text the agent reads aloud
    before confirming the booking.
    """

    provider_id: str
    property_external_id: str
    quote_id: str
    price_total: float
    price_currency: str
    cancellation_policy: str
    expires_at_iso: str
    book_token: str


@dataclass
class BookingRequest:
    """What the user supplies (or the agent collects) to finalize a booking.

    Guest data lives here ONLY for the duration of the booking call. Never
    persisted. See `brain/Accommodation Booking — PCI & Payment Handoff` for
    the PCI design — card data NEVER appears in this dataclass.
    """

    quote_id: str
    book_token: str
    guest_first_name: str
    guest_last_name: str
    guest_email: str
    special_requests: str = ""


@dataclass
class BookingResult:
    """Returned to the voice agent after `service.book(...)`.

    Either `checkout_url` is set (user must pay on their phone) OR
    `confirmation_id` is set (booking confirmed synchronously, no payment
    needed because LiteAPI charges on confirmation webhook).

    `commission_estimate` is the affiliate cut we expect — for reconciliation
    only, never shown to the user.
    """

    provider_id: str
    success: bool
    checkout_url: str | None
    confirmation_id: str | None
    price_total: float
    price_currency: str
    commission_estimate: float
    error: str | None = None
