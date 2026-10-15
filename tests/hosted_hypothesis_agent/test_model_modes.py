from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError
from agent_framework.openai import RawOpenAIChatClient, RawOpenAIChatCompletionClient

from src.hosted_hypothesis_agent.agent import WorkflowOrchestrator
from src.hosted_hypothesis_agent.models import (
    ExecutionPlan,
    ExecutionResult,
    Hypothesis,
    ModelMode,
    ModelModes,
    PhaseRecord,
    PlanStep,
    WorkflowRequest,
)
from src.hosted_hypothesis_agent.runtime import (
    AgentRuntime,
    FoundryChatClient,
    ModelCallTelemetryMiddleware,
    validate_model_modes,
)
from src.hosted_hypothesis_agent.state_store import InMemoryWorkflowStore, WorkflowConflictError


class ModeRecordingRuntime:
    instances: list["ModeRecordingRuntime"] = []

    def __init__(self, foundry_call_id: str | None = None, model_modes=None) -> None:
        self.foundry_call_id = foundry_call_id
        self.model_modes = model_modes
        self.metrics = {}
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def configure_tool_ledger(self, ledger_get, ledger_save, event_callback=None) -> None:
        return None

    def create_session(self, session_id: str | None = None):
        from agent_framework import AgentSession

        return AgentSession(session_id=session_id)

    async def formulate_hypothesis(self, scenario: str) -> Hypothesis:
        return Hypothesis(statement=f"Test {scenario}", scenario_summary=scenario)

    async def plan(self, scenario, hypothesis, session, revision_comment=None):
        return ExecutionPlan(
            objective="Test modes",
            steps=[PlanStep(id=1, title="Research", description="Collect evidence", tool_category="weather")],
        ), []

    async def execute(self, plan, session):
        return ExecutionResult(completed_steps=[1], final_answer="Done"), []


class ModelModeModelTests(unittest.TestCase):
    def test_plan_continue_and_revise_accept_modes_with_completion_defaults(self) -> None:
        defaulted = WorkflowRequest(
            action="plan", scenario="Default models", client_request_id="default-models"
        )
        self.assertEqual(
            defaulted.model_modes.model_dump(mode="json"),
            {"planning": "completion", "execution": "completion"},
        )

        selected = {"planning": "decision", "execution": "completion"}
        plan = WorkflowRequest(
            action="plan",
            scenario="Selected models",
            client_request_id="selected-models",
            model_modes=selected,
        )
        continued = WorkflowRequest(
            action="continue",
            workflow_id="wf",
            scenario="Continue",
            client_request_id="continue-models",
            model_modes={"planning": "completion", "execution": "decision"},
        )
        revised = WorkflowRequest(
            action="revise",
            workflow_id="wf",
            phase_id="wf-p001",
            scenario="Revise",
            client_request_id="revise-models",
            model_modes={"planning": "decision", "execution": "decision"},
        )
        self.assertEqual(plan.model_modes.model_dump(mode="json"), selected)
        self.assertEqual(continued.model_modes.execution, "decision")
        self.assertEqual(revised.model_modes.planning, "decision")

    def test_modes_are_rejected_for_approval_and_invalid_values_fail_validation(self) -> None:
        approval = {
            "action": "approve",
            "workflow_id": "wf",
            "phase_id": "wf-p001",
            "client_request_id": "approve-models",
            "plan_revision": 1,
            "plan_digest": "sha256:" + "a" * 64,
            "decision": "approved",
            "model_modes": {"planning": "decision", "execution": "decision"},
        }
        with self.assertRaises(ValidationError):
            WorkflowRequest.model_validate(approval)
        with self.assertRaises(ValidationError):
            WorkflowRequest(
                action="plan",
                scenario="Bad mode",
                client_request_id="bad-mode",
                model_modes={"planning": "chat", "execution": "completion"},
            )

    def test_phase_modes_are_immutable_and_legacy_state_recovers_defaults(self) -> None:
        legacy = PhaseRecord.model_validate(
            {
                "phase_id": "wf-p001",
                "workflow_id": "wf",
                "ordinal": 1,
                "origin": "initial",
                "user_input": "legacy",
            }
        )
        self.assertEqual(
            legacy.model_modes.model_dump(mode="json"),
            {"planning": "completion", "execution": "completion"},
        )


class ModelModeOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ModeRecordingRuntime.instances.clear()
        self.config = patch.dict(
            "os.environ",
            {
                "DECISION_MODEL_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/decision",
                "DECISION_MODEL_DEPLOYMENT_NAME": "decision-model",
            },
        )
        self.config.start()
        self.addCleanup(self.config.stop)
        self.store = InMemoryWorkflowStore()
        self.orchestrator = WorkflowOrchestrator(self.store, runtime_factory=ModeRecordingRuntime)

    async def test_modes_are_persisted_and_passed_to_each_phase_runtime(self) -> None:
        accepted = await self.orchestrator.dispatch(
            WorkflowRequest(
                action="plan",
                scenario="Mode routing",
                client_request_id="plan-routing",
                model_modes={"planning": "decision", "execution": "completion"},
            ),
            "owner",
        )
        await self.orchestrator.wait(accepted["workflow_id"])
        first = await self.store.get_phase(accepted["workflow_id"], accepted["phase"]["phase_id"])
        self.assertEqual(first.model_modes.planning, "decision")
        self.assertEqual(ModeRecordingRuntime.instances[0].model_modes, first.model_modes)

        await self.orchestrator.dispatch(
            WorkflowRequest(
                action="approve",
                workflow_id=accepted["workflow_id"],
                phase_id=first.phase_id,
                client_request_id="approve-routing",
                plan_revision=first.plan_revision,
                plan_digest=first.plan_digest,
                decision="approved",
            ),
            "owner",
        )
        await self.orchestrator.wait(accepted["workflow_id"])
        continued = await self.orchestrator.dispatch(
            WorkflowRequest(
                action="continue",
                workflow_id=accepted["workflow_id"],
                scenario="Use decision execution",
                client_request_id="continue-routing",
                model_modes={"planning": "completion", "execution": "decision"},
            ),
            "owner",
        )
        await self.orchestrator.wait(accepted["workflow_id"])
        second = await self.store.get_phase(accepted["workflow_id"], continued["phase"]["phase_id"])
        self.assertEqual(second.model_modes.execution, "decision")
        self.assertEqual(first.model_modes.planning, "decision")
        self.assertEqual(ModeRecordingRuntime.instances[-1].model_modes, second.model_modes)

    async def test_modes_participate_in_idempotency(self) -> None:
        request = {
            "action": "plan",
            "scenario": "Idempotent modes",
            "client_request_id": "same-id",
            "model_modes": {"planning": "completion", "execution": "completion"},
        }
        accepted = await self.orchestrator.dispatch(WorkflowRequest.model_validate(request), "owner")
        replayed = await self.orchestrator.dispatch(WorkflowRequest.model_validate(request), "owner")
        self.assertEqual(replayed["workflow_id"], accepted["workflow_id"])
        self.assertTrue(replayed["request"]["replayed"])
        changed = {
            **request,
            "model_modes": {"planning": "decision", "execution": "completion"},
        }
        with self.assertRaisesRegex(WorkflowConflictError, "request_conflict"):
            await self.orchestrator.dispatch(WorkflowRequest.model_validate(changed), "owner")

    async def test_decision_without_configuration_fails_before_phase_creation_or_claim(self) -> None:
        with patch.dict(
            "os.environ",
            {"DECISION_MODEL_PROJECT_ENDPOINT": "", "DECISION_MODEL_DEPLOYMENT_NAME": ""},
        ):
            with self.assertRaisesRegex((RuntimeError, ValueError), "decision"):
                await self.orchestrator.dispatch(
                    WorkflowRequest(
                        action="plan",
                        scenario="Missing decision configuration",
                        client_request_id="missing-decision",
                        model_modes={"planning": "decision", "execution": "completion"},
                    ),
                    "owner",
                )
        workflow_id = __import__("uuid").uuid5(
            __import__("uuid").NAMESPACE_URL, "owner:missing-decision"
        ).hex
        with self.assertRaises(KeyError):
            await self.store.get_meta(workflow_id)
        self.assertEqual(self.orchestrator._tasks, {})


