"""The model learns which MCP servers are enabled, without earlier messages changing."""

import asyncio
import json
from copy import deepcopy

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from pcode.agent import create_agent, has_mcp_servers
from pcode.live import AgentRuntime
from pcode.mcp import MCPState
from pcode.mcp_notice import INSTRUCTIONS, TAG, MCPServers, render


def notices(messages) -> list[str]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
        and isinstance(part.content, str)
        and part.content.startswith(TAG)
    ]


def sent(messages) -> list[tuple]:
    """The parts in wire order, by role.

    Pydantic AI merges an appended reminder into the preceding request once it is
    history, and providers merge consecutive user messages anyway, so message
    boundaries are not what the cache sees; the part sequence is.
    """
    return [
        (message.kind, part.part_kind, getattr(part, "content", None), getattr(part, "args", None))
        for message in messages
        for part in message.parts
    ]


@pytest.fixture
def config(monkeypatch, tmp_path):
    path = tmp_path / "mcp.json"
    monkeypatch.setenv("PCODE_MCP_CONFIG", str(path))

    def write(servers):
        path.write_text(json.dumps({"mcpServers": servers}))

    return write


def probe() -> str:
    """A local tool, so each turn makes a second request."""
    return "probed"


def recording_runtime():
    """A runtime whose model calls `probe` once per turn, recording every request."""
    requests = []

    async def model(messages, info):
        requests.append((notices(messages), info.instructions or ""))
        if isinstance(messages[-1].parts[-1], UserPromptPart):
            yield {0: DeltaToolCall(name="probe", json_args="{}", tool_call_id="probe")}
        else:
            yield "done"

    agent = Agent(
        FunctionModel(stream_function=model),
        toolsets=[FunctionToolset([probe])],
        capabilities=[MCPServers(instruct=True)],
    )
    return AgentRuntime(agent), requests


def test_list_is_appended_when_it_changes_and_never_rewrites_history():
    runtime, requests = recording_runtime()

    async def turn(prompt):
        before = sent(runtime.history)
        requests.clear()
        async for _ in runtime.stream(prompt):
            pass
        # Earlier parts are exactly what was sent before: the cached prefix
        # survives every change to the server list.
        assert sent(runtime.history)[: len(before)] == before
        return [seen for seen, _ in requests]

    async def run():
        # Nothing enabled and nothing to retract: no list at all.
        assert await turn("one") == [[], []]
        assert INSTRUCTIONS in requests[0][1]

        runtime.mcp.enabled["gdrive"] = FunctionToolset([])
        runtime.mcp.descriptions["gdrive"] = "Google Drive\n  files and docs"
        gdrive = render([("gdrive", "Google Drive files and docs")])
        assert "- gdrive: Google Drive files and docs" in gdrive
        # Sent with the turn's first request, and not again for the tool result.
        assert await turn("two") == [[gdrive], [gdrive]]
        # Unchanged, so nothing new, on this turn or any later one.
        assert await turn("three") == [[gdrive], [gdrive]]

        runtime.mcp.enabled["glean"] = FunctionToolset([])
        both = render([("gdrive", "Google Drive files and docs"), ("glean", None)])
        assert (await turn("four"))[0] == [gdrive, both]
        assert both.endswith("- glean\n</mcp-servers>")

        runtime.mcp.disable("gdrive")
        runtime.mcp.disable("glean")
        none = render([])
        assert "No MCP servers are enabled" in none
        assert (await turn("five"))[0] == [gdrive, both, none]
        assert (await turn("six"))[0] == [gdrive, both, none]
        assert notices(runtime.history) == [gdrive, both, none]

    asyncio.run(run())


class Unreachable(FunctionToolset):
    async def __aenter__(self):
        raise ConnectionError("handshake refused")


def test_a_server_that_fails_to_connect_is_listed_as_unavailable():
    runtime, requests = recording_runtime()
    down = render([("remote", None)], {"remote": "failed"})
    up = render([("remote", None)])
    assert "- remote (failed to connect this turn; its tools are unavailable)" in down

    async def turn():
        requests.clear()
        async for _ in runtime.stream("go"):
            pass
        return requests[0][0]

    async def run():
        runtime.mcp.enabled["remote"] = Unreachable([])
        # The turn still runs (probe is called), and the model is told why the
        # server it was promised has no tools.
        assert await turn() == [down]
        assert len(requests) == 2
        runtime.mcp.enabled["remote"] = FunctionToolset([])
        assert await turn() == [down, up]

    asyncio.run(run())


