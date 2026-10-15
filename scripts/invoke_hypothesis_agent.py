"""Call the phased hosted agent (POST invocations); optionally follow durable events."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from typing import Any

import httpx

from scripts._auth import EntraTokenAuth
from scripts._helpers import env, load_env


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("scenario")
    follow_up = subparsers.add_parser("continue")
    follow_up.add_argument("workflow_id")
    follow_up.add_argument("scenario")
    revise = subparsers.add_parser("revise")
    revise.add_argument("workflow_id")
    revise.add_argument("phase_id")
    revise.add_argument("scenario", help="Feedback on a terminal phase; appends a new tail")
    for action in (plan, follow_up, revise):
        action.add_argument("--planning-model-mode", choices=("completion", "decision"), default="completion")
        action.add_argument("--execution-model-mode", choices=("completion", "decision"), default="completion")
    for name in ("approve", "decide"):
        approve = subparsers.add_parser(name, help="Approve/reject/replan the exact phase revision")
        approve.add_argument("workflow_id")
        approve.add_argument("phase_id")
        approve.add_argument("plan_revision", type=int)
        approve.add_argument("plan_digest")
        approve.add_argument("decision", choices=("approved", "rejected", "revise"))
        approve.add_argument("--comment")
    for name in ("status", "progress", "events", "cancel"):
        action = subparsers.add_parser(name)
        action.add_argument("workflow_id")
        action.add_argument("--phase-id")
        if name == "events":
            action.add_argument("--after-seq", type=int, default=0)
            action.add_argument("--limit", type=int, default=50)
            action.add_argument("--follow", action="store_true", help="Stream replay and live SSE frames")
        elif name == "cancel":
            action.add_argument("--comment")
    for action in subparsers.choices.values():
        action.add_argument("--request-id", help="Reuse only for an identical idempotent mutation retry")
    return parser


def _endpoint() -> str:
    if override := env("HYPOTHESIS_AGENT_INVOCATIONS_URL"):
        return override
    project = env("AZURE_AI_PROJECT_ENDPOINT", required=True).rstrip("/")
    agent_name = env("HYPOTHESIS_HOSTED_AGENT_NAME", "scenario-hosted-hypothesis-agent")
    return f"{project}/agents/{agent_name}/endpoint/protocols/invocations"


def request_body(args: argparse.Namespace) -> dict[str, Any]:
    body = {"action": "approve" if args.action == "decide" else args.action,
            "client_request_id": args.request_id or uuid.uuid4().hex}
    for field in ("workflow_id", "phase_id", "scenario", "plan_revision", "plan_digest", "decision", "comment", "after_seq", "limit"):
        if getattr(args, field, None) is not None:
            body[field] = getattr(args, field)
    if args.action == "events":
        body["stream"] = args.follow
    if args.action in {"plan", "continue", "revise"}:
        body["model_modes"] = {
            "planning": args.planning_model_mode,
            "execution": args.execution_model_mode,
        }
    return body


async def _invoke(body: dict[str, Any]) -> dict[str, Any]:
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(auth=EntraTokenAuth(), timeout=60.0) as client:
                response = await client.post(_endpoint(), params={"api-version": "v1"}, json=body)
                response.raise_for_status()
                payload = response.json()
            break
        except httpx.TransportError:
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)
    if not isinstance(payload, dict):
        raise RuntimeError("invocation response is not a JSON object")
    return payload


async def wait_for_phase(workflow_id: str, phase_id: str, stages: set[str], *, timeout: float = 600, interval: float = 2) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = await _invoke({
            "action": "status",
            "workflow_id": workflow_id,
            "phase_id": phase_id,
            "client_request_id": uuid.uuid4().hex,
        })
        stage = payload.get("phase", {}).get("stage")
        if stage in stages:
            return payload
        if stage in {"failed", "cancelled", "rejected"}:
            raise RuntimeError(f"phase terminated before requested stage: {payload}")
        await asyncio.sleep(interval)
    raise TimeoutError(f"phase {phase_id} did not reach {sorted(stages)} within {timeout}s")


async def _run(args: argparse.Namespace) -> None:
    load_env()
    body = request_body(args)
    if body.get("stream"):
        async with httpx.AsyncClient(auth=EntraTokenAuth(), timeout=httpx.Timeout(60, read=None)) as client:
            async with client.stream("POST", _endpoint(), params={"api-version": "v1"}, json=body) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    print(line, flush=True)
    else:
        print(json.dumps(await _invoke(body), indent=2))


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
