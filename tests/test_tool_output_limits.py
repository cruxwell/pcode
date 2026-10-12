"""Exercise actual model-visible history, spill retrieval, and Coder composition."""

import asyncio
import json
import shlex
import sys
from datetime import timedelta

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturn,
    ToolReturnPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.subagents import SubAgents
from pydantic_ai_harness.tool_output_limits import LocalFileStore, ToolOutputLimits

from pcode.agent import create_coder
from pcode.config import configure
from pcode.live import AgentRuntime
from pcode.preferences import SETTINGS, load_preferences, preferences_path, save_preferences
from pcode.runtime import ToolSummary
from pcode.shell import REDUCED_SHELL_OUTPUT
from pcode.tool_display import job_status, result_detail
from pcode.tool_output_limits import create_tool_output_limits, tool_results_path


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("EXA_API_KEY", raising=False)


def returns(messages):
    return [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


def invoke(capability, tool, *, args=None):
    def model(messages, info):
        part = TextPart("Done") if returns(messages) else ToolCallPart(tool.__name__, args or {})
        return ModelResponse(parts=[part])

    async def stream(messages, info):
        part = model(messages, info).parts[0]
        if isinstance(part, TextPart):
            yield part.content
        else:
            yield {0: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}

    return Agent(
        FunctionModel(model, stream_function=stream), tools=[tool], capabilities=[capability]
    ).run_sync("Inspect")


@pytest.mark.parametrize(
    "key,value",
    [
        ("mode", "spill"),
        ("mode", "truncate"),
        ("mode", "off"),
        ("threshold", "8000"),
        ("preview_chars", "500"),
        ("max_chars", "2000"),
        ("strategy", "head"),
        ("strategy", "tail"),
        ("strategy", "head_tail"),
        ("retention_hours", "24"),
        ("retention_hours", "0"),
    ],
)
def test_config_round_trip(key, value):
    key = f"tool_output_{key}"
    configure(["set", key, value])
    assert configure(["get", key]) == value
    assert load_preferences()[key] == value
    configure(["unset", key])
    assert configure(["get", key]) == SETTINGS[key].default


@pytest.mark.parametrize("key", ["threshold", "preview_chars", "max_chars"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "many", "", " 20", "２０"])
def test_invalid_numeric_config_does_not_write(key, value):
    with pytest.raises(ValueError, match="positive integer"):
        configure(["set", f"tool_output_{key}", value])
    assert not preferences_path().exists()


@pytest.mark.parametrize(
    "key,value", [("mode", "summarize"), ("strategy", "middle"), ("retention_hours", "-1")]
)
def test_invalid_mode_strategy_and_retention(key, value):
    with pytest.raises(ValueError):
        configure(["set", f"tool_output_{key}", value])
    assert not preferences_path().exists()


def test_defaults_invalid_saved_values_and_snapshot(tmp_path):
    save_preferences(tool_output_mode="invalid", tool_output_threshold="oops")
    coder = create_coder(tmp_path)
    limits = [c for c in coder.capabilities if isinstance(c, ToolOutputLimits)]
    assert len(limits) == 1  # No second, upstream 64k truncation capability.
    parent = limits[0]
    children = next(c for c in coder.capabilities if isinstance(c, SubAgents))
    child = next(c for c in children.shared_capabilities if isinstance(c, ToolOutputLimits))
    assert child is not parent
    assert child.bands == parent.bands
    assert parent.bands[0].over == 10000
    assert parent.bands[0].action.preview_chars == 1000
    assert parent.bands[0].action.then.max_chars == 4000
    assert parent.store.cleanup_after is None
    assert not tool_results_path().exists()  # Configuration/creation is side-effect free.
    save_preferences(tool_output_threshold="20000", tool_output_retention_hours="24")
    assert parent.bands[0].over == 10000
    new = create_tool_output_limits()
    assert new.bands[0].over == 20000
    assert new.store.cleanup_after == timedelta(hours=24)


@pytest.mark.parametrize("size,spills", [(9999, False), (10000, True), (120000, True)])
def test_threshold_and_lossless_spill_before_coder_cap(tmp_path, size, spills):
    payload = "x" * size

    def large_result():
        return payload

    result = invoke(create_coder(tmp_path), large_result)
    part = returns(result.all_messages())[0]
    if spills:
        assert len(part.content) < 1600
        assert "read_tool_result" in part.content
        handle = part.metadata["overflow_handle"]
        assert (tool_results_path() / handle).read_text() == payload
        assert tool_results_path().stat().st_mode & 0o777 == 0o700
    else:
        assert part.content == payload
        assert not tool_results_path().exists()
    assert result.usage.requests == 2  # No summarization model request.


@pytest.mark.parametrize("mode", ["spill", "truncate", "off"])
def test_serialized_history_can_read_spills_in_fresh_agent_even_when_disabled(mode):
    payload = "".join(f"row {i:04d} detail\n" for i in range(3000))

    def large_result():
        return payload

    first = invoke(create_tool_output_limits(), large_result)
    history = ModelMessagesTypeAdapter.validate_json(first.all_messages_json())
    handle = returns(history)[0].metadata["overflow_handle"]
    save_preferences(tool_output_mode=mode)

    def model(messages, info):
        assert "read_tool_result" in {t.name for t in info.function_tools}
        parts = returns(messages)
        if parts[-1].tool_name != "read_tool_result":
            return ModelResponse(
                parts=[ToolCallPart("read_tool_result", {"handle": handle, "pattern": "row 1500"})]
            )
        assert "row 1500 detail" in parts[-1].content
        assert "row 1501" not in parts[-1].content
        assert len(parts[0].content) < 1600  # The old return stays reduced in history.
        return ModelResponse(parts=[TextPart("Found")])

    result = Agent(FunctionModel(model), capabilities=[create_tool_output_limits()]).run_sync(
        "Find row 1500", message_history=history
    )
    assert result.output == "Found"


@pytest.mark.parametrize("strategy", ["head", "tail", "head_tail"])
@pytest.mark.parametrize("store_failure", [False, True])
def test_configured_truncation_and_spill_failure_fallback(strategy, store_failure, monkeypatch):
    save_preferences(
        tool_output_mode="spill" if store_failure else "truncate",
        tool_output_threshold="500",
        tool_output_max_chars="200",
        tool_output_strategy=strategy,
    )
    if store_failure:

        async def fail(*args, **kwargs):
            raise OSError("cannot write")

        monkeypatch.setattr(LocalFileStore, "write", fail)

    def large_result():
        return "HEAD" + "x" * 1000 + "TAIL"

    part = returns(invoke(create_tool_output_limits(), large_result).all_messages())[0]
    assert len(part.content) <= 200
    assert "truncated" in part.content
    assert ("HEAD" in part.content) == (strategy != "tail")
    assert ("TAIL" in part.content) == (strategy != "head")
    assert not tool_results_path().exists()


def test_off_disables_coder_reduction_not_just_spilling(tmp_path):
    save_preferences(tool_output_mode="off")
    payload = "x" * 120000

    def large_result():
        return payload

    part = returns(invoke(create_coder(tmp_path), large_result).all_messages())[0]
    assert part.content == payload
    assert not tool_results_path().exists()


def test_structured_results_and_content_spill_separately_and_preserve_metadata():
    payload = [{"row": i, "data": "x" * 100} for i in range(200)]
    content = "Content\n" * 2000

    def mcp_like_result():
        return ToolReturn(return_value=payload, content=content, metadata={"source": "test"})

    result = invoke(create_tool_output_limits(), mcp_like_result)
    part = returns(result.all_messages())[0]
    assert part.metadata["source"] == "test"
    stored = (tool_results_path() / part.metadata["overflow_handle"]).read_text()
    assert json.loads(stored) == payload
    assert len(stored.splitlines()) > 200  # Indented JSON can be paged, not one giant line.
    content_handle = part.metadata["overflow_content_handle"]
    assert content_handle != part.metadata["overflow_handle"]
    assert (tool_results_path() / content_handle).read_text() == content
    assert content not in result.all_messages_json().decode()


def test_real_worker_and_parent_share_retrievable_spills(tmp_path):
    (tmp_path / "large.txt").write_text("line of evidence\n" * 2000)
    child_handle = None

    async def model(messages, info):
        nonlocal child_handle
        parent = "delegate_task" in {t.name for t in info.function_tools}
        parts = returns(messages)
        assert "read_tool_result" in {t.name for t in info.function_tools}
        if not parts:
            name = "delegate_task" if parent else "read_file"
            args = (
                {"agent_name": "worker", "task": "Read large.txt"}
                if parent
                else {"path": "large.txt"}
            )
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}
        elif not parent:
            part = parts[-1]
            assert len(part.content) < 1600
            child_handle = part.metadata["overflow_handle"]
            yield "Worker answer " * 1000
        elif parts[-1].tool_name == "delegate_task":
            assert len(parts[-1].content) < 1600  # Parent also reduces the child answer.
            assert child_handle
            yield {
                0: DeltaToolCall(
                    name="read_tool_result",
                    json_args=json.dumps({"handle": child_handle, "limit": 2}),
                )
            }
        else:
            assert "line of evidence" in parts[-1].content
            yield "Done"

    result = Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])

    async def run():
        async with result.run_stream("Explore") as response:
            assert await response.get_output() == "Done"

    asyncio.run(run())


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("mode", ["spill", "truncate"])
def test_reduced_shell_keeps_handles_and_status(tmp_path, delegated, mode):
    save_preferences(
        tool_output_mode=mode,
        tool_output_strategy="head",
        tool_output_max_chars="100",
        tool_output_preview_chars="100",
    )
    source = "import sys; print('x' * 22000); sys.exit(7)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
    observed_shell = False

    async def model(messages, info):
        nonlocal observed_shell
        parent = "delegate_task" in {t.name for t in info.function_tools}
        parts = returns(messages)
        if not parts:
            name = "delegate_task" if parent and delegated else "shell"
            args = (
                {"agent_name": "worker", "task": "Run the test command"}
                if name == "delegate_task"
                else {"command": command}
            )
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}
        else:
            if parts[-1].tool_name == "shell":
                observed_shell = True
                text = parts[-1].content
                assert len(text) < 2000
                # The job marker is control information, never reduced away.
                assert job_status(text) == ("j1 · exit 7", True)
            yield "Done"

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        events = [event async for event in runtime.stream("Run")]
        shell = next(e for e in events if isinstance(e, ToolSummary) and e.name == "shell")
        assert shell.failed
        assert "exit 7" in shell.detail
        assert shell.result.startswith(REDUCED_SHELL_OUTPUT)
        assert observed_shell

    asyncio.run(run())


