from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from src.travel_mcp_server.hotel_service import _HOTELS, mcp as hotel_mcp, quote_hotel
from src.travel_mcp_server.server import app
from src.travel_mcp_server.transport_service import _CITIES as TRANSPORT_CITIES, mcp as transport_mcp, route_options
from src.travel_mcp_server.transport_security import transport_security_settings
from src.travel_mcp_server.weather_service import _CITIES as WEATHER_CITIES, mcp as weather_mcp, weather_for


class TravelServiceTests(unittest.TestCase):
    def test_each_mcp_server_uses_stateless_http(self) -> None:
        for server in (weather_mcp, hotel_mcp, transport_mcp):
            self.assertTrue(server.settings.stateless_http)

    def test_transport_security_allows_configured_public_host(self) -> None:
        host = "travel-mcp-server.example.azurecontainerapps.io"
        settings = transport_security_settings(
            allowed_hosts=f"{host},{host}:*",
            allowed_origins=f"https://{host},https://{host}:*",
        )
        self.assertTrue(settings.enable_dns_rebinding_protection)
        self.assertIn(host, settings.allowed_hosts)
        self.assertIn(f"{host}:*", settings.allowed_hosts)
        self.assertNotIn("untrusted.example", settings.allowed_hosts)

    def test_each_independent_service_covers_thirty_cities(self) -> None:
        self.assertEqual(len(WEATHER_CITIES), 30)
        self.assertEqual(len(_HOTELS), 30)
        self.assertEqual(len(TRANSPORT_CITIES), 30)
        self.assertIsNot(WEATHER_CITIES, TRANSPORT_CITIES)

    def test_weather_is_repeatable_and_date_sensitive(self) -> None:
        first = weather_for("Berlin", "2026-08-12")
        self.assertEqual(first, weather_for("Berlin", "2026-08-12"))
        self.assertNotEqual(first, weather_for("Berlin", "2026-08-13"))
        self.assertGreater(
            weather_for("Cape Town", "2026-01-15")["temperature_c"],
            weather_for("Cape Town", "2026-07-15")["temperature_c"],
        )

    def test_hotel_quote_is_repeatable_and_includes_capabilities(self) -> None:
        quote = quote_hotel("Barcelona", "2026-08-12", "2026-08-15")
        self.assertEqual(quote, quote_hotel("Barcelona", "2026-08-12", "2026-08-15"))
        self.assertTrue({"pool", "beach", "sport"}.issubset(quote["capabilities"]))
        self.assertEqual(len(quote["nightly_prices"]), 3)

    def test_transport_modes_follow_route_distance(self) -> None:
        regional = route_options("Berlin", "Prague", "2026-08-12")
        self.assertEqual(regional, route_options("Berlin", "Prague", "2026-08-12"))
        self.assertEqual({"airplane", "train", "car"}, {item["mode"] for item in regional["options"]})
        self.assertEqual(
            ["airplane"],
            [item["mode"] for item in route_options("Seattle", "Rome", "2026-08-12")["options"]],
        )

    def test_host_exposes_each_mcp_path_without_sessions(self) -> None:
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        }
        with TestClient(app, base_url="http://localhost:8000") as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            for path in ("/weather/mcp", "/hotels/mcp", "/travel/mcp"):
                response = client.post(
                    path,
                    json=initialize,
                    headers={"accept": "application/json, text/event-stream"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("mcp-session-id", response.headers)


if __name__ == "__main__":
    unittest.main()