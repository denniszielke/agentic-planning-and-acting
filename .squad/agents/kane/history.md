📌 Imported from my-team on 2026-10-15T16:51:35.084Z. Portable knowledge carried over; project learnings from previous project preserved below.

# Kane — History

## Learnings
- Team created 2026-10-09 for long-running-agentic-reasoning (Python, Bicep, Node.js).

## Onboarding

- **Test workflow invariants at the state boundary:** approval must bind to the current plan revision/digest, execution must transition once, and ownership/idempotency must hold (`tests/hosted_hypothesis_agent/test_workflow.py`, `test_orchestrator.py`; `src/hosted_hypothesis_agent/models.py`, `agent.py`).
- **Probe trust and integration boundaries explicitly:** verify one toolbox per specialist, no session propagation, caller ownership, and the mounted stateless MCP paths through focused fakes/HTTP clients (`tests/hosted_hypothesis_agent/test_runtime.py`, `test_orchestrator.py`, `tests/travel_mcp_server/test_services.py`). Keep cloud dependencies mocked in unit tests and complement them with deployed smoke checks (`AGENTS.md`).
- **Exploit determinism for repeatable service assertions:** compare identical inputs and meaningful date/route changes without external services (`tests/travel_mcp_server/test_services.py`; `src/travel_mcp_server/weather_service.py`, `hotel_service.py`, `transport_service.py`).

**Coverage gaps:** `agent.py` contract cases remain untested end-to-end: missing authenticated caller, expired workflow, stale revision, conflicting reuse of a request ID, and revise flow; Blob store tests check ETag use but not create/CAS conflicts (`test_workflow.py`, `test_orchestrator.py`). The AG-UI suite only covers a successful stream and missing workflow state—not upstream errors/non-object responses or frontend behavior (`tests/hypothesis_ag_ui/test_server.py`). Travel service tests emphasize happy-path determinism; invalid city/date/stay boundaries, hotel capability/budget/availability recommendations, transport priority ranking, and tool error JSON are not exercised (`tests/travel_mcp_server/test_services.py`).
