"""Smoke tests for the LiteAPI provider against mocked HTTP responses."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from accommodation.models import BookingRequest, Property, SearchQuery
from accommodation.providers.liteapi import LiteApiProvider


def _mock_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(
        transport=transport,
        headers={"X-API-Key": "test", "Accept": "application/json"},
    )


@pytest.mark.asyncio
async def test_search_returns_properties_with_markup():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3.0/hotels/rates"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "hotel": {
                            "id": "h1",
                            "name": "Test Hotel",
                            "latitude": 38.7,
                            "longitude": -9.1,
                            "address": "1 Test St",
                            "rating": 4.2,
                            "reviewCount": 100,
                            "hotelImages": ["http://example.com/a.jpg"],
                        },
                        "roomTypes": [
                            {
                                "offerId": "offer-1",
                                "rates": [
                                    {
                                        "rateId": "r1",
                                        "retailRate": {
                                            "total": [{"amount": 200.0, "currency": "GBP"}]
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        )

    provider = LiteApiProvider(api_key="test", env="sandbox", markup_pct=10.0, client=_mock_client(handler))
    query = SearchQuery(location="Lisbon", check_in=date(2026, 6, 1), check_out=date(2026, 6, 3))
    results = await provider.search(query)
    assert len(results) == 1
    p = results[0]
    assert p.name == "Test Hotel"
    assert p.price_total == 220.0  # 200 * 1.10
    assert p.book_token == "offer-1"
    assert p.provider_id == "liteapi"
    assert p.extras["wholesale_total"] == 200.0
    await provider.aclose()


@pytest.mark.asyncio
async def test_search_skips_malformed_hotels():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"hotel": {"id": "h1", "name": "Bad"}, "roomTypes": []},
                    {
                        "hotel": {"id": "h2", "name": "Good", "latitude": 0, "longitude": 0},
                        "roomTypes": [
                            {
                                "offerId": "g1",
                                "rates": [
                                    {
                                        "retailRate": {
                                            "total": [{"amount": 50, "currency": "GBP"}]
                                        }
                                    }
                                ],
                            }
                        ],
                    },
                ]
            },
        )

    provider = LiteApiProvider(api_key="test", env="sandbox", markup_pct=0, client=_mock_client(handler))
    query = SearchQuery(location="X", check_in=date(2026, 6, 1), check_out=date(2026, 6, 2))
    results = await provider.search(query)
    assert len(results) == 1
    assert results[0].name == "Good"
    await provider.aclose()


@pytest.mark.asyncio
async def test_book_returns_checkout_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3.0/rates/book"
        return httpx.Response(
            200,
            json={
                "data": {
                    "bookingId": "BK123",
                    "paymentUrl": "https://liteapi.travel/pay/abc",
                    "price": {"amount": 220.0, "currency": "GBP"},
                    "wholesalePrice": {"amount": 200.0, "currency": "GBP"},
                }
            },
        )

    provider = LiteApiProvider(api_key="test", env="sandbox", markup_pct=10.0, client=_mock_client(handler))
    req = BookingRequest(
        quote_id="q1",
        book_token="prebook-1",
        guest_first_name="Alice",
        guest_last_name="Example",
        guest_email="a@example.com",
    )
    result = await provider.book(req)
    assert result.success is True
    assert result.checkout_url == "https://liteapi.travel/pay/abc"
    assert result.confirmation_id == "BK123"
    assert result.commission_estimate == 20.0  # 220 - 200
    await provider.aclose()


@pytest.mark.asyncio
async def test_search_404_raises_provider_error():
    from accommodation.providers.base import ProviderError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    provider = LiteApiProvider(api_key="test", env="sandbox", client=_mock_client(handler))
    query = SearchQuery(location="Nowhere", check_in=date(2026, 6, 1), check_out=date(2026, 6, 2))
    with pytest.raises(ProviderError):
        await provider.search(query)
    await provider.aclose()


def test_property_dedup_key():
    p1 = Property(
        provider_id="liteapi", external_id="x", name="Marriott  ", lat=38.71234, lng=-9.13456,
        address="", price_total=100, price_currency="GBP", rating=None, review_count=None,
        images=[], book_token="t",
    )
    assert p1.dedup_key == ("liteapi", 38.7123, -9.1346, "marriott")


def test_search_query_nights():
    q = SearchQuery(location="X", check_in=date(2026, 6, 1), check_out=date(2026, 6, 4))
    assert q.nights == 3
