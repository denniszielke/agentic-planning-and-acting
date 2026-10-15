"""Shared helpers for the hypothesis workflow deployment scripts."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
APP_BICEP = ROOT / "infra" / "core" / "host" / "app.bicep"
EXTERNAL_MODEL_ACCESS_BICEP = ROOT / "infra" / "core" / "ai" / "external-model-access.bicep"
COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
_COGNITIVE_ACCOUNT_ID = re.compile(
    r"^/subscriptions/(?P<subscription>[^/]+)/resourceGroups/(?P<resource_group>[^/]+)"
    r"/providers/Microsoft\.CognitiveServices/accounts/(?P<account>[^/]+)$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# .env handling
# --------------------------------------------------------------------------- #
def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Read ``.env`` into a dict and merge it into ``os.environ`` (no override)."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            values[key] = value
            os.environ.setdefault(key, value)
    return values


def save_env(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    """Insert/replace ``KEY=value`` lines in ``.env`` (creates it if missing)."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value
    print(f"  ↳ wrote {', '.join(updates)} to {path.name}")


def env(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(key, default if default is not None else "")
    if required and not value:
        sys.exit(f"ERROR: required environment variable {key} is not set (run `azd up` first).")
    return value


# --------------------------------------------------------------------------- #
# Azure CLI wrappers
# --------------------------------------------------------------------------- #
def run(cmd: list[str], *, capture: bool = False) -> str:
    """Run a command, echoing it. Returns stdout when ``capture`` is True."""
    print("+ " + " ".join(cmd))
    result = subprocess.run(cmd, check=True, text=True, capture_output=capture)
    return (result.stdout or "").strip() if capture else ""


def registry_name() -> str:
    """Bare ACR name (e.g. ``myregistry``), derived from env if needed."""
    name = env("AZURE_CONTAINER_REGISTRY_NAME")
    if name:
        return name
    endpoint = env("AZURE_CONTAINER_REGISTRY_ENDPOINT") or env("AZURE_REGISTRY", required=True)
    return endpoint.split(".", 1)[0]


@lru_cache(maxsize=1)
def _generated_tag() -> str:
    """A unique, traceable image tag for this process: ``<git-sha>-<utc-ts>``.

    Cached so every image built in a single run shares one tag. Falls back to a
    bare UTC timestamp when git metadata is unavailable.
    """
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, text=True, capture_output=True, check=True,
        )
        sha = proc.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        sha = ""
    return f"{sha}-{ts}" if sha else ts


def resolve_image_tag(explicit: str | None = None) -> str:
    """Resolve the image tag to build/deploy.

    Precedence: explicit argument (e.g. ``--tag``) → ``IMAGE_TAG`` env → a unique
    generated ``<git-sha>-<utc-ts>`` tag. Never returns ``latest`` so each build
    is uniquely addressable and deployments are reproducible.
    """
    return explicit or os.getenv("IMAGE_TAG") or _generated_tag()


