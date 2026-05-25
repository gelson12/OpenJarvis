"""Smoke tests for the Apify Airbnb provider against mocked HTTP responses."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from accommodation.models import BookingRequest, SearchQuery
from accommodation.providers.apify_airbnb import ApifyAirbnbProvider


def _mock_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


@pytest.mark.asyncio
async def test_search_normalizes_airbnb_listings():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "tri_angle~airbnb-scraper" in request.url.path
        assert "token=test" in request.url.query.decode() if isinstance(request.url.query, bytes) else "token=test" in str(request.url.query)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "12345",
                    "name": "Cozy studio in Camden",
                    "latitude": 51.541,
                    "longitude": -0.142,
                    "address": "Camden, London",
                    "pricing": {"total": 240.0, "currency": "GBP"},
                    "rating": 4.85,
                    "reviewsCount": 73,
                    "images": ["https://a0.muscache.com/im/x.jpg"],
                    "url": "https://www.airbnb.com/rooms/12345",
                }
            ],
        )

    provider = ApifyAirbnbProvider(token="test", client=_mock_client(handler))
    query = SearchQuery(
        location="London", check_in=date(2026, 6, 1), check_out=date(2026, 6, 3)
    )
    results = await provider.search(query)
    assert len(results) == 1
    p = results[0]
    assert p.name == "Cozy studio in Camden"
    assert p.price_total == 240.0
    assert p.price_currency == "GBP"
    assert p.book_token == "https://www.airbnb.com/rooms/12345"
    assert p.provider_id == "apify_airbnb"
    assert p.extras.get("is_redirect_provider") is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_search_uses_nightly_rate_when_no_total():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "999",
                    "name": "Loft",
                    "pricing": {"rate": 80.0, "currency": "GBP"},
                    "url": "https://www.airbnb.com/rooms/999",
                }
            ],
        )

    provider = ApifyAirbnbProvider(token="test", client=_mock_client(handler))
    query = SearchQuery(
        location="X", check_in=date(2026, 6, 1), check_out=date(2026, 6, 4)
    )  # 3 nights
    results = await provider.search(query)
    assert len(results) == 1
    assert results[0].price_total == 240.0  # 80 × 3 nights
    await provider.aclose()


@pytest.mark.asyncio
async def test_search_skips_priceless_listings():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "1", "name": "No price"},
                {
                    "id": "2",
                    "name": "Has price",
                    "pricing": {"total": 100.0, "currency": "GBP"},
                    "url": "https://www.airbnb.com/rooms/2",
                },
            ],
        )

    provider = ApifyAirbnbProvider(token="test", client=_mock_client(handler))
    query = SearchQuery(
        location="X", check_in=date(2026, 6, 1), check_out=date(2026, 6, 2)
    )
    results = await provider.search(query)
    assert len(results) == 1
    assert results[0].name == "Has price"
    await provider.aclose()


@pytest.mark.asyncio
async def test_book_returns_airbnb_url_as_checkout():
    provider = ApifyAirbnbProvider(token="test", client=httpx.AsyncClient())
    req = BookingRequest(
        quote_id="https://www.airbnb.com/rooms/777",
        book_token="https://www.airbnb.com/rooms/777",
        guest_first_name="A",
        guest_last_name="B",
        guest_email="a@b.c",
    )
    result = await provider.book(req)
    assert result.success is True
    assert result.checkout_url == "https://www.airbnb.com/rooms/777"
    assert result.confirmation_id is None
    assert result.commission_estimate == 0.0
    await provider.aclose()


def test_can_book_is_false():
    """Sanity: Apify Airbnb is read-only — the handler relies on this."""
    # Don't instantiate to avoid the env check; introspect the class attr.
    assert ApifyAirbnbProvider.can_book.fget(
        type("X", (), {"can_book": ApifyAirbnbProvider.can_book})()
    ) is False
