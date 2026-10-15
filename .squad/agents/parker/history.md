📌 Imported from my-team on 2026-10-15T16:51:35.084Z. Portable knowledge carried over; project learnings from previous project preserved below.

# Parker — History

## Learnings
- Team created 2026-10-09 for long-running-agentic-reasoning (Python, Bicep, Node.js).

## Onboarding
- **Model the platform as composable, environment-parameterized IaC.** `azure.yaml` selects Bicep and `infra/main.bicep` composes Foundry, storage, registry, identity, Container Apps, and monitoring modules; `infra/main.parameters.json` wires azd environment values and feature toggles.
- **Make deployment order explicit and promotion readiness-gated.** `AGENTS.md` orders host → toolbox registration → hosted agent; `scripts/_helpers.py` builds uniquely tagged (never `latest`) images and deploys Container Apps via `infra/core/host/app.bicep`; `scripts/deploy_hosted_hypothesis_agent.py` waits for `Active`, applies instance-identity grants, then sends 100% traffic to the version.
- **Prefer identity-based, repeatable access and durable environment handoffs.** `infra/core/storage/storage.bicep` disables shared keys/public blob access and grants Blob Data Contributor; `scripts/_helpers.py` checks role assignments before creating them and centrally loads/saves `.env`; `scripts/deploy_hosted_hypothesis_agent.py` grants only the needed toolbox/model/storage roles to the hosted identity.

Risks to keep visible: `scripts/register_hypothesis_toolboxes.py` deletes each current toolbox before creating its replacement, so a failed registration can cause interruption; `infra/core/storage/storage.bicep` and `infra/core/host/app.bicep` permit public network exposure, so validate intended production ingress/network restrictions (storage still disables anonymous blobs and shared-key access).
