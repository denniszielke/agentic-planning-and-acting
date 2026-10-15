"""Delete the Foundry agent and toolboxes created for the workflow.

This script removes management-plane objects. Run ``azd down`` separately to
remove the provisioned Azure resources.

    python -m scripts.delete_agents
"""

from __future__ import annotations

from scripts._helpers import env, load_env, project_client

DEFAULT_AGENT_NAME = "scenario-hosted-hypothesis-agent"


def main() -> None:
    load_env()
    from azure.core.exceptions import ResourceNotFoundError

    toolbox_names = [
        env("WEATHER_TOOLBOX_NAME", "weather"),
        env("HOTELS_TOOLBOX_NAME", "hotels"),
        env("TRAVEL_TOOLBOX_NAME", "travel"),
    ]

    with project_client() as client:
        for name in [env("HYPOTHESIS_HOSTED_AGENT_NAME", DEFAULT_AGENT_NAME)]:
            try:
                client.agents.delete(agent_name=name, force=True)
                print(f"Deleted agent '{name}'")
            except ResourceNotFoundError:
                print(f"Agent '{name}' not found, skipping")

        for name in toolbox_names:
            try:
                client.toolboxes.delete(name=name)
                print(f"Deleted toolbox '{name}'")
            except ResourceNotFoundError:
                print(f"Toolbox '{name}' not found, skipping")

    print("State-store items expire by TTL; deleting an agent is not a data migration.")
    print("\nFoundry cleanup complete. Run `azd down` to remove the Azure resources.")


if __name__ == "__main__":
    main()
