from __future__ import annotations

import unittest
from datetime import timedelta

from src.hosted_hypothesis_agent.models import (
    ApprovalRecord, ExecutionPlan, Hypothesis, PhaseRecord, PhaseStage, PlanStep,
    WorkflowMeta, WorkflowStatus, utc_now,
)
from src.hosted_hypothesis_agent.state_store import InMemoryWorkflowStore, WorkflowConflictError


def _plan(description: str = "Find evidence") -> ExecutionPlan:
    return ExecutionPlan(objective="Test hypothesis", steps=[PlanStep(
        id=1, title="Research", description=description, tool_category="weather")])


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def test_plan_digest_is_stable_and_revision_sensitive(self) -> None:
        self.assertEqual(_plan().digest(), _plan().digest())
        self.assertNotEqual(_plan().digest(), _plan("Different evidence").digest())

    def test_plan_rejects_more_than_five_steps(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most 5 items"):
            ExecutionPlan(objective="Too long", steps=[PlanStep(id=i, title=f"Step {i}", description="Research", tool_category="weather") for i in range(1, 7)])

    def test_stale_approval_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ApprovalRecord(phase_id="p1", decision="approved", approver_id="u1", plan_revision=1,
                           plan_digest="sha256:" + "x" * 64)

    async def test_state_store_uses_create_once_and_version_cas(self) -> None:
        store = InMemoryWorkflowStore()
        phase = PhaseRecord(phase_id="wf-p001", workflow_id="wf", ordinal=1, origin="initial", user_input="scenario")
        meta = WorkflowMeta(workflow_id="wf", owner_id="u1", title="scenario", expires_at=utc_now() + timedelta(days=1),
                            phase_ids=[phase.phase_id], tail_phase_id=phase.phase_id)
        await store.create(meta, phase)
        first = await store.get_meta("wf")
        stale = await store.get_meta("wf")
        first.meta.title = "changed"
        updated = await store.save_meta(first.meta, first.etag)
        self.assertEqual(updated.meta.version, 1)
        with self.assertRaises(WorkflowConflictError):
            await store.save_meta(stale.meta, stale.etag)

    async def test_session_checkpoint_and_tool_ledger_are_idempotent(self) -> None:
        store = InMemoryWorkflowStore()
        phase = PhaseRecord(phase_id="wf-p001", workflow_id="wf", ordinal=1, origin="initial", user_input="scenario")
        meta = WorkflowMeta(workflow_id="wf", owner_id="u1", title="scenario", expires_at=utc_now() + timedelta(days=1),
                            phase_ids=[phase.phase_id], tail_phase_id=phase.phase_id)
        await store.create(meta, phase)
        ref = await store.save_checkpoint("wf", phase.phase_id, "seed", {"x": 1})
        await store.save_checkpoint("wf", phase.phase_id, "seed", {"x": 2})
        self.assertEqual(await store.get_checkpoint(ref), {"x": 1})
        await store.save_tool_result("wf", phase.phase_id, "key", {"result": "cached"})
        await store.save_tool_result("wf", phase.phase_id, "key", {"result": "other"})
        self.assertEqual(await store.get_tool_result("wf", phase.phase_id, "key"), {"result": "cached"})

    def test_approval_is_bound_to_plan_revision_digest(self) -> None:
        plan = _plan()
        approval = ApprovalRecord(phase_id="wf-p001", decision="approved", approver_id="owner", plan_revision=1,
                                  plan_digest=plan.digest())
        self.assertEqual(approval.plan_digest, plan.digest())
        self.assertEqual(PhaseStage.AWAITING_APPROVAL, "awaiting_approval")
        self.assertEqual(WorkflowStatus.ACTIVE, "active")


if __name__ == "__main__":
    unittest.main()
