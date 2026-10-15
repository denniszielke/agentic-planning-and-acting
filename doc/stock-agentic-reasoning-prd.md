# Stock Agentic Reasoning Sample — Product Requirements

**Status:** Proposed; awaiting user approval  
**Owner:** Ripley (architecture and review)  
**Last updated:** 2026-10-15  
**Product posture:** Synthetic educational simulation; not financial advice or live trading

> **Approval gate:** No implementation, dependency change, infrastructure change, or travel-component removal may start until the user explicitly approves this PRD. **Approve PRD** accepts the v1 defaults in §18 and supersedes, for this stock project only, the prior 2026-10-02 dependency cutoff with the combined policy in §14. Approval authorizes W01–W28 only; it does not authorize W29–W31, deployment to Azure, traffic changes, or deletion of existing resources. Those actions require separate explicit authorization.

## 1. Executive summary

Evolve the current human-approved travel hypothesis workflow into a stock-investment simulation that demonstrates long agentic reasoning. A user defines a portfolio, companies, assumptions, competitor interactions, market side effects, constraints, and a shared time horizon. Microsoft Agent Framework supplies the harness primitives, `AgentModeProvider`, model/provider integration, and visible `TodoProvider` plan in **planning mode**. The application owns phase/revision/digest construction, owner binding, durable approval records, and enforcement; only its explicit, version-bound human approval path may switch the same phase to **execution mode**. Execution advances a deterministic synthetic market and applies simulated buy, hold, and sell decisions across multiple companies.

The product will provide:

- an Agent Framework DevUI experience for developer inspection;
- a purpose-built AG-UI React experience for scenario authoring, approvals, progress, and results;
- a `Mem0ContextProvider`-based long-term memory layer for evidence-backed strategy lessons, user preferences, and planning-process improvements across sessions;
- Foundry hosted-agent deployment and Foundry state-store durability;
- tools implemented directly in agents, with no MCP server, Foundry toolbox, brokerage, or live-market dependency.

The existing phase, event, checkpoint, idempotency, isolation, deployment, and approval concepts are retained where aligned. Travel-specific agents, tools, MCP hosting, toolbox registration, schemas, and UI language are replaced.

## 2. Problem

The current sample proves durable, human-approved planning and execution, but its travel domain and external MCP/toolbox topology obscure the desired demonstration:

1. Long reasoning should coordinate assumptions, interacting companies, simultaneous positions, constraints, and multiple time steps.
2. Planning and execution must be visibly distinct and switched only by explicit human input.
3. The plan and its progress must be inspectable through Agent Framework `TodoProvider`.
4. The same workflow must be usable from both DevUI and a polished AG-UI frontend.
5. The sample must remain deployable as a Foundry hosted agent without requiring an MCP runtime.

## 3. Goals and success measures

### Goals

- Demonstrate a 3–12 step, human-approved reasoning plan over a multi-company synthetic investment scenario.
- Support concurrent positions and decisions for sets of stocks over one shared, discrete time horizon.
- Make every material assumption, planned step, tool result, action, and state transition visible and auditable.
- Learn across sessions without weakening user isolation: retain user-scoped preferences and evidence-backed strategy/planning lessons, while allowing only sanitized, explicitly shareable lessons into agent-wide memory.
- Preserve durable recovery, caller isolation, idempotent commands, optimistic concurrency, and linear follow-up phases.
- Keep the domain safely synthetic and clearly separated from brokerage execution and individualized advice.
- Offer equivalent workflow control and state in DevUI and AG-UI, with AG-UI optimized for reviewers.

### Launch success measures

- 100% of execution runs have a matching approved `phase_id`, `plan_revision`, and `plan_digest`.
- 100% of accepted mutating requests are idempotently replayable by `client_request_id`.
- A killed hosted run resumes without duplicating a simulated action or completed todo.
- Given identical canonical scenario input, seed, engine version, and approved action set, repeated runs produce a byte-equivalent deterministic result projection; variable audit-envelope metadata is excluded from that comparison.
- Planning makes zero calls to execution-only tools; rejected plans make zero portfolio mutations.
- DevUI and AG-UI show the same todo, phase, approval, and result state for a workflow.
- A new session for the same authenticated user can retrieve approved preferences and relevant prior lessons; another user cannot retrieve user-scoped memories.
- Automated accessibility checks find no critical violations in primary AG-UI flows.

## 4. Non-goals

- Real, delayed, or historical market-data ingestion.
- Brokerage connectivity, order routing, account linking, custody, payment, or real trade confirmations.
- Predictions, guarantees, optimized personalized recommendations, suitability assessment, tax advice, or fiduciary advice.
- Modeling every market microstructure, derivative, short sale, margin loan, currency conversion, dividend, or corporate action in v1.
- Autonomous mode switching, approval inferred from natural language, or execution before approval.
- Treating remembered strategies as market facts, guaranteed recommendations, or permission to execute; storing hidden chain-of-thought or unrestricted raw workflow transcripts as memory.
- Branching/what-if trees. Follow-ups remain linear phases; comparisons may be separate workflows with different seeds or assumptions.
- MCP server deployment, Foundry toolbox registration, web search, shell access, CodeAct, or background internet research in v1.
- Migrating existing travel workflow data into the stock schema.

## 5. Personas and use cases

| Persona | Need | Primary use case |
|---|---|---|
| Evaluator | Understand long agentic reasoning and controls | Create a scenario, inspect todos, revise and approve the plan, follow execution evidence |
| Developer | Inspect Agent Framework behavior quickly | Run the hosted/local agent in DevUI, switch modes, view tool calls and todos |
| Reviewer | Validate assumptions and decisions | Compare plan revision/digest, approve/reject/revise, audit events and reproducibility metadata |
| Operator | Deploy and diagnose safely | Deploy the hosted agent, inspect traces/state, verify isolation and restart recovery |

Representative use case: “Over eight monthly steps, simulate simultaneous positions in ALPHA, BETA, and GAMMA. Assume ALPHA reduces prices, increasing unit demand but pressuring margins; BETA launches a competing product after two months; a shared supply shock raises costs. Keep at least 20% cash, cap any company at 35% of portfolio value, and decide which specified shares to buy, hold, or sell.”

## 6. Detailed scenario model

### 6.1 Canonical input

Each scenario is canonical JSON with a `schema_version` and:

