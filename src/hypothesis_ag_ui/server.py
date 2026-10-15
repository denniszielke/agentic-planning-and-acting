"""Local AG-UI adapter and static host for the phased hypothesis workflow."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "frontend" / "dist"
SCOPE = "https://ai.azure.com/.default"
logger = logging.getLogger(__name__)
MUTATING_ACTIONS = {"plan", "approve", "continue", "revise", "cancel"}
PAUSED_STAGES = {"completed", "rejected", "failed", "cancelled", "awaiting_approval"}
load_dotenv(ROOT.parent.parent / ".env", override=False)


class BearerAuth(httpx.Auth):
    def __init__(self) -> None:
        credential = DefaultAzureCredential()
        self._token_provider = get_bearer_token_provider(credential, SCOPE)

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self._token_provider()}"
        yield request


def upstream_url() -> str:
    if explicit := os.getenv("HYPOTHESIS_AGENT_INVOCATIONS_URL", "").strip():
        return explicit.rstrip("/")
    project = os.getenv("AZURE_AI_PROJECT_ENDPOINT", "").strip().rstrip("/")
    if not project:
        raise RuntimeError(
            "Set HYPOTHESIS_AGENT_INVOCATIONS_URL or AZURE_AI_PROJECT_ENDPOINT."
        )
    agent = os.getenv(
        "HYPOTHESIS_HOSTED_AGENT_NAME", "scenario-hosted-hypothesis-agent"
    )
    return f"{project}/agents/{agent}/endpoint/protocols/invocations"


@asynccontextmanager
async def lifespan(app: FastAPI):
    existing = getattr(app.state, "http", None)
    if existing is not None:
        yield
        return
    try:
        app.state.http = httpx.AsyncClient(auth=BearerAuth(), timeout=30.0)
    except ImportError as exc:
        if "socksio" not in str(exc):
            raise
        logger.warning("SOCKS proxy support is unavailable; using direct HTTP")
        app.state.http = httpx.AsyncClient(
            auth=BearerAuth(), timeout=30.0, trust_env=False
        )
    try:
        yield
    finally:
        await app.state.http.aclose()
        del app.state.http


app = FastAPI(title="Hypothesis AG-UI", lifespan=lifespan)


def event_stream(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def workflow_control(payload: dict[str, Any]) -> dict[str, Any]:
    state = payload.get("state")
    if isinstance(state, dict) and isinstance(state.get("workflow"), dict):
        return state["workflow"]
    if isinstance(payload.get("action"), str):
        return payload
    raise HTTPException(
        422,
        "Invocation must be a workflow request or contain RunAgentInput.state.workflow",
    )


def response_error(response: httpx.Response) -> Any:
    try:
        data = response.json()
    except (ValueError, AttributeError):
        return {"code": "upstream_error", "message": response.text}
    if isinstance(data, dict):
        return data.get("error", data.get("detail", data))
    return {"code": "upstream_error", "message": str(data)}


async def invoke_workflow(request: Request, control: dict[str, Any]) -> dict[str, Any]:
    response = await request.app.state.http.post(
        upstream_url(), params={"api-version": "v1"}, json=control
    )
    if response.is_error:
        raise HTTPException(response.status_code, detail=response_error(response))
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Hypothesis agent returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise HTTPException(502, "Hypothesis agent returned a non-object response")
    return result


def response_phase(result: dict[str, Any]) -> dict[str, Any]:
    phase = result.get("phase")
    return phase if isinstance(phase, dict) else {}


def is_phase_paused(result: dict[str, Any]) -> bool:
    phase = response_phase(result)
    stage = str(phase.get("stage", ""))
    return stage in PAUSED_STAGES or not result.get("run") and result.get("status") != "active"


def parse_sse_data(data: str) -> dict[str, Any] | None:
    try:
        event = json.loads(data)
    except (TypeError, ValueError):
        return None
    return event if isinstance(event, dict) else None


def agui_workflow_delta(envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "STATE_DELTA",
        "delta": [{"op": "replace", "path": "/workflow", "value": envelope}],
    }


def read_control(action: str, workflow_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "action": action,
        "workflow_id": workflow_id,
        "client_request_id": uuid.uuid4().hex,
        **fields,
    }


async def relay_phase_events(
    request: Request,
    control: dict[str, Any],
    initial: dict[str, Any],
    *,
    replay_paused: bool = False,
) -> AsyncIterator[str]:
    workflow_id = str(initial.get("workflow_id") or control.get("workflow_id") or "")
    phase = response_phase(initial)
    phase_id = str(phase.get("phase_id") or control.get("phase_id") or "")
    progress = initial.get("progress") if isinstance(initial.get("progress"), dict) else {}
    cursor = int(control.get("after_seq", 0) if replay_paused else progress.get("last_event_seq", 0) or 0)
    reconnects = 0

    while workflow_id and (replay_paused or not is_phase_paused(initial)):
        events_control = read_control(
            "events", workflow_id, phase_id=phase_id or None, after_seq=cursor, stream=True,
        )
        try:
            async with request.app.state.http.stream(
                "POST",
                upstream_url(),
                params={"api-version": "v1"},
                json=events_control,
                timeout=None,
            ) as response:
                if response.is_error:
                    body = await response.aread()
                    logger.warning("Event stream returned %s: %s", response.status_code, body[:500])
                    raise httpx.HTTPStatusError(
                        "event stream failed", request=response.request, response=response
                    )
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if await request.is_disconnected():
                        return
                    if line.startswith("id:"):
                        try:
                            cursor = max(cursor, int(line[3:].strip()))
                        except ValueError:
                            pass
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif not line and data_lines:
                        event = parse_sse_data("\n".join(data_lines))
                        data_lines.clear()
                        if event is None:
                            continue
                        cursor = max(cursor, int(event.get("seq") or 0))
                        yield event_stream({"type": "CUSTOM", "name": "workflow_event", "value": event})
                        if event.get("type") in {"phase.completed", "phase.failed", "phase.cancelled"}:
                            reconnects = 3
                            break
                        if event.get("type") == "stage.changed" and event.get("data", {}).get("to") in PAUSED_STAGES:
                            reconnects = 3
                            break
            if reconnects >= 3:
                break
            # A closed stream can mean a transient disconnect or a human pause.
            initial = await invoke_workflow(
                request,
                read_control("status", workflow_id, phase_id=phase_id or None),
            )
            if is_phase_paused(initial):
                break
            reconnects += 1
            await asyncio.sleep(min(0.25 * reconnects, 1.0))
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.info("Workflow event stream interrupted; reconnecting: %s", exc)
            reconnects += 1
            try:
                initial = await invoke_workflow(
                    request,
                    read_control("progress", workflow_id, phase_id=phase_id or None),
                )
            except HTTPException:
                if reconnects >= 4:
                    break
                await asyncio.sleep(0.5)
                continue
            progress = initial.get("progress")
            if isinstance(progress, dict):
                yield event_stream({"type": "CUSTOM", "name": "workflow_progress", "value": progress})
            if is_phase_paused(initial) or reconnects >= 4:
                break
            await asyncio.sleep(min(0.5 * reconnects, 2.0))

    if workflow_id:
        try:
            final = await invoke_workflow(
                request,
                read_control("status", workflow_id, phase_id=phase_id or None),
            )
            yield event_stream(agui_workflow_delta(final))
        except HTTPException as exc:
            logger.warning("Could not refresh phase after event stream: %s", exc.detail)


@app.post("/api/agent")
async def run_agent(request: Request) -> StreamingResponse:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(422, "AG-UI request must be an object")
    thread_id = str(payload.get("threadId") or "hypothesis-workflow")
    run_id = str(payload.get("runId") or "workflow-run")
    control = workflow_control(payload)
    streaming_events = control.get("action") == "events" and control.get("stream") is True
    if streaming_events:
        status_control = read_control("status", control.get("workflow_id"))
        if control.get("phase_id") is not None:
            status_control["phase_id"] = control["phase_id"]
        result = await invoke_workflow(request, status_control)
    else:
        result = await invoke_workflow(request, control)

    async def events() -> AsyncIterator[str]:
        yield event_stream({"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id})
        yield event_stream({"type": "STATE_SNAPSHOT", "snapshot": {"workflow": result}})
        if streaming_events:
            async for update in relay_phase_events(request, control, result, replay_paused=True):
                yield update
        elif control.get("action") in MUTATING_ACTIONS and not is_phase_paused(result):
            async for update in relay_phase_events(request, control, result):
                yield update
        yield event_stream({
            "type": "RUN_FINISHED",
            "threadId": thread_id,
            "runId": run_id,
            "result": result,
        })

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def status_response(request: Request, workflow_id: str, phase_id: str | None = None) -> dict[str, Any]:
    control = read_control("status", workflow_id)
    if phase_id is not None:
        control["phase_id"] = phase_id
    return await invoke_workflow(request, control)


@app.get("/api/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    return await status_response(request, workflow_id)


@app.get("/api/workflows/{workflow_id}/phases/{phase_id}")
async def get_workflow_phase(workflow_id: str, phase_id: str, request: Request) -> dict[str, Any]:
    return await status_response(request, workflow_id, phase_id)


@app.get("/api/workflows/{workflow_id}/progress")
async def get_workflow_progress(workflow_id: str, request: Request, phase_id: str | None = None) -> dict[str, Any]:
    control = read_control("progress", workflow_id)
    if phase_id is not None:
        control["phase_id"] = phase_id
    return await invoke_workflow(request, control)


@app.get("/api/workflows/{workflow_id}/events")
async def get_workflow_events(
    workflow_id: str,
    request: Request,
    phase_id: str | None = None,
    after_seq: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    control = read_control(
        "events", workflow_id,
        after_seq=max(0, after_seq), limit=max(1, min(limit, 200)), stream=False,
    )
    if phase_id is not None:
        control["phase_id"] = phase_id
    return await invoke_workflow(request, control)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        candidate = (DIST / path).resolve()
        if path and candidate.is_relative_to(DIST) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "5178")))
