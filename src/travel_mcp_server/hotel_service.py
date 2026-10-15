"""Independent deterministic hotel MCP service for the travel scenario."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP

from .transport_security import transport_security_settings


_HOTELS = [
    ("Amsterdam", "Canal House", 185, ["pool", "sport"]),
    ("Athens", "Acropolis View", 150, ["pool", "sport"]),
    ("Barcelona", "Marina Ramblas", 175, ["pool", "beach", "sport"]),
    ("Berlin", "Tiergarten Lodge", 135, ["sport"]),
    ("Boston", "Harbor Commons", 210, ["pool", "sport"]),
    ("Cairo", "Nile Pavilion", 125, ["pool", "sport", "golf"]),
    ("Cape Town", "Table Bay Retreat", 165, ["pool", "beach", "golf"]),
    ("Chicago", "Lakeshore Athletic", 195, ["pool", "sport"]),
    ("Copenhagen", "Nyhavn House", 180, ["sport"]),
    ("Denver", "Front Range Club", 155, ["pool", "sport", "golf"]),
    ("Dubai", "Jumeirah Sands", 245, ["pool", "beach", "sport", "golf"]),
    ("Dublin", "Liffey Court", 155, ["sport", "golf"]),
    ("Edinburgh", "Castle Links", 160, ["sport", "golf"]),
    ("Helsinki", "Baltic Sauna Hotel", 145, ["pool", "sport"]),
    ("Lisbon", "Tagus Terrace", 145, ["pool", "beach"]),
    ("London", "Regent Exchange", 230, ["pool", "sport"]),
    ("Los Angeles", "Pacific Palms", 225, ["pool", "beach", "sport", "golf"]),
    ("Madrid", "Retiro House", 155, ["pool", "sport"]),
    ("Miami", "South Beach Club", 235, ["pool", "beach", "sport", "golf"]),
    ("Montreal", "Laurentian Place", 150, ["pool", "sport"]),
    ("Munich", "Isar Garden", 155, ["pool", "sport"]),
    ("New York", "Hudson Assembly", 260, ["pool", "sport"]),
    ("Oslo", "Fjord House", 175, ["pool", "sport"]),
    ("Paris", "Rive Gauche Hotel", 220, ["pool", "sport"]),
    ("Prague", "Vltava Court", 125, ["pool", "sport"]),
    ("Reykjavik", "Lava Fields Hotel", 190, ["pool", "sport", "golf"]),
    ("Rome", "Aventine Garden", 185, ["pool", "sport"]),
    ("Seattle", "Sound and Cedar", 190, ["pool", "sport", "golf"]),
    ("Stockholm", "Archipelago House", 165, ["pool", "sport"]),
    ("Vienna", "Danube Salon", 165, ["pool", "sport"]),
]


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("dates must use ISO format YYYY-MM-DD") from exc


def _hotel_index(city: str) -> int:
    normalized = city.strip().casefold()
    for index, hotel in enumerate(_HOTELS):
        if hotel[0].casefold() == normalized:
            return index
    raise ValueError(f"Unknown city '{city}'.")


def quote_hotel(city: str, check_in: str, check_out: str) -> dict[str, Any]:
    """Calculate a stable hotel quote from location and stay dates."""
    index = _hotel_index(city)
    arrival = _parse_date(check_in)
    departure = _parse_date(check_out)
    nights = (departure - arrival).days
    if nights < 1 or nights > 30:
        raise ValueError("stay must be between 1 and 30 nights")
    location, name, base_price, capabilities = _HOTELS[index]
    nightly_prices = []
    for offset in range(nights):
        night = arrival + timedelta(days=offset)
        weekend = 24 if night.weekday() >= 4 else 0
        seasonal = ((night.month * 7 + index * 3) % 9 - 4) * 6
        demand = ((night.day + night.month + index) % 6) * 5
        nightly_prices.append({"date": night.isoformat(), "price_usd": max(75, base_price + weekend + seasonal + demand)})
    total = sum(item["price_usd"] for item in nightly_prices)
    return {
        "city": location,
        "hotel": name,
        "check_in": arrival.isoformat(),
        "check_out": departure.isoformat(),
        "nights": nights,
        "capabilities": capabilities,
        "nightly_prices": nightly_prices,
        "total_usd": total,
        "average_nightly_usd": round(total / nights, 2),
        "availability": (arrival.day + arrival.month * 2 + index) % 11 != 0,
    }


mcp = FastMCP(
    name="travel-hotels",
    instructions="Use these tools for deterministic hotel capabilities, availability, and prices.",
    stateless_http=True,
    transport_security=transport_security_settings(),
)


@mcp.tool()
def list_hotels(city: str | None = None) -> str:
    """List hotels, optionally filtered to one supported city."""
    try:
        hotels = [_HOTELS[_hotel_index(city)]] if city else _HOTELS
        return json.dumps(
            {"hotels": [{"city": item[0], "hotel": item[1], "capabilities": item[3]} for item in hotels]},
            indent=2,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)


@mcp.tool()
def get_hotel_quote(city: str, check_in: str, check_out: str) -> str:
    """Get deterministic availability and nightly pricing for a stay."""
    try:
        return json.dumps(quote_hotel(city, check_in, check_out), indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)


@mcp.tool()
def recommend_hotels(
    city: str,
    check_in: str,
    check_out: str,
    required_capabilities: list[str] | None = None,
    max_total_usd: int | None = None,
) -> str:
    """Recommend the location's hotel against capability and budget constraints."""
    try:
        quote = quote_hotel(city, check_in, check_out)
        required = {item.strip().casefold() for item in required_capabilities or []}
        offered = {item.casefold() for item in quote["capabilities"]}
        quote["matches_capabilities"] = sorted(required & offered)
        quote["missing_capabilities"] = sorted(required - offered)
        quote["within_budget"] = max_total_usd is None or quote["total_usd"] <= max_total_usd
        quote["recommended"] = quote["availability"] and not quote["missing_capabilities"] and quote["within_budget"]
        return json.dumps({"recommendations": [quote] if quote["recommended"] else [], "evaluated": [quote]}, indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)