- **Identity:** scenario title, simulation currency, user-supplied labels, and optional notes.
- **Time:** inclusive start date, step unit (`day`, `week`, or `month`), and 2–24 steps. All companies and positions share this clock.
- **Reproducibility:** required integer seed; simulation engine version; canonical-input SHA-256 digest.
- **Portfolio:** starting cash; positions keyed by synthetic ticker; shares and synthetic cost basis.
- **Companies:** 2–20 unique synthetic tickers, names, starting synthetic price, sector, revenue index, margin, demand/capacity indices, and volatility parameter.
- **Assumptions:** typed, visible rules with target(s), effective step/range, magnitude, lag, duration, rationale, and optional confidence label.
- **Interactions:** directed edges from a company or market factor to one or more companies, with effect type, signed weight, lag, saturation cap, and rationale.
- **Decision policy request:** the set of eligible tickers and allowed actions (`buy`, `hold`, `sell`), plus optional candidate quantities. The agent may not introduce an ineligible ticker.
- **Constraints:** minimum cash amount/ratio; maximum position amount/ratio; maximum turnover per step; optional sector cap; no negative cash, negative shares, shorting, leverage, or fractional shares in v1.

Input validation rejects unknown tickers, duplicate IDs, invalid dates, cycles with zero-lag same-step semantics, non-finite numbers, out-of-range values, and impossible starting constraints. Validation reports all field errors without invoking a model.

### 6.2 Assumptions and effects

Supported v1 assumption types are:

- baseline price/revenue/demand trend;
- company-specific demand, price, cost, margin, capacity, or volatility shock;
- market-wide demand, cost, interest-sentiment, or volatility shock;
- competitor substitution/complement effect;
- delayed launch, capacity, or supply effect.

Assumptions are user claims, not facts. The UI and outputs label them **User assumption** and never silently add one. A model-proposed normalization or derived effect appears separately and requires inclusion in the approved plan.

At each time step the engine:

1. reads the immutable prior-step snapshot for every company;
2. computes seeded exogenous shocks and all active assumptions;
3. computes directed competitor/market effects from that same prior snapshot;
4. applies documented caps and deterministic formulas;
5. commits all company values simultaneously;
6. evaluates approved portfolio actions against the resulting snapshot and constraints;
7. records a before/after ledger and explanation.

Using one prior snapshot prevents ticker iteration order from changing outcomes. Effects are bounded; prices and operating indices cannot become negative. No stochastic value may use process-randomized hashing or wall-clock time.

### 6.3 Portfolio decisions

An approved plan may produce an action batch per step:

- `buy(ticker, whole_shares)` decreases cash and increases shares;
- `sell(ticker, whole_shares)` increases cash and decreases shares, never below zero;
- `hold(ticker)` records an explicit no-change decision and rationale.

One batch may contain actions for multiple companies. The simulator prices the whole batch from one market snapshot and validates it atomically. If any action violates cash, concentration, turnover, eligibility, or holdings constraints, the batch is rejected with reasons and no partial mutation. Deterministic action ordering is ticker ascending after validation, solely for ledger presentation.

The output includes time-series company state, portfolio value/cash/exposures, accepted or rejected action batches, constraint checks, synthetic gain/loss, risk indicators (concentration, drawdown, volatility proxy), and a concise explanation tied to assumptions and evidence. “Buy/hold/sell” always means a simulated action in this scenario.

### 6.4 Reproducibility result and audit record

Every completed phase produces two explicitly separated records:

1. **Deterministic result projection:** canonical scenario digest; seed and engine/schema versions; approved action set; ordered synthetic company time series; portfolio snapshots; constraint outcomes; action ledger; and computed result/risk metrics. It uses canonical JSON serialization, stable ordering and numeric normalization, and excludes timestamps, actor/invocation IDs, recovery count, image tag, model metadata, prompt/agent metadata, and other runtime provenance. Identical canonical scenario input, seed, engine version, and approved actions must produce byte-identical projection bytes.
2. **Variable audit envelope:** redacted canonical input; plan revision/digest and todo snapshot; authenticated actor and approval record; ordered events; tool input/output digests; checkpoints; timestamps; recovery count; model deployment aliases; prompt/agent version; image tag; and a digest/reference to the deterministic result projection. This envelope is complete and ordered but is not expected to be byte-identical across executions or recoveries.

Model-generated plans need not be identical across fresh workflows. Once approved, however, execution consumes the immutable approved plan and deterministic simulator; a replay uses persisted tool results or the same deterministic computation.

## 7. Mode and human-control contract

Agent Framework provides mode/provider and harness primitives; it is not the authorization boundary. Application workflow logic creates and validates `workflow_id`, `phase_id`, `plan_revision`, and `plan_digest`, binds them to the authenticated owner, persists the approval decision, and guards every execution transition and tool call.

### 7.1 Planning mode

- A new, continue, or revise request begins in `plan` mode through `AgentModeProvider`; the mode is visible in every client.
- The harness may validate/normalize the scenario, inspect synthetic input, and create/update todos. It must not call market-evolution or portfolio-action tools.
- `TodoProvider` contains 3–12 concrete, ordered items with stable IDs and measurable completion criteria. Planning ends at `awaiting_approval`.
- The server freezes a canonical plan snapshot, todo snapshot, `plan_revision`, and `sha256:` digest. The plan is immutable after approval.
- The human may:
  - **Approve:** echo `workflow_id`, `phase_id`, revision, and digest; server records identity/time/comment, then explicitly sets `execute` mode.
  - **Revise:** provide a comment; before execution this creates the next revision in the same phase, returns to planning, refreshes todos, and requires fresh approval.
  - **Reject:** terminate the phase without any execution.
  - **Cancel:** cancel active planning where safely interruptible; otherwise record the request and stop at the next durable boundary.

Natural-language agreement, opening a page, polling, or tool approval cannot switch mode.

### 7.2 Execution mode

- Execution starts only after the exact current revision and digest are approved by the authenticated workflow owner.
- The harness executes only approved todo scope. Adding/removing/rewording a material step returns to planning and invalidates approval.
- Before each tool call, the current todo/step becomes running; after its durable result and checkpoint commit, it becomes complete. Failure leaves it incomplete and records a retryable/non-retryable error.
- **Continue** is accepted only from completed or failed tails and appends a planning phase.
- **Revise a terminal phase** appends a new planning phase with `based_on_phase_id`; it never mutates history. Later phases based on an older result are shown as superseded for interpretation, not deleted.
- **Reject** is terminal. **Cancel** never implies sell/liquidate and has no external financial effect.

### 7.3 TodoProvider lifecycle and state mapping

`TodoProvider` is the Agent Framework planning/execution surface; Foundry workflow records and checkpoints are the durable authority. Stable todo ID format is `{phase_id}:{step_number}`.

