📌 Imported from my-team on 2026-10-15T16:51:35.084Z. Portable knowledge carried over; project learnings from previous project preserved below.

# Ripley — History

## Learnings
- Team created 2026-10-09 for long-running-agentic-reasoning (Python, Bicep, Node.js).

## Onboarding
- **Separate planning, approval, and execution:** the hosted orchestrator persists a plan before waiting; only an explicit decision transitions an approved plan into execution (`src/hosted_hypothesis_agent/agent.py`, `models.py`). The harness is instructed to avoid tool execution in plan mode and execute only the immutable approved plan (`src/hosted_hypothesis_agent/runtime.py`).
- **Keep component boundaries narrow and evidence auditable:** the coordinator delegates weather, hotel, and transport work to specialists that each receive only their matching MCP toolbox; the local AG-UI adapter and CLI are separate invocation clients (`runtime.py`, `src/travel_mcp_server/server.py`, `src/hypothesis_ag_ui/server.py`, `scripts/invoke_hypothesis_agent.py`). Workflow state and tool activity are persisted centrally (`workflow_store.py`, `models.py`).
- **Treat approval as a versioned, identity-bound contract:** decisions must match owner, current revision, and canonical SHA-256 plan digest; Blob ETags/version checks protect concurrent mutation. What-if is a linked child of a completed baseline, replans evidence, and requires separate approval rather than mutating the baseline (`models.py`, `workflow_store.py`, `agent.py`, `AGENTS.md`).

Risks: travel MCP evidence is deterministic/synthetic rather than live data (`README.md`); initial workflow creation precedes model planning, so an interrupted plan may leave an existing record that a same-request retry simply returns (`agent.py`); workflow storage allows public network access by default (`infra/core/storage/storage.bicep`).
