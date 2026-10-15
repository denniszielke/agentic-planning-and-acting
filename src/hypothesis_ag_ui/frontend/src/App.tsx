import { HttpAgent } from "@ag-ui/client";
import type { BaseEvent, Message } from "@ag-ui/core";
import {
  AlertTriangle,
  Check,
  ChevronRight,
  CircleDot,
  Clock3,
  CloudSun,
  FlaskConical,
  Hotel,
  LoaderCircle,
  Plane,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  UserRound,
  X,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

type AgentStats = { runs?: number; model_calls?: number; input_tokens?: number | null; output_tokens?: number | null; total_tokens?: number | null; tool_calls?: number; duration_ms?: number };
type Stats = {
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  tool_calls?: number;
  tool_calls_by_category?: Record<string, number>;
  model_calls?: number;
  wall_clock_ms?: number;
  awaiting_human_ms?: number;
  by_agent?: Record<string, AgentStats>;
  usage_complete?: boolean;
};
type ActionFlags = { can_revise?: boolean; can_continue?: boolean; can_approve?: boolean; can_cancel?: boolean };
type ModelMode = "decision" | "completion";
type ModelModes = { planning: ModelMode; execution: ModelMode };
type Step = { id?: number; step_id?: number; title: string; description?: string; tool_category?: "weather" | "hotels" | "travel"; expected_evidence?: string[]; completion_criteria?: string[] };
type Plan = { objective: string; steps: Step[] };
type PhaseSummary = {
  phase_id: string; ordinal: number; origin?: string; parent_phase_id?: string | null; based_on_phase_id?: string | null;
  title?: string; stage: "pending" | "hypothesizing" | "planning" | "awaiting_approval" | "approved" | "executing" | "completed" | "rejected" | "failed" | "cancelled"; is_tail?: boolean; created_at?: string; started_at?: string | null; ended_at?: string | null;
  plan_revision?: number | null; plan_digest?: string | null; step_counts?: { total?: number; completed?: number; failed?: number };
  model_modes?: Partial<ModelModes>; stats?: Stats; actions?: ActionFlags; error?: { code?: string; message?: string } | string | null;
};
type PlanRevision = { revision: number; plan: Plan; plan_digest: string; produced_at?: string; feedback?: string | null; stats?: Stats };
type Hypothesis = { statement: string; assumptions?: string[]; evidence_needed?: string[]; success_criteria?: string[]; risks_and_constraints?: string[] };
type WorkflowEvent = {
  schema_version?: number; seq?: number; event_id?: string; workflow_id?: string; phase_id?: string;
  at?: string; persisted_at?: string | null; actor?: string; actor_id?: string | null; type?: string;
  stage?: string | null; step_id?: number | null;
  correlation?: { client_request_id?: string | null; invocation_id?: string | null; input_id?: string | null; recovery_count?: number };
  data?: Record<string, unknown>; duration_ms?: number | null;
};
type Progress = {
  phase_id?: string; stage?: string; state?: "running" | "waiting_human" | "done"; percent?: number; current: null;
  steps?: Array<{ step_id: number; status: string; started_at?: string | null; ended_at?: string | null }>;
  stage_started_at?: string; last_event_seq?: number; heartbeat_at?: string; heartbeat_age_ms?: number; stalled?: boolean; recovery_count?: number;
  events_degraded?: boolean; stats_so_far?: Stats;
};
type PhaseView = PhaseSummary & {
  hypothesis?: Hypothesis; plan_revisions?: PlanRevision[]; plan?: Plan; approval?: { phase_id?: string; decision?: string; approver_id?: string; decided_at?: string; comment?: string | null; plan_revision?: number; plan_digest?: string };
  execution?: { completed_steps?: number; final_answer?: string; artifacts?: Array<Record<string, unknown>>; warnings?: string[] } | null;
  step_states?: Array<{ step_id: number; status: string; started_at?: string | null; ended_at?: string | null; evidence_ref?: unknown; tool_calls?: unknown }>;
  tool_activity?: Array<Record<string, unknown>>; stats?: Stats; stats_by_agent?: Record<string, AgentStats>; error?: { code?: string; message?: string } | string | null;
};
type EventsResponse = { workflow_id: string; events: WorkflowEvent[]; next_after_seq: number; server_time: string };
type Workflow = {
  workflow_id: string; status?: "active" | "idle" | "failed"; tail_phase_id?: string; phase?: PhaseView; phases?: PhaseSummary[]; progress?: Progress | null;
  run?: { phase_id?: string; task_id?: string; input_id?: string; invocation_id?: string; stream_id?: string; stage?: string; claimed_at?: string; heartbeat_at?: string } | null;
  request?: { client_request_id: string; replayed: boolean }; server_time?: string;
};
type ApiFailure = { status?: number; code?: string; message: string; current?: Record<string, unknown> };

const toolMeta = {
  weather: { label: "Weather", icon: CloudSun },
  hotels: { label: "Hotels", icon: Hotel },
  travel: { label: "Transportation", icon: Plane },
};
const makeId = () => crypto.randomUUID();
const terminal = new Set(["completed", "rejected", "failed", "cancelled"]);
const defaultModelModes: ModelModes = { planning: "completion", execution: "completion" };

function latestPlan(phase?: PhaseView): Plan | undefined {
  return phase?.plan_revisions?.at(-1)?.plan ?? phase?.plan;
}
function phaseModelModes(phase?: Pick<PhaseSummary, "model_modes">): ModelModes {
  return {
    planning: phase?.model_modes?.planning === "decision" ? "decision" : "completion",
    execution: phase?.model_modes?.execution === "decision" ? "decision" : "completion",
  };
}
function modelModeLabel(mode: ModelMode) {
  return mode === "decision" ? "Decision model" : "Completion model";
}
function unwrapWorkflow(value: unknown): Workflow | null {
  if (!value || typeof value !== "object") return null;
  const data = value as Record<string, unknown>;
  if (data.workflow && typeof data.workflow === "object") return data.workflow as Workflow;
  return data.workflow_id ? (data as Workflow) : null;
}
function parseFailure(reason: unknown): ApiFailure {
  const item = (reason && typeof reason === "object" ? reason : {}) as Record<string, unknown>;
  const rawMessage = reason instanceof Error ? reason.message : String(reason);
  const status = Number(item.status ?? (item.response as Record<string, unknown> | undefined)?.status ?? rawMessage.match(/\b(409|4\d\d|5\d\d)\b/)?.[0]) || undefined;
  let message = rawMessage;
  const raw = item.detail ?? item.body ?? item.data;
  let body: Record<string, unknown> | undefined;
  if (raw && typeof raw === "object") body = raw as Record<string, unknown>;
  else if (typeof raw === "string") {
    try { body = JSON.parse(raw) as Record<string, unknown>; } catch { /* keep the transport message */ }
  }
  const detail = body?.detail && typeof body.detail === "object" ? body.detail as Record<string, unknown> : body;
  const error = detail?.error && typeof detail.error === "object" ? detail.error as Record<string, unknown> : detail;
  const code = typeof error?.code === "string" ? error.code : undefined;
  const serverMessage = typeof error?.message === "string" ? error.message : undefined;
  if (status === 409 || ["phase_in_progress", "stale_plan", "request_conflict"].includes(code ?? "")) {
    const explanation = serverMessage ?? (code === "stale_plan" ? "The plan revision changed before your decision was accepted." : code === "phase_in_progress" ? "This phase is still running. Wait for it to pause or finish, then try again." : "This request conflicts with a newer workflow action.");
    message = `${explanation} (409 conflict${code ? `: ${code}` : ""}). Refresh the workflow before retrying.`;
  } else if (serverMessage) message = serverMessage;
  return { status, code, message, current: error?.current as Record<string, unknown> | undefined };
}
function stamp(iso?: string | null) {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return <span>{iso}</span>;
  return <time dateTime={iso} title={date.toISOString()}>{new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date)}</time>;
}
function duration(ms?: number | null) {
  if (ms == null) return null;
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}
function statsPresent(stats?: Stats) {
  return !!stats && ([stats.total_tokens, stats.input_tokens, stats.output_tokens, stats.tool_calls, stats.model_calls, stats.wall_clock_ms, stats.awaiting_human_ms].some((value) => value != null) || Object.values(stats.tool_calls_by_category ?? {}).some((count) => count != null) || Object.values(stats.by_agent ?? {}).some((agent) => agent.duration_ms != null || agent.total_tokens != null || agent.tool_calls != null));
}

