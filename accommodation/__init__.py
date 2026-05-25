"""
Accommodation booking module — voice-driven search, availability, and booking
across multiple OTA/aggregator providers.

Phase 1 ships with LiteAPI. Booking.com Demand API, Homestay affiliate, and
Expedia Rapid plug in via `providers/` once partnership applications approve.

See `brain/Accommodation Booking — Implementation Plan.md` in the vault for
the full design.
"""

from accommodation.models import (
    BookingRequest,
    BookingResult,
    Property,
    Quote,
    SearchQuery,
)
from accommodation.service import AccommodationService

__all__ = [
    "AccommodationService",
    "BookingRequest",
    "BookingResult",
    "Property",
    "Quote",
    "SearchQuery",
]
