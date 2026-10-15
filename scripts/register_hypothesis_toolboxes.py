"""Register the deterministic travel scenario services as Foundry toolboxes."""

from __future__ import annotations

from scripts._helpers import env, load_env, project_client, save_env

TOOLBOXES = {
    "WEATHER": ("weather", "Deterministic city weather by date", "/weather/mcp"),
    "HOTELS": ("hotels", "Deterministic hotel capabilities, availability, and prices", "/hotels/mcp"),
    "TRAVEL": ("travel", "Deterministic transportation options by route and date", "/travel/mcp"),
}


def main() -> None:
    load_env()
    project_endpoint = env("AZURE_AI_PROJECT_ENDPOINT", required=True).rstrip("/")
    host_url = env("TRAVEL_MCP_HOST_URL", required=True).rstrip("/")

    from azure.ai.projects.models import MCPToolboxTool

    updates: dict[str, str] = {}
    with project_client() as client:
        for prefix, (default_name, description, path) in TOOLBOXES.items():
            name = env(f"{prefix}_TOOLBOX_NAME", default_name)
            mcp_url = env(f"{prefix}_MCP_URL") or f"{host_url}{path}"
            # Create another version under the stable name before switching clients.
            # Keep previous versions for agents whose env still references them;
            # teardown owns deletion, not registration.
            created = client.toolboxes.create_version(
                name=name,
                description=description,
                tools=[
                    MCPToolboxTool(
                        server_label=default_name,
                        server_url=mcp_url,
                        require_approval="never",
                    )
                ],
            )
            endpoint = f"{project_endpoint}/toolboxes/{name}/versions/{created.version}/mcp?api-version=v1"
            updates[f"{prefix}_TOOLBOX_NAME"] = name
            updates[f"{prefix}_TOOLBOX_ENDPOINT"] = endpoint
            print(f"Registered toolbox '{name}' version {created.version}")

    save_env(updates)


if __name__ == "__main__":
    main()