"""Validated, versioned contracts for phased hypothesis workflows."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ToolCategory = Literal["weather", "hotels", "travel"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PhaseStage(StrEnum):
    PENDING = "pending"
    HYPOTHESIZING = "hypothesizing"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowStatus(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    FAILED = "failed"


class PhaseOrigin(StrEnum):
    INITIAL = "initial"
    CONTINUE = "continue"
    REVISE = "revise"


class ActorKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class ModelMode(StrEnum):
    COMPLETION = "completion"
    DECISION = "decision"


class ModelModes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    planning: ModelMode = ModelMode.COMPLETION
    execution: ModelMode = ModelMode.COMPLETION


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    statement: str = Field(min_length=1)
    scenario_summary: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    evidence_needed: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)
    guidance: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    risks_and_constraints: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tool_category: ToolCategory
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_evidence: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1)
    steps: list[PlanStep] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_step_ids(self) -> ExecutionPlan:
        if [step.id for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("plan step IDs must be contiguous and start at 1")
        return self

    def digest(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase_id: str
    decision: Literal["approved", "rejected", "revise"]
    approver_id: str = Field(min_length=1)
    decided_at: datetime = Field(default_factory=utc_now)
    plan_revision: int = Field(ge=1)
    plan_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    comment: str | None = Field(default=None, max_length=2000)


class ToolActivity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: Literal["planning", "execution"]
    tool_category: ToolCategory
    tool_name: str
    started_at: datetime
    duration_ms: int = Field(ge=0)
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    citations: list[str] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed_steps: list[int] = Field(default_factory=list)
    final_answer: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AgentStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str
    runs: int = 0
    model_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    tool_calls: int = 0
    duration_ms: int = 0


class PhaseStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    tool_calls: int = 0
    tool_calls_by_category: dict[str, int] = Field(default_factory=dict)
    model_calls: int = 0
    wall_clock_ms: int = 0
    awaiting_human_ms: int = 0
    by_agent: dict[str, AgentStats] = Field(default_factory=dict)
    usage_complete: bool = True


class StepState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: int
    status: Literal["pending", "running", "completed", "failed", "skipped"] = "pending"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    evidence_ref: str | None = None
    tool_calls: int = 0


class PhaseLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_phase_id: str | None = None
    based_on_phase_id: str | None = None


class PlanRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    plan: ExecutionPlan
    plan_digest: str
    produced_at: datetime = Field(default_factory=utc_now)
    feedback: str | None = None
    stats: PhaseStats = Field(default_factory=PhaseStats)


class PhaseError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str = Field(max_length=4000)
    at: datetime = Field(default_factory=utc_now)
    retryable: bool = False


class PhaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    phase_id: str
    workflow_id: str
    ordinal: int = Field(ge=1)
    link: PhaseLink = Field(default_factory=PhaseLink)
    origin: PhaseOrigin
    model_modes: ModelModes = Field(default_factory=ModelModes)
    stage: PhaseStage = PhaseStage.PENDING
    user_input: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    hypothesis: Hypothesis | None = None
    plan_revisions: list[PlanRevision] = Field(default_factory=list)
    approval: ApprovalRecord | None = None
    revision_feedback: str | None = None
    execution: ExecutionResult | None = None
    step_states: list[StepState] = Field(default_factory=list)
    seed_session_ref: str | None = None
    end_session_ref: str | None = None
    session_checkpoint_ref: str | None = None
    session_snapshot: dict[str, Any] = Field(default_factory=dict)
    stats: PhaseStats = Field(default_factory=PhaseStats)
    error: PhaseError | None = None
    recovery_count: int = 0
    cancel_requested: bool = False
    version: int = 0

    @property
    def latest_plan(self) -> ExecutionPlan | None:
        return self.plan_revisions[-1].plan if self.plan_revisions else None

    @property
    def plan_revision(self) -> int:
        return self.plan_revisions[-1].revision if self.plan_revisions else 0

    @property
    def plan_digest(self) -> str | None:
        return self.plan_revisions[-1].plan_digest if self.plan_revisions else None


class ActiveRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase_id: str
    task_id: str
    input_id: str
    invocation_id: str
    stream_id: str
    stage: PhaseStage
    claimed_at: datetime = Field(default_factory=utc_now)
    heartbeat_at: datetime = Field(default_factory=utc_now)
    call_id: str | None = None


class WorkflowMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    workflow_id: str
    owner_id: str
    title: str
    status: WorkflowStatus = WorkflowStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    phase_ids: list[str]
    tail_phase_id: str
    active_run: ActiveRun | None = None
    event_seq: int = 0
    totals: PhaseStats = Field(default_factory=PhaseStats)
    version: int = 0


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    seq: int
    event_id: str
    workflow_id: str
    phase_id: str
    at: datetime = Field(default_factory=utc_now)
    persisted_at: datetime | None = None
    actor: ActorKind
    actor_id: str
    type: str
    stage: PhaseStage | None = None
    step_id: int | None = None
    correlation: dict[str, str | int | None] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = None


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["plan", "approve", "continue", "revise", "status", "progress", "events", "cancel"]
    workflow_id: str | None = None
    phase_id: str | None = None
    scenario: str | None = Field(default=None, max_length=50_000)
    model_modes: ModelModes | None = None
    client_request_id: str = Field(min_length=1, max_length=200)
    plan_revision: int | None = Field(default=None, ge=1)
    plan_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    decision: Literal["approved", "rejected", "revise"] | None = None
    comment: str | None = Field(default=None, max_length=2000)
    after_seq: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=50, ge=1, le=200)
    stream: bool = False

    @model_validator(mode="after")
    def validate_action_fields(self) -> WorkflowRequest:
        phase_actions = {"plan", "continue", "revise"}
        if self.action in phase_actions:
            self.model_modes = self.model_modes or ModelModes()
        elif self.model_modes is not None:
            raise ValueError("model_modes is supported only for plan, continue, and revise")
        if self.action == "plan" and (self.workflow_id is not None or not self.scenario):
            raise ValueError("plan requires scenario and must not include workflow_id")
        if self.action != "plan" and not self.workflow_id:
            raise ValueError(f"{self.action} requires workflow_id")
        if self.action in {"continue", "revise"} and not self.scenario:
            raise ValueError(f"{self.action} requires scenario")
        if self.action == "approve" and any(x is None for x in (self.plan_revision, self.plan_digest, self.decision)):
            raise ValueError("approve requires plan_revision, plan_digest, and decision")
        if self.action == "revise" and not self.phase_id:
            raise ValueError("revise requires phase_id")
        return self
