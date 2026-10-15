"""Build and deploy the multi-path travel scenario MCP host."""

from __future__ import annotations

from scripts._helpers import ROOT, acr_build, deploy_container_app, env, load_env, run, save_env, tag_from_cli

APP_NAME = "travel-mcp-server"
PORT = 8093


def main(tag: str | None = None) -> None:
    load_env()
    image = acr_build(APP_NAME, ROOT / "src" / "travel_mcp_server" / "Dockerfile", tag=tag)
    default_domain = run(
        [
            "az", "containerapp", "env", "show",
            "--name", env("AZURE_CONTAINER_ENVIRONMENT_NAME", required=True),
            "--resource-group", env("AZURE_RESOURCE_GROUP", required=True),
            "--subscription", env("AZURE_SUBSCRIPTION_ID", required=True),
            "--query", "properties.defaultDomain",
            "-o", "tsv",
        ],
        capture=True,
    )
    public_host = f"{APP_NAME}.{default_domain}"
    container_env = {
        "TRAVEL_MCP_HOST": "0.0.0.0",
        "TRAVEL_MCP_PORT": str(PORT),
        "TRAVEL_MCP_ALLOWED_HOSTS": f"{public_host},{public_host}:*",
        "TRAVEL_MCP_ALLOWED_ORIGINS": f"https://{public_host},https://{public_host}:*",
        "IMAGE_TAG": image.rsplit(":", 1)[-1],
    }
    if connection_string := env("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        container_env["APPLICATIONINSIGHTS_CONNECTION_STRING"] = connection_string

    url = deploy_container_app(
        APP_NAME,
        image,
        target_port=PORT,
        env_vars=container_env,
        cpu=env("TRAVEL_MCP_CPU", "1"),
        memory=env("TRAVEL_MCP_MEMORY", "2.0Gi"),
        readiness_path="/health",
        min_replicas=int(env("TRAVEL_MCP_MIN_REPLICAS", "1")),
    ).rstrip("/")
    save_env(
        {
            "TRAVEL_MCP_HOST_URL": url,
            "WEATHER_MCP_URL": f"{url}/weather/mcp",
            "HOTELS_MCP_URL": f"{url}/hotels/mcp",
            "TRAVEL_MCP_URL": f"{url}/travel/mcp",
            "TRAVEL_MCP_IMAGE": image,
        }
    )
    print(f"\nTravel scenario MCP services ready at {url}/health")


if __name__ == "__main__":
    main(tag_from_cli())