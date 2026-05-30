"""Tests for the dedup logic and provider-health signal in the aggregator."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from accommodation.aggregator import Aggregator, dedup
from accommodation.models import Property, SearchQuery
from accommodation.providers.base import ProviderError


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


# ── Provider-health signal: outage must NOT read as "no listings" ────────


class _FakeProvider:
    def __init__(self, pid: str, *, fail: bool, results=None):
        self.id = pid
        self._fail = fail
        self._results = results or []

    @property
    def can_book(self) -> bool:
        return True

    async def search(self, query, limit: int = 20):
        if self._fail:
            raise ProviderError("Name or service not known")
        return self._results

    async def aclose(self):
        pass


def _query() -> SearchQuery:
    today = date(2026, 6, 1)
    return SearchQuery(location="Lisbon", check_in=today, check_out=today + timedelta(days=2))


@pytest.mark.asyncio
async def test_all_providers_failed_flag_set_on_total_outage():
    agg = Aggregator([_FakeProvider("liteapi", fail=True),
                      _FakeProvider("apify_airbnb", fail=True)])
    out = await agg.search(_query())
    assert out == []
    assert agg.last_all_providers_failed is True  # honest: outage, not empty


@pytest.mark.asyncio
async def test_partial_failure_is_not_total_outage():
    good = _p("liteapi", "Hotel", 38.7, -9.1, 100)
    agg = Aggregator([_FakeProvider("liteapi", fail=False, results=[good]),
                      _FakeProvider("apify_airbnb", fail=True)])
    out = await agg.search(_query())
    assert len(out) == 1
    assert agg.last_all_providers_failed is False


@pytest.mark.asyncio
async def test_genuine_empty_is_not_an_outage():
    agg = Aggregator([_FakeProvider("liteapi", fail=False, results=[])])
    out = await agg.search(_query())
    assert out == []
    assert agg.last_all_providers_failed is False  # market empty, not down
