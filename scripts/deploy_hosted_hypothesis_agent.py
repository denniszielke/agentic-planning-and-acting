"""Build and deploy the hosted hypothesis workflow on invocations protocol 2.0.0."""

from __future__ import annotations

from dataclasses import dataclass
import time
from urllib.parse import urlparse

from scripts._helpers import (
    ROOT,
    acr_build,
    ensure_agent_access,
    ensure_external_model_access,
    ensure_toolbox_role,
    env,
    load_env,
    parse_cognitive_account_resource_id,
    project_client,
    run,
    save_env,
    tag_from_cli,
)

AGENT_NAME = "scenario-hosted-hypothesis-agent"
PROTOCOLS = [("invocations", "2.0.0")]


@dataclass(frozen=True)
class DecisionModelConfig:
    project_endpoint: str
    deployment_name: str
    account_resource_id: str
    tenant_id: str


def decision_model_config() -> DecisionModelConfig | None:
    values = {
        "DECISION_MODEL_PROJECT_ENDPOINT": env("DECISION_MODEL_PROJECT_ENDPOINT"),
        "DECISION_MODEL_DEPLOYMENT_NAME": env("DECISION_MODEL_DEPLOYMENT_NAME"),
        "DECISION_MODEL_ACCOUNT_RESOURCE_ID": env("DECISION_MODEL_ACCOUNT_RESOURCE_ID"),
        "DECISION_MODEL_TENANT_ID": env("DECISION_MODEL_TENANT_ID"),
    }
    configured = [key for key, value in values.items() if value]
    if not configured:
        return None
    required = [
        "DECISION_MODEL_PROJECT_ENDPOINT",
        "DECISION_MODEL_DEPLOYMENT_NAME",
        "DECISION_MODEL_ACCOUNT_RESOURCE_ID",
    ]
    missing = [key for key in required if not values[key]]
    if missing:
        raise ValueError(
            "External decision model configuration is partial; set all of "
            f"{', '.join(required)} or leave all DECISION_MODEL_* values empty. "
            f"Missing: {', '.join(missing)}."
        )

    endpoint = values["DECISION_MODEL_PROJECT_ENDPOINT"]
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path in ("", "/")
    ):
        raise ValueError(
            "DECISION_MODEL_PROJECT_ENDPOINT must be an HTTPS Foundry project endpoint "
            "without credentials, query parameters, or a fragment."
        )
    if parsed.hostname.lower() == "ai.azure.com":
        raise ValueError(
            "DECISION_MODEL_PROJECT_ENDPOINT must be the Foundry project endpoint, "
            "not an ai.azure.com portal URL."
        )
    if any(character.isspace() for character in values["DECISION_MODEL_DEPLOYMENT_NAME"]):
        raise ValueError("DECISION_MODEL_DEPLOYMENT_NAME must not contain whitespace.")
    parse_cognitive_account_resource_id(values["DECISION_MODEL_ACCOUNT_RESOURCE_ID"])
    return DecisionModelConfig(
        project_endpoint=endpoint,
        deployment_name=values["DECISION_MODEL_DEPLOYMENT_NAME"],
        account_resource_id=values["DECISION_MODEL_ACCOUNT_RESOURCE_ID"],
        tenant_id=values["DECISION_MODEL_TENANT_ID"],
    )


def validate_decision_model(config: DecisionModelConfig) -> None:
    subscription_id, resource_group, account_name = parse_cognitive_account_resource_id(
        config.account_resource_id
    )
    local_subscription = env("AZURE_SUBSCRIPTION_ID", required=True)
    local_tenant = run([
        "az", "account", "show", "--subscription", local_subscription,
        "--query", "tenantId", "-o", "tsv",
    ], capture=True)
    target_tenant = run([
        "az", "account", "show", "--subscription", subscription_id,
        "--query", "tenantId", "-o", "tsv",
    ], capture=True)
    if local_tenant.lower() != target_tenant.lower():
        raise ValueError(
            "Cross-tenant decision model access is not supported: the local Foundry "
            f"subscription tenant is {local_tenant}, but the target subscription tenant is {target_tenant}."
        )
    if config.tenant_id and config.tenant_id.lower() != target_tenant.lower():
        raise ValueError(
            "DECISION_MODEL_TENANT_ID does not match the tenant reported for "
            f"the target subscription ({target_tenant})."
        )
    deployment = run([
        "az", "cognitiveservices", "account", "deployment", "show",
        "--subscription", subscription_id,
        "--resource-group", resource_group,
        "--name", account_name,
        "--deployment-name", config.deployment_name,
        "--query", "name",
        "-o", "tsv",
    ], capture=True)
    if deployment != config.deployment_name:
        raise RuntimeError(
            f"Decision model deployment lookup returned {deployment!r}, "
            f"expected {config.deployment_name!r}."
        )
    print(
        f"  ↳ validated external decision deployment '{config.deployment_name}' "
        f"on {config.account_resource_id}"
    )


def grant_hosted_agent_access(
    principal_id: str,
    decision_model: DecisionModelConfig | None,
) -> None:
    ensure_agent_access(principal_id)
    if not decision_model:
        return
    if env("SKIP_ROLE_ASSIGNMENTS", "false").strip().lower() == "true":
        print(
            "  ↳ SKIP_ROLE_ASSIGNMENTS applies only to azd infrastructure; "
            "the required external model grant remains enabled"
        )
    ensure_external_model_access(principal_id, decision_model.account_resource_id)


