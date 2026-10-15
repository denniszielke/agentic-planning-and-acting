"""Transport security settings shared by the travel MCP services."""

from __future__ import annotations

import os

from mcp.server.transport_security import TransportSecuritySettings


def transport_security_settings(
    allowed_hosts: str | None = None,
    allowed_origins: str | None = None,
) -> TransportSecuritySettings:
    hosts = [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        *(
            item.strip()
            for item in (allowed_hosts if allowed_hosts is not None else os.getenv("TRAVEL_MCP_ALLOWED_HOSTS", "")).split(",")
            if item.strip()
        ),
    ]
    origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        *(
            item.strip()
            for item in (
                allowed_origins
                if allowed_origins is not None
                else os.getenv("TRAVEL_MCP_ALLOWED_ORIGINS", "")
            ).split(",")
            if item.strip()
        ),
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )
