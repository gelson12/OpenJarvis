"""
AccommodationService — the facade both voice agents call.

Lifecycle: build once at worker startup with `AccommodationService.from_env()`,
hold the reference, call `search/quote/book` per voice turn, close on shutdown.

Search results are cached briefly to absorb the common pattern where the user
asks the same question twice ("show me hotels in Lisbon" → tap a property →
"go back"). Cache is intentionally short (30s) and keyed on the full search
shape; availability and quotes are NEVER cached because rates can move
intra-minute.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from accommodation.aggregator import Aggregator
from accommodation.handoff import TelegramSender
from accommodation.models import (
    BookingRequest,
    BookingResult,
    Property,
    Quote,
    SearchQuery,
)
from accommodation.providers.base import Provider, ProviderError
from accommodation.providers.liteapi import LiteApiProvider

_log = logging.getLogger("accommodation.service")

_SEARCH_CACHE_TTL_S = 30.0


@dataclass
class _CacheEntry:
    expires_at: float
    properties: list[Property]


class AccommodationService:
    def __init__(
        self,
        aggregator: Aggregator,
        telegram: TelegramSender | None = None,
    ):
        self.aggregator = aggregator
        self.telegram = telegram or TelegramSender()
        self._search_cache: dict[tuple, _CacheEntry] = {}

    @classmethod
    def from_env(cls) -> "AccommodationService | None":
        """Build with whatever providers env vars enable. Returns None when no
        providers are configured — callers treat None as "feature disabled"
        and gracefully tell the user."""
        providers: list[Provider] = []
        if os.environ.get("LITEAPI_KEY", "").strip():
            try:
                providers.append(LiteApiProvider())
            except ProviderError as e:
                _log.warning("liteapi disabled: %s", e)
        # Future providers register here.
        if not providers:
            return None
        return cls(aggregator=Aggregator(providers))

    def _cache_key(self, query: SearchQuery) -> tuple:
        return (
            query.location.strip().lower(),
            query.check_in.isoformat(),
            query.check_out.isoformat(),
            query.guests,
            query.rooms,
            query.currency,
            tuple(sorted(query.preferred_providers)),
        )

    async def search(self, query: SearchQuery, limit: int = 20) -> list[Property]:
        key = self._cache_key(query)
        now = time.monotonic()
        cached = self._search_cache.get(key)
        if cached and cached.expires_at > now:
            return cached.properties[:limit]
        properties = await self.aggregator.search(query, limit_per_provider=limit)
        # Sort by price ascending — voice surfaces "cheapest first" by default.
        properties.sort(key=lambda p: p.price_total)
        self._search_cache[key] = _CacheEntry(
            expires_at=now + _SEARCH_CACHE_TTL_S, properties=properties
        )
        # Bound cache size — discard expired entries occasionally.
        if len(self._search_cache) > 64:
            self._search_cache = {
                k: v for k, v in self._search_cache.items() if v.expires_at > now
            }
        return properties[:limit]

    async def quote(self, prop: Property) -> Quote:
        provider = self.aggregator.get(prop.provider_id)
        return await provider.quote(prop)

    async def book(
        self,
        request: BookingRequest,
        property_name: str,
        provider_id: str,
        notify_telegram: bool = True,
    ) -> BookingResult:
        provider = self.aggregator.get(provider_id)
        result = await provider.book(request)
        if result.success and result.checkout_url and notify_telegram:
            sent = await self.telegram.send_checkout_link(
                url=result.checkout_url,
                property_name=property_name,
                price_total=result.price_total,
                currency=result.price_currency,
            )
            if not sent:
                _log.info(
                    "telegram unavailable; caller will publish checkout link to HUD"
                )
        return result

    async def aclose(self) -> None:
        await self.aggregator.aclose()