| Workflow state | TodoProvider state | Durable projection |
|---|---|---|
| Draft plan | Added, incomplete | Plan revision draft event |
| `awaiting_approval` | Incomplete, frozen snapshot | Revision/digest + todo snapshot checkpoint |
| `approved` | Incomplete | Approval record bound to snapshot |
| Step running | Incomplete | Separate step status `running` + start event |
| Step complete | Complete | Tool-result digest, step checkpoint, completion event |
| Step failed/cancelled | Incomplete | Error/cancel event; never falsely complete |
| Revised before execution | New provider list for new revision | Prior snapshot retained in audit history |
| Superseded terminal basis | No mutation of old list | Phase link/superseded projection |

Agent sessions, including TodoProvider state, are checkpointed after planning and every execution step. Recovery hydrates the session and verifies it against the durable plan/todo digest. A mismatch fails closed rather than reconstructing silently. Todo removals during revision remain visible in the prior revision snapshot.

Before workflow implementation depends on this behavior, an isolated compatibility spike must serialize an Agent Framework session containing `TodoProvider` state through the intended custom Foundry-store adapter, terminate the writing process, hydrate it in a separate process, and verify exact todo IDs, order, completion state, mode-relevant session data, and digest. It must also exercise incompatible/corrupt payload failure. The published TodoProvider sample demonstrates same-session persistence only and is not evidence of custom-store or cross-process recovery compatibility.

### 7.4 Durability, idempotency, and concurrency

- Retain Foundry state-store collections for workflows, append-only events, and checkpoints/tool results; default idle TTL remains seven days and item payloads remain below the existing 1 MB safety ceiling.
- Every mutating command requires `client_request_id`. Same ID and same canonical body returns the stored response with `replayed=true`; same ID with a different body returns `409 request_conflict`.
- Workflow creation is deterministic for owner/request ID. Recovery must finish interrupted initial planning rather than treating a pre-plan record as success.
- One active mutation/agent run per workflow. ETag/version mismatches, stale approval tuples, or concurrent mode changes return 409 with current safe state.
- Each simulated action batch has a deterministic idempotency key derived from workflow, phase, approved digest, step, and time index. A retry returns its ledger result and never applies twice.
- Heartbeats and stale claims support restart recovery. Invalid recovered `x-agent-foundry-call-id` fails closed; caller identity is never substituted from request content.
- Reads may run concurrently. Event sequence numbers are monotonic per workflow; reconnect uses `after_seq` without duplicate state effects.

### 7.5 Long-term learning context

The stock reasoning harness uses Agent Framework `Mem0ContextProvider` as a separate long-term context layer. Unlike workflow checkpoints, this layer persists across newly created sessions and is not governed by the workflow's seven-day idle TTL.

Memory is divided into explicit classes and scopes:

| Memory class | Default scope | Eligible content | Write rule |
|---|---|---|---|
| User preference | Authenticated user (`user_id` + explicit `search_user_id`) | Response/detail preferences, planning style, preferred risk/constraint presentation, and UI choices | Explicit user instruction or user-confirmed inference only |
| Strategy outcome | Authenticated user | Synthetic strategy pattern, scenario conditions, actions, constraint outcomes, result metrics, and user disposition | Only after a durable terminal run; store evidence and outcome, never a bare "winning strategy" claim |
| Planning-process lesson | Authenticated user | Useful todo decomposition, revision reason, failure/recovery pattern, and reviewer feedback | Only after completion/rejection/revision provides attributable evidence |
| Shared strategy/process lesson | Agent-wide (`agent_id` + explicit `search_agent_id`) | De-identified pattern safe for every user, with applicability limits and aggregate/synthetic evidence | Opt-in promotion policy; no user text, portfolio, ticker set, preference, identity, or workflow payload |

User-scoped retrieval is the default and is always explicit; storage scope never implicitly becomes retrieval scope. Agent-wide retrieval is separately configured and disabled until W09A proves isolation and the promotion/redaction policy. A memory record includes a stable ID, class, scope, sanitized content, applicability tags, source workflow/result digests where allowed, engine/prompt/agent versions, evidence summary, confidence, creation/update timestamps, and supersession status.

A "successful strategy" means only that a synthetic run completed, respected constraints, met its declared scenario objective, and was accepted or positively rated by the user. Synthetic gain alone is insufficient. Memories retain the assumptions and conditions under which an outcome occurred, may record failures and counterexamples, and must not be phrased as predictions or financial advice.

At the start of planning, the provider retrieves a bounded, relevance-ranked set of memories for the authenticated user and, when enabled, sanitized agent-wide lessons. Retrieved memory is untrusted context: it cannot add an assumption, expand eligible tickers/actions, alter constraints, change mode, satisfy approval, or call a tool. Any memory that materially influences a proposed plan is surfaced by memory ID, scope, concise summary, and applicability caveat in the plan. The approved plan snapshot records the selected memory IDs and content digests. Memory is excluded from the deterministic result projection but its retrieval/write provenance belongs in the variable audit envelope.

Writes occur only after the corresponding workflow/event/result evidence is durable. Replays use a deterministic memory-operation key and cannot create duplicates. Failed, cancelled, rejected, or later-disproved lessons are marked with outcome/supersession state rather than silently deleted. Users can list, inspect, correct, forget, and export their user-scoped memories from both clients. Deletion is owner-authorized and auditable; agent-wide promotion/removal is an operator-governed action. Provider unavailability is visible and does not fall back to another user's or unscoped memory; planning may continue without long-term context only when the UI clearly reports degraded memory and the request does not require a memory mutation.

## 8. Functional requirements

| ID | Requirement | Measurable acceptance criteria |
|---|---|---|
| FR-001 | Create and validate a canonical stock scenario. | Valid 2–20 company/2–24 step input is accepted; malformed references and impossible constraints return field-level 422 errors without model/tool calls. |
| FR-002 | Support simultaneous multi-company evolution. | A test with three interacting companies proves each step uses one prior snapshot and is invariant to input company order. |
| FR-003 | Support synthetic buy, hold, and sell batches. | Atomic multi-ticker batches update cash/shares correctly; invalid batches produce zero partial changes. |
| FR-004 | Enforce cash and risk constraints. | Tests cover minimum cash, no shorting/leverage, concentration, turnover, and optional sector limits at every action step. |
| FR-005 | Make deterministic results reproducible without conflating runtime audit metadata. | Same canonical input, seed, engine version, and approved actions produce byte-equivalent §6.4 deterministic result projections in 20 repeated runs; audit envelopes may differ in timestamps, recovery, image, model, actor, and invocation metadata while retaining a valid projection digest. |
| FR-006 | Use Agent Framework harness modes under application-owned approval enforcement. | Agent Framework supplies mode/provider and harness primitives; new phases begin in plan mode, while application logic validates owner/phase/revision/digest and the durable approval record before invoking `set_agent_mode(..., "execute")`; direct provider/tool calls cannot bypass the guard, and mode appears in status/events. |
| FR-007 | Use `TodoProvider`. | Plan creates 3–12 stable todos; DevUI and AG-UI show them; completion matches durable step state after restart. |
| FR-008 | Bind approval to immutable scope. | Wrong phase/revision/digest/owner returns 409/403; approved exact tuple starts execution and is recorded with timestamp. |
| FR-009 | Support revise/reject/continue/cancel. | State-transition tests cover pre-execution revision, rejection with zero actions, linear follow-up, terminal revision links, and cancellation boundaries. |
| FR-010 | Persist workflow state and recover. | Process termination after every durable boundary resumes without lost completed steps or duplicated actions. |
| FR-011 | Guarantee request idempotency and concurrency safety. | Replay and body-conflict tests pass; two simultaneous mutations yield one accepted claim and one 409. |
| FR-012 | Embed tools directly in agents. | Runtime has no MCP client/server/toolbox import or endpoint requirement; all domain tools are registered Python functions/providers. |
| FR-013 | Provide DevUI. | Developer can discover the agent, create/resume a session, request planning, approve the exact plan to switch to execution, view todos/tool calls, and complete the reference scenario. |
| FR-014 | Provide AG-UI. | Reviewer can author/import a scenario, inspect assumptions/plan/digest, revise/reject/approve, reconnect, follow steps/events, and inspect/export results. |
| FR-015 | Project one canonical state to both UIs. | Contract tests feed the same workflow envelope to both adapters and verify matching phase, mode, todo, progress, approval, and result values. |
| FR-016 | Keep the sample safe. | Every primary screen/result states “Synthetic simulation — not financial advice or live trading”; no network/brokerage tool exists. |
| FR-017 | Preserve auditability. | Ordered export includes assumptions, revisions, approval, todos, tool/result digests, actions, errors, actor IDs, and timestamps. |
| FR-018 | Enforce dependency provenance. | CI fails on unpinned Python requirements, wrong/unavailable authenticated feed, untrusted metadata, unavailable/yanked/missing-date releases, any publication date after 2026-10-07 UTC, or any release younger than seven complete 24-hour periods at validation time. |
| FR-019 | Persist safe long-term learning across sessions. | A fresh session for the same authenticated user retrieves relevant preferences and evidence-backed strategy/planning lessons; cross-user probes retrieve none; agent-wide retrieval returns only promoted sanitized lessons; replay creates no duplicate memory; both UIs support inspect/correct/forget/export; memory cannot change approved scope or execution mode. |

## 9. Proposed agent and tool architecture

### 9.1 Components

1. **Stock workflow host (Foundry hosted container):** retains invocations protocol, task dispatch, phase controls, recovery, and owner enforcement.
2. **Stock reasoning harness:** built with `create_harness_agent`, explicit `AgentModeProvider`, `TodoProvider`, audit middleware, and bounded iteration count.
3. **Scenario analyst agent:** turns user assumptions into a testable plan. It has read-only validation/inspection functions only.
4. **Simulation/portfolio agent capability:** execution-only Python tools, registered directly with the harness or a bounded specialist agent. No tool crosses a network boundary.
5. **Deterministic domain library:** pure functions for validation, synthetic evolution, constraint evaluation, action application, metrics, and canonical digests.
6. **Foundry workflow store:** workflows, events, checkpoints/session snapshots, request replay records, and tool/action ledger.
7. **Long-term memory context:** `Mem0ContextProvider` with explicit user-scoped storage/retrieval and an independently governed, sanitized agent-wide scope for shareable lessons.
8. **DevUI endpoint** and **AG-UI adapter/frontend:** two projections over the same workflow API and durable state.

A separate specialist may be used for simulation/portfolio analysis to keep prompts bounded, but it receives direct Python tools and the exact approved step only. It cannot alter mode, approval, scenario, or todo scope. Per-service-call history persistence is required if Agent Framework handoff workflow abstractions are adopted.

### 9.2 Embedded tool catalog

| Tool | Mode | Effect |
|---|---|---|
| `validate_scenario` | Plan/execute, read-only | Schema, reference, and feasibility validation |
| `summarize_assumptions` | Plan, read-only | Structured visible summary; no new assumptions |
| `initialize_simulation` | Execute | Deterministic initial snapshot/checkpoint |
| `advance_market_step` | Execute | Simultaneously evolves all companies for one step |
| `evaluate_action_batch` | Execute, read-only | Constraint and risk checks against one snapshot |
| `apply_simulated_actions` | Execute | Atomically updates the synthetic portfolio ledger |
| `calculate_portfolio_metrics` | Execute, read-only | Value, allocation, drawdown, concentration, volatility proxy |
| `finalize_simulation_report` | Execute | Produces structured result from persisted evidence |

Execution tools reject calls without the active approved phase context. Inputs and outputs are size-limited, validated, redacted for logs, and persisted by digest. Tools never fetch prices, browse the web, or contact a broker.

## 10. User experiences

### 10.1 DevUI requirements

- Expose discoverable stock agent/workflow entities using the supported Agent Framework DevUI integration.
- Provide a seeded reference scenario and developer-editable JSON input.
- Surface session/thread ID, workflow/phase ID, current mode, todos, tool calls, interrupts/approvals, errors, and final structured output.
- Surface retrieved-memory summaries and provenance, memory degradation, and user-scoped inspect/correct/forget/export operations without exposing other users' memories.
- Require the same server-side approval tuple and authenticated owner checks as AG-UI; DevUI controls must not bypass the workflow API.
- Support reconnect/resume and a clear “new scenario” action.
- DevUI is a developer surface, not the production reviewer UX; no feature may exist only in DevUI if needed to complete the core approval flow.

### 10.2 AG-UI requirements

