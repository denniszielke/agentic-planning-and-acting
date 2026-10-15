"""Approve and execute the exact plan saved by test_hypothesis_plan."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from scripts._helpers import ROOT, load_env
from scripts.invoke_hypothesis_agent import _invoke, wait_for_phase

DEFAULT_INPUT = ROOT / "results" / "hypothesis-plan-test.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--follow-up", help="Optional continue-phase planning/lineage smoke test (then reject)")
    parser.add_argument("--revise", help="Optional revise-from-original planning smoke test (then reject)")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--comment", default="Approved by the deployed-agent test script.")
    return parser


def _load_plan(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"plan file does not exist: {path}; run test_hypothesis_plan first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("plan file must contain a JSON object")
    phase = payload.get("phase", {})
    required = ("phase_id", "plan_revision", "plan_digest")
    missing = [key for key in required if not phase.get(key)]
    if missing:
        raise RuntimeError(f"plan file is missing: {', '.join(missing)}")
    if not payload.get("workflow_id"):
        raise RuntimeError("plan file is missing workflow_id")
    return payload


async def _run(args: argparse.Namespace) -> None:
    load_env()
    plan = _load_plan(args.input)
    body = {
        "action": "approve",
        "workflow_id": plan["workflow_id"],
        "phase_id": plan["phase"]["phase_id"],
        "plan_revision": plan["phase"]["plan_revision"],
        "plan_digest": plan["phase"]["plan_digest"],
        "decision": "approved",
        "comment": args.comment,
        "client_request_id": f"approve-test-{uuid.uuid4().hex}",
    }
    await _invoke(body)
    replay = await _invoke(body)
    if not replay.get("request", {}).get("replayed"):
        raise RuntimeError("identical approval retry was not reported as replayed")
    payload = await wait_for_phase(plan["workflow_id"], body["phase_id"], {"completed"}, timeout=args.timeout)
    print(json.dumps(payload, indent=2))
    for action, scenario in (("continue", args.follow_up), ("revise", args.revise)):
        if not scenario:
            continue
        request = {"action": action, "workflow_id": plan["workflow_id"], "scenario": scenario,
                   "client_request_id": f"{action}-test-{uuid.uuid4().hex}"}
        if action == "revise":
            request["phase_id"] = body["phase_id"]
        created = await _invoke(request)
        phase_id = created["phase"]["phase_id"]
        planned = await wait_for_phase(plan["workflow_id"], phase_id, {"awaiting_approval"}, timeout=args.timeout)
        phase = planned["phase"]
        if phase["origin"] != action or phase["based_on_phase_id"] != body["phase_id"]:
            raise RuntimeError(f"incorrect phase lineage: {phase}")
        # Freeze this smoke-test phase, allowing the next revise to append safely.
        await _invoke({"action": "approve", "workflow_id": plan["workflow_id"], "phase_id": phase_id,
                       "plan_revision": phase["plan_revision"], "plan_digest": phase["plan_digest"],
                       "decision": "rejected", "client_request_id": uuid.uuid4().hex})
        print(json.dumps(planned, indent=2))


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
