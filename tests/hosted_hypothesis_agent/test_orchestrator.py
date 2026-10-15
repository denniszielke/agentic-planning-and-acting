from __future__ import annotations

import json
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

os.environ.setdefault("AZURE_AI_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/test")

from agent_framework import AgentSession, set_agent_mode  # noqa: E402
from starlette.requests import Request  # noqa: E402

from src.hosted_hypothesis_agent.agent import (  # noqa: E402
    WorkflowOrchestrator,
    _caller_id,
    _phase_error_code,
    handle_invoke,
)
from src.hosted_hypothesis_agent.models import (  # noqa: E402
    ActiveRun, ApprovalRecord, ExecutionPlan, ExecutionResult, Hypothesis, PlanStep, PhaseRecord, PhaseStage,
    WorkflowMeta, WorkflowRequest, WorkflowStatus, utc_now,
)
from src.hosted_hypothesis_agent.state_store import InMemoryWorkflowStore, WorkflowConflictError  # noqa: E402


class FakeRuntime:
    received_call_ids: list[str | None] = []
    received_plan_sessions: list[dict] = []
    execute_count = 0
    plan_gate = None
    plan_started = None

    def __init__(self, foundry_call_id: str | None = None) -> None:
        self.received_call_ids.append(foundry_call_id)
        self.metrics = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def configure_tool_ledger(self, ledger_get, ledger_save, event_callback=None) -> None:
        self.ledger_get = ledger_get
        self.ledger_save = ledger_save
        self.event_callback = event_callback

    def create_session(self, session_id: str | None = None) -> AgentSession:
        session = AgentSession(session_id=session_id)
        set_agent_mode(session, "plan", available_modes=("plan", "execute"))
        return session

    async def formulate_hypothesis(self, scenario: str) -> Hypothesis:
        return Hypothesis(statement=f"Test {scenario}", scenario_summary=scenario)

    async def plan(self, scenario, hypothesis, session, revision_comment=None):
        type(self).received_plan_sessions.append(session.to_dict())
        if type(self).plan_started is not None:
            type(self).plan_started.set()
        if type(self).plan_gate is not None:
            await type(self).plan_gate.wait()
        return ExecutionPlan(objective="Test the scenario", steps=[PlanStep(
            id=1, title="Research", description="Collect evidence", tool_category="weather")]), []

    async def execute(self, plan, session):
        type(self).execute_count += 1
        session.state["qa_execution"] = type(self).execute_count
        set_agent_mode(session, "execute", available_modes=("plan", "execute"))
        return ExecutionResult(completed_steps=[1], final_answer="Evidence collected."), []


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeRuntime.received_call_ids.clear()
        FakeRuntime.received_plan_sessions.clear()
        FakeRuntime.execute_count = 0
        FakeRuntime.plan_gate = None
        FakeRuntime.plan_started = None
        self.store = InMemoryWorkflowStore()
        self.orchestrator = WorkflowOrchestrator(self.store, runtime_factory=FakeRuntime)

    def test_caller_id_uses_foundry_protocol_user_header(self) -> None:
        request = Request({"type": "http", "headers": [(b"x-agent-user-id", b"foundry-user-1")]})
        self.assertEqual(_caller_id(request), "foundry-user-1")

    def test_model_inference_forbidden_is_not_mislabeled_as_call_id_failure(self) -> None:
        forbidden = RuntimeError("ChatClientException: Error code: 403 - Forbidden")
        invalid_call = RuntimeError(
            "The supplied 'x-agent-foundry-call-id' header does not match an active turn"
        )

        self.assertEqual(_phase_error_code(forbidden, "call-live"), "agent_error")
        self.assertEqual(_phase_error_code(invalid_call, "call-live"), "call_id_unusable")

    async def test_model_inference_forbidden_persists_agent_error(self) -> None:
        class ForbiddenRuntime(FakeRuntime):
            async def formulate_hypothesis(self, scenario: str) -> Hypothesis:
                raise RuntimeError("ChatClientException: Error code: 403 - Forbidden")

        orchestrator = WorkflowOrchestrator(self.store, runtime_factory=ForbiddenRuntime)
        accepted = await orchestrator.dispatch(
            WorkflowRequest(
                action="plan",
                scenario="forbidden model",
                client_request_id="model-forbidden",
            ),
            "caller-1",
            "call-live",
        )
        await orchestrator.wait(accepted["workflow_id"])

        phase = await self.store.get_phase(
            accepted["workflow_id"],
            accepted["phase"]["phase_id"],
        )
        self.assertEqual(phase.stage, PhaseStage.FAILED)
        self.assertEqual(phase.error.code, "agent_error")
        self.assertTrue(phase.error.retryable)

    async def _invoke(self, control, headers=None):
        async def receive():
            return {"type": "http.request", "body": json.dumps(control).encode()}

        request = Request({"type": "http", "headers": headers or []}, receive)
        with patch("src.hosted_hypothesis_agent.agent._orchestrator", return_value=self.orchestrator):
            result = await handle_invoke(request)
        return result.status_code, json.loads(result.body)

    async def test_missing_identity_and_empty_call_id_fail_before_work_starts(self) -> None:
        control = {"action": "plan", "scenario": "Private trip", "client_request_id": "identity"}
        with patch.dict(os.environ, {"ALLOW_INSECURE_LOCAL_CALLER": "false"}):
            for headers, code in [
                ([(b"x-caller-id", b"untrusted")], "caller_unavailable"),
                ([(b"x-agent-user-id", b"caller-1"), (b"x-agent-foundry-call-id", b"")], "call_id_unusable"),
            ]:
                with self.subTest(code=code):
                    status, body = await self._invoke(control, headers)
                    self.assertEqual((status, body["error"]["code"]), (401, code))
        self.assertEqual(FakeRuntime.received_call_ids, [])
        self.assertEqual(self.orchestrator._tasks, {})

    async def test_expiry_boundary_rejects_reads_mutations_and_replays(self) -> None:
        workflow_id, approval, _ = await self._create_completed()
        stored = await self.store.get_meta(workflow_id)
        before = await self.store.get_phase(workflow_id, stored.meta.tail_phase_id)
        controls = [
            {"action": action, "workflow_id": workflow_id, "client_request_id": f"expired-{action}"}
            for action in ("status", "progress", "events", "cancel")
        ] + [
            {"action": "continue", "workflow_id": workflow_id, "client_request_id": "expired-continue", "scenario": "More"},
            {"action": "revise", "workflow_id": workflow_id, "phase_id": before.phase_id,
             "client_request_id": "expired-revise", "scenario": "Change"},
            approval.model_dump(mode="json"),
        ]
        with patch("src.hosted_hypothesis_agent.agent.utc_now", return_value=stored.meta.expires_at):
            for control in controls:
                with self.subTest(action=control["action"]):
                    status, body = await self._invoke(control, [(b"x-agent-user-id", b"caller-1")])
                    self.assertEqual((status, body["error"]["code"]), (410, "workflow_expired"))
        self.assertEqual((await self.store.get_meta(workflow_id)).meta, stored.meta)
        self.assertEqual(await self.store.get_phase(workflow_id, before.phase_id), before)
        self.assertEqual(FakeRuntime.execute_count, 1)

    async def _create_completed(self):
        accepted = await self.orchestrator.dispatch(
            WorkflowRequest(action="plan", scenario="Visit Berlin", client_request_id="plan-1"), "caller-1")
        await self.orchestrator.wait(accepted["workflow_id"])
        status = await self.orchestrator.dispatch(
            WorkflowRequest(action="status", workflow_id=accepted["workflow_id"], client_request_id="status-1"), "caller-1")
        phase = status["phase"]
        approval = WorkflowRequest(action="approve", workflow_id=accepted["workflow_id"], client_request_id="approve-1",
                                   plan_revision=phase["plan_revision"], plan_digest=phase["plan_digest"], decision="approved")
        approved = await self.orchestrator.dispatch(approval, "caller-1")
        await self.orchestrator.wait(accepted["workflow_id"])
        return accepted["workflow_id"], approval, approved

    async def test_plan_approve_execute_and_idempotent_retry(self) -> None:
        accepted = await self.orchestrator.dispatch(
            WorkflowRequest(action="plan", scenario="working scenario", client_request_id="plan-1"),
            "caller-1", "call-plan-1")
        self.assertEqual(accepted["phase"]["stage"], PhaseStage.PENDING)
        await self.orchestrator.wait(accepted["workflow_id"])
        status = await self.orchestrator.dispatch(
            WorkflowRequest(action="status", workflow_id=accepted["workflow_id"], client_request_id="status-1"), "caller-1")
        phase = status["phase"]
        self.assertEqual(phase["stage"], PhaseStage.AWAITING_APPROVAL)
        approval = WorkflowRequest(action="approve", workflow_id=accepted["workflow_id"], client_request_id="approve-1",
                                   plan_revision=phase["plan_revision"], plan_digest=phase["plan_digest"], decision="approved")
        approved = await self.orchestrator.dispatch(approval, "caller-1", "call-approve-1")
        await self.orchestrator.wait(accepted["workflow_id"])
        completed = await self.orchestrator.dispatch(
            WorkflowRequest(action="status", workflow_id=accepted["workflow_id"], client_request_id="status-2"), "caller-1")
        retried = await self.orchestrator.dispatch(approval, "caller-1")
        self.assertEqual(completed["phase"]["stage"], PhaseStage.COMPLETED)
        self.assertEqual(completed["phase"]["execution"]["final_answer"], "Evidence collected.")
        self.assertTrue(retried["request"]["replayed"])
        self.assertEqual(FakeRuntime.execute_count, 1)
        self.assertEqual(FakeRuntime.received_call_ids, ["call-plan-1", "call-approve-1"])

    async def test_stale_plan_digest_rejected(self) -> None:
        accepted = await self.orchestrator.dispatch(
            WorkflowRequest(action="plan", scenario="Scenario", client_request_id="plan-stale"), "caller-1")
        await self.orchestrator.wait(accepted["workflow_id"])
        with self.assertRaisesRegex(WorkflowConflictError, "stale_plan"):
            await self.orchestrator.dispatch(WorkflowRequest(action="approve", workflow_id=accepted["workflow_id"],
                client_request_id="stale-approve", plan_revision=1, plan_digest="sha256:" + "0" * 64,
                decision="approved"), "caller-1")

    async def test_replanning_requires_fresh_revision_and_matching_digest(self) -> None:
        accepted = await self.orchestrator.dispatch(
            WorkflowRequest(action="plan", scenario="Scenario", client_request_id="replan"), "caller-1")
        workflow_id = accepted["workflow_id"]
        await self.orchestrator.wait(workflow_id)
        original = await self.store.get_phase(workflow_id, accepted["phase"]["phase_id"])
        control = {"action": "approve", "workflow_id": workflow_id, "phase_id": original.phase_id,
                   "client_request_id": "replan-decision", "plan_revision": original.plan_revision,
                   "plan_digest": original.plan_digest, "decision": "revise", "comment": "Lower emissions"}
        status, _ = await self._invoke(control, [(b"x-agent-user-id", b"caller-1")])
        self.assertEqual(status, 202)
        await self.orchestrator.wait(workflow_id)
        current = await self.store.get_phase(workflow_id, original.phase_id)
        self.assertEqual(current.plan_revision, 2)
        self.assertEqual(current.plan_revisions[0], original.plan_revisions[0])
        self.assertIsNone(current.approval)
        for revision, digest in [
            (original.plan_revision, current.plan_digest),
            (current.plan_revision, "sha256:" + "0" * 64),
        ]:
            with self.subTest(revision=revision):
                stale = {**control, "client_request_id": f"stale-{revision}", "decision": "approved",
                         "plan_revision": revision, "plan_digest": digest}
                status, body = await self._invoke(stale, [(b"x-agent-user-id", b"caller-1")])
                self.assertEqual((status, body["error"]["code"]), (409, "stale_plan"))
                self.assertEqual(body["error"]["current"], {
                    "phase_id": current.phase_id, "plan_revision": 2, "plan_digest": current.plan_digest,
                })
        self.assertEqual(await self.store.get_phase(workflow_id, current.phase_id), current)
        self.assertEqual(FakeRuntime.execute_count, 0)
        fresh = {**control, "client_request_id": "fresh", "decision": "approved",
                 "plan_revision": current.plan_revision, "plan_digest": current.plan_digest}
        status, _ = await self._invoke(fresh, [(b"x-agent-user-id", b"caller-1")])
        self.assertEqual(status, 202)
        await self.orchestrator.wait(workflow_id)
        self.assertEqual(FakeRuntime.execute_count, 1)

    async def test_continue_appends_linked_phase_and_revise_uses_earlier_phase(self) -> None:
        workflow_id, _, _ = await self._create_completed()
        meta = (await self.store.get_meta(workflow_id)).meta
        original = await self.store.get_phase(workflow_id, meta.tail_phase_id)
        original_snapshot = await self.store.get_checkpoint(original.end_session_ref)
        continue_response = await self.orchestrator.dispatch(WorkflowRequest(action="continue", workflow_id=workflow_id,
            scenario="Add a day in Prague", client_request_id="continue-1"), "caller-1")
        await self.orchestrator.wait(workflow_id)
        continued = await self.orchestrator.dispatch(WorkflowRequest(action="status", workflow_id=workflow_id,
            client_request_id="status-3"), "caller-1")
        self.assertEqual(continued["phases"][1]["parent_phase_id"], continued["phases"][0]["phase_id"])
        self.assertEqual(continued["phases"][1]["based_on_phase_id"], continued["phases"][0]["phase_id"])
        self.assertEqual(continue_response["phase"]["ordinal"], 2)
        second_record = await self.store.get_phase(workflow_id, continued["phase"]["phase_id"])
        self.assertEqual(second_record.seed_session_ref, original.end_session_ref)
        self.assertEqual(FakeRuntime.received_plan_sessions[-1], original_snapshot)
        self.assertEqual(second_record.stage, PhaseStage.AWAITING_APPROVAL)
        self.assertIsNone(second_record.approval)
        self.assertEqual(FakeRuntime.execute_count, 1)

        second = continued["phases"][1]
        approve_second = WorkflowRequest(action="approve", workflow_id=workflow_id, client_request_id="approve-2",
            plan_revision=second["plan_revision"], plan_digest=second["plan_digest"], decision="approved")
        await self.orchestrator.dispatch(approve_second, "caller-1")
        await self.orchestrator.wait(workflow_id)
        second_completed = await self.store.get_phase(workflow_id, second["phase_id"])
        revised = await self.orchestrator.dispatch(WorkflowRequest(action="revise", workflow_id=workflow_id,
            phase_id=continued["phases"][0]["phase_id"], scenario="Prefer lower emissions", client_request_id="revise-1"), "caller-1")
        await self.orchestrator.wait(workflow_id)
        phases = await self.orchestrator.dispatch(WorkflowRequest(action="status", workflow_id=workflow_id,
            client_request_id="status-4"), "caller-1")
        self.assertEqual(revised["phase"]["ordinal"], 3)
        self.assertEqual(phases["phases"][2]["parent_phase_id"], phases["phases"][1]["phase_id"])
        self.assertEqual(phases["phases"][2]["based_on_phase_id"], phases["phases"][0]["phase_id"])
        self.assertEqual(phases["phases"][1]["stage"], "completed")
        revised_record = await self.store.get_phase(workflow_id, revised["phase"]["phase_id"])
        self.assertEqual(revised_record.seed_session_ref, original.end_session_ref)
        self.assertEqual(FakeRuntime.received_plan_sessions[-1], original_snapshot)
        self.assertNotEqual(original_snapshot, await self.store.get_checkpoint(second_completed.end_session_ref))
        self.assertEqual(revised_record.stage, PhaseStage.AWAITING_APPROVAL)
        self.assertIsNone(revised_record.approval)
        self.assertEqual(await self.store.get_phase(workflow_id, original.phase_id), original)
        self.assertEqual(await self.store.get_phase(workflow_id, second["phase_id"]), second_completed)
        self.assertEqual(await self.store.get_checkpoint(original.end_session_ref), original_snapshot)
        self.assertEqual(FakeRuntime.execute_count, 2)

    async def test_mutation_request_id_conflict_does_not_append_or_execute_again(self) -> None:
        workflow_id, approval, _ = await self._create_completed()
        headers = [(b"x-agent-user-id", b"caller-1")]
        status, body = await self._invoke(
            approval.model_copy(update={"decision": "rejected"}).model_dump(mode="json"), headers)
        self.assertEqual((status, body["error"]["code"]), (409, "request_conflict"))
        followup = {"action": "continue", "workflow_id": workflow_id,
                    "scenario": "Add a day", "client_request_id": "followup"}
        status, accepted = await self._invoke(followup, headers)
        self.assertEqual(status, 202)
        await self.orchestrator.wait(workflow_id)
        before = (await self.store.get_meta(workflow_id)).meta
        status, replay = await self._invoke(followup, headers)
        self.assertEqual(status, 202)
        self.assertTrue(replay["request"]["replayed"])
        self.assertEqual(replay["phase"]["phase_id"], accepted["phase"]["phase_id"])
        for changes in [
            {"scenario": "Different trip"},
            {"action": "revise", "phase_id": before.phase_ids[0]},
        ]:
            with self.subTest(changes=changes):
                status, body = await self._invoke({**followup, **changes}, headers)
                self.assertEqual((status, body["error"]["code"]), (409, "request_conflict"))
        self.assertEqual((await self.store.get_meta(workflow_id)).meta, before)
        self.assertEqual(len(before.phase_ids), 2)
        self.assertEqual(FakeRuntime.execute_count, 1)

    async def test_request_id_conflict_and_owner_isolation(self) -> None:
        accepted = await self.orchestrator.dispatch(WorkflowRequest(action="plan", scenario="private", client_request_id="plan-private"), "caller-1")
        await self.orchestrator.wait(accepted["workflow_id"])
        with self.assertRaises(PermissionError):
            await self.orchestrator.dispatch(WorkflowRequest(action="status", workflow_id=accepted["workflow_id"], client_request_id="status-x"), "caller-2")
        with self.assertRaisesRegex(WorkflowConflictError, "request_conflict"):
            await self.orchestrator.dispatch(WorkflowRequest(action="plan", scenario="different", client_request_id="plan-private"), "caller-1")

    async def test_events_and_progress_are_timestamped_and_ordered(self) -> None:
        accepted = await self.orchestrator.dispatch(WorkflowRequest(action="plan", scenario="scenario", client_request_id="plan-events"), "caller-1")
        await self.orchestrator.wait(accepted["workflow_id"])
        page = await self.orchestrator.dispatch(WorkflowRequest(action="events", workflow_id=accepted["workflow_id"], client_request_id="events-1", after_seq=0), "caller-1")
        seqs = [event["seq"] for event in page["events"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertTrue(all(event["at"].endswith("Z") for event in page["events"]))
        progress = await self.orchestrator.dispatch(WorkflowRequest(action="progress", workflow_id=accepted["workflow_id"], client_request_id="progress-1"), "caller-1")
        self.assertEqual(progress["progress"]["state"], "waiting_human")

    async def test_stale_run_resumes_from_durable_phase_and_emits_recovery(self) -> None:
        workflow_id = "a" * 32
        phase_id = f"{workflow_id}-p001"
        old = utc_now() - timedelta(minutes=3)
        phase = PhaseRecord(phase_id=phase_id, workflow_id=workflow_id, ordinal=1, origin="initial",
                           user_input="scenario", stage=PhaseStage.PLANNING,
                           hypothesis=Hypothesis(statement="Testable", scenario_summary="Scenario"), started_at=old)
        meta = WorkflowMeta(workflow_id=workflow_id, owner_id="caller-1", title="scenario",
                            expires_at=utc_now() + timedelta(days=7), phase_ids=[phase_id], tail_phase_id=phase_id,
                            active_run=ActiveRun(phase_id=phase_id, task_id=workflow_id, input_id="input-1",
                                invocation_id="inv-1", stream_id="stream-1", stage=PhaseStage.PLANNING,
                                claimed_at=old, heartbeat_at=old))
        await self.store.create(meta, phase)
        response = await self.orchestrator.dispatch(WorkflowRequest(action="status", workflow_id=workflow_id,
            client_request_id="status-recover"), "caller-1")
        await self.orchestrator.wait(workflow_id)
        recovered = await self.store.get_phase(workflow_id, phase_id)
        self.assertEqual(recovered.stage, PhaseStage.AWAITING_APPROVAL)
        self.assertEqual(recovered.recovery_count, 1)
        events = await self.store.events(workflow_id)
        self.assertIn("run.recovered", [event.type for event in events])
        self.assertIsNone((await self.store.get_meta(workflow_id)).meta.active_run)
        self.assertEqual(response["workflow_id"], workflow_id)

    async def test_stale_recovery_uses_active_call_id_never_persisted_expired_one(self) -> None:
        class ActiveTurnStore(InMemoryWorkflowStore):
            def __init__(self, active: set[str]) -> None:
                super().__init__()
                self.active = active
                self.seen: list[str | None] = []

            def __getattribute__(self, name):
                attr = super().__getattribute__(name)
                if name.startswith("_") or not callable(attr) or name in {"owner_id"}:
                    return attr

                async def guarded(*args, **kwargs):
                    call_id = kwargs.get("call_id")
                    if call_id is None and name in _CALL_ID_POSITION and len(args) > _CALL_ID_POSITION[name]:
                        call_id = args[_CALL_ID_POSITION[name]]
                    self.seen.append(call_id)
                    if call_id is not None and call_id not in self.active:
                        raise RuntimeError("The supplied 'x-agent-foundry-call-id' header does not match an active turn")
                    return await attr(*args, **kwargs)
                return guarded

        _CALL_ID_POSITION = {"get_meta": 1, "get_phase": 2, "save_meta": 2, "save_phase": 1, "append_event": 3,
                             "events": 3, "phases": 2, "get_checkpoint": 1, "save_checkpoint": 4,
                             "get_tool_result": 3, "save_tool_result": 4, "request": 2, "create_request": 3,
                             "save_meta_and_claim": 4}
        store = ActiveTurnStore({"call-live"})
        orchestrator = WorkflowOrchestrator(store, runtime_factory=FakeRuntime)
        accepted = await orchestrator.dispatch(WorkflowRequest(action="plan", scenario="scenario", client_request_id="plan-seed"), "caller-1")
        workflow_id = accepted["workflow_id"]
        await orchestrator.wait(workflow_id)
        phase_id = f"{workflow_id}-p001"
        old = utc_now() - timedelta(minutes=3)
        phase = await store.get_phase(workflow_id, phase_id)
        phase.stage = PhaseStage.APPROVED
        phase.approval = ApprovalRecord(phase_id=phase_id, decision="approved", approver_id="caller-1",
                                        plan_revision=phase.plan_revision, plan_digest=phase.plan_digest)
        await store.save_phase(phase)
        loaded = await store.get_meta(workflow_id)
        meta = loaded.meta
        meta.active_run = ActiveRun(phase_id=phase_id, task_id=workflow_id, input_id="input-1",
                                    invocation_id="inv-1", stream_id="stream-1", stage=PhaseStage.APPROVED,
                                    claimed_at=old, heartbeat_at=old, call_id="call-expired")
        await store.save_meta(meta, loaded.etag if hasattr(loaded, "etag") else None)
        FakeRuntime.received_call_ids = []
        await orchestrator.dispatch(WorkflowRequest(action="status", workflow_id=workflow_id,
                                                    client_request_id="status-live"), "caller-1", "call-live")
        await orchestrator.wait(workflow_id)
        self.assertNotIn("call-expired", store.seen)
        self.assertEqual(FakeRuntime.received_call_ids, ["call-live"])
        self.assertEqual((await store.get_phase(workflow_id, phase_id)).stage, PhaseStage.COMPLETED)
        self.assertIsNone((await store.get_meta(workflow_id)).meta.active_run)

    async def test_cancel_is_idempotent_and_cooperative(self) -> None:
        import asyncio
        FakeRuntime.plan_started = asyncio.Event()
        FakeRuntime.plan_gate = asyncio.Event()
        accepted = await self.orchestrator.dispatch(WorkflowRequest(action="plan", scenario="scenario", client_request_id="plan-cancel"), "caller-1")
        await FakeRuntime.plan_started.wait()
        cancel = WorkflowRequest(action="cancel", workflow_id=accepted["workflow_id"], client_request_id="cancel-1")
        first = await self.orchestrator.dispatch(cancel, "caller-1")
        FakeRuntime.plan_gate.set()
        await self.orchestrator.wait(accepted["workflow_id"])
        phase = await self.store.get_phase(accepted["workflow_id"], accepted["phase"]["phase_id"])
        self.assertEqual(phase.stage, PhaseStage.CANCELLED)
        replay = await self.orchestrator.dispatch(cancel, "caller-1")
        self.assertTrue(replay["request"]["replayed"])
        self.assertEqual(first["phase"]["stage"], "planning")

    async def test_running_phase_rejects_continue(self) -> None:
        accepted = await self.orchestrator.dispatch(WorkflowRequest(action="plan", scenario="scenario", client_request_id="plan-running"), "caller-1")
        meta = await self.store.get_meta(accepted["workflow_id"])
        meta.meta.active_run.heartbeat_at = meta.meta.active_run.heartbeat_at
        with self.assertRaisesRegex(WorkflowConflictError, "phase_in_progress"):
            await self.orchestrator.dispatch(WorkflowRequest(action="continue", workflow_id=accepted["workflow_id"],
                scenario="another phase", client_request_id="continue-running"), "caller-1")
        await self.orchestrator.wait(accepted["workflow_id"])


if __name__ == "__main__":
    unittest.main()