- **Scenario editor:** structured portfolio, companies, assumptions, interactions, horizon, seed, eligible actions, and constraints; JSON import/export; client and server validation.
- **Planning view:** visible plan mode, revision, digest, todos, assumptions, expected evidence, constraint summary, and revision history.
- **Memory view:** show which user-scoped and sanitized shared lessons informed planning, their applicability caveats, and controls to inspect, correct, forget, or export user memories.
- **Approval console:** distinct Approve, Request revision, and Reject actions; confirmation summarizes simulated scope and echoes exact identifiers. Buttons disable on stale/running state.
- **Execution view:** step/todo timeline, live mode/stage, current synthetic date, tool activity, reconnect status, cancellation, and constraint failures.
- **Results view:** synchronized charts/tables for company paths, cash/positions/value, action ledger, risk metrics, assumption/effect trace, reproducibility record, warnings, and audit export.
- **Conflict UX:** 409 errors explain stale revision, active run, or reused request ID and offer refresh without silently retrying a mutation.
- **Safety UX:** persistent synthetic-data badge; disclaimer adjacent to action controls and results; use “simulate buy/sell,” never “place order.”

### 10.3 AG-UI event/state projection

The backend emits standard run/text events where useful and projects the canonical workflow through `STATE_SNAPSHOT`/`STATE_DELTA`. Domain activity uses named `CUSTOM` events. Required state keys are:

`workflow`, `phase`, `mode`, `scenario_summary`, `assumptions`, `memory_summary`, `todos`, `approval`, `progress`, `portfolio`, `market`, `action_ledger`, `stats`, `errors`, and `reproducibility`.

Required custom events include `mode.changed`, `memory.retrieved`, `memory.written`, `memory.corrected`, `memory.forgotten`, `memory.degraded`, `todo.changed`, `stage.changed`, `tool.started`, `tool.completed`, `tool.failed`, `market.step_completed`, `action.batch_evaluated`, `action.batch_applied`, `approval.recorded`, and terminal phase events. Events contain IDs/digests and bounded summaries, not hidden chain-of-thought or unrestricted memory content. On reconnect, the client fetches a snapshot then resumes ordered events after the last sequence. AG-UI `thread_id` aids correlation but is never an authorization boundary; scope resolves from authenticated caller identity.

## 11. Foundry hosting, state, and deployment

### Retained and evolved

- Retain ACR build, unique image tags, Foundry hosted-agent versions, activation wait, 100% traffic routing, invocations protocol, managed identity, Application Insights, Foundry project/model configuration, optional external decision model, and Bicep-owned access roles.
- Retain `Foundry User` for hosted state operations and `Cognitive Services OpenAI User` for inference, subject to least-privilege validation.
- Retain workflow/event/checkpoint state stores, call-ID propagation, seven-day default idle TTL, optimistic versions, 1 MB platform limit with a lower application guard, and explicit deployed recovery/isolation tests.
- Configure the long-term memory backend and `Mem0ContextProvider` independently from workflow TTL/state. Its credentials and endpoints use managed configuration, user and agent retrieval scopes are explicit, and provider health/isolation are deployment gates.
- Rename travel/hypothesis configuration and resources only where migration safety permits. New stock store names are preferred to avoid schema collision; there is no travel-data migration.

### Removed or replaced

| Current component | Disposition |
|---|---|
| `src/travel_mcp_server/` and its container image/app | Remove from desired state after stock cutover; retain git history only as migration reference |
| `scripts/deploy_travel_mcp_server.py` | Remove; no replacement deployment step |
| `scripts/register_hypothesis_toolboxes.py` | Remove; no toolbox registration step |
| Weather/hotel/travel toolbox versions and endpoint/allowed-tool variables | Remove from stock runtime; inventory before optional manual cleanup |
| Travel specialists and `MCPStreamableHTTPTool` runtime | Replace with stock agents using direct Python tools |
| Travel schemas/prompts/tests and travel UI labels | Replace with stock scenario equivalents |
| Container Apps environment created solely for travel MCP | Remove from future desired state if no shared dependency; never delete a shared resource automatically |
| Existing hosted-agent deployment script | Evolve/rename for the stock image, direct-tool environment, state names, roles, and smoke tests |
| Existing travel artifacts/state | No migration; preserve until separately authorized retention/export decision |

After W21–W28 static validation and separate explicit deployment authorization, the W29–W30 live deployment order becomes:

```text
azd up → deploy stock hosted agent → run authorized hosted smoke/isolation/recovery tests
```

There is no public MCP host or toolbox registration step. Incremental Bicep does not delete obsolete resources; cleanup requires an inventory, ownership check, and separate user authorization.

## 12. Safety, security, and isolation

- All prices, companies, news-like events, and outcomes are synthetic/demo data. Synthetic tickers should avoid representing a claim about a real issuer; any familiar symbols in documentation are labeled illustrative mock values.
- Every output exposes assumptions, seed, engine version, limitations, and uncertainty. No hidden chain-of-thought is stored or shown; concise rationales and tool evidence are.
- The agent never asks for brokerage credentials, account numbers, personal risk profile, or regulated suitability data. Logs and telemetry exclude free-form full payloads by default.
- No brokerage SDK, market-data API, web search, arbitrary URL fetch, shell, filesystem write, or MCP endpoint is available to the runtime.
- Caller identity comes from the hosted request context. Workflow, session snapshot, event, checkpoint, and export reads enforce owner scope. Cross-user existence is not disclosed.
- User-scoped memory keys derive only from authenticated caller identity. Request bodies cannot select another `user_id`; agent-wide search is disabled unless the sanitized shared-memory policy is enabled and tested.
- Raw scenarios, positions, free-form user text, hidden reasoning, approval comments, and user preferences are never promoted to agent-wide memory. Memory content is treated as untrusted data and cannot override system instructions or approval guards.
- Tool outputs and user text are untrusted data, not instructions. Schema validation, bounded strings/collections, canonical serialization, allow-listed actions, and execution context checks reduce prompt/tool injection risk.
- Secrets use environment/managed identity, never scenario data or client state. RBAC remains least privilege.
- Rate, payload, model-iteration, todo-count, time-horizon, event-page, and execution-time limits are enforced.

## 13. Quality attributes

### Observability

- Correlate traces with workflow, phase, invocation, AG-UI thread, client request, step/todo, tool, and action-batch IDs.
- Record stage/mode durations, human wait, model/tool calls, tokens when available, recoveries, constraint rejections, replay/cache hits, stream reconnects, and failures.
- Record bounded memory retrieval/write latency, result counts, scope, promotion/rejection reason, duplicate suppression, degraded operation, correction, and deletion without logging memory content.
- Dashboards/queries distinguish planning from execution and simulation rejection from system failure. Never emit model hidden reasoning or full sensitive prompts.

### Accessibility