def test_spill_preview_not_reported_as_complete_file_or_search_metrics():
    preview = "[Tool output too large (20,000 chars); stored to handle 'test'.]\na.py\nb.py"
    for tool in ("read_file", "grep", "list_files"):
        detail, failed = result_detail(tool, {"path": "."}, preview, "success")
        assert "Output stored" in detail
        assert not failed


def test_single_long_line_is_recoverable_through_documented_shell_path(tmp_path):
    payload = "x" * 60000 + "TARGET" + "y" * 60000

    def large_result():
        return payload

    async def model(messages, info):
        parts = returns(messages)
        if not parts:
            yield {0: DeltaToolCall(name="large_result", json_args="{}")}
        elif parts[-1].tool_name == "large_result":
            handle = parts[-1].metadata["overflow_handle"]
            assert str(tool_results_path()) in info.instructions
            source = (
                "from pathlib import Path; "
                f"print(Path({str(tool_results_path() / handle)!r}).read_text()[60000:60006])"
            )
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
            yield {0: DeltaToolCall(name="shell", json_args=json.dumps({"command": command}))}
        else:
            assert "TARGET" in parts[-1].content
            yield "Found"

    result = Agent(
        FunctionModel(stream_function=model),
        tools=[large_result],
        capabilities=[create_coder(tmp_path)],
    ).run_sync("Find target")
    assert result.output == "Found"


