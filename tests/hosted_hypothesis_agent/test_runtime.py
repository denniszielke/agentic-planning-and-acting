from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.hosted_hypothesis_agent.runtime import TOOL_CATEGORIES, ToolAuditMiddleware, build_specialists


class FakeAgent:
    def __init__(self, name: str) -> None:
        self.name = name

    def as_tool(self, **kwargs):  # noqa: ANN003, ANN201
        return kwargs


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def as_agent(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        return FakeAgent(kwargs["name"])


async def _resolve(value):
    return value


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_specialists_each_own_one_toolbox_and_independent_session(self) -> None:
        client = FakeClient()
        toolboxes = [object(), object(), object()]

        specialists, tools = build_specialists(client, toolboxes)

        self.assertEqual(len(specialists), 3)
        for index, category in enumerate(TOOL_CATEGORIES):
            self.assertEqual(client.calls[index]["tools"], [toolboxes[index]])
            self.assertEqual(tools[index]["name"], category)
            self.assertFalse(tools[index]["propagate_session"])

    async def test_tool_result_is_replayed_without_calling_tool_again(self) -> None:
        cached = {"forecast": "clear"}
        saved = []
        middleware = ToolAuditMiddleware("execution", ledger_get=lambda key: _resolve(cached),
                                         ledger_save=lambda key, value: _resolve(saved.append((key, value))))
        context = SimpleNamespace(
            function=SimpleNamespace(name="weather"), arguments={"city": "Berlin"}, result=None)
        calls = 0

        async def call_next() -> None:
            nonlocal calls
            calls += 1
            context.result = {"forecast": "cloudy"}

        await middleware.process(context, call_next)
        self.assertEqual(calls, 0)
        self.assertEqual(context.result, cached)
        self.assertTrue(middleware.activities[0].output_summary["cached"])
        self.assertEqual(saved, [])

    async def test_specialist_delegation_is_audited_by_category(self) -> None:
        middleware = ToolAuditMiddleware("execution")
        context = SimpleNamespace(
            function=SimpleNamespace(name="weather"),
            arguments={"task": "Get Berlin weather for 2026-09-14."},
            result=None,
        )

        async def call_next() -> None:
            context.result = "Berlin: clear, 20 C"

        await middleware.process(context, call_next)

        self.assertEqual(len(middleware.activities), 1)
        self.assertEqual(middleware.activities[0].tool_category, "weather")
        self.assertEqual(middleware.activities[0].tool_name, "weather")


if __name__ == "__main__":
    unittest.main()