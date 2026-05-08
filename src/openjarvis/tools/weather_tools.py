"""Weather tools — free via Open-Meteo (no API key, no rate-limit drama).

Open-Meteo's API is unauthenticated, polite, and returns clean JSON.
We use it for both current conditions and 7-day forecasts. The agent
calls these with a (lat, lon) pair OR a place name; place names get
resolved to coordinates via the geocode_search tool first.

Why Open-Meteo
--------------
- Free, no API key, no signup
- Hourly + daily forecasts, all major weather metrics
- Stable URL schema; weather codes match WMO standard
- No "fair use" surprises that bite OpenWeatherMap free-tier users

Reference: https://open-meteo.com/en/docs
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


_OPEN_METEO_BASE = "https://api.open-meteo.com/v1"
_TIMEOUT = 8.0


# WMO weather code -> human label. Open-Meteo uses these codes for both
# current and forecast. Compact map for the most common values; the
# agent can interpret them or pass through verbatim.
_WMO_LABELS: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _label(code: Any) -> str:
    try:
        return _WMO_LABELS.get(int(code), f"WMO code {code}")
    except (TypeError, ValueError):
        return str(code)


def _ok(name: str, payload: Any) -> ToolResult:
    if not isinstance(payload, str):
        try:
            payload = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            payload = str(payload)
    return ToolResult(tool_name=name, content=payload or "(no content)", success=True)


def _err(name: str, exc: Exception) -> ToolResult:
    return ToolResult(tool_name=name, content=f"weather error: {exc}", success=False)


@ToolRegistry.register("weather_current")
class WeatherCurrentTool(BaseTool):
    """Current weather at a (lat, lon). Uses Open-Meteo (free, no key)."""

    tool_id = "weather_current"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="weather_current",
            description=(
                "Current weather at a location given as latitude + "
                "longitude. Returns temperature, apparent temperature, "
                "humidity, wind, and a human-readable condition label. "
                "If you only have a place name, call geocode_search FIRST "
                "to get coordinates, then pass them here."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "latitude": {
                        "type": "number",
                        "description": "Decimal degrees, e.g. 51.5074 for London.",
                    },
                    "longitude": {
                        "type": "number",
                        "description": "Decimal degrees, e.g. -0.1278 for London.",
                    },
                    "units": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "Temperature units (default celsius).",
                    },
                },
                "required": ["latitude", "longitude"],
            },
            category="weather",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            lat = float(params["latitude"])
            lon = float(params["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            return _err(self.spec.name, ValueError(f"need numeric latitude+longitude: {exc}"))
        units = params.get("units", "celsius")
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                resp = c.get(
                    f"{_OPEN_METEO_BASE}/forecast",
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "current": (
                            "temperature_2m,apparent_temperature,relative_humidity_2m,"
                            "weather_code,wind_speed_10m,wind_direction_10m,is_day"
                        ),
                        "temperature_unit": units,
                        "wind_speed_unit": "kmh",
                        "timezone": "auto",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            return _err(self.spec.name, exc)

        cur = data.get("current") or {}
        out = {
            "latitude": data.get("latitude"),
            "longitude": data.get("longitude"),
            "timezone": data.get("timezone"),
            "time": cur.get("time"),
            "temperature": cur.get("temperature_2m"),
            "feels_like": cur.get("apparent_temperature"),
            "humidity_percent": cur.get("relative_humidity_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "wind_direction_deg": cur.get("wind_direction_10m"),
            "condition": _label(cur.get("weather_code")),
            "is_day": bool(cur.get("is_day")),
            "units": units,
        }
        return _ok(self.spec.name, out)


@ToolRegistry.register("weather_forecast")
class WeatherForecastTool(BaseTool):
    """7-day weather forecast at a (lat, lon)."""

    tool_id = "weather_forecast"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="weather_forecast",
            description=(
                "Multi-day weather forecast (default 7 days). Returns "
                "daily min/max temperature, precipitation, weather "
                "condition. Use after geocode_search if you only have "
                "a place name. For 'is it going to rain tomorrow?' "
                "style questions, this is the right tool."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "days": {
                        "type": "integer",
                        "description": "Number of forecast days (1-16, default 7).",
                    },
                    "units": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                    },
                },
                "required": ["latitude", "longitude"],
            },
            category="weather",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            lat = float(params["latitude"])
            lon = float(params["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            return _err(self.spec.name, ValueError(f"need numeric latitude+longitude: {exc}"))
        days = max(1, min(16, int(params.get("days", 7))))
        units = params.get("units", "celsius")
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                resp = c.get(
                    f"{_OPEN_METEO_BASE}/forecast",
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "daily": (
                            "weather_code,temperature_2m_max,temperature_2m_min,"
                            "precipitation_sum,precipitation_probability_max,"
                            "wind_speed_10m_max,sunrise,sunset"
                        ),
                        "temperature_unit": units,
                        "wind_speed_unit": "kmh",
                        "timezone": "auto",
                        "forecast_days": days,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            return _err(self.spec.name, exc)

        daily = data.get("daily") or {}
        times = daily.get("time", [])
        days_out = []
        for i, date in enumerate(times):
            days_out.append(
                {
                    "date": date,
                    "min_temp": (daily.get("temperature_2m_min") or [])[i] if i < len(daily.get("temperature_2m_min") or []) else None,
                    "max_temp": (daily.get("temperature_2m_max") or [])[i] if i < len(daily.get("temperature_2m_max") or []) else None,
                    "precipitation_mm": (daily.get("precipitation_sum") or [])[i] if i < len(daily.get("precipitation_sum") or []) else None,
                    "precipitation_chance_percent": (daily.get("precipitation_probability_max") or [])[i] if i < len(daily.get("precipitation_probability_max") or []) else None,
                    "wind_kmh_max": (daily.get("wind_speed_10m_max") or [])[i] if i < len(daily.get("wind_speed_10m_max") or []) else None,
                    "condition": _label((daily.get("weather_code") or [None])[i] if i < len(daily.get("weather_code") or []) else None),
                    "sunrise": (daily.get("sunrise") or [])[i] if i < len(daily.get("sunrise") or []) else None,
                    "sunset": (daily.get("sunset") or [])[i] if i < len(daily.get("sunset") or []) else None,
                }
            )
        out = {
            "latitude": data.get("latitude"),
            "longitude": data.get("longitude"),
            "timezone": data.get("timezone"),
            "units": units,
            "days": days_out,
        }
        return _ok(self.spec.name, out)


__all__ = ["WeatherCurrentTool", "WeatherForecastTool"]