def tag_from_cli(description: str = "Build and deploy a uniquely-tagged image.") -> str | None:
    """Parse an optional ``--tag/-t`` from argv (returns ``None`` when omitted)."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--tag", "-t", default=None,
        help="Custom image tag to build and deploy (default: IMAGE_TAG env or a generated unique tag).",
    )
    args, _ = parser.parse_known_args()
    return args.tag


def acr_build(image: str, dockerfile: Path, context: Path = ROOT, *, tag: str | None = None) -> str:
    """Build+push an image in ACR (`az acr build`). Returns the full image ref.

    ``tag`` defaults to :func:`resolve_image_tag` (a unique ``<git-sha>-<utc-ts>``
    tag, or ``IMAGE_TAG`` when set) so images are never overwritten under
    ``latest``.
    """
    registry = registry_name()
    reference = f"{image}:{resolve_image_tag(tag)}"
    run([
        "az", "acr", "build",
        "--registry", registry,
        "--image", reference,
        "--file", str(dockerfile),
        str(context),
    ])
    login_server = env("AZURE_CONTAINER_REGISTRY_ENDPOINT") or f"{registry}.azurecr.io"
    full = f"{login_server}/{reference}"
    print(f"  ↳ built {full}")
    return full


def deploy_container_app(
    name: str,
    image: str,
    *,
    target_port: int,
    env_vars: dict[str, str],
    cpu: str = "1",
    memory: str = "2.0Gi",
    readiness_path: str = "",
    min_replicas: int = 0,
    external: bool = True,
) -> str:
    """Deploy a Container App and return its HTTPS URL."""
    env_json = json.dumps([{"name": key, "value": value} for key, value in env_vars.items()])
    parameters = [
        f"name={name}",
        f"imageName={image}",
        f"targetPort={target_port}",
        f"containerAppsEnvironmentName={env('AZURE_CONTAINER_ENVIRONMENT_NAME', required=True)}",
        f"containerRegistryName={registry_name()}",
        f"identityName={env('AZURE_MANAGED_IDENTITY_NAME', required=True)}",
        f"envJson={env_json}",
        f"containerCpuCoreCount={cpu}",
        f"containerMemory={memory}",
        f"readinessProbePath={readiness_path}",
        f"minReplicas={min_replicas}",
        f"external={'true' if external else 'false'}",
    ]
    uri = run(
        [
            "az", "deployment", "group", "create",
            "--resource-group", env("AZURE_RESOURCE_GROUP", required=True),
            "--name", f"deploy-{name}",
            "--template-file", str(APP_BICEP),
            "--parameters", *parameters,
            "--query", "properties.outputs.uri.value",
            "-o", "tsv",
        ],
        capture=True,
    )
    print(f"  ↳ {name} available at {uri}")
    return uri


# --------------------------------------------------------------------------- #
# Foundry project client
# --------------------------------------------------------------------------- #
def project_client() -> Any:
    """Construct an ``AIProjectClient`` for the provisioned Foundry project."""
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    endpoint = env("AZURE_AI_PROJECT_ENDPOINT", required=True)
    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential(), allow_preview=True)


# --------------------------------------------------------------------------- #
# Toolbox RBAC (hosted agent → Foundry toolbox)
# --------------------------------------------------------------------------- #
def _account_resource_id() -> str:
    return env("AZURE_AI_PROJECT_ID", required=True).split("/projects/", 1)[0]


def account_principal_id() -> str:
    return run(["az", "resource", "show", "--ids", _account_resource_id(),
                "--query", "identity.principalId", "-o", "tsv"], capture=True)


def ensure_agent_access(principal_id: str, *, include_inference: bool = True) -> None:
    """Apply Bicep-owned tool, model and Foundry state access for a new identity."""
    if not principal_id:
        raise RuntimeError("Agent identity is missing; refusing to route traffic without access grants.")
    assignment_parameters = []
    roles = [("foundryAssignmentName", "53ca6127-db72-4b80-b1b0-d745d6d5456d")]
    if include_inference:
        roles.append(("inferenceAssignmentName", COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID))
    for parameter, role_id in roles:
        # Adopt legacy assignment IDs to avoid RoleAssignmentExists on upgrade.
        assignment = run([
            "az", "role", "assignment", "list", "--assignee", principal_id,
            "--scope", _account_resource_id(), "--role", role_id,
            "--query", "[0].name", "-o", "tsv",
        ], capture=True)
        if assignment:
            assignment_parameters.append(f"{parameter}={assignment}")
    run([
        "az", "deployment", "group", "create",
        "--resource-group", env("AZURE_RESOURCE_GROUP", required=True),
        "--name", f"agent-access-{principal_id}",
        "--template-file", str(ROOT / "infra/core/ai/agent-access.bicep"),
        "--parameters", f"accountName={_account_resource_id().rsplit('/', 1)[-1]}",
        f"principalId={principal_id}", f"includeInference={str(include_inference).lower()}",
        *assignment_parameters,
        "--only-show-errors", "-o", "none",
    ])


def parse_cognitive_account_resource_id(resource_id: str) -> tuple[str, str, str]:
    """Return subscription, resource group and account from a full account ARM ID."""
    match = _COGNITIVE_ACCOUNT_ID.fullmatch(resource_id)
    if not match:
        raise ValueError(
            "DECISION_MODEL_ACCOUNT_RESOURCE_ID must be a full "
            "/subscriptions/<id>/resourceGroups/<group>/providers/"
            "Microsoft.CognitiveServices/accounts/<name> ARM resource ID."
        )
    return match["subscription"], match["resource_group"], match["account"]


def ensure_external_model_access(principal_id: str, account_resource_id: str) -> None:
    """Grant model inference on an external Cognitive Services account."""
    if not principal_id:
        raise RuntimeError("Agent identity is missing; refusing to route traffic without access grants.")
    subscription_id, resource_group, account_name = parse_cognitive_account_resource_id(account_resource_id)
    assignment = run([
        "az", "role", "assignment", "list",
        "--subscription", subscription_id,
        "--assignee", principal_id,
        "--scope", account_resource_id,
        "--role", COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID,
        "--query", "[0].name",
        "-o", "tsv",
    ], capture=True)
    assignment_parameters = [f"assignmentName={assignment}"] if assignment else []
    run([
        "az", "deployment", "group", "create",
        "--subscription", subscription_id,
        "--resource-group", resource_group,
        "--name", f"agent-external-model-access-{principal_id}",
        "--template-file", str(EXTERNAL_MODEL_ACCESS_BICEP),
        "--parameters",
        f"accountName={account_name}",
        f"principalId={principal_id}",
        *assignment_parameters,
        "--only-show-errors",
        "-o", "none",
    ])


def ensure_toolbox_role() -> None:
    ensure_agent_access(account_principal_id(), include_inference=False)