def test_session_tools_page_themselves_and_are_never_spilled():
    payload = {"text": "x" * 20000, "next_offset": 16000}

    def read_session():
        return payload

    part = returns(invoke(create_tool_output_limits(), read_session).all_messages())[0]
    assert part.content == payload
    assert not tool_results_path().exists()


@pytest.mark.parametrize("later_turn", [False, True])
def test_reading_a_spill_is_shown_by_its_source_and_window_not_its_handle(later_turn):
    """A later turn's read finds the spilling call in history, not this run's calls."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models.function import AgentInfo

    from pcode.runtime import RunStatus, ToolStarted

    payload = "".join(f"row {i:04d}\n" for i in range(3000))

    async def run():
        async def model(messages, info: AgentInfo):
            parts = returns(messages)
            prompt = next(
                p.content
                for m in reversed(messages)
                if isinstance(m, ModelRequest)
                for p in m.parts
                if isinstance(p, UserPromptPart)
            )
            if not parts:
                yield {0: DeltaToolCall(name="large_result", json_args="{}")}
            elif parts[-1].tool_name == "large_result" and (prompt == "Read") == later_turn:
                handle = parts[-1].metadata["overflow_handle"]
                args = json.dumps({"handle": handle, "offset": 200, "limit": 50})
                yield {0: DeltaToolCall(name="read_tool_result", json_args=args)}
            else:
                yield "Done"

        agent = Agent(
            FunctionModel(stream_function=model), capabilities=[create_tool_output_limits()]
        )

        @agent.tool_plain
        def large_result() -> str:
            return payload

        runtime = AgentRuntime(agent)
        events = [e async for e in runtime.stream("Inspect")]
        if later_turn:
            events = [e async for e in runtime.stream("Read")]
        reads = [e for e in events if getattr(e, "name", "") == "read_tool_result"]
        started = next(e for e in reads if isinstance(e, ToolStarted))
        summary = next(e for e in reads if isinstance(e, ToolSummary))
        assert started.detail == "large_result output · lines 201–250"
        assert summary.detail == "large_result output · lines 201–250 → 50 of 3,000 lines"
        status = [
            e.text for e in events if isinstance(e, RunStatus) and "read_tool_result" in e.text
        ]
        assert status == ["Running read_tool_result · large_result output · lines 201–250…"]
        assert not any("toolu" in e.detail or "/" in e.detail for e in reads)

    asyncio.run(run())
