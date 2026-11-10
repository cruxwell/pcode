"""Plan step sizes: accepted by the tools, kept through storage, and weighting progress."""

import asyncio

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.planning import InMemoryPlanStore

from pcode.plan_preview import project_plan
from pcode.plan_sizes import plan_progress
from pcode.planning import IdentifiedPlanning, SizedPlanItem


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([], None),
        ([{"status": "pending"}] * 2, 0.0),
        # Unsized steps weigh the same, so this is the old count.
        ([{"status": "completed"}] + [{"status": "pending"}] * 3, 0.25),
        # A cancelled step leaves the total rather than stranding the bar short.
        ([{"status": "completed"}, {"status": "cancelled"}], 1.0),
        ([{"status": "cancelled"}], None),
        # The running step counts half.
        ([{"status": "in_progress"}, {"status": "pending"}], 0.25),
        # S=1, M=3, L=8: a done S beside a pending L barely moves it.
        ([{"status": "completed", "size": "S"}, {"status": "pending", "size": "L"}], 1 / 9),
        ([{"status": "completed", "size": "L"}, {"status": "pending"}], 8 / 11),
        # An unknown size from an old or hand-edited record counts as M.
        ([{"status": "completed", "size": "XL"}, {"status": "pending"}], 0.5),
    ],
)
def test_plan_progress(items, expected):
    assert plan_progress(items) == (None if expected is None else pytest.approx(expected))


def test_tools_store_the_size():
    async def run():
        store = InMemoryPlanStore()
        calls = 0

        async def model(messages, info):
            nonlocal calls
            calls += 1
            if calls == 1:
                items = [
                    {"id": "a", "content": "Read", "size": "S"},
                    {"id": "b", "content": "Build", "size": "L"},
                ]
                return ModelResponse(parts=[ToolCallPart("write_plan", {"items": items})])
            if calls == 2:
                args = {"content": "Check", "size": "M"}
                return ModelResponse(parts=[ToolCallPart("add_task", args)])
            if calls == 3:
                return ModelResponse(parts=[ToolCallPart("add_task", {"content": "Commit"})])
            return ModelResponse(parts=[TextPart("ok")])

        agent = Agent(FunctionModel(model), capabilities=[IdentifiedPlanning(store=store)])
        async with agent:
            await agent.run("go")
        items = await store.get_items()
        assert [item.model_dump()["size"] for item in items] == ["S", "L", "M", None]
        # As restore and navigate read it back from a saved session.
        dumped = [item.model_dump(mode="json") for item in items]
        assert [SizedPlanItem.model_validate(item).size for item in dumped] == ["S", "L", "M", None]

    asyncio.run(run())


def test_preview_keeps_the_size():
    part = ToolCallPart("write_plan", {"items": [{"content": "Build", "size": "L"}]}, "call")
    assert project_plan([], part)[0]["size"] == "L"


def test_the_plan_tools_offer_one_status_tool_and_no_subtask_fields():
    seen = {}

    def model(messages, info):
        seen["tools"] = {tool.name: tool for tool in info.function_tools}
        return ModelResponse(parts=[TextPart("ok")])

    Agent(FunctionModel(model), capabilities=[IdentifiedPlanning()]).run_sync("hi")
    tools = seen["tools"]
    assert "update_task_status" not in tools and "update_task_statuses" in tools
    schema = str(tools["write_plan"].parameters_json_schema)
    assert "parent_id" not in schema and "depends_on" not in schema and "size" in schema
    for tool in tools.values():
        assert "update_task_status" not in tool.description.replace("update_task_statuses", "")
    # Hidden fields still validate, so stored steps round-trip.
    assert SizedPlanItem(content="x", depends_on=["a"]).depends_on == ["a"]
