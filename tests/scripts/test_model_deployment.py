from __future__ import annotations

import os
import unittest
from unittest.mock import call, patch

from scripts._helpers import (
    COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID,
    ensure_external_model_access,
)
from scripts.deploy_hosted_hypothesis_agent import (
    DecisionModelConfig,
    decision_model_config,
    validate_decision_model,
)

ACCOUNT_ID = (
    "/subscriptions/external-sub/resourceGroups/models-rg/providers/"
    "Microsoft.CognitiveServices/accounts/external-models"
)


class DecisionModelConfigurationTests(unittest.TestCase):
    def test_partial_external_configuration_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"DECISION_MODEL_PROJECT_ENDPOINT": "https://example.test/projects/decision"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "partial.*Missing"):
                decision_model_config()

    def test_empty_external_configuration_is_optional(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(decision_model_config())

    def test_external_deployment_lookup_uses_target_account(self) -> None:
        config = DecisionModelConfig(
            project_endpoint="https://dzwestus3-resource.services.ai.azure.com/api/projects/dzwestus3",
            deployment_name="Decision-1",
            account_resource_id=ACCOUNT_ID,
            tenant_id="tenant-a",
        )
        with (
            patch.dict(os.environ, {"AZURE_SUBSCRIPTION_ID": "local-sub"}, clear=True),
            patch(
                "scripts.deploy_hosted_hypothesis_agent.run",
                side_effect=["tenant-a", "tenant-a", "Decision-1"],
            ) as run,
        ):
            validate_decision_model(config)
        self.assertEqual(
            run.call_args_list[-1],
            call(
                [
                    "az",
                    "cognitiveservices",
                    "account",
                    "deployment",
                    "show",
                    "--subscription",
                    "external-sub",
                    "--resource-group",
                    "models-rg",
                    "--name",
                    "external-models",
                    "--deployment-name",
                    "Decision-1",
                    "--query",
                    "name",
                    "-o",
                    "tsv",
                ],
                capture=True,
            ),
        )

    def test_cross_tenant_configuration_fails_before_deployment_lookup(self) -> None:
        config = DecisionModelConfig(
            project_endpoint="https://dzwestus3-resource.services.ai.azure.com/api/projects/dzwestus3",
            deployment_name="Decision-1",
            account_resource_id=ACCOUNT_ID,
            tenant_id="",
        )
        with (
            patch.dict(os.environ, {"AZURE_SUBSCRIPTION_ID": "local-sub"}, clear=True),
            patch(
                "scripts.deploy_hosted_hypothesis_agent.run",
                side_effect=["tenant-local", "tenant-external"],
            ) as run,
        ):
            with self.assertRaisesRegex(ValueError, "Cross-tenant"):
                validate_decision_model(config)
        self.assertEqual(run.call_count, 2)


class ExternalModelAccessTests(unittest.TestCase):
    def test_rbac_lookup_and_deployment_are_scoped_to_external_account(self) -> None:
        with (
            patch.dict(os.environ, {"AZURE_LOCATION": "westus3"}, clear=True),
            patch("scripts._helpers.run", side_effect=["existing-assignment", ""]) as run,
        ):
            ensure_external_model_access("agent-principal", ACCOUNT_ID)
        role_lookup, deployment = run.call_args_list
        self.assertIn("--scope", role_lookup.args[0])
        self.assertEqual(
            role_lookup.args[0][role_lookup.args[0].index("--scope") + 1],
            ACCOUNT_ID,
        )
        self.assertIn(COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID, role_lookup.args[0])
        self.assertEqual(
            deployment.args[0][deployment.args[0].index("--subscription") + 1],
            "external-sub",
        )
        self.assertEqual(
            deployment.args[0][deployment.args[0].index("--resource-group") + 1],
            "models-rg",
        )
        self.assertIn("accountName=external-models", deployment.args[0])
        self.assertIn("assignmentName=existing-assignment", deployment.args[0])


if __name__ == "__main__":
    unittest.main()
