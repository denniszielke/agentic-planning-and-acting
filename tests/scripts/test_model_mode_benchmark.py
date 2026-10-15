from __future__ import annotations

import argparse
import os
import unittest
from unittest.mock import patch

from agent_framework.openai import OpenAIChatCompletionClient

from scripts.benchmark_model_modes import (
    _event_usage,
    _percentile95,
    summarize,
    validate_deployed_configuration,
    validate_responses_transport,
)


class BenchmarkGuardTests(unittest.TestCase):
    def test_explicit_confirmation_and_complete_deployed_configuration_are_required(self) -> None:
        args = argparse.Namespace(confirm_deployed=False, runs=3)
        with self.assertRaisesRegex(RuntimeError, "--confirm-deployed"):
            validate_deployed_configuration(args)
        args.confirm_deployed = True
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DECISION_MODEL_DEPLOYMENT_NAME"):
                validate_deployed_configuration(args)

    def test_valid_configuration_does_not_contact_deployment(self) -> None:
        args = argparse.Namespace(confirm_deployed=True, runs=2)
        values = {
            "AZURE_AI_PROJECT_ENDPOINT": "https://completion.invalid/projects/test",
            "HYPOTHESIS_HOSTED_AGENT_NAME": "agent",
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "completion",
            "DECISION_MODEL_PROJECT_ENDPOINT": (
                "https://dzwestus3-resource.services.ai.azure.com/api/projects/dzwestus3"
            ),
            "DECISION_MODEL_DEPLOYMENT_NAME": "Decision-1",
        }
        with patch.dict(os.environ, values, clear=True):
            validate_deployed_configuration(args)

    def test_integrated_benchmark_transport_is_responses_compatible(self) -> None:
        self.assertEqual(validate_responses_transport(), "responses")
        with self.assertRaisesRegex(RuntimeError, "Responses API.*chat completions"):
            validate_responses_transport(OpenAIChatCompletionClient)

    def test_every_partial_decision_configuration_fails_before_benchmark(self) -> None:
        args = argparse.Namespace(confirm_deployed=True, runs=1)
        base = {
            "AZURE_AI_PROJECT_ENDPOINT": "https://completion.invalid/projects/test",
            "HYPOTHESIS_HOSTED_AGENT_NAME": "agent",
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "completion",
        }
        decision = {
            "DECISION_MODEL_PROJECT_ENDPOINT": (
                "https://dzwestus3-resource.services.ai.azure.com/api/projects/dzwestus3"
            ),
            "DECISION_MODEL_DEPLOYMENT_NAME": "Decision-1",
        }
        for present, missing in (
            ("DECISION_MODEL_PROJECT_ENDPOINT", "DECISION_MODEL_DEPLOYMENT_NAME"),
            ("DECISION_MODEL_DEPLOYMENT_NAME", "DECISION_MODEL_PROJECT_ENDPOINT"),
        ):
            with self.subTest(missing=missing), patch.dict(
                os.environ, {**base, present: decision[present]}, clear=True
            ):
                with self.assertRaisesRegex(RuntimeError, missing):
                    validate_deployed_configuration(args)


class BenchmarkSummaryTests(unittest.TestCase):
    def test_p95_uses_nearest_rank(self) -> None:
        self.assertEqual(_percentile95([1, 2, 3, 4, 100]), 100)

    def test_summary_reports_latency_tokens_and_incomplete_usage_without_ranking(self) -> None:
        samples = [
            {
                "planning_elapsed_ms": 100,
                "execution_elapsed_ms": 300,
                "planning_usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "total_tokens": 14,
                    "usage_complete": True,
                },
                "execution_usage": {
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                    "usage_complete": False,
                },
            },
            {
                "planning_elapsed_ms": 200,
                "execution_elapsed_ms": 500,
                "planning_usage": {
                    "input_tokens": 20,
                    "output_tokens": 6,
                    "total_tokens": 26,
                    "usage_complete": True,
                },
                "execution_usage": {
                    "input_tokens": 30,
                    "output_tokens": 8,
                    "total_tokens": 38,
                    "usage_complete": True,
                },
            },
        ]
        result = summarize(samples)
        self.assertEqual(result["planning"]["elapsed_ms"], {"median": 150.0, "p95": 200.0})
        self.assertEqual(result["planning"]["tokens"]["total_tokens"], {"median": 20.0, "p95": 26.0})
        self.assertFalse(result["execution"]["usage_complete"])
        self.assertNotIn("winner", result)
        self.assertNotIn("superior", result)

    def test_event_usage_never_turns_missing_values_into_zero(self) -> None:
        result = _event_usage(
            [
                {
                    "type": "model.usage",
                    "data": {
                        "usage_details": {
                            "input_tokens": 20,
                            "output_tokens": None,
                            "total_tokens": None,
                        },
                        "usage_complete": False,
                    },
                }
            ],
            phase_complete=False,
        )
        self.assertEqual(result["input_tokens"], 20)
        self.assertIsNone(result["output_tokens"])
        self.assertIsNone(result["total_tokens"])
        self.assertFalse(result["usage_complete"])


if __name__ == "__main__":
    unittest.main()
