"""Benchmark deployed planning/execution model-mode combinations.

This harness creates fresh approved workflows and reports observed latency and
usage. It does not rank modes or claim that one mode is superior.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

from scripts._helpers import load_env
from scripts.invoke_hypothesis_agent import _invoke, wait_for_phase

MODES = (
    ("completion", "completion"),
    ("decision", "completion"),
    ("completion", "decision"),
    ("decision", "decision"),
)
DEFAULT_SCENARIO = (
    "Compare a two-night Berlin and Prague trip using weather, hotel, and "
    "transportation evidence. Produce a bounded plan and execute it after approval."
)


def validate_responses_transport(client_type: type | None = None) -> str:
    from agent_framework.openai import RawOpenAIChatClient, RawOpenAIChatCompletionClient
    from src.hosted_hypothesis_agent.runtime import FoundryChatClient

    selected = client_type or FoundryChatClient
    if not issubclass(selected, RawOpenAIChatClient) or issubclass(
        selected, RawOpenAIChatCompletionClient
    ):
        raise RuntimeError(
            "decision benchmark requires the Responses API transport; "
            "chat completions are not supported by Decision-1"
        )
    return "responses"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="Fresh workflows per mode pair")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--confirm-deployed",
        action="store_true",
        help="Required acknowledgement that the configured hosted agent may incur cost",
    )
    return parser


def validate_deployed_configuration(args: argparse.Namespace) -> None:
    if not args.confirm_deployed:
        raise RuntimeError("--confirm-deployed is required; this benchmark creates and executes deployed workflows")
    if args.runs < 1:
        raise ValueError("--runs must be at least 1")
    missing = [
        name
        for name in (
            "AZURE_AI_PROJECT_ENDPOINT",
            "HYPOTHESIS_HOSTED_AGENT_NAME",
            "AZURE_AI_MODEL_DEPLOYMENT_NAME",
            "DECISION_MODEL_PROJECT_ENDPOINT",
            "DECISION_MODEL_DEPLOYMENT_NAME",
        )
        if not os.getenv(name, "").strip()
    ]
    if missing:
        raise RuntimeError(f"deployed benchmark configuration is incomplete: {', '.join(missing)}")


def _usage(stats: dict[str, Any] | None) -> dict[str, Any]:
    stats = stats or {}
    values = {
        "input_tokens": stats.get("input_tokens"),
        "output_tokens": stats.get("output_tokens"),
        "total_tokens": stats.get("total_tokens"),
        "usage_complete": stats.get("usage_complete") is True,
    }
    if any(values[name] is None for name in ("input_tokens", "output_tokens", "total_tokens")):
        values["usage_complete"] = False
    return values


def _event_usage(events: list[dict[str, Any]], phase_complete: bool) -> dict[str, Any]:
    usage_events = [event for event in events if event.get("type") == "model.usage"]
    result: dict[str, Any] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        values = [
            event.get("data", {}).get("usage_details", {}).get(name)
            for event in usage_events
        ]
        result[name] = sum(values) if values and all(isinstance(value, int) for value in values) else None
    result["usage_complete"] = (
        phase_complete
        and bool(usage_events)
        and all(event.get("data", {}).get("usage_complete") is True for event in usage_events)
        and all(result[name] is not None for name in ("input_tokens", "output_tokens", "total_tokens"))
    )
    return result


async def _usage_events(workflow_id: str, phase_id: str, after_seq: int = 0) -> tuple[list[dict[str, Any]], int]:
    page = await _invoke(
        {
            "action": "events",
            "workflow_id": workflow_id,
            "phase_id": phase_id,
            "after_seq": after_seq,
            "limit": 200,
            "client_request_id": f"benchmark-events-{uuid.uuid4().hex}",
        }
    )
    return page.get("events", []), int(page.get("next_after_seq", after_seq))


def _percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"sample_count": len(samples)}
    for phase in ("planning", "execution"):
        elapsed = [float(sample[f"{phase}_elapsed_ms"]) for sample in samples]
        usage = [sample[f"{phase}_usage"] for sample in samples]
        summary[phase] = {
            "elapsed_ms": {
                "median": statistics.median(elapsed),
                "p95": _percentile95(elapsed),
            },
            "tokens": {
                name: {
                    "median": statistics.median(
                        [item[name] for item in usage if isinstance(item.get(name), int)]
                    )
                    if any(isinstance(item.get(name), int) for item in usage)
                    else None,
                    "p95": _percentile95(
                        [float(item[name]) for item in usage if isinstance(item.get(name), int)]
                    )
                    if any(isinstance(item.get(name), int) for item in usage)
                    else None,
                }
                for name in ("input_tokens", "output_tokens", "total_tokens")
            },
            "usage_complete": all(item.get("usage_complete") is True for item in usage),
        }
    return summary


async def run_sample(
    planning_mode: str,
    execution_mode: str,
    scenario: str,
    timeout: float,
) -> dict[str, Any]:
    model_modes = {"planning": planning_mode, "execution": execution_mode}
    started = time.perf_counter()
    created = await _invoke(
        {
            "action": "plan",
            "scenario": scenario,
            "model_modes": model_modes,
            "client_request_id": f"benchmark-plan-{uuid.uuid4().hex}",
        }
    )
    workflow_id = created["workflow_id"]
    phase_id = created["phase"]["phase_id"]
    planned = await wait_for_phase(
        workflow_id, phase_id, {"awaiting_approval"}, timeout=timeout
    )
    planning_elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    phase = planned["phase"]
    if phase.get("model_modes") != model_modes:
        raise RuntimeError(f"deployed phase did not preserve requested model modes: {phase.get('model_modes')}")
    planning_events, planning_cursor = await _usage_events(workflow_id, phase_id)
    planning_usage = _event_usage(
        planning_events, phase.get("stats", {}).get("usage_complete") is True
    )
    approval_started = time.perf_counter()
    await _invoke(
        {
            "action": "approve",
            "workflow_id": workflow_id,
            "phase_id": phase_id,
            "plan_revision": phase["plan_revision"],
            "plan_digest": phase["plan_digest"],
            "decision": "approved",
            "client_request_id": f"benchmark-approve-{uuid.uuid4().hex}",
        }
    )
    completed = await wait_for_phase(workflow_id, phase_id, {"completed"}, timeout=timeout)
    execution_elapsed_ms = round((time.perf_counter() - approval_started) * 1000, 3)
    execution_events, _ = await _usage_events(workflow_id, phase_id, planning_cursor)
    total_usage = _usage(completed["phase"].get("stats"))
    return {
        "workflow_id": workflow_id,
        "phase_id": phase_id,
        "model_modes": model_modes,
        "planning_elapsed_ms": planning_elapsed_ms,
        "execution_elapsed_ms": execution_elapsed_ms,
        "planning_usage": planning_usage,
        "execution_usage": _event_usage(
            execution_events,
            completed["phase"].get("stats", {}).get("usage_complete") is True,
        ),
        "reported_phase_usage": total_usage,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    load_env()
    validate_deployed_configuration(args)
    transport = validate_responses_transport()
    report: dict[str, Any] = {
        "statement": "Observed deployed measurements only; no mode is ranked or claimed superior.",
        "model_api": transport,
        "runs_per_mode": args.runs,
        "scenario": args.scenario,
        "modes": {},
    }
    for planning_mode, execution_mode in MODES:
        key = f"{planning_mode[0].upper()}/{execution_mode[0].upper()}"
        samples = [
            await run_sample(planning_mode, execution_mode, args.scenario, args.timeout)
            for _ in range(args.runs)
        ]
        report["modes"][key] = {
            "model_modes": {"planning": planning_mode, "execution": execution_mode},
            "samples": samples,
            "summary": summarize(samples),
        }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    args = _parser().parse_args()
    report = asyncio.run(_run(args))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
