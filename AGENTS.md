# AGENTS.md — operations runbook

Operational guidance for the human-approved, phased travel workflow. See
[README.md](README.md), [implemented API](doc/lra-api-implemented.md), and
[dependency audit](doc/dependency-audit.md).

## Components and desired state

| Component | Runs on | Entry point | Configuration |
|---|---|---|---|
| Hypothesis agent | Foundry hosted container | `scripts.deploy_hosted_hypothesis_agent` | `HYPOTHESIS_HOSTED_AGENT_NAME`, optional `DECISION_MODEL_*` |
| Durable phases/events/checkpoints | Foundry state store | Agent SDK get-or-create | `LRA_STORE_*`, `WORKFLOW_TTL_HOURS=168` |
| Travel MCP host | Azure Container Apps | `scripts.deploy_travel_mcp_server` | `TRAVEL_MCP_HOST_URL` |
| Weather/hotel/travel toolboxes | Foundry | `scripts.register_hypothesis_toolboxes` | Three `*_TOOLBOX_ENDPOINT` values |
| Workflow console | Local Python/React | `src.hypothesis_ag_ui.server` | <http://127.0.0.1:5178> |

Scripts run from the repository root, read `.env`, and use
`DefaultAzureCredential`. Authenticate with `az login` and `azd auth login`.
Bicep owns infrastructure and access-role desired state; Python only starts image
builds, service deployments, toolbox versions and the Bicep access deployment.

## Provision/deploy order

```bash
azd up
python -m scripts.deploy_travel_mcp_server
python -m scripts.register_hypothesis_toolboxes
python -m scripts.deploy_hosted_hypothesis_agent
```

Order remains **public MCP host → toolbox versions → hosted agent**. The host
serves `/weather/mcp`, `/hotels/mcp`, `/travel/mcp`, with deterministic catalogs;
the coordinator delegates only approved steps to matching specialists.

Registration does not delete old toolboxes before replacement: it creates new
versions and saves endpoints only after all creations succeed. Old versions are
retained because deployed agents reference exact versions. Teardown removes
complete toolboxes after deleting the agent.

Agent deployment uses unique image tags, waits 15 minutes for `Active`, then
applies `infra/core/ai/agent-access.bicep` to the returned instance identity before
routing 100% traffic. **Foundry User** supplies `Microsoft.CognitiveServices/*`
data actions for tools/state; **Cognitive Services OpenAI User** supplies model
inference. The built-in Foundry User definition was inspected read-only; no
separate state-store-specific role is documented in the state-store guidance.
Do not grant caller impersonation: store item operations must retain the opaque
`x-agent-foundry-call-id`. Permission grants alone do not prove hosted state-store
availability or user isolation; verify both after a separately authorized deploy.

External decision-model use is optional and requires a complete
`DECISION_MODEL_PROJECT_ENDPOINT`, `DECISION_MODEL_DEPLOYMENT_NAME`, and
`DECISION_MODEL_ACCOUNT_RESOURCE_ID` set. `DECISION_MODEL_TENANT_ID` is an
optional tenant assertion. Deployment rejects portal URLs, partial settings,
missing/inaccessible deployments, and cross-tenant targets before image build.
After obtaining the hosted-version identity, it applies
`infra/core/ai/external-model-access.bicep` in the target account's resource
group to grant only **Cognitive Services OpenAI User** on the existing account.
This supports same-tenant cross-subscription targets and does not provision or
duplicate the model. The deployer must be able to read the external
account/deployment and create role assignments at its scope.
`SKIP_ROLE_ASSIGNMENTS=true` controls only the role assignments in the `azd up`
infrastructure deployment. It never skips `ensure_agent_access` or the required
external model grant performed by the hosted-agent deployment before routing
traffic.

No Azure Storage account/connection or Blob Contributor assignment is created.
No `WORKFLOW_STORAGE_*` or `AZURE_STORAGE_ACCOUNT_NAME` is consumed by scripts.
No data migration is implemented. Removed Bicep declarations do not delete old
resources in incremental deployments; review obsolete storage and stale azd/env
outputs before manually deleting them. Do not delete a shared resource merely
because this application stopped using it.

## Controls and retention

```dotenv
TRAVEL_MCP_CPU=1
TRAVEL_MCP_MEMORY=2.0Gi
TRAVEL_MCP_MIN_REPLICAS=1
HOSTED_AGENT_CPU=1
HOSTED_AGENT_MEMORY=2Gi
DECISION_MODEL_PROJECT_ENDPOINT=https://dzwestus3-resource.services.ai.azure.com/api/projects/dzwestus3
DECISION_MODEL_DEPLOYMENT_NAME=Decision-1
DECISION_MODEL_ACCOUNT_RESOURCE_ID=/subscriptions/5ce5c590-e0ee-48cd-92f5-f5c7c04191a5/resourceGroups/westus3foundry/providers/Microsoft.CognitiveServices/accounts/dzwestus3-resource
DECISION_MODEL_TENANT_ID=1f0086c8-42bf-41a5-89c3-dc0d80c298e3
WORKFLOW_TTL_HOURS=168
LRA_STORE_WORKFLOWS=lra-workflows
LRA_STORE_EVENTS=lra-events
LRA_STORE_CHECKPOINTS=lra-checkpoints
LRA_HEARTBEAT_SECONDS=15
LRA_STALE_CLAIM_SECONDS=90
HARNESS_MAX_ITERATIONS=12
```

