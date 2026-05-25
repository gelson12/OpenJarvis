"""Tests for the dedup logic in the aggregator."""

from __future__ import annotations

from accommodation.aggregator import dedup
from accommodation.models import Property


def _p(provider: str, name: str, lat: float, lng: float, price: float) -> Property:
    return Property(
        provider_id=provider, external_id=f"{provider}-{name}", name=name,
        lat=lat, lng=lng, address="", price_total=price, price_currency="GBP",
        rating=None, review_count=None, images=[], book_token="t",
    )


def test_dedup_keeps_unique_locations():
    properties = [
        _p("liteapi", "A", 38.7, -9.1, 100),
        _p("liteapi", "B", 51.5, -0.1, 200),
    ]
    out = dedup(properties)
    assert len(out) == 2


def test_dedup_merges_same_location_similar_names_keep_cheaper():
    properties = [
        _p("liteapi", "Marriott London", 51.5, -0.1, 250),
        _p("booking", "Marriott London Hotel", 51.5, -0.1, 200),
    ]
    out = dedup(properties)
    assert len(out) == 1
    assert out[0].price_total == 200
    assert out[0].provider_id == "booking"


def test_dedup_keeps_different_buildings_same_address():
    """Adjacent properties (same building) with very different names are
    preserved — they're really different listings (e.g. apartments in the
    same block)."""
    properties = [
        _p("airbnb", "Cozy studio Camden", 51.541, -0.142, 80),
        _p("airbnb", "Luxury 2-bed Camden", 51.541, -0.142, 220),
    ]
    out = dedup(properties)
    assert len(out) == 2
