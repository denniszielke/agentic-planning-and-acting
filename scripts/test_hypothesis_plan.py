"""Submit a planning request to the deployed hypothesis agent and save its response."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from scripts._helpers import ROOT, load_env
from scripts.invoke_hypothesis_agent import _invoke, wait_for_phase

DEFAULT_SCENARIO = (
    "Test the hypothesis that Seattle is currently warmer than Sydney. "
    "Create a verification plan using the available tools, but do not execute it until approved."
)
DEFAULT_OUTPUT = ROOT / "results" / "hypothesis-plan-test.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--comment", default="Planning test against the deployed agent.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


async def _run(args: argparse.Namespace) -> None:
    load_env()
    request = {
        "action": "plan", "scenario": args.scenario, "comment": args.comment,
        "client_request_id": f"plan-test-{uuid.uuid4().hex}",
    }
    payload = await _invoke(request)
    replay = await _invoke(request)
    if not replay.get("request", {}).get("replayed") or replay.get("workflow_id") != payload.get("workflow_id"):
        raise RuntimeError("identical planning retry did not replay the same workflow")
    phase_id = payload.get("phase", {}).get("phase_id")
    if not payload.get("workflow_id") or not phase_id:
        raise RuntimeError("planning acceptance must contain workflow_id and phase.phase_id")
    payload = await wait_for_phase(payload["workflow_id"], phase_id, {"awaiting_approval"}, timeout=args.timeout)
    phase = payload["phase"]
    required = ("phase_id", "plan_revision", "plan_digest")
    missing = [key for key in required if not phase.get(key)]
    if missing:
        raise RuntimeError(f"planning response is missing: {', '.join(missing)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"\nSaved approval input to {args.output}")


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