- Meet WCAG 2.2 AA for primary AG-UI flows: keyboard-only completion, visible focus, semantic headings/forms/tables, labeled errors, non-color status cues, screen-reader announcements for live progress, reduced-motion support, and chart data tables.
- Approval/rejection controls require descriptive labels and do not depend on icons or color.

### Performance and reliability

- Scenario validation: p95 under 500 ms locally without a model.
- Status/read API: p95 under 1 s excluding Azure network variance.
- Bounded long-term memory retrieval: p95 under 2 s excluding provider network variance; timeout degrades visibly without broadening scope.
- First streamed planning event: p95 under 5 s excluding model queueing.
- A 20-company, 24-step pure simulation: p95 under 2 s in unit benchmarks.
- AG-UI applies a 200-event replay page without blocking the main thread for more than 100 ms per task.
- Tool/model calls have explicit timeouts and bounded retries. Retries occur only at idempotent boundaries.
- The UI degrades to polling if event streaming fails and visibly marks stale state.

## 14. Dependency, feed, and cutoff policy

- User approval of this PRD explicitly supersedes the active 2026-10-02 maximum publication date recorded in `.squad/decisions.md` **for this stock project only**. The new fixed maximum publication date is **2026-10-07 UTC**.
- The rolling minimum-age control is retained. At each validation, every selected release must both (a) be published on or before 2026-10-07 UTC and (b) be at least seven complete 24-hour periods old. The effective latest allowed timestamp is therefore the earlier of the end of 2026-10-07 UTC and `validation_time - 168 hours`. Both tests fail closed.
- Python packages must install only through `https://packagefeedproxy.microsoft.io/pypi/simple`. Requirements are exact stable pins; no ranges, prereleases, direct URLs, editable installs, or unpinned transitive runtime dependencies.
- npm continues through `https://packagefeedproxy.microsoft.io/npm/`; lockfile integrity and all optional/platform entries remain checked.
- Authenticated access to both approved proxies and to a trustworthy, documented publication-date metadata source for every selected Python and npm release is a prerequisite to dependency selection, lock generation, or clean restore. Credentials remain outside the repository. W01 must identify the supported metadata path and record access evidence; W19 and all dependent work remain blocked if access or trustworthy metadata is unavailable.
- Do not name or select new versions in implementation planning until availability, publication timestamps, and the combined fixed-cutoff/rolling-age rule are verified.
- Adapt `scripts/check_package_age.py` to:
  - enforce both approved feeds/configuration;
  - inventory all Python pins and every npm lock entry;
  - obtain publication evidence through the configured proxy/its supported metadata;
  - reject unavailable, entirely yanked, prerelease, missing-date, post-cutoff, or younger-than-seven-complete-days releases;
  - fail closed on proxy/metadata errors;
  - emit auditable package/version/source/publication-date/feed evidence;
  - prohibit CLI flags from relaxing the fixed cutoff or approved feeds.
- CI creates a clean environment using the approved Python feed, uses `npm ci` with the approved npm feed, runs the age checker with cache disabled, and archives evidence. Existing pins are not grandfathered.

## 15. Testing and acceptance strategy

1. **Pure domain tests:** schema/property tests, simultaneous update order invariance, seeded golden vectors, effect bounds, atomic action batches, constraints, canonical digests.
2. **Agent/mode tests:** plan-only tool denial, todo creation, explicit mode switch, immutable scope, max iterations, direct-tool registration, bounded memory influence, and no MCP imports/endpoints.
3. **Workflow tests:** every state transition, revision/rejection/continue/cancel, owner isolation, stale approvals, ETags, idempotent requests/actions, expiry, 1 MB guard, event ordering.
4. **Memory tests:** cross-session retrieval, user/agent scope isolation, sanitized promotion, success qualification, provenance, correction/forget/export, idempotent writes, prompt-injection resistance, provider timeout/degradation, and cross-user denial.
5. **Recovery fault injection:** stop after claim, mode change, each tool result, action ledger write, todo completion, terminal save, and memory write; prove no duplication or false completion.
6. **API/AG-UI contract tests:** snapshots/deltas/custom events, memory summaries/events, resume cursor, stream-to-poll fallback, error mapping, thread scope.
7. **Frontend tests:** scenario authoring, keyboard approval, stale conflict, revision, reconnect, memory controls/provenance, charts plus data tables, safety copy, accessibility automation.
8. **DevUI smoke:** discovery, session continuity, cross-session memory, todos, interrupt/approval, results.
9. **Deployment tests:** before separate deployment authorization, static Bicep build, image build, template/role analysis, and deployment-command validation only; after authorization in W29–W30, hosted activation, live RBAC, health/invocation, state-store access, memory-provider isolation, cross-user denial, restart recovery, and telemetry correlation.
10. **Dependency tests:** approved feeds, clean restore, publication cutoff, yanked/missing dates, lock/manifest drift.

Release acceptance requires all automated suites, a reference-scenario golden run in both UIs, and separately authorized deployed recovery and cross-user isolation checks. Local fakes cannot satisfy hosted state/isolation gates.

## 16. Rollout and migration

1. Approve this PRD, thereby accepting the §18 defaults and authorizing W01 prerequisite discovery.
2. Build stock domain and workflow behind stock-specific names; do not alter the deployed travel route.
3. Add the long-term memory context provider, prove same-user cross-session retrieval and cross-user isolation, then add DevUI and AG-UI stock paths and run local contract/golden tests.
4. After separate explicit deployment authorization, provision new stock state-store names and deploy a parallel stock hosted-agent version in W29.
5. Run the authorized hosted smoke, recovery, and isolation gates in W30.
6. Route the sample UI/CLI to stock only after gate approval; retain travel deployment for rollback during a defined observation window.
7. Remove MCP/toolbox steps from desired deployment and operator documentation.
8. Inventory travel Container App, toolbox versions, environment values, images, and state. Export if required; delete only with separate authorization and confirmed ownership.

