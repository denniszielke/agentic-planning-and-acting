"""Independent deterministic weather MCP service for the travel scenario."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP

from .transport_security import transport_security_settings


_CITIES = [
    ("Amsterdam", "NL", 52.37, 4.90, "temperate-maritime", 10, 7),
    ("Athens", "GR", 37.98, 23.73, "mediterranean", 18, 3),
    ("Barcelona", "ES", 41.39, 2.17, "mediterranean", 17, 4),
    ("Berlin", "DE", 52.52, 13.41, "continental", 10, 6),
    ("Boston", "US", 42.36, -71.06, "continental", 11, 7),
    ("Cairo", "EG", 30.04, 31.24, "desert", 23, 1),
    ("Cape Town", "ZA", -33.92, 18.42, "mediterranean", 17, 5),
    ("Chicago", "US", 41.88, -87.63, "continental", 10, 7),
    ("Copenhagen", "DK", 55.68, 12.57, "temperate-maritime", 9, 7),
    ("Denver", "US", 39.74, -104.99, "semi-arid", 11, 4),
    ("Dubai", "AE", 25.20, 55.27, "desert", 28, 1),
    ("Dublin", "IE", 53.35, -6.26, "temperate-maritime", 10, 8),
    ("Edinburgh", "GB", 55.95, -3.19, "temperate-maritime", 9, 8),
    ("Helsinki", "FI", 60.17, 24.94, "continental", 6, 6),
    ("Lisbon", "PT", 38.72, -9.14, "mediterranean", 17, 4),
    ("London", "GB", 51.51, -0.13, "temperate-maritime", 11, 7),
    ("Los Angeles", "US", 34.05, -118.24, "mediterranean", 19, 3),
    ("Madrid", "ES", 40.42, -3.70, "continental-mediterranean", 15, 3),
    ("Miami", "US", 25.76, -80.19, "tropical", 25, 8),
    ("Montreal", "CA", 45.50, -73.57, "continental", 7, 7),
    ("Munich", "DE", 48.14, 11.58, "continental", 9, 7),
    ("New York", "US", 40.71, -74.01, "continental", 13, 7),
    ("Oslo", "NO", 59.91, 10.75, "continental", 7, 6),
    ("Paris", "FR", 48.86, 2.35, "temperate", 12, 6),
    ("Prague", "CZ", 50.08, 14.44, "continental", 9, 6),
    ("Reykjavik", "IS", 64.15, -21.94, "subpolar", 5, 8),
    ("Rome", "IT", 41.90, 12.50, "mediterranean", 17, 5),
    ("Seattle", "US", 47.61, -122.33, "temperate-maritime", 12, 8),
    ("Stockholm", "SE", 59.33, 18.07, "continental", 7, 6),
    ("Vienna", "AT", 48.21, 16.37, "continental", 11, 6),
]

_SEASONAL_OFFSETS = (-7, -6, -2, 3, 7, 11, 13, 12, 8, 3, -2, -6)
_SOUTHERN_OFFSETS = (13, 12, 8, 3, -2, -6, -7, -6, -2, 3, 7, 11)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must use ISO format YYYY-MM-DD") from exc


def _city_index(city: str) -> int:
    normalized = city.strip().casefold()
    for index, item in enumerate(_CITIES):
        if item[0].casefold() == normalized:
            return index
    raise ValueError(f"Unknown city '{city}'.")


def weather_for(city: str, on_date: str) -> dict[str, Any]:
    """Calculate stable weather from city metadata and calendar date."""
    index = _city_index(city)
    target = _parse_date(on_date)
    name, country, latitude, longitude, climate, annual_temp, rain_level = _CITIES[index]
    offsets = _SOUTHERN_OFFSETS if latitude < 0 else _SEASONAL_OFFSETS
    cycle = (target.day + target.month * 3 + index * 5) % 20
    temperature = annual_temp + offsets[target.month - 1] + (cycle % 7) - 3
    wet_threshold = rain_level + (2 if target.month in {10, 11, 12, 1, 2} else 0)
    if temperature <= 1 and cycle < wet_threshold:
        condition = "snow"
        precipitation = 3 + cycle % 6
    elif cycle < max(1, wet_threshold // 3):
        condition = "heavy rain"
        precipitation = 8 + cycle % 9
    elif cycle < wet_threshold:
        condition = "light rain"
        precipitation = 1 + cycle % 5
    elif cycle in {17, 18}:
        condition = "overcast"
        precipitation = 0
    elif cycle in {15, 16, 19}:
        condition = "partly cloudy"
        precipitation = 0
    else:
        condition = "clear"
        precipitation = 0
    return {
        "city": name,
        "country": country,
        "date": target.isoformat(),
        "climate": climate,
        "temperature_c": temperature,
        "condition": condition,
        "precipitation_mm": precipitation,
        "humidity_percent": 38 + (cycle * 3 + rain_level * 4) % 58,
        "wind_kph": 5 + (target.day * 2 + target.month + index) % 34,
        "coordinates": {"latitude": latitude, "longitude": longitude},
    }


mcp = FastMCP(
    name="travel-weather",
    instructions="Use these tools for deterministic weather evidence by city and date.",
    stateless_http=True,
    transport_security=transport_security_settings(),
)


@mcp.tool()
def list_cities() -> str:
    """List all supported cities and climate classifications."""
    return json.dumps(
        {"cities": [{"city": item[0], "country": item[1], "climate": item[4]} for item in _CITIES]},
        indent=2,
    )


@mcp.tool()
def get_weather(city: str, date: str) -> str:
    """Get deterministic weather for a city on an ISO date."""
    try:
        return json.dumps(weather_for(city, date), indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc), "known_cities": [item[0] for item in _CITIES]}, indent=2)


@mcp.tool()
def get_forecast(city: str, start_date: str, days: int = 7) -> str:
    """Get deterministic weather for up to 31 days from an ISO start date."""
    try:
        start = _parse_date(start_date)
        count = max(1, min(days, 31))
        return json.dumps(
            {
                "city": _CITIES[_city_index(city)][0],
                "start_date": start.isoformat(),
                "days": count,
                "forecast": [weather_for(city, (start + timedelta(days=offset)).isoformat()) for offset in range(count)],
            },
            indent=2,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)