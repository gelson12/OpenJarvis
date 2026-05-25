"""
Multi-provider fan-out + dedup.

Phase 1 has one provider (LiteAPI) so dedup is a no-op. The interface is built
to match Phase 3 when Booking.com + Vrbo + Homestay light up and the same
property (especially Sonder) will appear via multiple feeds.

Dedup key per `Property.dedup_key` already covers (provider_id, lat₄, lng₄,
normalized_name). For cross-provider merge we additionally fuzzy-match on
stdlib SequenceMatcher ratio > 0.80 (tuned empirically: handles "Marriott
London" vs "Marriott London Hotel" while keeping distinct buildings at the
same lat/lng separate). Swap in rapidfuzz token-set-ratio later if accuracy
matters more.
"""

from __future__ import annotations

import asyncio
import logging
from difflib import SequenceMatcher

from accommodation.models import Property, SearchQuery
from accommodation.providers.base import Provider, ProviderError

_log = logging.getLogger("accommodation.aggregator")


def _name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()


def _group_key(p: Property) -> tuple[float, float]:
    """Cross-provider grouping by location alone — name fuzz applied later."""
    return (round(p.lat, 4), round(p.lng, 4))


def dedup(properties: list[Property]) -> list[Property]:
    """Keep the cheapest property when multiple providers list the same lat/lng
    with name similarity > 0.80."""
    by_loc: dict[tuple[float, float], list[Property]] = {}
    for p in properties:
        by_loc.setdefault(_group_key(p), []).append(p)
    out: list[Property] = []
    for group in by_loc.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        # Within the same lat/lng bucket, merge entries with similar names;
        # keep the cheapest of each merged cluster.
        clusters: list[list[Property]] = []
        for prop in sorted(group, key=lambda p: p.price_total):
            placed = False
            for cluster in clusters:
                if _name_similarity(cluster[0].name, prop.name) > 0.80:
                    cluster.append(prop)
                    placed = True
                    break
            if not placed:
                clusters.append([prop])
        out.extend(cluster[0] for cluster in clusters)
    return out


class Aggregator:
    """Fan-out search across the registered providers in parallel.
    Single-provider failures are logged and skipped, never propagated — a
    booking-search shouldn't crash because one supplier had a bad day.
    """

    def __init__(self, providers: list[Provider]):
        if not providers:
            raise ValueError("Aggregator needs at least one provider")
        self.providers = providers

    async def search(self, query: SearchQuery, limit_per_provider: int = 20) -> list[Property]:
        preferred = set(query.preferred_providers)
        active = (
            [p for p in self.providers if p.id in preferred]
            if preferred
            else self.providers
        )
        if not active:
            active = self.providers
        results = await asyncio.gather(
            *(self._safe_search(p, query, limit_per_provider) for p in active),
            return_exceptions=False,
        )
        combined = [item for batch in results for item in batch]
        return dedup(combined)

    async def _safe_search(
        self, provider: Provider, query: SearchQuery, limit: int
    ) -> list[Property]:
        try:
            return await provider.search(query, limit=limit)
        except ProviderError as e:
            _log.warning("provider %s search failed: %s", provider.id, e)
            return []
        except Exception as e:  # noqa: BLE001
            _log.exception("provider %s crashed: %s", provider.id, e)
            return []

    def get(self, provider_id: str) -> Provider:
        for p in self.providers:
            if p.id == provider_id:
                return p
        raise KeyError(provider_id)

    async def aclose(self) -> None:
        for p in self.providers:
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                pass
