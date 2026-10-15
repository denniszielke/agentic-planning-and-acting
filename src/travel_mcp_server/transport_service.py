"""Independent deterministic transportation MCP service for the travel scenario."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from .transport_security import transport_security_settings


_CITIES = [
    ("Amsterdam", "NL", 52.37, 4.90), ("Athens", "GR", 37.98, 23.73),
    ("Barcelona", "ES", 41.39, 2.17), ("Berlin", "DE", 52.52, 13.41),
    ("Boston", "US", 42.36, -71.06), ("Cairo", "EG", 30.04, 31.24),
    ("Cape Town", "ZA", -33.92, 18.42), ("Chicago", "US", 41.88, -87.63),
    ("Copenhagen", "DK", 55.68, 12.57), ("Denver", "US", 39.74, -104.99),
    ("Dubai", "AE", 25.20, 55.27), ("Dublin", "IE", 53.35, -6.26),
    ("Edinburgh", "GB", 55.95, -3.19), ("Helsinki", "FI", 60.17, 24.94),
    ("Lisbon", "PT", 38.72, -9.14), ("London", "GB", 51.51, -0.13),
    ("Los Angeles", "US", 34.05, -118.24), ("Madrid", "ES", 40.42, -3.70),
    ("Miami", "US", 25.76, -80.19), ("Montreal", "CA", 45.50, -73.57),
    ("Munich", "DE", 48.14, 11.58), ("New York", "US", 40.71, -74.01),
    ("Oslo", "NO", 59.91, 10.75), ("Paris", "FR", 48.86, 2.35),
    ("Prague", "CZ", 50.08, 14.44), ("Reykjavik", "IS", 64.15, -21.94),
    ("Rome", "IT", 41.90, 12.50), ("Seattle", "US", 47.61, -122.33),
    ("Stockholm", "SE", 59.33, 18.07), ("Vienna", "AT", 48.21, 16.37),
]


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


def _distance_km(origin: tuple[Any, ...], destination: tuple[Any, ...]) -> int:
    lat1, lon1, lat2, lon2 = map(math.radians, (origin[2], origin[3], destination[2], destination[3]))
    delta_latitude = lat2 - lat1
    delta_longitude = lon2 - lon1
    haversine = math.sin(delta_latitude / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_longitude / 2) ** 2
    return round(6371 * 2 * math.asin(math.sqrt(haversine)))


def route_options(origin: str, destination: str, departure_date: str) -> dict[str, Any]:
    """Calculate stable route options from cities and a calendar date."""
    origin_index = _city_index(origin)
    destination_index = _city_index(destination)
    if origin_index == destination_index:
        raise ValueError("origin and destination must be different")
    travel_date = _parse_date(departure_date)
    start, end = _CITIES[origin_index], _CITIES[destination_index]
    distance = _distance_km(start, end)
    cycle = (travel_date.day + travel_date.month * 3 + origin_index + destination_index * 2) % 7
    candidates = [
        ("airplane", True, max(75, round(distance * 0.11 + 65 + cycle * 9)), round(2.2 + distance / 780, 1), 142),
        ("train", distance <= 1400 and start[1] not in {"IS", "IE"} and end[1] not in {"IS", "IE"}, max(35, round(distance * 0.10 + cycle * 5)), round(1.0 + distance / 150, 1), 36),
        ("car", distance <= 1800, max(30, round(distance * 0.13 + cycle * 3)), round(distance / 90 + 0.8, 1), 171),
    ]
    options = []
    for mode, supported, price, duration, emissions in candidates:
        available = supported and (cycle + len(mode)) % 6 != 0
        if supported:
            options.append({
                "mode": mode,
                "available": available,
                "price_usd": price,
                "duration_hours": duration,
                "co2_kg": round(distance * emissions / 1000, 1),
            })
    return {
        "origin": start[0],
        "destination": end[0],
        "departure_date": travel_date.isoformat(),
        "distance_km": distance,
        "options": options,
    }


mcp = FastMCP(
    name="travel-transportation",
    instructions="Use these tools for deterministic air, train, and car route evidence by date.",
    stateless_http=True,
    transport_security=transport_security_settings(),
)


@mcp.tool()
def list_cities() -> str:
    """List cities supported by the transportation network."""
    return json.dumps({"cities": [{"city": item[0], "country": item[1]} for item in _CITIES]}, indent=2)


@mcp.tool()
def get_route_options(origin: str, destination: str, departure_date: str) -> str:
    """Get deterministic available transportation modes, prices, and durations."""
    try:
        return json.dumps(route_options(origin, destination, departure_date), indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)


@mcp.tool()
def recommend_transport(
    origin: str,
    destination: str,
    departure_date: str,
    priority: str = "balanced",
) -> str:
    """Rank available modes by cost, duration, emissions, or a balanced score."""
    try:
        result = route_options(origin, destination, departure_date)
        available = [option for option in result["options"] if option["available"]]
        if priority == "cost":
            key = lambda item: (item["price_usd"], item["duration_hours"])
        elif priority == "duration":
            key = lambda item: (item["duration_hours"], item["price_usd"])
        elif priority == "emissions":
            key = lambda item: (item["co2_kg"], item["duration_hours"])
        elif priority == "balanced":
            key = lambda item: item["price_usd"] / 10 + item["duration_hours"] * 2 + item["co2_kg"] / 20
        else:
            raise ValueError("priority must be cost, duration, emissions, or balanced")
        result["priority"] = priority
        result["recommendations"] = sorted(available, key=key)
        return json.dumps(result, indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)