Rollback routes clients to the last known hosted version and preserves stock state. It does not recreate or depend on travel MCP after final cleanup.

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Buy/hold/sell language is mistaken for advice or a real order | Persistent synthetic labeling, simulated verbs, no live data/broker tools, visible assumptions and limitations |
| Model invents assumptions or expands approved scope | Typed input, plan diff, frozen digest/todos, direct tools enforcing approved context |
| TodoProvider and workflow state diverge | Stable IDs, checkpointed session, digest verification, fail-closed recovery, contract tests |
| Memory leaks between users or preserves unsafe/raw content | Explicit user retrieval scope, sanitized opt-in agent scope, authenticated controls, redaction/promotion policy, adversarial isolation tests |
| Remembered "successful" strategy becomes stale or misleading | Store conditions/evidence/outcomes, include failures, version and supersede lessons, surface applicability, never treat memory as fact or approval |
| Long-term memory provider is unavailable | Bounded timeout, visible degraded mode for reads, fail closed for writes/required memory operations, no unscoped fallback |
| Determinism is undermined by concurrency/order/time | Seeded PRNG, canonical ordering, prior-step snapshots, no wall clock in calculations, golden tests |
| State payload exceeds Foundry limit | Bounded scenario, compact checkpoints, application guard below 1 MB, paged events; no undocumented blob offload |
| DevUI semantics diverge from custom AG-UI | One workflow service/state contract and cross-client projection tests |
| Removing travel infrastructure deletes shared resources | Desired-state removal first; inventory, ownership validation, and separately approved manual deletion |
| Approved feed lacks reliable publication metadata | Fail closed; document proxy-supported evidence mechanism before selecting versions |
| Initial planning crash leaves an incomplete workflow | Recoverable planning claim/checkpoint and fault-injection acceptance gate |

## 18. Approval defaults and prerequisite discovery

Approval accepts these safe v1 defaults:

1. The reference scenario uses clearly fictional tickers only.
2. Twenty companies and 24 steps are fixed service maxima; deployments may configure lower limits only.
3. A rejected constraint batch fails the execution step with no partial mutation; `continue` may then append a new planning phase.
4. Owner-authorized audit export may contain the canonical scenario body; telemetry and non-owner surfaces use only the redacted body plus digest.
5. Travel remains available for rollback through at least 14 consecutive healthy days after stock cutover. Cleanup still requires an inventory, confirmed ownership, export/retention decision, and separate deletion authorization.
6. W01 must prove authenticated access to the approved PyPI and npm proxies and choose/document their trustworthy publication-date metadata path (proxy-supported metadata or an approved internal metadata endpoint). This is a prerequisite, not a deferrable release question: W19 and downstream dependency/build work must fail closed and remain blocked if access, dates, or trust provenance cannot be established.
7. A repository-root `README.md` is currently absent. This PRD does not cite one as an existing repository source and does not recreate it; the current operator source remains `AGENTS.md`, while any new root README is a separately scoped documentation decision.
8. Long-term memory is user-scoped by default and persists across sessions until owner-authorized deletion or a separately approved retention policy. User preferences never enter agent-wide memory. Only de-identified strategy/planning lessons that pass the promotion policy may be shared through explicit agent scope; agent-wide sharing remains disabled until W09A passes.

No downstream application work may start until PRD approval and W01 completion. W01 may not weaken these defaults; changing one requires an approved PRD revision.

## 19. Phased implementation plan

Each item is independently reviewable and sized to at most one working day. `L` means the upper bound of one day, not a multi-day item. Ripley owns architecture/review gates only.

| ID | Phase | Assignee | Size | Dependencies | Deliverable | Acceptance gate |
|---|---|---|---|---|---|---|
| W01 | Architecture | Ripley | S | PRD approval | Approved interface/state/mode decision checklist plus authenticated proxy/publication-metadata prerequisite evidence required by §14 and §18 | Defaults recorded; trustworthy metadata path proven or downstream work marked blocked; no application code |
| W02 | Domain | Ash | M | W01 | Stock scenario Pydantic contracts and canonicalization | FR-001 validation tests pass |
| W03 | Domain | Ash | L | W02 | Deterministic market-step pure functions | Order-invariance and seeded golden tests pass |
| W04 | Domain | Ash | L | W02 | Portfolio action/constraint pure functions | Atomicity and FR-003/004 tests pass |
| W05 | Workflow | Ash | M | W02, W09 | Stock phase/plan/result/event contracts and separate deterministic-result/audit-envelope digest mapping | Serialization, §6.4 projection, and migration-boundary tests pass |
| W06 | Workflow | Ash | L | W03, W04, W05 | Direct embedded stock tool catalog with execution-context guards | No MCP dependency; tool unit tests pass |
| W07 | Workflow | Ash | L | W05, W09 | Harness with `AgentModeProvider` and `TodoProvider` planning path plus application-owned approval guard | Plan creates frozen 3–12 todos; execution tools and direct mode bypass denied |
| W08 | Workflow | Ash | L | W06, W07 | Approved execution loop, todo completion, deterministic action ledger | Exact approval required; replay applies actions once |
| W09 | Compatibility spike | Ash | M | W01 | Isolated TodoProvider custom-store serialization/hydration and cross-process recovery spike/test | A writer process persists session/todos through the intended adapter; a fresh reader process hydrates exact IDs/order/state/digest; corrupt/incompatible data fails closed; same-session persistence alone does not pass |
| W09A | Memory compatibility spike | Ash | M | W01 | `Mem0ContextProvider` user/agent scope, authenticated identity mapping, backend persistence, package/configuration, and promotion/redaction proof | Fresh-session same-user retrieval passes; cross-user probes return none; raw/user-specific content cannot enter shared scope; provider/auth failures are explicit |
| W09B | Long-term memory integration | Ash | L | W05, W07, W09A | Memory schemas, bounded planning retrieval, evidence-backed/idempotent writes, provenance events, and inspect/correct/forget/export service operations | FR-019 tests pass; memory cannot mutate assumptions, todos, approval, mode, or execution scope |
| W10 | Durability | Ash | L | W08, W09 | Execution recovery, idempotency keys, concurrency handling | Fault/replay/409 matrix passes |
| W11 | Architecture | Ripley | M | W06, W09B, W10 | Direct-tool, mode, memory, state, and safety architecture review | Written review approval; implementation changes routed back to Ash |
| W12 | DevUI | Lambert | M | W07, W09B | Agent Framework DevUI entity/endpoint and seeded developer scenario | Agent discoverable; plan/todos and memory provenance/controls visible |
| W13 | DevUI | Lambert | M | W08, W12 | DevUI approval/resume/result flow | Reference scenario completes without bypass |
| W14 | AG-UI | Lambert | L | W05, W09B | Typed AG-UI state/event client and reconnect reducer | Snapshot/delta/memory/event contract tests pass |
| W15 | AG-UI | Lambert | L | W14 | Accessible scenario editor and validation UI | Keyboard flow and field error tests pass |
| W16 | AG-UI | Lambert | L | W08, W14 | Plan/todo/revision/approval console | Stale tuple blocked; revise/reject/approve tests pass |
| W17 | AG-UI | Lambert | L | W10, W14 | Execution timeline, reconnect/conflict/cancel UX | Stream/poll and 409 tests pass |
| W18 | AG-UI | Lambert | L | W03, W04, W17 | Results, assumption trace, risk tables/charts, audit export | Safety copy and accessible data alternatives pass |
| W19 | Automation | Parker | M | W01 | Python/npm feed and combined fixed-cutoff/rolling-age checker adaptation | Authenticated proxy/metadata prerequisite is proven; negative feed/date/age/yank tests fail closed |
| W20 | Automation | Parker | M | W06, W19 | Stock container requirements/Docker build using approved feed | Clean proxy-only build and evidence pass |
| W21 | Infrastructure | Parker | L | W05, W20 | Stock hosted deployment/state configuration artifacts; remove MCP/toolbox runtime inputs | Static schemas/templates/scripts validate; image can be built locally; no Azure deployment, state provisioning, role assignment, traffic change, or Active-state claim |
| W22 | Infrastructure | Parker | M | W21 | Bicep/azd desired-state change removing travel-only Container Apps dependency where safe | Bicep build passes; shared-resource inventory recorded |
| W23 | Automation | Parker | S | W21, W22 | Deployment, invocation, cleanup, and operator script/docs updates | Target order has no MCP/toolbox step; cleanup requires authorization |
| W24 | QA | Kane | L | W03, W04, W08, W09B | Domain and agent golden/invariant/mode/memory test suite | FR-001–008, FR-012, and FR-019 pass |
| W25 | QA | Kane | L | W09B, W10 | Workflow recovery/idempotency/memory/isolation fault suite | FR-009–011 and memory fault gates pass locally; hosted-only gates identified |
| W26 | QA | Kane | L | W13, W16, W17, W18 | Cross-client contract, E2E, and accessibility suite | FR-013–017 pass in local CI |
| W27 | QA | Kane | M | W19, W20, W23 | Dependency/build/deployment static validation suite | FR-018, clean restore, Bicep/image/template/script checks pass without live Azure deployment |
| W28 | Review | Ripley | M | W11, W24, W25, W26, W27 | Architecture and acceptance evidence review | No critical gaps; explicit recommendation for hosted validation |
| W29 | Hosted deployment | Parker | M | W21, W23, W28, separate explicit deploy authorization | Provision stock state configuration, apply roles, deploy candidate, wait for Active, and collect activation/RBAC/telemetry evidence | Agent Active; invocation healthy; no traffic cutover or travel deletion |
| W30 | Hosted validation | Kane | L | W29 | Hosted smoke, restart recovery, cross-user isolation, replay tests | All release gates pass with evidence |
| W31 | Final review | Ripley | S | W30 | Go/no-go and migration cleanup recommendation | User approves cutover separately |