def test_a_running_turn_follows_enable_and_disable_at_its_next_request():
    """/mcp during a turn acts like steering: the next request has the change."""
    events = []

    class Tracked(FunctionToolset):
        async def __aenter__(self):
            events.append("enter")
            return await super().__aenter__()

        async def __aexit__(self, *args):
            events.append("exit")
            return await super().__aexit__(*args)

    def lookup() -> str:
        return "found"

    seen = []

    async def model(messages, info):
        seen.append((sorted(tool.name for tool in info.function_tools), notices(messages)))
        step = len(seen)
        if step == 1:
            runtime.mcp.enabled["docs"] = Tracked([lookup])
            yield {0: DeltaToolCall(name="probe", json_args="{}", tool_call_id="p1")}
        elif step == 2:
            yield {0: DeltaToolCall(name="lookup", json_args="{}", tool_call_id="l1")}
        elif step == 3:
            runtime.mcp.disable("docs")
            yield {0: DeltaToolCall(name="probe", json_args="{}", tool_call_id="p2")}
        else:
            yield "done"

    agent = Agent(
        FunctionModel(stream_function=model),
        toolsets=[FunctionToolset([probe])],
        capabilities=[MCPServers(instruct=False)],
    )
    runtime = AgentRuntime(agent)

    async def run():
        async for _ in runtime.stream("go"):
            pass

    asyncio.run(run())
    listed, none = render([("docs", None)]), render([])
    assert seen == [
        (["probe"], []),
        (["lookup", "probe"], [listed]),
        (["lookup", "probe"], [listed]),
        (["probe"], [listed, none]),
    ]
    # Connected once when enabled, closed once when disabled, not per step.
    assert events == ["enter", "exit"]
    assert any(
        isinstance(part, ToolReturnPart) and part.content == "found"
        for message in runtime.history
        for part in getattr(message, "parts", ())
    )


def test_cancelling_the_first_connection_closes_the_servers_already_open():
    from pydantic_ai.toolsets import CombinedToolset

    events = []
    started = asyncio.Event()

    class Fast(FunctionToolset):
        async def __aenter__(self):
            events.append("fast enter")
            return self

        async def __aexit__(self, *args):
            events.append("fast exit")

    class Slow(FunctionToolset):
        async def __aenter__(self):
            started.set()
            await asyncio.Event().wait()

    state = MCPState()
    state.enabled.update(fast=Fast([]), slow=Slow([]))

    async def run():
        # Entered as a run would: inside a stack that exits only what entered.
        entering = asyncio.create_task(CombinedToolset([state.live()]).__aenter__())
        await started.wait()
        entering.cancel()
        with pytest.raises(asyncio.CancelledError):
            await entering

    asyncio.run(run())
    assert events == ["fast enter", "fast exit"]


def test_list_returns_after_compaction_drops_it():
    runtime, requests = recording_runtime()
    summary = [
        ModelRequest(
            parts=[
                UserPromptPart(content="Summary of the conversation so far: read the design doc.")
            ]
        ),
        ModelResponse(parts=[TextPart(content="Understood.")]),
    ]

    async def run():
        runtime.mcp.enabled["gdrive"] = FunctionToolset([])
        async for _ in runtime.stream("one"):
            pass
        assert len(notices(runtime.history)) == 1
        runtime.history = deepcopy(summary)
        async for _ in runtime.stream("two"):
            pass
        assert notices(runtime.history) == [render([("gdrive", None)])]

        # Nothing enabled after compaction: nothing left to retract either.
        runtime.mcp.disable("gdrive")
        runtime.history = deepcopy(summary)
        requests.clear()
        async for _ in runtime.stream("three"):
            pass
        assert notices(runtime.history) == []

    asyncio.run(run())


def test_worker_learns_the_servers_of_the_turn_that_delegated(config, tmp_path):
    config({"remote": {"command": "remote-mcp"}})
    parent, child = [], []

    async def model(messages, info):
        names = {tool.name for tool in info.function_tools}
        if "delegate_task" in names:
            parent.append((notices(messages), info.instructions or ""))
            if len(parent) == 1:
                task = json.dumps({"agent_name": "worker", "task": "Look it up"})
                yield {0: DeltaToolCall(name="delegate_task", json_args=task, tool_call_id="d")}
            else:
                yield "Done"
            return
        child.append((notices(messages), info.instructions or ""))
        yield "Worker complete"

    agent = create_agent("test", tmp_path)
    runtime = AgentRuntime(agent)
    runtime.mcp.enabled["remote"] = FunctionToolset([])
    runtime.mcp.descriptions["remote"] = "Remote records"

    async def run():
        with agent.override(model=FunctionModel(stream_function=model)):
            async for _ in runtime.stream("Delegate it"):
                pass

    asyncio.run(run())
    listed = render([("remote", "Remote records")])
    assert parent[0] == ([listed], parent[0][1])
    assert child == [([listed], child[0][1])]
    assert INSTRUCTIONS in parent[0][1]
    assert INSTRUCTIONS in child[0][1]


def test_instruction_only_when_servers_are_configured(config):
    assert not has_mcp_servers()  # No file.
    config({})
    assert not has_mcp_servers()
    config({"bad name!": {"command": "x"}})
    assert not has_mcp_servers()  # A broken file must not stop the agent building.
    config({"docs": {"command": "docs-mcp"}})
    assert has_mcp_servers()
    assert MCPServers(instruct=False).get_instructions() is None
    assert MCPServers(instruct=True).get_instructions() == INSTRUCTIONS


def test_description_is_captured_on_enable_and_dropped_on_disable(config):
    config(
        {
            "docs": {"command": "docs-mcp", "description": "Internal API reference"},
            "plain": {"command": "plain-mcp"},
            "wrong": {"command": "wrong-mcp", "description": 3},
        }
    )
    state = MCPState()

    async def run():
        await state.enable("docs")
        await state.enable("plain")
        assert state.servers() == {"docs": "Internal API reference", "plain": None}
        state.disable("docs")
        assert state.servers() == {"plain": None}
        with pytest.raises(ValueError, match="description"):
            await state.enable("wrong")

    asyncio.run(run())