The TTL defaults to seven days in Bicep, deployment environment and application.
State-store creation settings only apply the first time; changing the retention
of an existing store requires an explicit SDK update/new names. Writes renew the
idle TTL; reads do not. Application expiry checks still apply. Items are limited
to 1 MB. Durable state events, not local filesystem replay, are authoritative.

## Update

After changing services, rerun travel deployment, registration and agent
deployment in that order. Agent-only changes only require hosted deployment.
Use `--tag <tag>` if an explicit image tag is operationally useful. Access grants
adopt existing assignment GUIDs to avoid `RoleAssignmentExists` when upgrading.

Use Python 3.13+ and Node 22.12+/24/26+. Create the single repository environment
with `python -m venv .venv`, activate it, and use plain `python` commands. Use
the configured npm proxy for every install/lock operation:

```bash
npm ci --registry=https://packagefeedproxy.microsoft.io/npm/
```

Lock tarballs may legitimately resolve to Microsoft's `ms-feed-*.pkgs.visualstudio.com`
backing feeds. Never rewrite them to canonical npm. The age checker checks all
pins and lock entries, including optional platform packages, against the fixed
Oct 2 cutoff and a rolling seven-day age requirement. Missing dates/yanked releases
fail closed; the proxy is not assumed to hide young versions.

## Phased approval/continue/revise contract

```bash
python -m scripts.invoke_hypothesis_agent plan "Compare Berlin and Prague"
python -m scripts.invoke_hypothesis_agent status <workflow-id>
python -m scripts.invoke_hypothesis_agent progress <workflow-id>
python -m scripts.invoke_hypothesis_agent approve \
  <workflow-id> <phase-id> <revision> <sha256:digest> approved
python -m scripts.invoke_hypothesis_agent events <workflow-id> --follow --after-seq 0
```

Plan/approval accept work with 202; poll until the phase is awaiting approval or
terminal. Echo all four approval identifiers from `phase`. `decide` aliases
approve; `rejected` terminates and approval decision `revise --comment ...`
replans in-place. Reusing `--request-id` with a different body produces 409.

```bash
python -m scripts.invoke_hypothesis_agent continue <workflow-id> "Add a day"
python -m scripts.invoke_hypothesis_agent revise <workflow-id> <phase-id> "Prioritize emissions"
python -m scripts.invoke_hypothesis_agent status <workflow-id> --phase-id <phase-id>
python -m scripts.invoke_hypothesis_agent cancel <workflow-id> --comment "Stop"
```

Continue accepts completed/failed tails. Revise accepts terminal targets and
appends after the current tail while retaining `based_on_phase_id`. Existing
phases remain immutable; every appended plan requires fresh approval. Active
phases reject concurrent mutations with 409; no steering or what-if branch exists.

## Validate without deploying

```bash
mkdir -p results
az bicep build --file infra/main.bicep --outfile results/main.json
bicep build infra/core/ai/agent-access.bicep --outfile results/agent-access.json
bicep build infra/core/ai/external-model-access.bicep --outfile results/external-model-access.json
python -m unittest discover -s tests/hosted_hypothesis_agent -p 'test_*.py'
python -m unittest discover -s tests/hypothesis_ag_ui -p 'test_*.py'
python -m unittest discover -s tests/travel_mcp_server -p 'test_*.py'
python -m unittest discover -s tests/scripts -p 'test_*.py'
python -m scripts.check_package_age
cd src/hypothesis_ag_ui/frontend && npm run test:dependencies && npm run build
```

After an authorized deployment only:

```bash
python -m scripts.test_hypothesis_plan
python -m scripts.test_hypothesis_approval --follow-up "Add a day" --revise "Lower emissions"
```

Planning polls to `awaiting_approval` and saves the nested phase envelope;
approval checks replay and polls to `completed`. Optional chained smoke phases
are planned then rejected to leave no unattended execution. Restart recovery and
cross-user isolation need separate deployed checks; local fakes cannot prove them.

## Troubleshoot

- **Toolbox/state 401/403:** verify Foundry User on the hosted instance identity,
  project endpoint, protocol 2.0.0, and original call ID. Recovered invalid call
  IDs must fail closed, never fall back to a supplied user ID.
- **Model 403:** verify Cognitive Services OpenAI User on the identity and, for
  an external decision model, on `DECISION_MODEL_ACCOUNT_RESOURCE_ID`.
- **History expires unexpectedly:** inspect application expiry and store TTL;
  changing an env variable does not mutate an existing store's retention.
- **Approval 409:** retrieve the exact current phase/revision/digest; do not reuse
  a request ID for a changed body.
- **State 413:** keep serialized items below 1 MB; there is no Blob offload.
- **Travel unavailable:** call `${TRAVEL_MCP_HOST_URL}/health`.
- **Age check fails:** inspect publish-date evidence; do not bypass it using a
  registry change or by assuming cache/lock timestamps are publish dates.

## Tear down

```bash
python -m scripts.delete_agents
azd down
```

Export any required state/history first. No automatic legacy Blob migration or
explicit cross-user state purge is performed by the cleanup script.