Work can parallelize only when dependencies permit; notably W03/W04 and W09/W09A after their prerequisites, W12/W14 after their prerequisites, W19 after W01, and later UI/test streams. W09 is an early TodoProvider compatibility gate: W05, W07, and their downstream application work cannot proceed until it passes. W09A is the independent long-term-memory isolation/persistence gate; W09B and all memory-dependent UI/test work remain blocked until it passes. W21–W28 produce and review local/static artifacts only. Only W29 may perform the first live Azure changes, and only after separate explicit deployment authorization. No item authorizes deletion of deployed travel resources.

## 20. Approval

Approval response should explicitly state one of:

- **Approve PRD** — accepts the §18 defaults and the §14 superseding cutoff/rolling-age policy, and authorizes routing W01–W28 subject to their dependencies. It does not authorize W29–W31, Azure deployment, traffic changes, cutover, or resource deletion.
- **Request revision** — identify sections or decisions to change; implementation remains blocked.
- **Reject PRD** — closes this proposal; implementation remains blocked.

## 21. References

### Microsoft Agent Framework examples

1. [Build your own claw and agent harness](https://github.com/microsoft/agent-framework/blob/main/python/samples/02-agents/harness/build_your_own_claw/README.md) — harness planning/modes, mock stock tools, approvals, durable memory, and simulated-trade safety.
2. [Agent Framework DevUI samples](https://github.com/microsoft/agent-framework/tree/main/python/samples/02-agents/devui) — developer entity discovery and interactive agent/workflow testing.
3. [AG-UI workflow handoff](https://github.com/microsoft/agent-framework/tree/main/python/samples/05-end-to-end/ag_ui_workflow_handoff) — thread state, interrupts, human approval, resume payloads, and custom React workflow UI.
4. [AG-UI single agent](https://github.com/microsoft/agent-framework/tree/main/python/samples/05-end-to-end/ag_ui_single_agent) — minimal endpoint, streaming events, server-side thread history, scope resolver, and trace correlation.
5. [`TodoProvider` sample](https://github.com/microsoft/agent-framework/blob/main/python/samples/02-agents/context_providers/todo_provider.py) — todo tools and session-persisted lifecycle across turns.
6. [Scaling harness capabilities](https://github.com/microsoft/agent-framework/blob/main/python/samples/02-agents/harness/build_your_own_claw/claw_step03_scaling_capabilities.py) — direct function tools, mode provider, bounded capabilities, simulated approval-required trades, and multi-agent patterns.
7. [`Mem0ContextProvider` sessions sample](https://github.com/microsoft/agent-framework/blob/main/python/samples/02-agents/context_providers/mem0/mem0_sessions.py) — explicit user-scoped and agent-scoped storage/retrieval, including memory reuse from newly created sessions.

### Repository sources

- [Operations runbook](../AGENTS.md)
- [Current hosted workflow orchestrator](../src/hosted_hypothesis_agent/agent.py)
- [Current workflow contracts](../src/hosted_hypothesis_agent/models.py)
- [Current agent harness/runtime](../src/hosted_hypothesis_agent/runtime.py)
- [Current Foundry state-store adapter](../src/hosted_hypothesis_agent/state_store.py)
- [Current AG-UI adapter](../src/hypothesis_ag_ui/server.py)
- [Current AG-UI frontend](../src/hypothesis_ag_ui/frontend/src/App.tsx)
- [Current AG-UI local instructions](../src/hypothesis_ag_ui/README.md)
- [Hosted-agent deployment](../scripts/deploy_hosted_hypothesis_agent.py)
- [Travel MCP deployment](../scripts/deploy_travel_mcp_server.py)
- [Travel toolbox registration](../scripts/register_hypothesis_toolboxes.py)
- [Package-age checker](../scripts/check_package_age.py)
- [Infrastructure entry point](../infra/main.bicep)
- [Approved squad decision: LRA phases and state store](../.squad/decisions.md)
