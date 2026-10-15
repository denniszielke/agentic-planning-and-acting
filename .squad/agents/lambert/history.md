📌 Imported from my-team on 2026-10-15T16:51:35.084Z. Portable knowledge carried over; project learnings from previous project preserved below.

# Lambert — History

## Learnings
- Team created 2026-10-09 for long-running-agentic-reasoning (Python, Bicep, Node.js).

## Onboarding
- **Preserve the approval boundary exactly:** decisions must carry the current `workflow_id`, `plan_revision`, and `plan_digest`; keep these visible and forward them unchanged (`AGENTS.md`; `src/hypothesis_ag_ui/frontend/src/App.tsx`).
- **Make human review legible and explicit:** render the hypothesis, assumptions, evidence, success criteria, risks, plan steps, status, and digest; execution stays blocked until the user decides (`README.md`; `src/hypothesis_ag_ui/frontend/src/App.tsx`).
- **Keep the UI adapter a narrow protocol bridge:** validate workflow state, invoke the upstream agent, and translate results to AG-UI state events (`src/hypothesis_ag_ui/server.py`; contract checks in `tests/hypothesis_ag_ui/test_server.py`).
- **Risk:** `server.py`'s `BearerAuth.auth_flow` currently assigns literal `******` to `Authorization` rather than using `_token_provider`; upstream auth may fail. Tests cover a successful plan stream and missing state, but no frontend behavior.


## 2026-10-13T19:59:19.941+02:00 — Authentication correction and LRA continuation

- Explicit correction to the earlier `BearerAuth.auth_flow` risk note: `src/hypothesis_ag_ui/server.py:30-37` constructs the Authorization header with a Bearer prefix and invokes `self._token_provider()`. The earlier literal-asterisk/no-provider claim was false; rendered search output masked this expression. Local AST inspection of line 36 verified provider invocation=true, Bearer prefix=true, literal asterisks=false, without printing credentials. This source check does not prove deployed upstream authentication.
- 📌 Team update (2026-10-13T19:59:19.941+02:00): Ripley's actual inbox decision is merged into decisions.md — decided by Ripley, approved by user 2026-10-09. Linear chained phases replace what-if; revisions append with `based_on_phase_id`; every appended plan requires fresh approval, with strict 409 during active execution. Use the canonical decision for direction, not historical onboarding notes.
- User resumed the paused LRA upgrade. Kane regression was requested synchronously but the tool converted it to background execution; in progress, no verified outcome yet. Scribe performed bookkeeping only.
