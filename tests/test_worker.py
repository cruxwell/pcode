"""The built-in worker can act, but keeps the parent's tool policies."""

import asyncio
import json

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from retry_parts import is_retry

from pcode.agent import create_agent
from pcode.ext import ExtensionAPI, ExtensionUI
from pcode.live import AgentRuntime
from pcode.runtime import EditCompleted


def tool_call(name, args, call_id="call"):
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)}


def returns(messages):
    return [
        part
        for message in messages
        for part in message.parts
        if isinstance(part, (ToolReturnPart, RetryPromptPart))
    ]


def test_worker_inherits_tools_instructions_guards_and_reports_edits(tmp_path):
    api = ExtensionAPI("worker_test", tmp_path, ExtensionUI())
    api.instructions("EXTENSION_GUIDANCE")
    guarded = []

    @api.tool
    def extension_tool() -> str:
        """Return a test result."""
        return "EXTENSION_RESULT"

    @api.hooks.on.before_tool_execute
    async def guard(ctx, *, call, tool_def, args):
        if tool_def.name == "write_file":
            guarded.append((ctx.agent.name, args["path"]))
            if args["path"] == "blocked.txt":
                raise ModelRetry("EXTENSION_BLOCKED")
        return args

    (tmp_path / "AGENTS.md").write_text("REPOSITORY_GUIDANCE")
    calls = [
        ("write_file", {"path": "created.txt", "content": "before\n"}),
        ("edit_file", {"path": "created.txt", "old_text": "before", "new_text": "after"}),
        ("extension_tool", {}),
        ("write_file", {"path": "blocked.txt", "content": "must not be written"}),
        ("write_file", {"path": ".env", "content": "test fixture, not a credential"}),
    ]
    parent_names = None
    child_requests = 0

    async def model(messages, info):
        nonlocal parent_names, child_requests
        names = {tool.name for tool in info.function_tools}
        if "delegate_task" in names:
            parent_names = names
            if not returns(messages):
                yield tool_call(
                    "delegate_task", {"agent_name": "worker", "task": "Implement"}, "delegate"
                )
            else:
                assert "Worker complete" in str(returns(messages)[-1].content)
                yield "Done"
            return
        assert names == parent_names - {
            "delegate_task",
            "task_worktree",
            "manage_worker",
        } | {"ask_parent"}
        assert {"write_file", "edit_file", "shell", "write_plan", "extension_tool"} <= names
        assert "EXTENSION_GUIDANCE" in info.instructions
        assert "REPOSITORY_GUIDANCE" in info.instructions
        assert "Prefer edit_file and write_file" in info.instructions
        results = returns(messages)
        if child_requests == 3:
            assert "EXTENSION_RESULT" in results[-1].content
        elif child_requests == 4:
            assert is_retry(results[-1])
            assert "EXTENSION_BLOCKED" in results[-1].content
        elif child_requests == 5:
            assert is_retry(results[-1])
        if child_requests < len(calls):
            name, args = calls[child_requests]
            yield tool_call(name, args, f"child-{child_requests}")
        else:
            yield "Worker complete"
        child_requests += 1

    agent = create_agent("test", tmp_path, extensions=api.capabilities())
    runtime = AgentRuntime(agent)

    async def run():
        with agent.override(model=FunctionModel(stream_function=model)):
            return [event async for event in runtime.stream("Implement")]

    events = asyncio.run(run())
    assert (tmp_path / "created.txt").read_text() == "after\n"
    assert not (tmp_path / "blocked.txt").exists()
    assert not (tmp_path / ".env").exists()
    assert ("worker", "blocked.txt") in guarded
    edits = [event for event in events if isinstance(event, EditCompleted)]
    assert [edit.call_id for edit in edits] == ["delegate:child-0", "delegate:child-1"]
    assert child_requests == 6


@pytest.mark.parametrize("mode", ["auto", "local", "off"])
def test_worker_inherits_native_and_local_web_policy(tmp_path, monkeypatch, mode):
    from pcode.ext import load_extensions
    from pcode.preferences import save_preferences

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    save_preferences(web_search=mode)
    loaded = load_extensions(tmp_path)
    child_seen = False
    parent_shape = None

    async def model(messages, info):
        nonlocal child_seen, parent_shape
        names = {tool.name for tool in info.function_tools}
        native = [tool.kind for tool in info.model_request_parameters.native_tools]
        if "delegate_task" in names:
            parent_shape = (
                names - {"delegate_task", "task_worktree"},
                native,
            )
            if not returns(messages):
                yield tool_call("delegate_task", {"agent_name": "worker", "task": "Research"})
            else:
                yield "Done"
            return
        child_seen = True
        assert (names, native) == parent_shape
        assert ("web_search" in native) == (mode == "auto")
        assert ("web_fetch" in native) == (mode == "auto")
        assert ("web_search" in names) == (mode == "local")
        assert ("get_page" in names) == (mode == "local")
        yield "Worker complete"

    agent = create_agent("test", tmp_path, extensions=loaded.capabilities)
    agent.run_sync("Research", model=FunctionModel(stream_function=model))
    assert child_seen


@pytest.mark.parametrize("concurrent", [False, True])
def test_runtime_tools_follow_enabled_state_and_concurrent_workers(tmp_path, concurrent):
    def remote_echo(value: str) -> str:
        """Echo through an enabled runtime toolset."""
        return value

    toolset = FunctionToolset([remote_echo])
    child_count = 0
    parent_requests = 0
    enabled = True

    async def model(messages, info):
        nonlocal child_count, parent_requests
        names = {tool.name for tool in info.function_tools}
        if "delegate_task" in names:
            parent_requests += 1
            if parent_requests % 2:
                count = 2 if concurrent else 1
                yield {
                    i: DeltaToolCall(
                        name="delegate_task",
                        json_args=json.dumps({"agent_name": "worker", "task": f"Task {i}"}),
                        tool_call_id=f"delegate-{i}",
                    )
                    for i in range(count)
                }
            else:
                yield "Done"
            return
        assert ("remote_echo" in names) == enabled
        assert "write_file" in names  # Runtime tools must not replace core tools.
        if enabled and not returns(messages):
            yield tool_call("remote_echo", {"value": "REMOTE_RESULT"})
        else:
            if enabled:
                assert returns(messages)[-1].content == "REMOTE_RESULT"
            child_count += 1
            yield "Worker complete"

    agent = create_agent("test", tmp_path)
    runtime = AgentRuntime(agent)
    runtime.mcp.enabled["remote"] = toolset

    async def run():
        nonlocal enabled
        with agent.override(model=FunctionModel(stream_function=model)):
            _ = [event async for event in runtime.stream("First")]
            runtime.mcp.disable("remote")
            enabled = False
            _ = [event async for event in runtime.stream("Second")]

    asyncio.run(run())
    assert child_count == (4 if concurrent else 2)
