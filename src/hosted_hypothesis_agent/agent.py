"""Foundry-hosted phased hypothesis workflow over the invocations protocol."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from agent_framework import AgentSession
from azure.ai.agentserver.core.tasks import RetryPolicy, TaskContext, multi_turn_task, set_resilient_tasks_enabled
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .models import (
    AgentStats, ApprovalRecord, ModelModes, PhaseError, PhaseOrigin, PhaseRecord, PhaseStage,
    PlanRevision, StepState, WorkflowEvent, WorkflowMeta, WorkflowRequest,
    WorkflowStatus, utc_now,
)
from .runtime import AgentRuntime, validate_model_modes
from .state_store import (
    FoundryWorkflowStore, InMemoryWorkflowStore, PhaseNotFoundError, SizeLimitError,
    WorkflowConflictError, WorkflowExpiredError, WorkflowNotFoundError, WorkflowStore,
)

logger = logging.getLogger(__name__)
TERMINAL_STAGES = {PhaseStage.COMPLETED, PhaseStage.REJECTED, PhaseStage.FAILED, PhaseStage.CANCELLED}
set_resilient_tasks_enabled(True)


def _build_store(owner_id: str | None = None) -> WorkflowStore:
    if os.getenv("ALLOW_IN_MEMORY_WORKFLOW_STORE", "").lower() in {"1", "true", "yes"}:
        logger.warning("Using non-durable in-memory workflow store")
        return InMemoryWorkflowStore()
    return FoundryWorkflowStore(owner_id or "local-development", store_names=(
        os.getenv("LRA_STORE_WORKFLOWS", "lra-workflows"),
        os.getenv("LRA_STORE_EVENTS", "lra-events"),
        os.getenv("LRA_STORE_CHECKPOINTS", "lra-checkpoints"),
    ), ttl_seconds=int(os.getenv("WORKFLOW_TTL_HOURS", "168")) * 3600)


def _caller_id(request: Request) -> str:
    if value := request.headers.get("x-agent-user-id"):
        return value
    if os.getenv("ALLOW_INSECURE_LOCAL_CALLER", "").lower() in {"1", "true", "yes"}:
        return request.headers.get("x-caller-id", "local-development")
    raise PermissionError("caller_unavailable: authenticated caller identity is unavailable")


def _safe_excerpt(value: str | None, limit: int = 240) -> str:
    return (value or "").replace("\n", " ")[:limit]


def _task_input_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_hash(control: WorkflowRequest) -> str:
    value = control.model_dump(mode="json")
    if value.get("model_modes") == ModelModes().model_dump(mode="json"):
        value.pop("model_modes")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _workflow_status(tail: PhaseRecord) -> WorkflowStatus:
    if tail.stage == PhaseStage.FAILED:
        return WorkflowStatus.FAILED
    if tail.stage in TERMINAL_STAGES:
        return WorkflowStatus.IDLE
    return WorkflowStatus.ACTIVE


def _phase_error_code(exc: Exception, call_id: str | None) -> str:
    if isinstance(exc, SizeLimitError):
        return "size_limit"
    message = str(exc).lower()
    call_id_markers = ("x-agent-foundry-call-id", "foundry call id", "call_id", "call-id")
    if call_id and any(marker in message for marker in call_id_markers):
        return "call_id_unusable"
    return "agent_error"


def _error(code: str, message: str, retryable: bool = False, current: dict[str, Any] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"error": {"code": code, "message": message, "retryable": retryable}}
    if current is not None:
        value["error"]["current"] = current
    return value


class WorkflowOrchestrator:
    def __init__(self, store: WorkflowStore, runtime_factory: Callable[..., AgentRuntime] = AgentRuntime,
                 use_resilient_tasks: bool = False) -> None:
        self.store = store
        self.runtime_factory = runtime_factory
        self.use_resilient_tasks = use_resilient_tasks
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def dispatch(self, control: WorkflowRequest, owner_id: str, foundry_call_id: str | None = None) -> dict[str, Any]:
        if control.action == "plan":
            return await self._new_workflow(control, owner_id, foundry_call_id)
        stored = await self.store.get_meta(control.workflow_id or "", foundry_call_id)
        meta = stored.meta
        if meta.owner_id != owner_id:
            raise PermissionError("not_owner: workflow belongs to a different caller")
        if meta.expires_at <= utc_now():
            raise WorkflowExpiredError("workflow_expired: workflow has expired")
        if control.action == "status":
            await self._resume_if_needed(meta, foundry_call_id)
            return await self._status(meta, control.phase_id, control.client_request_id, foundry_call_id)
        if control.action == "progress":
            await self._resume_if_needed(meta, foundry_call_id)
            return await self._progress(meta, control.phase_id, control.client_request_id, foundry_call_id)
        if control.action == "events":
            if control.phase_id:
                await self.store.get_phase(meta.workflow_id, control.phase_id, foundry_call_id)
            events = await self.store.events(meta.workflow_id, control.after_seq or 0, control.phase_id, foundry_call_id)
            page = events[: control.limit or 50]
            return {"workflow_id": meta.workflow_id, "events": [e.model_dump(mode="json") for e in page],
                    "next_after_seq": page[-1].seq if page else (control.after_seq or 0), "server_time": utc_now().isoformat().replace("+00:00", "Z")}
        digest = _request_hash(control)
        replay = await self.store.request(meta.workflow_id, control.client_request_id, foundry_call_id)
        if replay:
            if replay.get("body_digest") != digest:
                raise WorkflowConflictError("request_conflict: client_request_id was already used with a different body")
            response = replay["response"]
            response.setdefault("request", {})["replayed"] = True
            return response
        if meta.active_run is not None and control.action != "cancel":
            raise WorkflowConflictError("phase_in_progress: an agent phase is already running")
        if control.action == "approve":
            response = await self._approve(meta, control, owner_id, foundry_call_id)
        elif control.action == "continue":
            response = await self._append_phase(meta, control, owner_id, foundry_call_id, PhaseOrigin.CONTINUE)
        elif control.action == "revise":
            response = await self._append_phase(meta, control, owner_id, foundry_call_id, PhaseOrigin.REVISE)
        elif control.action == "cancel":
            response = await self._cancel(meta, control, owner_id, foundry_call_id)
        else:
            raise ValueError(f"unknown_action: unsupported action {control.action}")
        try:
            await self.store.create_request(meta.workflow_id, control.client_request_id,
                                            {"action": control.action, "body_digest": digest, "response": response}, foundry_call_id)
        except WorkflowConflictError:
            retry = await self.store.request(meta.workflow_id, control.client_request_id, foundry_call_id)
            if not retry or retry.get("body_digest") != digest:
                raise WorkflowConflictError("request_conflict: concurrent request used the same id")
            response = retry["response"]
            response.setdefault("request", {})["replayed"] = True
        return response

    async def _new_workflow(self, control: WorkflowRequest, owner_id: str, call_id: str | None) -> dict[str, Any]:
        workflow_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{owner_id}:{control.client_request_id}").hex
        digest = _request_hash(control)
        replay = await self.store.request(workflow_id, control.client_request_id, call_id)
        if replay:
            if replay.get("body_digest") != digest:
                raise WorkflowConflictError("request_conflict: client_request_id was already used with a different body")
            response = replay["response"]
            response.setdefault("request", {})["replayed"] = True
            return response
        modes = control.model_modes or ModelModes()
        validate_model_modes(modes)
        now = utc_now()
        phase_id = f"{workflow_id}-p001"
        phase = PhaseRecord(phase_id=phase_id, workflow_id=workflow_id, ordinal=1, origin=PhaseOrigin.INITIAL,
                            user_input=control.scenario or "", model_modes=modes,
                            stage=PhaseStage.PENDING)
        meta = WorkflowMeta(workflow_id=workflow_id, owner_id=owner_id, title=(control.scenario or "")[:120],
                            expires_at=now + timedelta(hours=int(os.getenv("WORKFLOW_TTL_HOURS", "168"))),
                            phase_ids=[phase_id], tail_phase_id=phase_id)
        try:
            await self.store.create(meta, phase, call_id)
        except WorkflowConflictError:
            replay = await self.store.request(workflow_id, control.client_request_id, call_id)
            if replay and replay.get("body_digest") == digest:
                response = replay["response"]
                response.setdefault("request", {})["replayed"] = True
                return response
            raise WorkflowConflictError("phase_in_progress: workflow creation is already being accepted")
        await self._event(meta, phase, "workflow.created", "human", owner_id,
                          {"scenario": _safe_excerpt(control.scenario)}, control.client_request_id, call_id)
        await self._event(meta, phase, "phase.created", "human", owner_id,
                          {"origin": "initial", "parent_phase_id": None, "based_on_phase_id": None,
                           "user_input": _safe_excerpt(control.scenario),
                           "model_modes": phase.model_modes.model_dump(mode="json")},
                          control.client_request_id, call_id)
        await self._event(meta, phase, "human.request", "human", owner_id,
                          {"action": "plan", "client_request_id": control.client_request_id}, control.client_request_id, call_id)
        claimed = await self._claim(meta, phase, control.client_request_id, call_id)
        response = await self._envelope(claimed.meta, phase, control.client_request_id, False, call_id)
        await self.store.create_request(workflow_id, control.client_request_id,
                                        {"action": "plan", "body_digest": digest, "response": response}, call_id)
        self._schedule(workflow_id, phase_id, call_id, control.client_request_id)
        return response

    async def _claim(self, meta: WorkflowMeta, phase: PhaseRecord, request_id: str, call_id: str | None) -> Any:
        stored = await self.store.get_meta(meta.workflow_id, call_id)
        if stored.meta.active_run:
            raise WorkflowConflictError("phase_in_progress: an agent phase is already running")
        latest = stored.meta.model_copy(deep=True)
        latest.status = WorkflowStatus.ACTIVE
        latest.updated_at = utc_now()
        return await self.store.save_meta_and_claim(latest, stored.etag, phase, _task_input_id(request_id), call_id)

    def _schedule(self, workflow_id: str, phase_id: str, call_id: str | None,
                  input_id: str | None = None, entry_mode: str = "fresh", input_id_is_safe: bool = False) -> None:
        task = self._tasks.get(workflow_id)
        if task is not None and not task.done():
            return
        safe_input_id = input_id if input_id_is_safe else _task_input_id(input_id or phase_id)
        if self.use_resilient_tasks:
            self._tasks[workflow_id] = asyncio.create_task(
                self._start_resilient_task(workflow_id, phase_id, call_id, safe_input_id))
        else:
            self._tasks[workflow_id] = asyncio.create_task(
                self._run_with_heartbeat(workflow_id, phase_id, call_id, entry_mode))

    async def _start_resilient_task(self, workflow_id: str, phase_id: str, call_id: str | None, input_id: str) -> None:
        run = await run_workflow_turn.start(
            task_id=workflow_id, input_id=input_id,
            input=RunInput(workflow_id=workflow_id, phase_id=phase_id,
                           owner_id=self.store.owner_id if isinstance(self.store, FoundryWorkflowStore) else "local-development",
                           call_id=call_id),
        )
        await run.result()

    async def _run_with_heartbeat(self, workflow_id: str, phase_id: str, call_id: str | None,
                                  entry_mode: str = "fresh", recovery_count: int = 0) -> None:
        beat = asyncio.create_task(self._heartbeat(workflow_id, phase_id, call_id))
        try:
            await self._run_phase(workflow_id, phase_id, call_id, entry_mode, recovery_count)
        finally:
            beat.cancel()
            try:
                await beat
            except asyncio.CancelledError:
                pass

    async def _heartbeat(self, workflow_id: str, phase_id: str, call_id: str | None) -> None:
        interval = max(1, int(os.getenv("LRA_HEARTBEAT_SECONDS", "15")))
        while True:
            await asyncio.sleep(interval)
            for _ in range(3):
                try:
                    stored = await self.store.get_meta(workflow_id, call_id)
                    if not stored.meta.active_run or stored.meta.active_run.phase_id != phase_id:
                        return
                    stored.meta.active_run.heartbeat_at = utc_now()
                    phase = await self.store.get_phase(workflow_id, phase_id, call_id)
                    stored.meta.active_run.stage = phase.stage
                    await self.store.save_meta(stored.meta, stored.etag, call_id)
                    break
                except WorkflowConflictError:
                    continue
                except Exception:
                    logger.exception("Workflow heartbeat failed")
                    break

    async def _resume_if_needed(self, meta: WorkflowMeta, request_call_id: str | None) -> None:
        active = meta.active_run
        stale_after = int(os.getenv("LRA_STALE_CLAIM_SECONDS", "90"))
        stale = (utc_now() - active.heartbeat_at).total_seconds() >= stale_after if active else False
        if not active or not stale or (meta.workflow_id in self._tasks and not self._tasks[meta.workflow_id].done()):
            return
        # The takeover is a CAS claim made under the current request's active Foundry turn. The call id persisted
        # on the claim belongs to an earlier turn that may have ended, so it is never reused for store access.
        stored = await self.store.get_meta(meta.workflow_id, request_call_id)
        current = stored.meta
        if (current.active_run is None or current.active_run.input_id != active.input_id or
                (utc_now() - current.active_run.heartbeat_at).total_seconds() < stale_after):
            return
        current.active_run.heartbeat_at = utc_now()
        current.active_run.call_id = request_call_id
        await self.store.save_meta(current, stored.etag, request_call_id)
        self._schedule(meta.workflow_id, active.phase_id, request_call_id, active.input_id, "recovered", input_id_is_safe=True)

    async def wait(self, workflow_id: str) -> None:
        task = self._tasks.get(workflow_id)
        if task:
            await task

    async def _run_phase(self, workflow_id: str, phase_id: str, call_id: str | None,
                         entry_mode: str = "fresh", recovery_count: int = 0) -> None:
        start = utc_now()
        phase: PhaseRecord | None = None
        try:
            phase = await self.store.get_phase(workflow_id, phase_id, call_id)
            prior_stage = phase.stage
            stored_meta = await self.store.get_meta(workflow_id, call_id)
            if stored_meta.meta.active_run is None or stored_meta.meta.active_run.phase_id != phase_id:
                return
            if prior_stage == PhaseStage.AWAITING_APPROVAL:
                await self._release_claim(workflow_id, phase_id, WorkflowStatus.ACTIVE, call_id)
                return
            if prior_stage in TERMINAL_STAGES:
                await self._release_claim(workflow_id, phase_id, _workflow_status(phase), call_id)
                return
            if phase.cancel_requested:
                phase.stage = PhaseStage.CANCELLED
                phase.ended_at = utc_now()
                await self.store.save_phase(phase, call_id)
                await self._release_claim(workflow_id, phase_id, WorkflowStatus.IDLE, call_id)
                return
            recovered = entry_mode == "recovered" or phase.started_at is not None
            if phase.started_at is None:
                phase.started_at = start
            if prior_stage not in {PhaseStage.APPROVED, PhaseStage.EXECUTING}:
                phase.stage = PhaseStage.HYPOTHESIZING if phase.hypothesis is None else PhaseStage.PLANNING
            phase.updated_at = start
            phase.recovery_count = max(phase.recovery_count + int(recovered), recovery_count)
            phase = await self.store.save_phase(phase, call_id)
            if recovered:
                await self._event(stored_meta.meta, phase, "run.recovered", "system", "workflow",
                                  {"entry_mode": "recovered", "recovery_count": phase.recovery_count,
                                   "resumed_from": phase.session_checkpoint_ref}, stored_meta.meta.active_run.input_id, call_id)
            run_agent = "HypothesisAgent" if phase.hypothesis is None else "ScenarioExecutorHarness"
            run_kind = "hypothesis" if phase.hypothesis is None else "plan"
            await self._event(stored_meta.meta, phase, "agent.started", "agent", run_agent,
                              {"agent": run_agent, "run_kind": run_kind}, stored_meta.meta.active_run.input_id, call_id)
            runtime_kwargs: dict[str, Any] = {}
            runtime_parameters = inspect.signature(self.runtime_factory).parameters.values()
            accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in runtime_parameters)
            parameter_names = {item.name for item in runtime_parameters}
            if call_id and ("foundry_call_id" in parameter_names or accepts_kwargs):
                runtime_kwargs["foundry_call_id"] = call_id
            if "model_modes" in parameter_names or accepts_kwargs:
                runtime_kwargs["model_modes"] = phase.model_modes
            runtime = self.runtime_factory(**runtime_kwargs)
            if hasattr(runtime, "configure_tool_ledger"):
                async def record_tool_event(event_type: str, data: dict[str, Any]) -> None:
                    current_meta = await self.store.get_meta(workflow_id, call_id)
                    await self._event(current_meta.meta, phase, event_type, "agent", "ScenarioExecutorHarness",
                                      data, current_meta.meta.active_run.input_id if current_meta.meta.active_run else None,
                                      call_id)

                runtime.configure_tool_ledger(
                    lambda key: self.store.get_tool_result(workflow_id, phase_id, key, call_id),
                    lambda key, value: self.store.save_tool_result(workflow_id, phase_id, key, value, call_id),
                    record_tool_event,
                )
            async with runtime:
                snapshot = await self._load_session(phase, runtime, call_id)
                if phase.hypothesis is None:
                    phase.hypothesis = await runtime.formulate_hypothesis(await self._scenario_for(phase, stored_meta.meta, call_id))
                    await self._refresh_cancel_flag(phase, call_id)
                    await self._event(stored_meta.meta, phase, "agent.completed", "agent", "HypothesisAgent",
                                      {"agent": "HypothesisAgent", "run_kind": "hypothesis"}, stored_meta.meta.active_run.input_id, call_id)
                    if phase.cancel_requested:
                        phase.stage = PhaseStage.CANCELLED
                        phase.ended_at = utc_now()
                        await self.store.save_phase(phase, call_id)
                        await self._event(stored_meta.meta, phase, "phase.cancelled", "system", "workflow", {},
                                          stored_meta.meta.active_run.input_id, call_id)
                        await self._release_claim(workflow_id, phase_id, WorkflowStatus.IDLE, call_id)
                        return
                    phase.session_snapshot = {}
                    phase.session_checkpoint_ref = await self.store.save_checkpoint(workflow_id, phase_id, "cp.0001", snapshot.to_dict(), call_id)
                    phase.stage = PhaseStage.PLANNING
                    phase.updated_at = utc_now()
                    phase = await self.store.save_phase(phase, call_id)
                if prior_stage not in {PhaseStage.APPROVED, PhaseStage.EXECUTING} and (not phase.plan_revisions or phase.stage == PhaseStage.PLANNING):
                    await self._event(stored_meta.meta, phase, "agent.started", "agent", "ScenarioExecutorHarness",
                                      {"agent": "ScenarioExecutorHarness", "run_kind": "plan"}, stored_meta.meta.active_run.input_id, call_id)
                    current_context = await self._planning_context(phase, stored_meta.meta, call_id)
                    plan, activities = await runtime.plan(current_context, phase.hypothesis, snapshot,
                                                         phase.revision_feedback or (phase.user_input if phase.origin == PhaseOrigin.REVISE else None))
                    await self._refresh_cancel_flag(phase, call_id)
                    if phase.cancel_requested:
                        phase.stage = PhaseStage.CANCELLED
                        phase.ended_at = utc_now()
                        await self.store.save_phase(phase, call_id)
                        await self._event(stored_meta.meta, phase, "phase.cancelled", "system", "workflow", {},
                                          stored_meta.meta.active_run.input_id, call_id)
                        await self._release_claim(workflow_id, phase_id, WorkflowStatus.IDLE, call_id)
                        return
                    revision = PlanRevision(revision=phase.plan_revision + 1, plan=plan, plan_digest=plan.digest(), feedback=phase.user_input if phase.origin == PhaseOrigin.REVISE else None)
                    phase.plan_revisions.append(revision)
                    phase.stats.wall_clock_ms += max(0, round((revision.produced_at - start).total_seconds() * 1000))
                    phase.revision_feedback = None
                    phase.step_states = [StepState(step_id=item.id) for item in plan.steps]
                    phase.stage = PhaseStage.AWAITING_APPROVAL
                    phase.updated_at = utc_now()
                    phase.session_snapshot = {}
                    phase.session_checkpoint_ref = await self.store.save_checkpoint(workflow_id, phase_id, f"cp.{revision.revision:04d}", snapshot.to_dict(), call_id)
                    self._merge_runtime_stats(phase, runtime)
                    phase = await self.store.save_phase(phase, call_id)
                    await self._usage_events(stored_meta.meta, phase, runtime, call_id)
                    await self._event(stored_meta.meta, phase, "stage.changed", "system", "workflow",
                                      {"from": PhaseStage.PLANNING.value, "to": PhaseStage.AWAITING_APPROVAL.value},
                                      stored_meta.meta.active_run.input_id, call_id)
                    await self._event(stored_meta.meta, phase, "agent.completed", "agent", "ScenarioExecutorHarness",
                                      {"agent": "ScenarioExecutorHarness", "run_kind": "plan"}, stored_meta.meta.active_run.input_id, call_id)
                    await self._event(stored_meta.meta, phase, "plan.produced", "agent", "ScenarioExecutorHarness",
                                      {"plan_revision": revision.revision, "plan_digest": revision.plan_digest,
                                       "steps": [x.title for x in plan.steps]}, stored_meta.meta.active_run.input_id, call_id)
                    for activity in activities:
                        phase.stats.tool_calls += 1
                        phase.stats.tool_calls_by_category[activity.tool_category] = phase.stats.tool_calls_by_category.get(activity.tool_category, 0) + 1
                        await self._event(stored_meta.meta, phase, "tool.completed", "agent", "ScenarioExecutorHarness",
                                          {**activity.model_dump(mode="json"), "cached": activity.output_summary.get("cached", False)}, stored_meta.meta.active_run.input_id, call_id)
                    phase = await self.store.save_phase(phase, call_id)
                    await self._release_claim(workflow_id, phase_id, WorkflowStatus.ACTIVE, call_id)
                    await self._event(stored_meta.meta, phase, "stage.changed", "system", "workflow",
                                      {"to": PhaseStage.AWAITING_APPROVAL.value}, stored_meta.meta.active_run.input_id, call_id)
                    return
                if prior_stage in {PhaseStage.APPROVED, PhaseStage.EXECUTING}:
                    latest = phase.plan_revisions[-1]
                    if phase.approval is None or phase.approval.plan_revision != latest.revision or phase.approval.plan_digest != latest.plan_digest:
                        raise ValueError("approved plan is missing or changed")
                    phase.stage = PhaseStage.EXECUTING
                    await self._event(stored_meta.meta, phase, "agent.started", "agent", "ScenarioExecutorHarness",
                                      {"agent": "ScenarioExecutorHarness", "run_kind": "execute"}, stored_meta.meta.active_run.input_id, call_id)
                    for step in phase.step_states:
                        if step.status != "completed":
                            step.status = "running"
                            step.started_at = step.started_at or utc_now()
                            await self._event(stored_meta.meta, phase, "step.started", "agent", "ScenarioExecutorHarness",
                                              {"step_id": step.step_id, "title": latest.plan.steps[step.step_id - 1].title},
                                              stored_meta.meta.active_run.input_id, call_id)
                    phase = await self.store.save_phase(phase, call_id)
                    execution_started = utc_now()
                    await self._event(stored_meta.meta, phase, "stage.changed", "system", "workflow",
                                      {"from": PhaseStage.APPROVED.value, "to": PhaseStage.EXECUTING.value},
                                      stored_meta.meta.active_run.input_id, call_id)
                    result, activities = await runtime.execute(latest.plan, snapshot)
                    await self._refresh_cancel_flag(phase, call_id)
                    if phase.cancel_requested:
                        phase.stage = PhaseStage.CANCELLED
                    else:
                        phase.execution = result
                        phase.stage = PhaseStage.COMPLETED
                    phase.session_snapshot = {}
                    phase.ended_at = utc_now()
                    phase.session_checkpoint_ref = await self.store.save_checkpoint(workflow_id, phase_id, "end", snapshot.to_dict(), call_id)
                    phase.end_session_ref = phase.session_checkpoint_ref
                    phase.updated_at = phase.ended_at
                    phase.stats.tool_calls += len(activities)
                    for activity in activities:
                        phase.stats.tool_calls_by_category[activity.tool_category] = phase.stats.tool_calls_by_category.get(activity.tool_category, 0) + 1
                        await self._event(stored_meta.meta, phase, "tool.completed", "agent", "ScenarioExecutorHarness",
                                          {**activity.model_dump(mode="json"), "cached": activity.output_summary.get("cached", False)}, stored_meta.meta.active_run.input_id, call_id)
                    completed = set(result.completed_steps)
                    for step in phase.step_states:
                        step.status = "completed" if step.step_id in completed else "failed"
                        step.ended_at = phase.ended_at
                        if step.status == "completed":
                            await self._event(stored_meta.meta, phase, "step.completed", "agent", "ScenarioExecutorHarness", {"step_id": step.step_id}, stored_meta.meta.active_run.input_id, call_id)
                    self._merge_runtime_stats(phase, runtime)
                    phase.stats.wall_clock_ms += max(0, round((phase.ended_at - execution_started).total_seconds() * 1000))
                    await self.store.save_phase(phase, call_id)
                    await self._usage_events(stored_meta.meta, phase, runtime, call_id)
                    await self._event(stored_meta.meta, phase, "agent.completed", "agent", "ScenarioExecutorHarness",
                                      {"agent": "ScenarioExecutorHarness", "run_kind": "execute"}, stored_meta.meta.active_run.input_id, call_id)
                    await self._event(stored_meta.meta, phase, "phase.completed" if phase.stage == PhaseStage.COMPLETED else "phase.cancelled",
                                      "system", "workflow", {"stats": phase.stats.model_dump(mode="json")}, stored_meta.meta.active_run.input_id, call_id)
                    await self._release_claim(workflow_id, phase_id, _workflow_status(phase), call_id)
        except Exception as exc:  # persisted failures are resumable only by explicit new phase/action
            logger.exception("Phase execution failed: %s/%s", workflow_id, phase_id)
            try:
                if phase is None:
                    phase = await self.store.get_phase(workflow_id, phase_id, call_id)
                phase.stage = PhaseStage.FAILED
                if isinstance(exc, SizeLimitError) and phase.execution is not None:
                    answer = phase.execution.final_answer
                    evidence_ref = None
                    try:
                        evidence_ref = await self.store.save_checkpoint(workflow_id, phase_id, "answer", {"text": answer}, call_id)
                    except SizeLimitError:
                        pass
                    prefix = answer[:200_000]
                    if evidence_ref:
                        prefix += f"\n\n[Full answer stored at {evidence_ref}]"
                        phase.execution.artifacts.append({"kind": "answer", "evidence_ref": evidence_ref})
                    else:
                        prefix += "\n\n[Answer truncated at the state-store size limit]"
                    phase.execution.final_answer = prefix
                    phase.execution.warnings.append("The final answer was truncated to fit the state-store item limit.")
                message = f"{type(exc).__name__}: {exc}"[:4000]
                error_code = _phase_error_code(exc, call_id)
                call_unusable = error_code == "call_id_unusable"
                phase.error = PhaseError(code=error_code, message=message, retryable=not call_unusable and error_code != "size_limit")
                phase.ended_at = utc_now()
                phase.updated_at = phase.ended_at
                await self.store.save_phase(phase, call_id)
                meta = await self._meta(workflow_id, call_id)
                await self._event(meta, phase, "agent.failed", "agent", "ScenarioExecutorHarness",
                                  {"run_kind": "workflow", "code": phase.error.code, "message": phase.error.message},
                                  meta.active_run.input_id if meta.active_run else None, call_id)
                await self._event(meta, phase, "phase.failed", "system", "workflow",
                                  {"code": phase.error.code, "message": phase.error.message}, None, call_id)
                await self._release_claim(workflow_id, phase_id, WorkflowStatus.FAILED, call_id)
            except Exception:
                logger.exception("Could not persist phase failure (call identity may be unusable)")

    async def _refresh_cancel_flag(self, phase: PhaseRecord, call_id: str | None) -> None:
        latest = await self.store.get_phase(phase.workflow_id, phase.phase_id, call_id)
        phase.cancel_requested = latest.cancel_requested
        phase.version = latest.version

    async def _load_session(self, phase: PhaseRecord, runtime: AgentRuntime, call_id: str | None) -> AgentSession:
        ref = phase.session_checkpoint_ref or phase.seed_session_ref
        if ref:
            try:
                value = await self.store.get_checkpoint(ref, call_id)
                if value:
                    return AgentSession.from_dict(value)
            except Exception:
                logger.exception("Unable to load a required session checkpoint for %s", phase.phase_id)
                raise
        session = runtime.create_session(session_id=phase.phase_id)
        phase.seed_session_ref = await self.store.save_checkpoint(phase.workflow_id, phase.phase_id, "seed", session.to_dict(), call_id)
        return session

    @staticmethod
    def _merge_runtime_stats(phase: PhaseRecord, runtime: Any) -> None:
        metrics = getattr(runtime, "metrics", {})
        if not metrics:
            phase.stats.usage_complete = False
            return
        for agent, values in metrics.items():
            previous = phase.stats.by_agent.get(agent, AgentStats(agent=agent))
            combined = previous.model_dump()
            combined.pop("agent", None)
            for key, value in values.items():
                if key in {"input_tokens", "output_tokens", "total_tokens"}:
                    combined[key] = (combined.get(key) or 0) + value if value is not None else combined.get(key)
                elif key in {"runs", "model_calls", "tool_calls", "duration_ms"}:
                    combined[key] = combined.get(key, 0) + value
            phase.stats.by_agent[agent] = AgentStats(agent=agent, **combined)
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                value = values.get(key)
                if value is not None:
                    current = getattr(phase.stats, key)
                    setattr(phase.stats, key, (current or 0) + value)
            phase.stats.model_calls += values.get("model_calls", 0)
        telemetry = getattr(runtime, "model_call_telemetry", None)
        phase.stats.usage_complete = phase.stats.usage_complete and bool(telemetry) and all(
            item.get("usage_complete") is True for item in telemetry
        )

    async def _usage_events(self, meta: WorkflowMeta, phase: PhaseRecord, runtime: Any, call_id: str | None) -> None:
        telemetry = getattr(runtime, "model_call_telemetry", None)
        if telemetry is not None:
            for item in telemetry:
                await self._event(meta, phase, "model.usage", "agent", item["agent"], item,
                                  meta.active_run.input_id if meta.active_run else None, call_id)
            return
        for agent, values in getattr(runtime, "metrics", {}).items():
            await self._event(meta, phase, "model.usage", "agent", agent,
                              {"agent": agent, "mode": None, "deployment_alias": None,
                               "elapsed_ms": values.get("duration_ms"), "call_count": values.get("model_calls", 0),
                               "usage_details": {"input_tokens": values.get("input_tokens"),
                                                 "output_tokens": values.get("output_tokens"),
                                                 "total_tokens": values.get("total_tokens")},
                               "usage_complete": False},
                              meta.active_run.input_id if meta.active_run else None, call_id)

    async def _scenario_for(self, phase: PhaseRecord, meta: WorkflowMeta, call_id: str | None) -> str:
        if phase.origin == PhaseOrigin.INITIAL:
            return phase.user_input
        if phase.origin == PhaseOrigin.CONTINUE:
            based = await self.store.get_phase(meta.workflow_id, phase.link.based_on_phase_id or "", call_id)
            return f"Prior phase result:\n{based.execution.final_answer if based.execution else ''}\n\nFollow-up request:\n{phase.user_input}"
        based = await self.store.get_phase(meta.workflow_id, phase.link.based_on_phase_id or "", call_id)
        prior = based.latest_plan.model_dump_json() if based.latest_plan else "No prior plan"
        answer = based.execution.final_answer if based.execution else "No prior execution result"
        return f"Original scenario:\n{based.user_input}\n\nPrior plan:\n{prior}\n\nPrior result:\n{answer}\n\nFeedback:\n{phase.user_input}"

    async def _planning_context(self, phase: PhaseRecord, meta: WorkflowMeta, call_id: str | None) -> str:
        return await self._scenario_for(phase, meta, call_id)

    async def _append_phase(self, meta: WorkflowMeta, control: WorkflowRequest, owner_id: str, call_id: str | None, origin: PhaseOrigin) -> dict[str, Any]:
        stored = await self.store.get_meta(meta.workflow_id, call_id)
        meta = stored.meta
        if meta.active_run:
            raise WorkflowConflictError("phase_in_progress: an agent phase is already running")
        modes = control.model_modes or ModelModes()
        validate_model_modes(modes)
        phases = await self.store.phases(meta.workflow_id, meta, call_id)
        tail = phases[-1]
        if origin == PhaseOrigin.CONTINUE and tail.stage not in {PhaseStage.COMPLETED, PhaseStage.FAILED}:
            raise ValueError("invalid_stage: continue requires a completed or failed tail phase")
        if tail.stage not in TERMINAL_STAGES:
            raise WorkflowConflictError("phase_in_progress: the tail phase is not terminal")
        target = tail
        if origin == PhaseOrigin.REVISE:
            target = await self.store.get_phase(meta.workflow_id, control.phase_id or "", call_id)
            if target.stage not in {PhaseStage.COMPLETED, PhaseStage.REJECTED, PhaseStage.FAILED}:
                raise ValueError("invalid_stage: revise requires a terminal phase")
        ordinal = len(meta.phase_ids) + 1
        phase_id = f"{meta.workflow_id}-p{ordinal:03d}"
        phase = PhaseRecord(phase_id=phase_id, workflow_id=meta.workflow_id, ordinal=ordinal,
                            link={"parent_phase_id": tail.phase_id, "based_on_phase_id": target.phase_id},
                            origin=origin, user_input=control.scenario or "", model_modes=modes,
                            stage=PhaseStage.PENDING,
                            seed_session_ref=target.end_session_ref or target.session_checkpoint_ref or target.seed_session_ref)
        await self.store.create_phase(phase, call_id)
        updated = meta.model_copy(deep=True)
        updated.phase_ids.append(phase_id)
        updated.tail_phase_id = phase_id
        updated.active_run = None
        updated.status = WorkflowStatus.ACTIVE
        claimed = await self.store.save_meta_and_claim(updated, stored.etag, phase, _task_input_id(control.client_request_id), call_id)
        await self._event(claimed.meta, phase, "phase.created", "human", owner_id,
                          {"origin": origin.value, "parent_phase_id": tail.phase_id, "based_on_phase_id": target.phase_id,
                           "user_input": _safe_excerpt(phase.user_input),
                           "model_modes": phase.model_modes.model_dump(mode="json")},
                          control.client_request_id, call_id)
        await self._event(claimed.meta, phase, "human.request", "human", owner_id,
                          {"action": origin.value, "client_request_id": control.client_request_id}, control.client_request_id, call_id)
        response = await self._envelope(claimed.meta, phase, control.client_request_id, False, call_id)
        self._schedule(meta.workflow_id, phase_id, call_id, control.client_request_id)
        return response

    async def _approve(self, meta: WorkflowMeta, control: WorkflowRequest, owner_id: str, call_id: str | None) -> dict[str, Any]:
        phase_id = control.phase_id or meta.tail_phase_id
        phase = await self.store.get_phase(meta.workflow_id, phase_id, call_id)
        if phase.stage in TERMINAL_STAGES:
            return await self._envelope(meta, phase, control.client_request_id, False, call_id)
        if phase.stage != PhaseStage.AWAITING_APPROVAL:
            raise WorkflowConflictError("phase_in_progress: phase is not awaiting approval")
        revision = phase.plan_revisions[-1]
        if control.plan_revision != revision.revision or control.plan_digest != revision.plan_digest:
            raise WorkflowConflictError("stale_plan: approval does not match the current plan", current={"phase_id": phase.phase_id, "plan_revision": revision.revision, "plan_digest": revision.plan_digest})
        approval = ApprovalRecord(phase_id=phase.phase_id, decision=control.decision or "rejected", approver_id=owner_id,
                                  plan_revision=revision.revision, plan_digest=revision.plan_digest, comment=control.comment)
        phase.approval = approval
        phase.stats.awaiting_human_ms += max(0, round((approval.decided_at - revision.produced_at).total_seconds() * 1000))
        phase.updated_at = utc_now()
        await self._event(meta, phase, "human.decision", "human", owner_id,
                          {"decision": approval.decision, "plan_revision": revision.revision,
                           "plan_digest": revision.plan_digest, "comment": _safe_excerpt(approval.comment)}, control.client_request_id, call_id)
        if approval.decision == "rejected":
            phase.stage = PhaseStage.REJECTED
            phase.ended_at = utc_now()
            await self.store.save_phase(phase, call_id)
            await self._event(meta, phase, "stage.changed", "system", "workflow",
                              {"from": PhaseStage.AWAITING_APPROVAL.value, "to": PhaseStage.REJECTED.value}, control.client_request_id, call_id)
            await self._event(meta, phase, "phase.completed", "system", "workflow",
                              {"result": "rejected"}, control.client_request_id, call_id)
            await self._release_claim(meta.workflow_id, phase.phase_id, WorkflowStatus.IDLE, call_id)
            return await self._envelope(await self._meta(meta.workflow_id, call_id), phase, control.client_request_id, False, call_id)
        if approval.decision == "revise":
            phase.stage = PhaseStage.PLANNING
            phase.revision_feedback = control.comment
            phase.approval = None
            await self.store.save_phase(phase, call_id)
            await self._event(meta, phase, "stage.changed", "system", "workflow",
                              {"from": PhaseStage.AWAITING_APPROVAL.value, "to": PhaseStage.PLANNING.value}, control.client_request_id, call_id)
            claimed = await self._claim(meta, phase, control.client_request_id, call_id)
        else:
            phase.stage = PhaseStage.APPROVED
            await self.store.save_phase(phase, call_id)
            await self._event(meta, phase, "stage.changed", "system", "workflow",
                              {"from": PhaseStage.AWAITING_APPROVAL.value, "to": PhaseStage.APPROVED.value}, control.client_request_id, call_id)
            claimed = await self._claim(meta, phase, control.client_request_id, call_id)
        self._schedule(meta.workflow_id, phase.phase_id, call_id, control.client_request_id)
        return await self._envelope(claimed.meta, phase, control.client_request_id, False, call_id)

    async def _cancel(self, meta: WorkflowMeta, control: WorkflowRequest, owner_id: str, call_id: str | None) -> dict[str, Any]:
        if meta.active_run is None:
            raise WorkflowConflictError("phase_in_progress: no active phase can be cancelled")
        phase_id = control.phase_id or meta.active_run.phase_id
        if phase_id != meta.active_run.phase_id:
            raise WorkflowConflictError("phase_in_progress: only the active phase can be cancelled")
        phase = await self.store.get_phase(meta.workflow_id, phase_id, call_id)
        phase.cancel_requested = True
        await self.store.save_phase(phase, call_id)
        await self._event(meta, phase, "human.cancel", "human", owner_id, {"reason": control.comment}, control.client_request_id, call_id)
        return await self._envelope(meta, phase, control.client_request_id, False, call_id)

    async def _release_claim(self, workflow_id: str, phase_id: str, status: WorkflowStatus, call_id: str | None) -> None:
        stored = await self.store.get_meta(workflow_id, call_id)
        meta = stored.meta
        if meta.active_run and meta.active_run.phase_id == phase_id:
            meta.active_run = None
            meta.status = status
            meta.updated_at = utc_now()
            await self.store.save_meta(meta, stored.etag, call_id)

    async def _event(self, meta: WorkflowMeta, phase: PhaseRecord, typ: str, actor: str, actor_id: str,
                     data: dict[str, Any], request_id: str | None, call_id: str | None) -> None:
        at = utc_now()
        event_hash = hashlib.sha256(json.dumps(data, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()[:16]
        encoded_data = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
        if len(encoded_data.encode("utf-8")) > 8 * 1024:
            data = {"truncated": True, "excerpt": encoded_data[:6000]}
        event = WorkflowEvent(seq=0, event_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{phase.phase_id}:{request_id}:{phase.recovery_count}:{typ}:{event_hash}:{at.isoformat()}").hex,
                               workflow_id=meta.workflow_id, phase_id=phase.phase_id, at=at, actor=actor,
                               actor_id=hashlib.sha256(actor_id.encode()).hexdigest()[:16] if actor == "human" else actor_id,
                               type=typ, stage=phase.stage, data=data,
                               correlation={"client_request_id": request_id,
                                            "invocation_id": meta.active_run.invocation_id if meta.active_run else None,
                                            "input_id": meta.active_run.input_id if meta.active_run else None,
                                            "recovery_count": phase.recovery_count})
        try:
            await self.store.append_event(meta.workflow_id, phase.phase_id, event, call_id)
        except Exception:
            logger.exception("Could not persist workflow event %s", typ)

    async def _meta(self, workflow_id: str, call_id: str | None) -> WorkflowMeta:
        return (await self.store.get_meta(workflow_id, call_id)).meta

    async def _status(self, meta: WorkflowMeta, phase_id: str | None, request_id: str, call_id: str | None) -> dict[str, Any]:
        phases = await self.store.phases(meta.workflow_id, meta, call_id)
        selected = next((p for p in phases if p.phase_id == phase_id), phases[-1])
        if phase_id and selected.phase_id != phase_id:
            raise PhaseNotFoundError(phase_id)
        response = await self._envelope(meta, selected, request_id, False, call_id)
        if phase_id is None:
            response["phases"] = [self._summary(p, p.phase_id == meta.tail_phase_id) for p in phases]
        return response

    async def _progress(self, meta: WorkflowMeta, phase_id: str | None, request_id: str, call_id: str | None) -> dict[str, Any]:
        phase = await self.store.get_phase(meta.workflow_id, phase_id or meta.tail_phase_id, call_id)
        total = len(phase.step_states)
        complete = sum(1 for item in phase.step_states if item.status == "completed")
        percent = 100 if phase.stage == PhaseStage.COMPLETED else 15 + int(80 * complete / total) if total else 5
        state = "waiting_human" if phase.stage == PhaseStage.AWAITING_APPROVAL else "done" if phase.stage in TERMINAL_STAGES else "running"
        events = await self.store.events(meta.workflow_id, max(0, meta.event_seq - 100), phase.phase_id, call_id)
        run = meta.active_run if meta.active_run and meta.active_run.phase_id == phase.phase_id else None
        now = utc_now()
        heartbeat_age = int((now - run.heartbeat_at).total_seconds() * 1000) if run else None
        latest_tool_start = next((e for e in reversed(events) if e.type == "tool.started"), None)
        latest_tool_finish = next((e for e in reversed(events) if e.type in {"tool.completed", "tool.failed"}), None)
        current_tool = ({"tool_category": latest_tool_start.data.get("tool_category"),
                         "tool_name": latest_tool_start.data.get("tool_name"),
                         "agent": "ScenarioExecutorHarness", "since": latest_tool_start.at.isoformat().replace("+00:00", "Z")}
                        if latest_tool_start and (latest_tool_finish is None or latest_tool_start.seq > latest_tool_finish.seq) else None)
        current_step = next(({"step_id": s.step_id, "title": phase.latest_plan.steps[s.step_id - 1].title}
                             for s in phase.step_states if s.status == "running" and phase.latest_plan), None)
        progress = {"phase_id": phase.phase_id, "stage": phase.stage.value, "state": state, "percent": percent,
                    "current": current_tool or current_step,
                    "steps": [s.model_dump(mode="json") for s in phase.step_states],
                    "stage_started_at": phase.updated_at.isoformat().replace("+00:00", "Z"),
                    "last_event_seq": events[-1].seq if events else 0,
                    "heartbeat_at": run.heartbeat_at.isoformat().replace("+00:00", "Z") if run else None,
                    "heartbeat_age_ms": heartbeat_age, "stalled": bool(heartbeat_age and heartbeat_age > 60_000),
                    "recovery_count": phase.recovery_count, "events_degraded": False,
                    "stats_so_far": phase.stats.model_dump(mode="json")}
        response = await self._envelope(meta, phase, request_id, False, call_id)
        response["progress"] = progress
        return response

    async def _envelope(self, meta: WorkflowMeta, phase: PhaseRecord, request_id: str, replayed: bool, call_id: str | None) -> dict[str, Any]:
        phases = await self.store.phases(meta.workflow_id, meta, call_id)
        summary = self._summary(phase, phase.phase_id == meta.tail_phase_id)
        events = await self.store.events(meta.workflow_id, max(0, meta.event_seq - 200), phase.phase_id, call_id)
        view = {**summary, "hypothesis": phase.hypothesis.model_dump(mode="json") if phase.hypothesis else None,
                "plan_revisions": [p.model_dump(mode="json") for p in phase.plan_revisions],
                "approval": phase.approval.model_dump(mode="json") if phase.approval else None,
                "execution": phase.execution.model_dump(mode="json") if phase.execution else None,
                "step_states": [x.model_dump(mode="json") for x in phase.step_states],
                "tool_activity": [e.data for e in events if e.type.startswith("tool.")][-50:],
                "stats_by_agent": {name: stats.model_dump(mode="json") for name, stats in phase.stats.by_agent.items()},
                "error": phase.error.model_dump(mode="json") if phase.error else None}
        run = ({"phase_id": meta.active_run.phase_id, "task_id": meta.active_run.task_id,
                "input_id": meta.active_run.input_id, "invocation_id": meta.active_run.invocation_id,
                "stream_id": meta.active_run.stream_id, "stage": meta.active_run.stage.value,
                "claimed_at": meta.active_run.claimed_at.isoformat().replace("+00:00", "Z"),
                "heartbeat_at": meta.active_run.heartbeat_at.isoformat().replace("+00:00", "Z")}
               if meta.active_run else None)
        return {"workflow_id": meta.workflow_id, "status": meta.status.value, "tail_phase_id": meta.tail_phase_id,
                "server_time": utc_now().isoformat().replace("+00:00", "Z"), "phase": view,
                "run": run, "progress": None,
                "request": {"client_request_id": request_id, "replayed": replayed}}

    def _summary(self, phase: PhaseRecord, is_tail: bool) -> dict[str, Any]:
        latest = phase.plan_revisions[-1] if phase.plan_revisions else None
        total = len(phase.step_states)
        return {"phase_id": phase.phase_id, "ordinal": phase.ordinal, "origin": phase.origin.value,
                "parent_phase_id": phase.link.parent_phase_id, "based_on_phase_id": phase.link.based_on_phase_id,
                "model_modes": phase.model_modes.model_dump(mode="json"),
                "title": phase.user_input[:100], "stage": phase.stage.value, "is_tail": is_tail,
                "created_at": phase.created_at.isoformat().replace("+00:00", "Z"),
                "started_at": phase.started_at.isoformat().replace("+00:00", "Z") if phase.started_at else None,
                "ended_at": phase.ended_at.isoformat().replace("+00:00", "Z") if phase.ended_at else None,
                "plan_revision": latest.revision if latest else 0, "plan_digest": latest.plan_digest if latest else None,
                "step_counts": {"total": total, "completed": sum(x.status == "completed" for x in phase.step_states),
                                 "failed": sum(x.status == "failed" for x in phase.step_states)},
                "stats": phase.stats.model_dump(mode="json"),
                "actions": {"can_revise": phase.stage in {PhaseStage.COMPLETED, PhaseStage.REJECTED, PhaseStage.FAILED},
                            "can_continue": is_tail and phase.stage in {PhaseStage.COMPLETED, PhaseStage.FAILED},
                            "can_approve": phase.stage == PhaseStage.AWAITING_APPROVAL,
                            "can_cancel": phase.stage not in TERMINAL_STAGES and phase.stage != PhaseStage.AWAITING_APPROVAL},
                "error": phase.error.model_dump(mode="json") if phase.error else None}


async def _workflow_request(request: Request) -> WorkflowRequest:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ValueError("request body must be an object")
    state = payload.get("state")
    control = state.get("workflow", state) if isinstance(state, dict) else payload
    if isinstance(control, dict) and control.get("action") not in {"plan", "approve", "continue", "revise", "status", "progress", "events", "cancel"}:
        raise ValueError(f"unknown_action: unsupported action {control.get('action')}")
    return WorkflowRequest.model_validate(control)


class RunInput(BaseModel):
    workflow_id: str
    phase_id: str
    owner_id: str
    call_id: str | None = None


@multi_turn_task(name="workflow", steerable=False, timeout=timedelta(hours=2),
                 retry=RetryPolicy(max_attempts=2, initial_delay=timedelta(seconds=5)))
async def run_workflow_turn(ctx: TaskContext[RunInput]) -> dict[str, str]:
    orchestrator = _orchestrator(ctx.input.owner_id)
    phase = await orchestrator.store.get_phase(ctx.input.workflow_id, ctx.input.phase_id, ctx.input.call_id)
    if ctx.cancel_requested:
        phase.cancel_requested = True
        await orchestrator.store.save_phase(phase, ctx.input.call_id)
    worker = asyncio.create_task(orchestrator._run_with_heartbeat(
        ctx.input.workflow_id, ctx.input.phase_id, ctx.input.call_id,
        entry_mode=ctx.entry_mode, recovery_count=ctx.recovery_count))
    shutdown = asyncio.create_task(ctx.shutdown.wait())
    done, _ = await asyncio.wait({worker, shutdown}, return_when=asyncio.FIRST_COMPLETED)
    if shutdown in done and ctx.shutdown.is_set() and not worker.done():
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        phase = await orchestrator.store.get_phase(ctx.input.workflow_id, ctx.input.phase_id, ctx.input.call_id)
        meta = await orchestrator.store.get_meta(ctx.input.workflow_id, ctx.input.call_id)
        await orchestrator._event(meta.meta, phase, "run.deferred", "system", "workflow",
                                  {"shutdown": True}, meta.meta.active_run.input_id if meta.meta.active_run else None,
                                  ctx.input.call_id)
        return await ctx.exit_for_recovery()
    shutdown.cancel()
    try:
        await shutdown
    except asyncio.CancelledError:
        pass
    await worker
    return {"workflow_id": ctx.input.workflow_id, "phase_id": ctx.input.phase_id}


# Store selection is request-scoped in Foundry to bind state operations to the authenticated user.
app = InvocationAgentServerHost()
_orchestrators: dict[str, WorkflowOrchestrator] = {}


def _orchestrator(owner_id: str) -> WorkflowOrchestrator:
    if owner_id not in _orchestrators:
        _orchestrators[owner_id] = WorkflowOrchestrator(_build_store(owner_id), use_resilient_tasks=True)
    return _orchestrators[owner_id]


async def _stream_events(orchestrator: WorkflowOrchestrator, control: WorkflowRequest, owner_id: str, call_id: str | None):
    cursor = control.after_seq or 0
    last_keepalive = asyncio.get_running_loop().time()
    while True:
        items = await orchestrator.store.events(control.workflow_id or "", cursor, control.phase_id, call_id)
        for item in items:
            cursor = item.seq
            yield f"id: {item.seq}\nevent: {item.type}\ndata: {item.model_dump_json()}\n\n"
        meta = await orchestrator.store.get_meta(control.workflow_id or "", call_id)
        if meta.meta.owner_id != owner_id:
            return
        if meta.meta.active_run is None:
            break
        now = asyncio.get_running_loop().time()
        if now - last_keepalive >= 15:
            yield ": keep-alive\n\n"
            last_keepalive = now
        await asyncio.sleep(1)


@app.invoke_handler
async def handle_invoke(request: Request) -> Response:
    try:
        control = await _workflow_request(request)
        owner_id = _caller_id(request)
        call_id = request.headers.get("x-agent-foundry-call-id")
        if call_id == "":
            raise PermissionError("call_id_unusable: x-agent-foundry-call-id is present but empty")
        workflow_orchestrator = _orchestrator(owner_id)
        payload = await workflow_orchestrator.dispatch(control, owner_id, call_id)
        if control.action == "events" and control.stream:
            return StreamingResponse(_stream_events(workflow_orchestrator, control, owner_id, call_id), media_type="text/event-stream")
        http_status = 202 if control.action in {"plan", "continue", "revise", "cancel"} or (control.action == "approve" and control.decision != "rejected") else 200
        return JSONResponse(payload, status_code=http_status)
    except PermissionError as exc:
        code = "not_owner" if str(exc).startswith("not_owner:") else "call_id_unusable" if str(exc).startswith("call_id_unusable:") else "caller_unavailable"
        status = 403 if code == "not_owner" else 401
        return JSONResponse(_error(code, str(exc)), status_code=status)
    except WorkflowExpiredError as exc:
        return JSONResponse(_error("workflow_expired", str(exc)), status_code=410)
    except PhaseNotFoundError as exc:
        return JSONResponse(_error("phase_not_found", str(exc)), status_code=404)
    except WorkflowNotFoundError as exc:
        return JSONResponse(_error("workflow_not_found", str(exc)), status_code=404)
    except WorkflowConflictError as exc:
        current = getattr(exc, "current", None)
        code = str(exc).split(":", 1)[0]
        return JSONResponse(_error(code, str(exc), retryable=code == "busy", current=current), status_code=409)
    except SizeLimitError as exc:
        return JSONResponse(_error("size_limit", str(exc)), status_code=413)
    except (ValidationError, ValueError) as exc:
        text = str(exc)
        code = text.split(":", 1)[0] if ":" in text else "invalid_request"
        return JSONResponse(_error(code, text), status_code=422)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled workflow invocation failure")
        return JSONResponse(_error("internal_error", f"{type(exc).__name__}: {exc}", retryable=True), status_code=500)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    app.run()
