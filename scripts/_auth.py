"""HTTP authentication for Microsoft Foundry endpoints."""

from __future__ import annotations

import httpx
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

FOUNDRY_SCOPE = "https://ai.azure.com/.default"


class EntraTokenAuth(httpx.Auth):
    """Attach a Microsoft Entra bearer token to an HTTP request."""

    def __init__(self) -> None:
        self._token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), FOUNDRY_SCOPE
        )

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self._token_provider()}"
        yield request