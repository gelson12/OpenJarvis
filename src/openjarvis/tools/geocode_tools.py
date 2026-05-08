"""Geocoding tools — free via Nominatim (OpenStreetMap, no API key).

Two tools:
  - geocode_search: place name -> (lat, lon) + bounding box + display name
  - geocode_reverse: (lat, lon) -> address components (street, city, country)

Why Nominatim
-------------
- Free, no API key, no signup
- Solid coverage for cities, countries, well-known POIs
- Open data (OpenStreetMap)

Limitations
-----------
- Lower coverage for individual businesses than Google Places. If you
  need "find Joe's Coffee Shop on 5th Ave with hours and ratings",
  switch to Google Places (set GOOGLE_MAPS_API_KEY and we'll add a
  google_places_* tool).
- Nominatim usage policy requires a polite User-Agent and ≤1 req/sec.
  We send a sane User-Agent and rely on agent turn-taking for the rate
  limit (the agent issues one geocode per user turn typically).

Reference: https://nominatim.org/release-docs/latest/api/Search/
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


_NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
_USER_AGENT = "OpenJarvis/1.0 (https://github.com/gelson12/OpenJarvis)"
_TIMEOUT = 8.0


def _ok(name: str, payload: Any) -> ToolResult:
    if not isinstance(payload, str):
        try:
            payload = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            payload = str(payload)
    return ToolResult(tool_name=name, content=payload or "(no content)", success=True)


def _err(name: str, exc: Exception) -> ToolResult:
    return ToolResult(tool_name=name, content=f"geocode error: {exc}", success=False)


@ToolRegistry.register("geocode_search")
class GeocodeSearchTool(BaseTool):
    """Resolve a place name to coordinates. Use BEFORE weather tools."""

    tool_id = "geocode_search"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="geocode_search",
            description=(
                "Convert a place name (city, address, landmark) to "
                "latitude+longitude. Returns top matches with display "
                "names + bounding boxes. Use this BEFORE weather_current "
                "or weather_forecast when you only have a place name. "
                "Examples: 'London', 'Eiffel Tower', '221b Baker Street "
                "London'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text place name or address.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 5, max 10).",
                    },
                },
                "required": ["query"],
            },
            category="location",
        )

    def execute(self, **params: Any) -> ToolResult:
        query = (params.get("query") or "").strip()
        if not query:
            return _err(self.spec.name, ValueError("query is required"))
        limit = max(1, min(10, int(params.get("limit", 5))))
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                resp = c.get(
                    f"{_NOMINATIM_BASE}/search",
                    params={"q": query, "format": "json", "limit": limit},
                    headers={"User-Agent": _USER_AGENT},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            return _err(self.spec.name, exc)

        results = []
        for hit in data:
            results.append(
                {
                    "display_name": hit.get("display_name"),
                    "latitude": float(hit.get("lat")) if hit.get("lat") else None,
                    "longitude": float(hit.get("lon")) if hit.get("lon") else None,
                    "type": hit.get("type"),
                    "class": hit.get("class"),
                    "importance": hit.get("importance"),
                    "boundingbox": hit.get("boundingbox"),
                }
            )
        if not results:
            return _ok(
                self.spec.name,
                {"query": query, "results": [], "note": "no matches found"},
            )
        return _ok(
            self.spec.name,
            {"query": query, "results": results},
        )


@ToolRegistry.register("geocode_reverse")
class GeocodeReverseTool(BaseTool):
    """(lat, lon) -> human-readable address."""

    tool_id = "geocode_reverse"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="geocode_reverse",
            description=(
                "Convert latitude+longitude to a human-readable address. "
                "Returns street, city, country, postcode where available. "
                "Inverse of geocode_search."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                },
                "required": ["latitude", "longitude"],
            },
            category="location",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            lat = float(params["latitude"])
            lon = float(params["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            return _err(
                self.spec.name,
                ValueError(f"need numeric latitude+longitude: {exc}"),
            )
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                resp = c.get(
                    f"{_NOMINATIM_BASE}/reverse",
                    params={"lat": lat, "lon": lon, "format": "json"},
                    headers={"User-Agent": _USER_AGENT},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            return _err(self.spec.name, exc)

        addr = data.get("address") or {}
        out = {
            "display_name": data.get("display_name"),
            "latitude": float(data.get("lat")) if data.get("lat") else lat,
            "longitude": float(data.get("lon")) if data.get("lon") else lon,
            "house_number": addr.get("house_number"),
            "road": addr.get("road"),
            "neighbourhood": addr.get("neighbourhood") or addr.get("suburb"),
            "city": addr.get("city") or addr.get("town") or addr.get("village"),
            "county": addr.get("county"),
            "state": addr.get("state"),
            "postcode": addr.get("postcode"),
            "country": addr.get("country"),
            "country_code": addr.get("country_code"),
        }
        return _ok(self.spec.name, out)


__all__ = ["GeocodeSearchTool", "GeocodeReverseTool"]
