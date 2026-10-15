# Squad Decisions

## Active Decisions

### 2026-10-09: LRA phases and state store (approved by user)

**By:** Ripley
**Source:** decisions/inbox/ripley-lra-phases.md
**Merged by:** Scribe, 2026-10-13T19:59:19.941+02:00

#### Decision: LRA phases and state store (approved by user 2026-10-09)

- Replace direct Blob access with Foundry state store; no data migration; 1 MB item cap accepted; redeploy afterwards.
- What-if is replaced by linear chained phases in one workflow. Revising an earlier phase appends a new phase with `based_on_phase_id`; later phases are shown as superseded.
- Fail closed (no user_id fallback) if a captured call id is unusable in a recovered run.
- `continue` is allowed from a failed phase.
- Retention default is 7 days (was 24h).
- Strict 409 while a phase is running; no steering.
- Timestamps on every agent and human action; per-phase stats where obtainable, otherwise skipped.
- All packages updated, none younger than 7 days (published on or before 2026-10-02). npm registry: https://packagefeedproxy.microsoft.io/npm/.
- Bicep holds desired state; Python scripts still start deployments.
- Design source of truth: doc/lra-design.md.

## Governance

- All meaningful changes require team consensus
- Document architectural decisions here
- Keep history focused on work, decisions focused on direction
