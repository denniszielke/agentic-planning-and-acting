📌 Imported from my-team on 2026-10-15T16:51:35.084Z. Portable knowledge carried over; project learnings from previous project preserved below.

# Ash — History

## Learnings
- Team created 2026-10-09 for long-running-agentic-reasoning (Python, Bicep, Node.js).

## Onboarding
- **Approval is a hard protocol boundary:** plans are canonicalized and SHA-256 digested; approval must match the current revision and digest, with owner identity checked before status/execution (`src/hosted_hypothesis_agent/models.py`, `src/hosted_hypothesis_agent/agent.py`; `tests/hosted_hypothesis_agent/test_workflow.py`, `test_orchestrator.py`).
- **Workflow memory is durable, concurrency-safe state:** Blob JSON uses ETag compare-and-swap alongside record versions; workflow records track owner, expiry, request IDs and state transitions. What-if runs branch from completed baselines rather than rewriting them (`src/hosted_hypothesis_agent/workflow_store.py`, `src/hosted_hypothesis_agent/models.py`, `src/hosted_hypothesis_agent/agent.py`; `tests/hosted_hypothesis_agent/test_workflow.py`, `tests/hosted_hypothesis_agent/test_orchestrator.py`).
- **Delegate bounded evidence, not authority:** coordinator delegates each evidence category to a specialist with only its matching toolbox and an independent session; execution is scoped to the approved plan, tool activity is audited, and tool outputs are treated as untrusted (`src/hosted_hypothesis_agent/runtime.py`; `tests/hosted_hypothesis_agent/test_runtime.py`). Independent stateless MCP services are deterministic for reproducibility (`src/travel_mcp_server/server.py`, `weather_service.py`, `hotel_service.py`, `transport_service.py`; `tests/travel_mcp_server/test_services.py`).

Risks noticed: `src/hosted_hypothesis_agent/runtime.py` `_ToolboxAuth.auth_flow` does not call its token provider and assigns `Authorization` the literal `******`; verify whether this is intentional sanitization, since as written it appears to prevent toolbox authentication. Travel MCP evidence is deterministic synthetic catalog/formula output, not live availability, forecasts, or routing (`README.md`, `src/travel_mcp_server/weather_service.py`, `src/travel_mcp_server/hotel_service.py`, `src/travel_mcp_server/transport_service.py`).


## 2026-10-13T19:59:19.941+02:00 — Authentication correction and LRA continuation

- Explicit correction to the earlier `_ToolboxAuth.auth_flow` risk note: `src/hosted_hypothesis_agent/runtime.py:89-95` constructs the Authorization header with a Bearer prefix and invokes `self._token_provider()`. The earlier literal-asterisk/no-provider claim was false; rendered search output masked this expression. Local AST inspection of line 94 verified provider invocation=true, Bearer prefix=true, literal asterisks=false, without printing credentials. This source check does not prove deployed authentication or state-store access.
- 📌 Team update (2026-10-13T19:59:19.941+02:00): Ripley's actual inbox decision on linear LRA phases, Foundry state store, fail-closed recovery, seven-day retention and package/deployment constraints is merged into decisions.md — decided by Ripley, approved by user 2026-10-09. The earlier Blob/what-if onboarding description is historical, not the current direction.
- User resumed the paused LRA upgrade. Kane regression was requested synchronously but the tool converted it to background execution; in progress, no verified outcome yet. Scribe performed bookkeeping only.