def main(tag: str | None = None) -> None:
    load_env()
    try:
        decision_model = decision_model_config()
        if decision_model:
            validate_decision_model(decision_model)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    image = acr_build(AGENT_NAME, ROOT / "src" / "hosted_hypothesis_agent" / "Dockerfile", tag=tag)
    ensure_toolbox_role()

    container_env = {
        "AZURE_AI_PROJECT_ENDPOINT": env("AZURE_AI_PROJECT_ENDPOINT", required=True),
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": env("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4-nano"),
        "WORKFLOW_TTL_HOURS": env("WORKFLOW_TTL_HOURS", "168"),
        "LRA_STORE_WORKFLOWS": env("LRA_STORE_WORKFLOWS", "lra-workflows"),
        "LRA_STORE_EVENTS": env("LRA_STORE_EVENTS", "lra-events"),
        "LRA_STORE_CHECKPOINTS": env("LRA_STORE_CHECKPOINTS", "lra-checkpoints"),
        "LRA_HEARTBEAT_SECONDS": env("LRA_HEARTBEAT_SECONDS", "15"),
        "LRA_STALE_CLAIM_SECONDS": env("LRA_STALE_CLAIM_SECONDS", "90"),
        "HARNESS_MAX_ITERATIONS": env("HARNESS_MAX_ITERATIONS", "12"),
        "WEATHER_TOOLBOX_ENDPOINT": env("WEATHER_TOOLBOX_ENDPOINT", required=True),
        "HOTELS_TOOLBOX_ENDPOINT": env("HOTELS_TOOLBOX_ENDPOINT", required=True),
        "TRAVEL_TOOLBOX_ENDPOINT": env("TRAVEL_TOOLBOX_ENDPOINT", required=True),
        "WEATHER_ALLOWED_TOOLS": env("WEATHER_ALLOWED_TOOLS"),
        "HOTELS_ALLOWED_TOOLS": env("HOTELS_ALLOWED_TOOLS"),
        "TRAVEL_ALLOWED_TOOLS": env("TRAVEL_ALLOWED_TOOLS"),
        "IMAGE_TAG": image.rsplit(":", 1)[-1],
    }
    if decision_model:
        container_env.update({
            "DECISION_MODEL_PROJECT_ENDPOINT": decision_model.project_endpoint,
            "DECISION_MODEL_DEPLOYMENT_NAME": decision_model.deployment_name,
        })

    from azure.ai.projects.models import (
        AgentEndpointConfig,
        ContainerConfiguration,
        FixedRatioVersionSelectionRule,
        HostedAgentDefinition,
        InvocationsProtocolConfiguration,
        ProtocolConfiguration,
        ProtocolVersionRecord,
        VersionSelector,
    )

    with project_client() as client:
        created = client.agents.create_version(
            agent_name=AGENT_NAME,
            definition=HostedAgentDefinition(
                cpu=env("HOSTED_AGENT_CPU", "1"),
                memory=env("HOSTED_AGENT_MEMORY", "2Gi"),
                container_configuration=ContainerConfiguration(image=image),
                environment_variables=container_env,
                protocol_versions=[ProtocolVersionRecord(protocol=protocol, version=version) for protocol, version in PROTOCOLS],
            ),
            description="Resilient, human-approved linear phase workflow with Foundry state.",
            metadata={"enableVnextExperience": "true"},
            headers={"Foundry-Features": "HostedAgents=V1Preview"},
        )
        print(f"Created hosted agent '{AGENT_NAME}' version {created.version}; waiting for Active")
        deadline = time.time() + 900
        while time.time() < deadline:
            current = client.agents.get_version(agent_name=AGENT_NAME, agent_version=created.version)
            status = getattr(current, "status", None)
            status_value = getattr(status, "value", status)
            print(f"  status={status}")
            if status_value == "active":
                break
            if status_value in ("failed", "deleting", "deleted"):
                raise SystemExit(f"Hosted agent version entered terminal status: {status}")
            time.sleep(15)
        else:
            raise SystemExit("Timed out waiting for the hosted agent to become Active.")

        instance_identity = getattr(current, "instance_identity", None)
        principal_id = getattr(instance_identity, "principal_id", "")
        grant_hosted_agent_access(principal_id, decision_model)

        client.agents.update_details(
            agent_name=AGENT_NAME,
            agent_endpoint=AgentEndpointConfig(
                version_selector=VersionSelector(
                    version_selection_rules=[
                        FixedRatioVersionSelectionRule(agent_version=created.version, traffic_percentage=100)
                    ]
                ),
                protocol_configuration=ProtocolConfiguration(
                    invocations=InvocationsProtocolConfiguration(),
                ),
            ),
        )

    save_env({
        "HYPOTHESIS_HOSTED_AGENT_NAME": AGENT_NAME,
        "HYPOTHESIS_HOSTED_AGENT_IMAGE": image,
    })
    print(f"Hosted hypothesis agent ready: {AGENT_NAME} ({image})")


if __name__ == "__main__":
    main(tag_from_cli())