class ModelModeRuntimeTests(unittest.TestCase):
    def test_integrated_foundry_client_uses_responses_not_chat_completions(self) -> None:
        self.assertTrue(issubclass(FoundryChatClient, RawOpenAIChatClient))
        self.assertFalse(issubclass(FoundryChatClient, RawOpenAIChatCompletionClient))

    def test_decision_validation_requires_both_endpoint_and_deployment(self) -> None:
        modes = ModelModes(planning="decision", execution="completion")
        for values in (
            {},
            {"DECISION_MODEL_PROJECT_ENDPOINT": "https://example.test/projects/decision"},
            {"DECISION_MODEL_DEPLOYMENT_NAME": "decision-model"},
        ):
            with self.subTest(values=values), patch.dict("os.environ", values, clear=True):
                with self.assertRaisesRegex(ValueError, "model_mode_unavailable"):
                    validate_model_modes(modes)

    def test_hypothesis_plan_and_execution_construct_from_selected_clients(self) -> None:
        clients = []

        class FakeClient:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                self.agents = []
                clients.append(self)

            def as_agent(self, **kwargs):
                agent = SimpleNamespace(name=kwargs["name"], as_tool=lambda **tool_kwargs: tool_kwargs)
                self.agents.append((kwargs, agent))
                return agent

        harnesses = []

        def fake_harness(**kwargs):
            harness = SimpleNamespace(
                client=kwargs["client"],
                create_session=lambda session_id=None: SimpleNamespace(session_id=session_id),
            )
            harnesses.append(harness)
            return harness

        config = {
            "AZURE_AI_PROJECT_ENDPOINT": "https://completion.test/projects/main",
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "completion-model",
            "DECISION_MODEL_PROJECT_ENDPOINT": (
                "https://dzwestus3-resource.services.ai.azure.com/api/projects/dzwestus3"
            ),
            "DECISION_MODEL_DEPLOYMENT_NAME": "Decision-1",
        }
        with (
            patch.dict("os.environ", config, clear=True),
            patch("src.hosted_hypothesis_agent.runtime.DefaultAzureCredential", return_value=object()),
            patch("src.hosted_hypothesis_agent.runtime.FoundryChatClient", FakeClient),
            patch(
                "src.hosted_hypothesis_agent.runtime.build_toolboxes",
                return_value=([object()] * 3, []),
            ) as toolboxes,
            patch(
                "src.hosted_hypothesis_agent.runtime.build_specialists",
                side_effect=lambda client, _tools, **_kwargs: ([], [{"client": client}] * 3),
            ) as specialists,
            patch("src.hosted_hypothesis_agent.runtime.create_harness_agent", side_effect=fake_harness),
        ):
            runtime = AgentRuntime(
                foundry_call_id="call-live",
                model_modes=ModelModes(planning="decision", execution="completion"),
            )

        completion = next(client for client in clients if client.kwargs["model"] == "completion-model")
        decision = next(client for client in clients if client.kwargs["model"] == "Decision-1")
        self.assertEqual(
            decision.kwargs["project_endpoint"],
            "https://dzwestus3-resource.services.ai.azure.com/api/projects/dzwestus3",
        )
        self.assertIs(runtime.hypothesis_agent, decision.agents[0][1])
        self.assertNotIn("default_headers", completion.kwargs)
        self.assertNotIn("default_headers", decision.kwargs)
        self.assertIs(runtime.harnesses[ModelMode.DECISION].client, decision)
        self.assertIs(runtime.harnesses[ModelMode.COMPLETION].client, completion)
        self.assertEqual(
            [call.args[0] for call in specialists.call_args_list],
            [decision, completion],
        )
        self.assertTrue(all(call.args[1] == "call-live" for call in toolboxes.call_args_list))

    def test_usage_uses_sdk_details_and_missing_values_remain_incomplete(self) -> None:
        captured = []
        middleware = ModelCallTelemetryMiddleware(
            "HypothesisAgent",
            ModelMode.DECISION,
            "decision",
            lambda *args: captured.append(args),
        )
        context = SimpleNamespace(result=None)

        async def complete_call() -> None:
            context.result = SimpleNamespace(
                usage_details={
                    "input_token_count": 11,
                    "output_token_count": 7,
                    "total_token_count": 18,
                }
            )

        asyncio.run(middleware.process(context, complete_call))
        missing = ModelCallTelemetryMiddleware(
            "ScenarioExecutorHarness",
            ModelMode.COMPLETION,
            "completion",
            lambda *args: captured.append(args),
        )
        context.result = None

        async def missing_call() -> None:
            context.result = SimpleNamespace(usage_details=None)

        asyncio.run(missing.process(context, missing_call))
        complete_tokens = captured[0][-1]
        missing_tokens = captured[1][-1]
        self.assertEqual(
            complete_tokens,
            {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        )
        self.assertTrue(all(value is not None for value in complete_tokens.values()))
        self.assertEqual(
            missing_tokens,
            {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        )
        self.assertFalse(all(value is not None for value in missing_tokens.values()))

    def test_phase_usage_is_incomplete_when_specialist_sdk_usage_is_unavailable(self) -> None:
        phase = PhaseRecord(
            phase_id="wf-p001",
            workflow_id="wf",
            ordinal=1,
            origin="initial",
            user_input="scenario",
        )
        runtime = SimpleNamespace(
            metrics={
                "ScenarioExecutorHarness": {
                    "runs": 1,
                    "model_calls": 1,
                    "input_tokens": 9,
                    "output_tokens": 3,
                    "total_tokens": 12,
                    "duration_ms": 1,
                }
            }
        )
        WorkflowOrchestrator._merge_runtime_stats(phase, runtime)
        self.assertEqual(phase.stats.total_tokens, 12)
        self.assertFalse(phase.stats.usage_complete)


if __name__ == "__main__":
    unittest.main()
