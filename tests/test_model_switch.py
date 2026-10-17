"""`/model` and `/effort` mid-turn: the running turn's next request uses them, as steering lands."""

import asyncio

from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from test_prompt_model import Recorder

from pcode.agent import SideModel, create_coder
from pcode.live import AgentRuntime


def scripted(name: str, calls: list[str]) -> FunctionModel:
    """A model that calls `switch` on the first request of a turn and answers after."""

    async def stream(messages, info):
        calls.append(name)
        if len(calls) == 1:
            yield {0: DeltaToolCall(name="switch", json_args="{}")}
        else:
            yield f"done on {name}"

    return FunctionModel(stream_function=stream, model_name=name)


def runtime_switching_to(tmp_path, seen, calls, replacement):
    """A runtime whose `switch` tool does what `/model` or `/effort` does mid-turn."""
    agent = Agent(
        scripted("old", calls),
        capabilities=[create_coder(tmp_path), Recorder(seen)],
        model_settings={"temperature": 0.5, "top_p": 0.1},
    )
    runtime = AgentRuntime(agent)

    @agent.tool_plain
    def switch() -> str:
        replacement(runtime)
        return "switched"

    return runtime


def test_a_model_chosen_mid_turn_takes_the_turns_next_request(tmp_path):
    seen, calls = [], []

    def switch_model(runtime):
        runtime.replace_agent(
            Agent(
                scripted("new", calls),
                capabilities=[create_coder(tmp_path), Recorder(seen)],
                model_settings={"temperature": 0.9},
            )
        )

    runtime = runtime_switching_to(tmp_path, seen, calls, switch_model)

    async def run():
        return [event async for event in runtime.stream("go")]

    asyncio.run(run())
    assert calls == ["old", "new"]
    first, second = seen
    assert first["settings"] == {"temperature": 0.5, "top_p": 0.1}
    # The new agent's settings replace the old ones: nothing of the old model's carries over.
    assert second["model"] == "new"
    assert second["settings"] == {"temperature": 0.9}
    # One turn, finished on the new model.
    assert [node.prompt for node in runtime.tree.nodes.values() if node.prompt] == ["go"]


def test_effort_changed_mid_turn_takes_the_turns_next_request(tmp_path):
    seen, calls = [], []

    def change_effort(runtime):
        # What `apply_effort` does: replace the agent's settings, never mutate them.
        runtime.agent.model_settings = {"temperature": 0.5, "top_p": 0.7}

    runtime = runtime_switching_to(tmp_path, seen, calls, change_effort)

    async def run():
        return [event async for event in runtime.stream("go")]

    asyncio.run(run())
    assert [entry["settings"]["top_p"] for entry in seen] == [0.1, 0.7]


def test_a_turn_on_another_model_keeps_it_through_a_switch(tmp_path):
    seen, calls = [], []
    borrowed = SideModel("fn:side", scripted("side", calls), {"top_p": 0.3})

    def switch_model(runtime):
        runtime.replace_agent(Agent(scripted("new", calls), capabilities=[create_coder(tmp_path)]))

    runtime = runtime_switching_to(tmp_path, seen, calls, switch_model)

    async def run():
        return [event async for event in runtime.stream("go", model=borrowed)]

    asyncio.run(run())
    # `$MODEL` runs the whole turn there; the conversation's switch waits for the next turn.
    assert calls == ["side", "side"]
    assert all(entry["settings"].get("top_p") == 0.3 for entry in seen)
