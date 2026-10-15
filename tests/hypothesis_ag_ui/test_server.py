"""Focused contract tests for the phased AG-UI invocations adapter."""

from __future__ import annotations

import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.hosted_hypothesis_agent.models import WorkflowRequest
from src.hypothesis_ag_ui.server import BearerAuth, app


def response(body: dict, status_code: int = 200):
    return SimpleNamespace(
        is_error=status_code >= 400,
        status_code=status_code,
        json=lambda: body,
        text=json.dumps(body),
    )


class AdapterTests(unittest.TestCase):
    def tearDown(self) -> None:
        if hasattr(app.state, "http"):
            del app.state.http

    def test_streams_workflow_as_ag_ui_state(self) -> None:
        workflow = {
            "workflow_id": "workflow-1",
            "status": "idle",
            "phase": {"phase_id": "workflow-1-p001", "stage": "awaiting_approval"},
        }
        app.state.http = SimpleNamespace(post=AsyncMock(return_value=response(workflow)))
        with TestClient(app) as client:
            result = client.post(
                "/api/agent",
                json={
                    "threadId": "thread-1",
                    "runId": "run-1",
                    "messages": [],
                    "tools": [],
                    "context": [],
                    "state": {
                        "workflow": {
                            "action": "plan",
                            "scenario": "Test a claim",
                            "client_request_id": "request-1",
                        }
                    },
                },
            )

        self.assertEqual(result.status_code, 200)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in result.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            [event["type"] for event in events],
            ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"],
        )
        self.assertEqual(events[1]["snapshot"]["workflow"], workflow)
        request_body = app.state.http.post.await_args.kwargs["json"]
        self.assertEqual(request_body["scenario"], "Test a claim")

    def test_relays_workflow_events_for_active_phase(self) -> None:
        initial = {
            "workflow_id": "workflow-1",
            "status": "active",
            "tail_phase_id": "workflow-1-p001",
            "phase": {"phase_id": "workflow-1-p001", "stage": "pending"},
            "run": {"phase_id": "workflow-1-p001", "stage": "pending"},
        }
        final = {
            "workflow_id": "workflow-1",
            "status": "idle",
            "phase": {"phase_id": "workflow-1-p001", "stage": "completed"},
        }

        class StreamResponse:
            is_error = False
            request = None
            status_code = 200

            async def aiter_lines(self):
                yield "id: 3"
                yield 'data: {"seq":3,"phase_id":"workflow-1-p001","at":"2026-10-09T10:00:00Z","type":"phase.completed","data":{}}'
                yield ""

        class StreamClient:
            def __init__(self):
                self.post = AsyncMock(side_effect=[response(initial), response(final)])
                self.stream_json = None

            @asynccontextmanager
            async def stream(self, *_args, **kwargs):
                self.stream_json = kwargs["json"]
                WorkflowRequest.model_validate(self.stream_json)
                yield StreamResponse()

        app.state.http = StreamClient()
        with TestClient(app) as client:
            result = client.post(
                "/api/agent",
                json={"threadId": "t", "runId": "r", "state": {"workflow": {"action": "continue", "workflow_id": "workflow-1", "scenario": "More", "client_request_id": "continue-1"}}},
            )

        self.assertEqual(result.status_code, 200)
        events = [json.loads(line.removeprefix("data: ")) for line in result.text.splitlines() if line.startswith("data: ")]
        self.assertIn("CUSTOM", [event["type"] for event in events])
        self.assertEqual(events[-2]["delta"][0]["value"]["phase"]["stage"], "completed")
        controls = [call.kwargs["json"] for call in app.state.http.post.await_args_list]
        controls.insert(1, app.state.http.stream_json)
        self.assertEqual([control["action"] for control in controls], ["continue", "events", "status"])
        for control in controls:
            WorkflowRequest.model_validate(control)
        self.assertEqual(controls[0]["client_request_id"], "continue-1")
        self.assertEqual(len({control["client_request_id"] for control in controls}), 3)

    def test_events_stream_replays_after_requested_cursor(self) -> None:
        status = {"workflow_id": "workflow-1", "status": "idle", "phase": {"phase_id": "phase-1", "stage": "completed"}}

        class StreamResponse:
            is_error = False
            request = None
            status_code = 200

            async def aiter_lines(self):
                yield "id: 8"
                yield 'data: {"seq":8,"phase_id":"phase-1","at":"2026-10-09T10:00:00Z","type":"human.request","data":{"action":"revise"}}'
                yield ""

        class StreamClient:
            def __init__(self):
                self.post = AsyncMock(side_effect=[response(status), response(status), response(status)])
                self.stream_json = None

            @asynccontextmanager
            async def stream(self, _method, _url, **kwargs):
                self.stream_json = kwargs["json"]
                WorkflowRequest.model_validate(self.stream_json)
                yield StreamResponse()

        app.state.http = StreamClient()
        with TestClient(app) as client:
            result = client.post("/api/agent", json={"threadId": "t", "runId": "r", "state": {"workflow": {"action": "events", "workflow_id": "workflow-1", "phase_id": "phase-1", "stream": True, "after_seq": 7}}})
        events = [json.loads(line.removeprefix("data: ")) for line in result.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(result.status_code, 200)
        self.assertEqual(app.state.http.stream_json["after_seq"], 7)
        self.assertTrue(any(event.get("name") == "workflow_event" for event in events))
        controls = [call.kwargs["json"] for call in app.state.http.post.await_args_list]
        controls.insert(1, app.state.http.stream_json)
        self.assertEqual([control["action"] for control in controls], ["status", "events", "status", "status"])
        for control in controls:
            WorkflowRequest.model_validate(control)
        self.assertEqual(len({control["client_request_id"] for control in controls}), 4)

    def test_relay_recovery_reads_satisfy_the_hosted_request_contract(self) -> None:
        initial = {
            "workflow_id": "workflow-1", "status": "active",
            "phase": {"phase_id": "phase-1", "stage": "executing"},
            "run": {"phase_id": "phase-1"},
        }
        paused = {
            "workflow_id": "workflow-1", "status": "idle",
            "phase": {"phase_id": "phase-1", "stage": "completed"},
            "progress": {"state": "done"},
        }

        class StreamClient:
            def __init__(self):
                self.post = AsyncMock(side_effect=[response(initial), response(paused), response(paused)])
                self.stream_json = None

            @asynccontextmanager
            async def stream(self, _method, _url, **kwargs):
                self.stream_json = kwargs["json"]
                WorkflowRequest.model_validate(self.stream_json)
                raise httpx.ReadError("stream disconnected")
                yield

        app.state.http = StreamClient()
        with TestClient(app) as client:
            result = client.post("/api/agent", json={
                "action": "continue", "workflow_id": "workflow-1",
                "scenario": "More", "client_request_id": "continue-recovery",
            })
        self.assertEqual(result.status_code, 200)
        events = [json.loads(line.removeprefix("data: ")) for line in result.text.splitlines() if line.startswith("data: ")]
        self.assertTrue(any(event.get("name") == "workflow_progress" and event["value"] == paused["progress"] for event in events))
        self.assertEqual(events[-2]["delta"][0]["value"], paused)
        controls = [call.kwargs["json"] for call in app.state.http.post.await_args_list]
        controls.insert(1, app.state.http.stream_json)
        self.assertEqual([control["action"] for control in controls], ["continue", "events", "progress", "status"])
        for control in controls:
            WorkflowRequest.model_validate(control)
        self.assertEqual(controls[0]["client_request_id"], "continue-recovery")
        self.assertEqual(len({control["client_request_id"] for control in controls}), 4)

    def test_get_workflow_translates_to_status_action(self) -> None:
        workflow = {"workflow_id": "workflow-1", "phases": [], "phase": {"phase_id": "p001"}}
        app.state.http = SimpleNamespace(post=AsyncMock(return_value=response(workflow)))
        with TestClient(app) as client:
            result = client.get("/api/workflows/workflow-1")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), workflow)
        self.assertEqual(app.state.http.post.await_args.kwargs["json"], {"action": "status", "workflow_id": "workflow-1", "client_request_id": ANY})

    def test_get_progress_translates_to_progress_action(self) -> None:
        progress = {"workflow_id": "workflow-1", "progress": {"percent": 42}}
        app.state.http = SimpleNamespace(post=AsyncMock(return_value=response(progress)))
        with TestClient(app) as client:
            result = client.get("/api/workflows/workflow-1/progress?phase_id=phase-1")
        self.assertEqual(result.json(), progress)
        self.assertEqual(app.state.http.post.await_args.kwargs["json"], {"action": "progress", "workflow_id": "workflow-1", "phase_id": "phase-1", "client_request_id": ANY})

    def test_get_phase_and_events_preserve_query_contract(self) -> None:
        phase = {"workflow_id": "workflow-1", "phase": {"phase_id": "phase-1"}}
        events = {"events": [{"seq": 1, "at": "2026-10-09T10:00:00Z"}]}
        app.state.http = SimpleNamespace(post=AsyncMock(side_effect=[response(phase), response(events)]))
        with TestClient(app) as client:
            phase_result = client.get("/api/workflows/workflow-1/phases/phase-1")
            event_result = client.get("/api/workflows/workflow-1/events?phase_id=phase-1&after_seq=4&limit=500")
        self.assertEqual(phase_result.json(), phase)
        self.assertEqual(event_result.json(), events)
        self.assertEqual(app.state.http.post.await_args_list[0].kwargs["json"], {"action": "status", "workflow_id": "workflow-1", "phase_id": "phase-1", "client_request_id": ANY})
        self.assertEqual(app.state.http.post.await_args_list[1].kwargs["json"], {"action": "events", "workflow_id": "workflow-1", "phase_id": "phase-1", "after_seq": 4, "limit": 200, "stream": False, "client_request_id": ANY})

    def test_strict_conflict_status_and_code_are_preserved(self) -> None:
        app.state.http = SimpleNamespace(
            post=AsyncMock(return_value=response({"error": {"code": "phase_in_progress", "message": "Phase is running"}}, 409))
        )
        with TestClient(app) as client:
            result = client.post("/api/agent", json={"state": {"workflow": {"action": "continue", "workflow_id": "workflow-1"}}})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["detail"]["code"], "phase_in_progress")

    def test_upstream_approval_identity_and_expiry_errors_preserve_details(self) -> None:
        current = {"phase_id": "p001", "plan_revision": 2, "plan_digest": "sha256:" + "1" * 64}
        for status, code in [(401, "caller_unavailable"), (403, "not_owner"),
                             (409, "stale_plan"), (410, "workflow_expired")]:
            with self.subTest(status=status):
                error = {"code": code, "message": "Request denied", "retryable": False}
                if code == "stale_plan":
                    error["current"] = current
                app.state.http = SimpleNamespace(post=AsyncMock(return_value=response({"error": error}, status)))
                with TestClient(app) as client:
                    result = client.post("/api/agent", json={"action": "approve", "workflow_id": "wf"})
                self.assertEqual(result.status_code, status)
                self.assertEqual(result.json()["detail"], error)
                self.assertNotIn("RUN_STARTED", result.text)

    def test_successful_upstream_invalid_json_or_non_object_is_502(self) -> None:
        for value in ([], "not an object", None, ValueError("invalid JSON")):
            with self.subTest(value=value):
                upstream = response({})
                upstream.json = Mock(side_effect=value) if isinstance(value, Exception) else Mock(return_value=value)
                app.state.http = SimpleNamespace(post=AsyncMock(return_value=upstream))
                with TestClient(app) as client:
                    result = client.post("/api/agent", json={"action": "status", "workflow_id": "wf"})
                self.assertEqual(result.status_code, 502)
                self.assertIn("Hypothesis agent returned", result.json()["detail"])

    def test_read_helpers_are_accepted_by_the_hosted_request_contract(self) -> None:
        async def contract_checked_post(*_args, **kwargs):
            try:
                WorkflowRequest.model_validate(kwargs["json"])
            except ValidationError:
                return response({"error": {"code": "invalid_request", "message": "Invalid workflow request"}}, 422)
            return response({"workflow_id": "wf", "phase": {"phase_id": "wf-p001"}})

        app.state.http = SimpleNamespace(post=AsyncMock(side_effect=contract_checked_post))
        with TestClient(app) as client:
            for route in ("/api/workflows/wf", "/api/workflows/wf/phases/wf-p001",
                          "/api/workflows/wf/progress", "/api/workflows/wf/events"):
                with self.subTest(route=route):
                    result = client.get(route)
                    self.assertEqual(result.status_code, 200, result.text)
        request_ids = [call.kwargs["json"]["client_request_id"] for call in app.state.http.post.await_args_list]
        self.assertEqual(len(set(request_ids)), 4)

    def test_bearer_auth_uses_the_credential_token_provider(self) -> None:
        provider = Mock(return_value="qa-synthetic-token")
        with patch("src.hypothesis_ag_ui.server.DefaultAzureCredential", return_value=object()), \
             patch("src.hypothesis_ag_ui.server.get_bearer_token_provider", return_value=provider):
            auth = BearerAuth()
        request = next(auth.auth_flow(httpx.Request("POST", "https://example.invalid/invocations")))
        self.assertEqual(request.headers["Authorization"], "Bearer qa-synthetic-token")
        provider.assert_called_once_with()

    def test_accepts_direct_api_request_payload(self) -> None:
        control = {
            "action": "plan",
            "scenario": "Test a claim",
            "client_request_id": "request-direct",
        }
        app.state.http = SimpleNamespace(post=AsyncMock(return_value=response({"workflow_id": "workflow-1", "status": "idle"}, 200)))
        with TestClient(app) as client:
            result = client.post("/api/agent", json=control)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(app.state.http.post.await_args.kwargs["json"], control)

    def test_events_use_contract_default_limit(self) -> None:
        app.state.http = SimpleNamespace(post=AsyncMock(return_value=response({"workflow_id": "workflow-1", "events": [], "next_after_seq": 0})))
        with TestClient(app) as client:
            result = client.get("/api/workflows/workflow-1/events")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(app.state.http.post.await_args.kwargs["json"], {
            "action": "events", "workflow_id": "workflow-1", "after_seq": 0, "limit": 50, "stream": False, "client_request_id": ANY,
        })

    def test_rejects_missing_workflow_state(self) -> None:
        app.state.http = SimpleNamespace(post=AsyncMock())
        with TestClient(app) as client:
            result = client.post("/api/agent", json={"threadId": "thread-1", "runId": "run-1", "state": {}})
        self.assertEqual(result.status_code, 422)


if __name__ == "__main__":
    unittest.main()