export function App() {
  const [scenario, setScenario] = useState("");
  const [planModelModes, setPlanModelModes] = useState<ModelModes>(defaultModelModes);
  const [comment, setComment] = useState("");
  const [followUp, setFollowUp] = useState("");
  const [continueModelModes, setContinueModelModes] = useState<ModelModes>(defaultModelModes);
  const [reviseFeedback, setReviseFeedback] = useState("");
  const [reviseModelModes, setReviseModelModes] = useState<ModelModes>(defaultModelModes);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [phase, setPhase] = useState<PhaseView | null>(null);
  const [history, setHistory] = useState<WorkflowEvent[]>([]);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [selectedPhaseId, setSelectedPhaseId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const threadId = useRef(makeId());
  const agent = useRef(new HttpAgent({ url: "/api/agent", threadId: threadId.current }));
  const activeTail = !!workflow && (workflow.status === "active" || (workflow.phase?.phase_id === workflow.tail_phase_id && !terminal.has(workflow.phase?.stage ?? "")));

  const acceptWorkflow = useCallback((value: unknown, forcePhase = false) => {
    const next = unwrapWorkflow(value);
    if (!next) return;
    setWorkflow((current) => ({ ...current, ...next, phases: next.phases ?? current?.phases }));
    const nextPhase = next.phase;
    if (nextPhase) {
      setPhase((current) => forcePhase || !selectedPhaseId || selectedPhaseId === nextPhase.phase_id ? nextPhase : current);
      if (forcePhase || !selectedPhaseId || selectedPhaseId === nextPhase.phase_id) setSelectedPhaseId(nextPhase.phase_id);
    }
    if (next.progress) setProgress(next.progress);
  }, [selectedPhaseId]);

  const loadHistory = useCallback(async (workflowId: string, phaseId?: string) => {
    try {
      const pageSize = 200;
      let afterSeq = 0;
      const events: WorkflowEvent[] = [];
      while (true) {
        const query = new URLSearchParams({ limit: String(pageSize) });
        if (phaseId) query.set("phase_id", phaseId);
        if (afterSeq > 0) query.set("after_seq", String(afterSeq));
        const response = await fetch(`/api/workflows/${encodeURIComponent(workflowId)}/events?${query}`);
        if (!response.ok) return;
        const result = await response.json() as EventsResponse;
        events.push(...result.events);
        if (result.events.length < pageSize || result.next_after_seq <= afterSeq) break;
        afterSeq = result.next_after_seq;
      }
      setHistory(events.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0)));
    } catch { /* Keep the current view usable if the optional event page is unavailable. */ }
  }, []);

  useEffect(() => {
    if (workflow?.workflow_id && selectedPhaseId) void loadHistory(workflow.workflow_id, selectedPhaseId);
  }, [loadHistory, selectedPhaseId, workflow?.workflow_id]);

  const refresh = useCallback(async (workflowId: string, phaseId?: string) => {
    const url = phaseId
      ? `/api/workflows/${encodeURIComponent(workflowId)}/phases/${encodeURIComponent(phaseId)}`
      : `/api/workflows/${encodeURIComponent(workflowId)}`;
    const response = await fetch(url);
    if (!response.ok) throw parseFailure({ status: response.status, body: await response.text() });
    const result = await response.json() as Workflow;
    acceptWorkflow(result);
    if (result.phase) {
      setPhase(result.phase);
      setProgress(result.progress ?? null);
      setSelectedPhaseId(result.phase.phase_id);
      void loadHistory(workflowId, result.phase.phase_id);
    }
    return result;
  }, [acceptWorkflow, loadHistory]);

  async function invoke(control: Record<string, unknown>) {
    setBusy(true);
    setFailure(null);
    agent.current.setState({ workflow: control });
    const input = {
      threadId: threadId.current, runId: makeId(), messages: [] as Message[],
      state: { workflow: control }, tools: [], context: [], forwardedProps: {},
    };
    try {
      await new Promise<void>((resolve, reject) => {
        agent.current.run(input).subscribe({
          next: (event: BaseEvent) => {
            if (event.type === "STATE_SNAPSHOT") {
              const snapshot = (event as unknown as { snapshot: { workflow: Workflow } }).snapshot;
              acceptWorkflow(snapshot.workflow, ["plan", "continue", "revise"].includes(String(control.action)));
            } else if (event.type === "CUSTOM") {
              const custom = event as unknown as { name?: string; value?: WorkflowEvent | Progress };
              if (custom.name === "workflow_event") {
                const action = custom.value as WorkflowEvent;
                if (action?.at) setHistory((items) => items.some((item) => item.event_id === action.event_id) ? items : [...items, action].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0)));
              }
              if (custom.name === "workflow_progress") setProgress(custom.value as Progress);
            } else if (event.type === "STATE_DELTA") {
              const delta = (event as unknown as { delta?: Array<{ value?: unknown }> }).delta;
              if (delta?.[0]?.value) acceptWorkflow(delta[0].value, ["plan", "continue", "revise"].includes(String(control.action)));
            }
          },
          error: reject,
          complete: resolve,
        });
      });
    } catch (reason) {
      setFailure(parseFailure(reason));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!workflow?.workflow_id || !activeTail || busy) return;
    const timer = window.setInterval(() => {
      void fetch(`/api/workflows/${encodeURIComponent(workflow.workflow_id)}/progress`)
        .then(async (response) => {
          if (!response.ok) throw parseFailure({ status: response.status, body: await response.text() });
          return response.json() as Promise<Workflow>;
        })
        .then((next) => {
          acceptWorkflow(next);
          if (next.phase?.phase_id === selectedPhaseId) setPhase(next.phase);
          setProgress(next.progress ?? null);
        })
        .catch((reason: unknown) => setFailure(parseFailure(reason)));
    }, progress?.state === "waiting_human" ? 10_000 : 2_000);
    return () => window.clearInterval(timer);
  }, [acceptWorkflow, activeTail, busy, progress?.state, selectedPhaseId, workflow?.workflow_id]);

  function submitScenario(event: FormEvent) {
    event.preventDefault();
    if (!scenario.trim()) return;
    void invoke({ action: "plan", scenario: scenario.trim(), model_modes: planModelModes, client_request_id: makeId() });
  }
  function decide(decision: "approved" | "rejected" | "revise") {
    if (!workflow || !phase) return;
    const revision = phase.plan_revisions?.at(-1);
    void invoke({
      action: "approve", workflow_id: workflow.workflow_id, phase_id: phase.phase_id,
      plan_revision: revision?.revision ?? phase.plan_revision, plan_digest: revision?.plan_digest ?? phase.plan_digest,
      decision, comment: comment.trim() || undefined, client_request_id: makeId(),
    });
    setComment("");
  }
  function submitContinue(event: FormEvent) {
    event.preventDefault();
    if (!workflow || !followUp.trim()) return;
    void invoke({ action: "continue", workflow_id: workflow.workflow_id, scenario: followUp.trim(), model_modes: continueModelModes, client_request_id: makeId() });
    setFollowUp("");
  }
  function submitRevise(event: FormEvent) {
    event.preventDefault();
    if (!workflow || !phase || !reviseFeedback.trim()) return;
    void invoke({ action: "revise", workflow_id: workflow.workflow_id, phase_id: phase.phase_id, scenario: reviseFeedback.trim(), model_modes: reviseModelModes, client_request_id: makeId() });
    setReviseFeedback("");
  }
  async function cancelCurrentPhase() {
    if (!workflow || !phase) return;
    const cancelAgent = new HttpAgent({ url: "/api/agent", threadId: threadId.current });
    setFailure(null);
    const input = {
      threadId: threadId.current, runId: makeId(), messages: [] as Message[],
      state: { workflow: { action: "cancel", workflow_id: workflow.workflow_id, phase_id: phase.phase_id, client_request_id: makeId() } },
      tools: [], context: [], forwardedProps: {},
    };
    try {
      await new Promise<void>((resolve, reject) => cancelAgent.run(input).subscribe({
        next: (event: BaseEvent) => {
          if (event.type === "STATE_SNAPSHOT") acceptWorkflow((event as unknown as { snapshot: { workflow: Workflow } }).snapshot.workflow);
          else if (event.type === "STATE_DELTA") {
            const delta = (event as unknown as { delta?: Array<{ value?: unknown }> }).delta;
            if (delta?.[0]?.value) acceptWorkflow(delta[0].value);
          }
        }, error: reject, complete: resolve,
      }));
    } catch (reason) { setFailure(parseFailure(reason)); }
  }
  function reset() {
    agent.current.abortRun();
    threadId.current = makeId();
    agent.current = new HttpAgent({ url: "/api/agent", threadId: threadId.current });
    setWorkflow(null); setPhase(null); setHistory([]); setProgress(null); setSelectedPhaseId(null);
    setScenario(""); setPlanModelModes(defaultModelModes); setComment(""); setFollowUp(""); setContinueModelModes(defaultModelModes); setReviseFeedback(""); setReviseModelModes(defaultModelModes); setFailure(null);
  }
  function selectPhase(summary: PhaseSummary) {
    if (!workflow) return;
    setSelectedPhaseId(summary.phase_id);
    setFailure(null);
    void refresh(workflow.workflow_id, summary.phase_id).catch((reason: unknown) => setFailure(parseFailure(reason)));
  }

  const selectedIsTail = !!workflow && (phase?.phase_id === workflow.tail_phase_id || !phase?.phase_id);
  const plan = latestPlan(phase ?? undefined);
  const waiting = phase?.stage === "awaiting_approval";
  const selectedModelModes = phaseModelModes(phase ?? undefined);
  const actions = phase?.actions ?? {};
  const summaries: PhaseSummary[] = workflow?.phases ?? (workflow?.phase ? [workflow.phase] : []);
  const superseded = new Set<string>();
  for (const item of summaries) {
    if (item.origin === "revise" && item.based_on_phase_id) {
      const base = summaries.find((candidate) => candidate.phase_id === item.based_on_phase_id);
      if (base) for (const candidate of summaries) if (candidate.ordinal > base.ordinal && candidate.ordinal < item.ordinal) superseded.add(candidate.phase_id);
    }
  }

  return (
    <main>
      <header className="topbar">
        <div className="brand"><FlaskConical size={19} /><span>Hypothesis Workbench</span></div>
        <div className="status-cluster"><span className={`status-dot ${activeTail || busy ? "working" : ""}`} /><span>{busy ? "Updating phase" : activeTail ? "Phase in progress" : "Ready"}</span><button className="icon-button" onClick={reset} title="New workflow" aria-label="New workflow"><RotateCcw size={17} /></button></div>
      </header>

      <section className="workspace">
        <section className="content">
          {!workflow && !busy && <section className="scenario-panel">
            <div className="scenario-heading"><CircleDot size={20} /><div><h1>Frame a testable scenario</h1><p>The agent will formulate a hypothesis and prepare a bounded evidence plan for review.</p></div></div>
            <form onSubmit={submitScenario}><textarea value={scenario} onChange={(event) => setScenario(event.target.value)} placeholder="Example: Compare a four-night Berlin or Prague trip starting 2026-09-14, including weather, a hotel with a pool, and transportation from Amsterdam." autoFocus /><ModelModeSelectors value={planModelModes} onChange={setPlanModelModes} idPrefix="plan" /><div className="form-footer"><span>{scenario.length.toLocaleString()} / 50,000</span><button className="primary" disabled={!scenario.trim() || busy}><FlaskConical size={17} />Build hypothesis</button></div></form>
          </section>}
          {busy && !workflow && <LoadingState label="Formulating the hypothesis and research plan" />}
          {workflow && phase && <>
            <section className="title-row"><div><p className="eyebrow">{selectedIsTail ? "Current phase" : `Phase ${phase.ordinal} · history`}</p><h1>{phase.hypothesis?.statement ?? phase.title ?? `Phase ${phase.ordinal}`}</h1><p className="phase-subtitle">{phase.title && phase.title !== phase.hypothesis?.statement ? phase.title : `${phase.origin ?? "initial"} phase`} · created {stamp(phase.created_at)}</p><ModelModeBadges modelModes={selectedModelModes} /></div><span className={`status-pill ${phase.stage}`}>{phase.stage.replaceAll("_", " ")}</span></section>

            {progress && selectedIsTail && !terminal.has(phase.stage) && <ProgressPanel progress={progress} />}
            {progress?.stalled && selectedIsTail && <div className="error-banner"><AlertTriangle size={18} />No progress heartbeat has been received recently. The phase may be recovering.</div>}
            {progress?.events_degraded && <div className="notice-banner">Live history is temporarily degraded; phase status continues to refresh.</div>}
            {actions.can_cancel && selectedIsTail && <div className="cancel-row"><button className="secondary danger" onClick={() => void cancelCurrentPhase()}><X size={16} />Cancel running phase</button></div>}

            {phase.hypothesis && <section className="evidence-grid"><InfoList title="Assumptions" items={phase.hypothesis.assumptions} /><InfoList title="Evidence needed" items={phase.hypothesis.evidence_needed} /><InfoList title="Success criteria" items={phase.hypothesis.success_criteria} /><InfoList title="Risks & constraints" items={phase.hypothesis.risks_and_constraints} warning /></section>}

            {plan && <section className="plan-section"><div className="section-heading"><div><p className="eyebrow">{phase.plan_revisions?.length ? `Plan revision ${phase.plan_revisions.length}` : "Proposed research plan"}</p><h2>{plan.objective}</h2></div><span>{plan.steps.length} steps</span></div><div className="steps">{plan.steps.map((step, index) => {
              const meta = step.tool_category ? toolMeta[step.tool_category] : null;
              const Icon = meta?.icon ?? ChevronRight;
              const stepState = (phase.step_states ?? progress?.steps)?.find((candidate) => candidate.step_id === (step.id ?? step.step_id ?? index + 1));
              return <article className={`step ${stepState?.status ?? ""}`} key={step.id ?? step.step_id ?? index}><div className="step-index">{stepState?.status === "completed" ? <Check size={15} /> : step.id ?? step.step_id ?? index + 1}</div><div className="step-body"><div className="step-title"><h3>{step.title}</h3><span>{stepState?.status && <b className={`step-state ${stepState.status}`}>{stepState.status}</b>}{meta && <><Icon size={14} />{meta.label}</>}</span></div>{step.description && <p>{step.description}</p>}<div className="step-details">{step.expected_evidence && <div><b>Expected evidence</b>{step.expected_evidence.map((item) => <span key={item}><ChevronRight size={13} />{item}</span>)}</div>}{step.completion_criteria && <div><b>Complete when</b>{step.completion_criteria.map((item) => <span key={item}><Check size={13} />{item}</span>)}</div>}</div>{stepState && <small className="step-times">{stepState.started_at && <>Started {stamp(stepState.started_at)}</>}{stepState.ended_at && <> · Finished {stamp(stepState.ended_at)}</>}</small>}</div></article>;
            })}</div></section>}

            {waiting && actions.can_approve && selectedIsTail && <section className="approval"><div className="approval-heading"><ShieldCheck size={21} /><div><h2>Human approval required</h2><p>Execution is blocked until this exact plan revision and digest are approved.</p></div></div><textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Optional feedback for revision or rejection" /><div className="approval-actions"><button className="secondary danger" disabled={busy} onClick={() => decide("rejected")}><X size={17} />Reject</button><button className="secondary" disabled={busy} onClick={() => decide("revise")}><RefreshCw size={17} />Request revision</button><button className="primary" disabled={busy} onClick={() => decide("approved")}><Check size={17} />Approve & execute with {modelModeLabel(selectedModelModes.execution)}</button></div>{phase.plan_digest && <code className="digest">{phase.plan_digest}</code>}</section>}

            {phase.execution && <section className="result"><p className="eyebrow">Execution result</p><h2>Findings</h2><div className="answer">{phase.execution.final_answer}</div>{phase.execution.warnings?.length ? <InfoList title="Warnings" items={phase.execution.warnings} warning /> : null}</section>}
            {phase.approval && <div className="approval-record"><ShieldCheck size={16} />Decision: <b>{phase.approval.decision}</b> · {stamp(phase.approval.decided_at)}{phase.approval.comment && <p>{phase.approval.comment}</p>}</div>}
            {phase.error && <div className="error-banner"><AlertTriangle size={18} />{typeof phase.error === "string" ? phase.error : `${phase.error.code ? `${phase.error.code}: ` : ""}${phase.error.message ?? "The phase failed."}`}</div>}

            {actions.can_revise && <form className="action-composer" onSubmit={submitRevise}><h2>Revise from phase {phase.ordinal}</h2><p>Feedback creates a new phase based on this checkpoint. Later phases remain in history and are marked superseded.</p><textarea value={reviseFeedback} onChange={(event) => setReviseFeedback(event.target.value)} placeholder="What should change in the plan?" /><ModelModeSelectors value={reviseModelModes} onChange={setReviseModelModes} idPrefix="revise" /><div className="form-footer"><span>Creates a new plan for approval</span><button className="primary" disabled={!reviseFeedback.trim() || busy}><RefreshCw size={16} />Revise from here</button></div></form>}
            {selectedIsTail && actions.can_continue && <form className="action-composer continue-composer" onSubmit={submitContinue}><h2>Continue after execution</h2><p>Add the next request to the conversation; the completed phase stays as an immutable checkpoint.</p><textarea value={followUp} onChange={(event) => setFollowUp(event.target.value)} placeholder="What would you like the agents to plan and execute next?" /><ModelModeSelectors value={continueModelModes} onChange={setContinueModelModes} idPrefix="continue" /><div className="form-footer"><span>Starts a new linked phase</span><button className="primary" disabled={!followUp.trim() || busy}><ChevronRight size={17} />Continue</button></div></form>}
            {busy && workflow.phase?.stage !== "awaiting_approval" && <LoadingState label="Updating phase" compact />}

            <History events={history} />
            {phase.stats && (statsPresent(phase.stats) || Object.keys(phase.stats_by_agent ?? {}).length > 0) && <section className="phase-stats"><div className="section-heading"><div><p className="eyebrow">Phase usage</p><h2>Statistics</h2></div></div><StatsView stats={phase.stats} agents={phase.stats_by_agent} /></section>}
          </>}
          {failure && <div className="error-banner conflict-banner"><AlertTriangle size={18} /><span>{failure.message}{failure.current && <code>{JSON.stringify(failure.current)}</code>}</span><button className="secondary" onClick={() => workflow && void refresh(workflow.workflow_id).catch((reason: unknown) => setFailure(parseFailure(reason)))}>Refresh</button></div>}
        </section>

        <aside className="navigator">
          <div className="navigator-heading"><div><p className="eyebrow">Workflow</p><h2>Phase history</h2></div>{workflow && <button className="icon-button" onClick={() => void refresh(workflow.workflow_id).catch((reason: unknown) => setFailure(parseFailure(reason)))} aria-label="Refresh phases" title="Refresh phases"><RefreshCw size={16} /></button>}</div>
          {workflow && <div className="workflow-meta"><span>Workflow ID</span><code title={workflow.workflow_id}>{workflow.workflow_id.slice(0, 12)}</code></div>}
          {!workflow && <p className="muted navigator-empty">Your linked planning and execution phases will appear here.</p>}
          {!!summaries.length && <ol className="timeline">{[...summaries].sort((a, b) => a.ordinal - b.ordinal).map((item, index) => {
            const supersededPhase = superseded.has(item.phase_id);
            const isSelected = selectedPhaseId === item.phase_id;
            return <li key={item.phase_id} className={`timeline-item ${isSelected ? "selected" : ""} ${supersededPhase ? "superseded" : ""} ${item.is_tail ? "tail" : ""}`}>
              {index > 0 && <span className="timeline-connector" aria-hidden="true" />}
              <button className="phase-card" onClick={() => selectPhase(item)} aria-current={isSelected ? "step" : undefined}>
                <span className="phase-node">{item.ordinal}</span><span className="phase-card-body"><span className="phase-card-heading"><b>Phase {item.ordinal}</b><span className={`status-pill ${item.stage}`}>{item.stage.replaceAll("_", " ")}</span></span><span className="phase-title">{item.title || `${item.origin ?? "workflow"} phase`}</span><span className="phase-time">Created {stamp(item.created_at)}{item.started_at && <> · Started {stamp(item.started_at)}</>}{item.ended_at && <> · Ended {stamp(item.ended_at)}</>}</span><ModelModeBadges modelModes={phaseModelModes(item)} compact />{item.based_on_phase_id && item.based_on_phase_id !== item.parent_phase_id && <span className="lineage-chip">Based on #{summaries.find((candidate) => candidate.phase_id === item.based_on_phase_id)?.ordinal ?? "?"}</span>}
                {statsPresent(item.stats) && <span className="stats-chips">{item.stats?.total_tokens != null && <span>{item.stats.total_tokens.toLocaleString()} tokens</span>}{item.stats?.tool_calls != null && <span>{item.stats.tool_calls} tool calls</span>}{item.stats?.wall_clock_ms != null && <span>{duration(item.stats.wall_clock_ms)}</span>}{Object.entries(item.stats?.by_agent ?? {}).filter(([, agent]) => agent.duration_ms != null).map(([name, agent]) => <span key={name} title={`${name} duration`}>{name}: {duration(agent.duration_ms)}</span>)}</span>}
                {item.step_counts && <span className="step-counts">{item.step_counts.completed ?? 0}/{item.step_counts.total ?? 0} steps complete</span>}
              </span>
              {supersededPhase && <span className="superseded-label">Superseded</span>}
            </button>
          </li>;
          })}</ol>}
        </aside>
      </section>
    </main>
  );
}

function ModelModeSelectors({ value, onChange, idPrefix }: { value: ModelModes; onChange: (value: ModelModes) => void; idPrefix: string }) {
  return <fieldset className="model-mode-fields"><legend>Models for this phase</legend><label htmlFor={`${idPrefix}-planning-model`}><span>Planning model</span><select id={`${idPrefix}-planning-model`} value={value.planning} onChange={(event) => onChange({ ...value, planning: event.target.value as ModelMode })}><option value="completion">Completion model</option><option value="decision">Decision model</option></select><small>Includes hypothesis and plan generation.</small></label><label htmlFor={`${idPrefix}-execution-model`}><span>Execution model</span><select id={`${idPrefix}-execution-model`} value={value.execution} onChange={(event) => onChange({ ...value, execution: event.target.value as ModelMode })}><option value="completion">Completion model</option><option value="decision">Decision model</option></select></label></fieldset>;
}

function ModelModeBadges({ modelModes, compact = false }: { modelModes: ModelModes; compact?: boolean }) {
  return <span className={`model-mode-badges ${compact ? "compact" : ""}`} aria-label={`Planning: ${modelModeLabel(modelModes.planning)}; execution: ${modelModeLabel(modelModes.execution)}`}><span>Planning: {modelModeLabel(modelModes.planning)}</span><span>Execution: {modelModeLabel(modelModes.execution)}</span></span>;
}

function InfoList({ title, items = [], warning = false }: { title: string; items?: string[]; warning?: boolean }) {
  return <article className={`info-block ${warning ? "warning" : ""}`}><h3>{warning && <AlertTriangle size={15} />}{title}</h3>{items.length ? <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul> : <p>None specified</p>}</article>;
}
function LoadingState({ label, compact = false }: { label: string; compact?: boolean }) {
  return <div className={`loading ${compact ? "compact" : ""}`}><LoaderCircle className="spin" size={22} /><span>{label}</span></div>;
}
function ProgressPanel({ progress }: { progress: Progress }) {
  const percent = Math.max(0, Math.min(100, progress.percent ?? 0));
  return <section className={`progress-panel ${progress.state ?? ""}`}><div className="progress-heading"><div><p className="eyebrow">Live progress · {progress.stage?.replaceAll("_", " ") ?? "working"}</p><h2>{progress.state === "waiting_human" ? "Waiting for your decision" : progress.state === "done" ? "Phase complete" : "Agents are working"}</h2></div><b>{Math.round(percent)}%</b></div><div className="progress-track" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${percent}%` }} /></div><div className="progress-detail">{progress.heartbeat_at && <span>Last heartbeat {stamp(progress.heartbeat_at)}</span>}{progress.recovery_count ? <span>Recovery {progress.recovery_count}</span> : null}</div>{progress.steps?.length ? <div className="progress-steps">{progress.steps.map((step) => <span key={step.step_id} className={step.status}><i />Step {step.step_id}: {step.status}</span>)}</div> : null}</section>;
}
function StatsView({ stats, agents }: { stats: Stats; agents?: Record<string, AgentStats> }) {
  const metrics = [
    ["Total tokens", stats.total_tokens?.toLocaleString()], ["Input tokens", stats.input_tokens?.toLocaleString()],
    ["Output tokens", stats.output_tokens?.toLocaleString()], ["Tool calls", stats.tool_calls?.toLocaleString()],
    ["Model calls", stats.model_calls?.toLocaleString()], ["Wall-clock time", duration(stats.wall_clock_ms) ?? undefined],
    ["Waiting for approval", duration(stats.awaiting_human_ms) ?? undefined],
  ].filter(([, value]) => value != null);
  return <><div className="stat-grid">{metrics.map(([label, value]) => <div className="stat-card" key={label}><b>{value}</b><span>{label}</span></div>)}</div>{Object.keys(stats.tool_calls_by_category ?? {}).length > 0 && <div className="stats-chips">{Object.entries(stats.tool_calls_by_category ?? {}).map(([category, count]) => <span key={category}>{category}: {count} tool calls</span>)}</div>}{stats.usage_complete === false && <p className="muted">Token usage is partial; unavailable model calls are omitted.</p>}{(agents ?? stats.by_agent) && Object.keys(agents ?? stats.by_agent ?? {}).length > 0 && <div className="agent-stats">{Object.entries(agents ?? stats.by_agent ?? {}).map(([name, agent]) => <div key={name}><b>{name}</b><span>{agent.runs ?? 0} runs</span>{agent.total_tokens != null && <span>{agent.total_tokens.toLocaleString()} tokens</span>}{agent.tool_calls != null && <span>{agent.tool_calls} tool calls</span>}{agent.duration_ms != null && <span>{duration(agent.duration_ms)}</span>}</div>)}</div>}</>;
}
function History({ events }: { events: WorkflowEvent[] }) {
  const sorted = [...events].filter((event) => event.at).sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  return <section className="history-section"><div className="section-heading"><div><p className="eyebrow">Audit trail</p><h2>History</h2></div><span>{sorted.length} actions</span></div>{sorted.length ? <ol className="history-list">{sorted.map((event, index) => <li key={event.event_id ?? `${event.seq}-${index}`}><span className={`actor-icon ${event.actor ?? "system"}`}>{event.actor === "human" ? <UserRound size={14} /> : event.actor === "agent" ? <FlaskConical size={14} /> : <Clock3 size={14} />}</span><div><b>{(event.actor_id ?? event.actor ?? "System")} · {(event.type ?? "action").replaceAll(".", " ")}</b><p>{eventSummary(event)}</p></div><span className="history-time">{stamp(event.at)}</span></li>)}</ol> : <p className="muted">Action history will appear as the phase progresses.</p>}</section>;
}
function eventSummary(event: WorkflowEvent) {
  const data = event.data ?? {};
  const values = [data.title, data.action, data.decision, data.run_kind, data.message, data.scenario_excerpt, data.user_input, data.tool_category, event.step_id ? `Step ${event.step_id}` : undefined].filter((value) => typeof value === "string");
  return values.length ? values.join(" · ") : event.stage ?? "Phase activity";
}
