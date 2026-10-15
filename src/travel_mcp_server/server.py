"""Host independent weather, hotel, and transportation MCP servers by path."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .hotel_service import mcp as hotel_mcp
from .transport_service import mcp as transport_mcp
from .weather_service import mcp as weather_mcp


_HOST = os.getenv("TRAVEL_MCP_HOST", "127.0.0.1")
_PORT = int(os.getenv("TRAVEL_MCP_PORT", "8093"))
logger = logging.getLogger("travel_mcp_server")

if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(logger_name="travel_mcp_server")
    except Exception:  # noqa: BLE001
        logger.warning("Application Insights instrumentation could not be configured", exc_info=True)

weather_app = weather_mcp.streamable_http_app()
hotel_app = hotel_mcp.streamable_http_app()
transport_app = transport_mcp.streamable_http_app()


async def health_check(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "services": {
                "weather": "/weather/mcp",
                "hotels": "/hotels/mcp",
                "travel": "/travel/mcp",
            },
        }
    )


@asynccontextmanager
async def lifespan(_: Starlette) -> AsyncIterator[None]:
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(weather_mcp.session_manager.run())
        await stack.enter_async_context(hotel_mcp.session_manager.run())
        await stack.enter_async_context(transport_mcp.session_manager.run())
        yield


app = Starlette(
    routes=[
        Route("/health", health_check, methods=["GET"]),
        Mount("/weather", app=weather_app),
        Mount("/hotels", app=hotel_app),
        Mount("/travel", app=transport_app),
    ],
    lifespan=lifespan,
)


def main() -> None:
    logging.basicConfig(level=os.environ.get("MCP_LOG_LEVEL", "INFO"))
    logger.info("Starting travel MCP host - image_tag=%s", os.getenv("IMAGE_TAG", "unknown"))
    uvicorn.run(app, host=_HOST, port=_PORT)


if __name__ == "__main__":
    main()