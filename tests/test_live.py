import asyncio
import os
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.capabilities import CombinedCapability, LocalWorkspace
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness import Coder
from rich.console import Console

from pcode.agent import create_agent, create_coder
from pcode.app import PreviewApp
from pcode.live import AgentRuntime, error_message
from pcode.runtime import Message, RunStatus, TextDelta, ToolStarted, ToolSummary
from pcode.ui import TerminalOutput, create_prompt


def test_codex_uses_native_model_with_only_wire_dialect_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)
    with (
        patch("pcode.agent.Agent") as constructor,
        patch("pydantic_ai.models.openai_codex.OpenAICodexModel") as model,
    ):
        create_agent("openai-codex:gpt-5.6-luna", tmp_path)
    model.assert_called_once_with(
        "gpt-5.6-luna", profile={"openai_supports_prompt_cache_breakpoints": False}
    )
    assert constructor.call_args.args == (model.return_value,)
    assert isinstance(constructor.call_args.kwargs["capabilities"][0], CombinedCapability)


def test_other_model_strings_are_passed_unchanged(tmp_path):
    with patch("pcode.agent.Agent") as constructor:
        create_agent("openai:example", tmp_path)
    assert constructor.call_args.args == ("openai:example",)


def test_stream_runs_real_coder_read_tool_and_retains_history(tmp_path):
    (tmp_path / "sample.txt").write_text("a unique workspace marker")
    requests = []

    async def model(messages, info):
        requests.append(messages)
        names = {tool.name for tool in info.function_tools}
        assert {"read_file", "edit_file", "shell"} <= names
        if len(requests) == 1:
            yield "Looking at the file."
            yield {0: DeltaToolCall(name="read_file", json_args='{"path":"sample.txt"}')}
        elif len(requests) == 2:
            results = [
                p for message in messages for p in message.parts if isinstance(p, ToolReturnPart)
            ]
            assert any("a unique workspace marker" in str(p.content) for p in results)
            yield "The file contains "
            yield "the workspace marker."
        else:
            assert len(messages) > len(requests[0])
            yield "I remember the previous answer."

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=model), capabilities=[LocalWorkspace(tmp_path), Coder()]
        )
    )

    async def run():
        events = [event async for event in runtime.stream("Read sample.txt")]
        started = next(event for event in events if isinstance(event, ToolStarted))
        completed = next(event for event in events if isinstance(event, ToolSummary))
        assert started.name == "read_file"
        assert started.detail == "sample.txt"
        assert started.call_id == completed.call_id
        assert events.index(started) < events.index(completed)

        assert [e.markdown for e in events if isinstance(e, Message)] == [
            "Looking at the file.",
            "The file contains the workspace marker.",
        ]
        assert any(isinstance(e, TextDelta) for e in events)
        assert any(isinstance(e, RunStatus) and "read_file" in e.text for e in events)
        assert any(isinstance(e, ToolSummary) and e.name == "read_file" for e in events)
        assert runtime.turns == 1
        assert runtime.history
        assert runtime.input_tokens > 0
        followup = [event async for event in runtime.stream("What did you read?")]
        assert Message("I remember the previous answer.") in followup
        assert runtime.turns == 2
        old_id = runtime.conversation_id
        runtime.reset()
        assert runtime.history == []
        assert runtime.turns == 0
        assert runtime.conversation_id != old_id

    asyncio.run(run())


@pytest.mark.parametrize("outside_path", ["absolute", "relative", "symlink"])
def test_coder_file_paths_are_unconfined_but_workspace_relative(tmp_path, outside_path):
    import json

    from pydantic_ai_harness.filesystem import FileSystem
    from pydantic_ai_harness.repo_context import RepoContext
    from pydantic_ai_harness.shell import Shell

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside marker")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside marker")
    (workspace / "link.txt").symlink_to(outside)
    requested_path = {
        "absolute": str(outside),
        "relative": "../outside.txt",
        "symlink": "link.txt",
    }[outside_path]
    coder = create_coder(workspace)
    assert next(c for c in coder.capabilities if isinstance(c, Shell)).workdir == workspace
    local = next(c for c in coder.capabilities if isinstance(c, LocalWorkspace))
    assert Path(local.working_dir) == workspace
    context = next(c for c in coder.capabilities if isinstance(c, RepoContext))
    assert context.root == workspace
    filesystem = next(c for c in coder.capabilities if isinstance(c, FileSystem))
    # The workspace remains the base, not an access boundary.
    assert filesystem.root_dir == "/"
    assert all(pattern.startswith("**/") for pattern in filesystem.read_only_patterns)
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: DeltaToolCall(name="read_file", json_args=json.dumps({"path": requested_path}))
            }
        elif requests == 2:
            parts = [p for message in messages for p in message.parts]
            assert any("outside marker" in str(getattr(p, "content", "")) for p in parts)
            assert not any(isinstance(p, RetryPromptPart) for p in parts)
            yield {0: DeltaToolCall(name="read_file", json_args=json.dumps({"path": "inside.txt"}))}
        else:
            results = [
                p for message in messages for p in message.parts if isinstance(p, ToolReturnPart)
            ]
            assert any("inside marker" in str(p.content) for p in results)
            yield "Finished"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model), capabilities=[coder]))

    async def run():
        events = [event async for event in runtime.stream("Read inside and outside the repository")]
        assert Message("Finished") in events

    asyncio.run(run())


def test_coder_allows_all_commands_by_default(tmp_path):
    from pydantic_ai_harness.shell import Shell
    from pydantic_ai_harness.shell._capability import LLM_API_KEY_ENV_PATTERNS

    from pcode.shell_tools import JobShell

    coder = create_coder(tmp_path)
    shell = next(c for c in coder.capabilities if isinstance(c, Shell))
    assert not shell.allowed_commands
    assert not shell.denied_commands
    assert not shell.denied_operators
    assert shell.allow_interactive
    assert type(shell) is JobShell
    assert list(shell.get_toolset().tools) == [
        "shell",
        "wait_for_job",
        "job_output",
        "stop_job",
        "list_jobs",
    ]
    # Commands get the whole host environment, minus provider credentials.
    assert list(shell.denied_env_patterns) == list(LLM_API_KEY_ENV_PATTERNS)
    # direnv's banner would otherwise corrupt piped command output.
    assert shell.env["DIRENV_LOG_FORMAT"] == ""
    assert shell.env["PATH"] == os.environ["PATH"]
    env = {**shell.env, "ANTHROPIC_API_KEY": "synthetic", "KEEP_ME": "1"}
    resolved = replace(shell, env=env).get_toolset()._resolve_env()
    assert "ANTHROPIC_API_KEY" not in resolved
    assert resolved["KEEP_ME"] == "1"


def test_a_run_writes_into_the_turn_context_it_was_given():
    """Turn state reaches the turn, not the runtime.

    One turn runs at a time today, so the runtime hands `_stream` its active
    context. What this pins is that nothing inside the run reaches around that
    argument: given another context, the run fills that one and leaves the
    active branch alone.
    """
    from pcode.turn import TurnContext

    async def model(messages, info):
        yield "An answer for the branch."

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    runtime.auto_compact = True  # AutoCompaction then snapshots into the given context.
    branch = TurnContext(run_id="branch")

    async def run():
        events = [event async for event in runtime._stream("question", branch)]
        assert Message("An answer for the branch.") in events
        # The run's history, its request snapshot and what the footer reads.
        assert [
            part.content
            for message in branch.history
            for part in message.parts
            if isinstance(part, UserPromptPart)
        ] == ["question"]
        assert branch.context_history is not None
        assert branch.checkpoint.step > 0
        assert runtime.context is not branch
        assert runtime.history == []
        assert runtime.context_history is None

    asyncio.run(run())


def test_stream_has_no_model_request_limit():
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        if requests <= 55:
            yield {0: DeltaToolCall(name="noop", json_args="{}")}
        else:
            yield "Finished beyond both the old and library-default caps."

    agent = Agent(FunctionModel(stream_function=model))

    @agent.tool_plain
    def noop() -> str:
        return "ok"

    runtime = AgentRuntime(agent)

    async def run():
        events = [event async for event in runtime.stream("Run a long tool loop")]
        assert Message("Finished beyond both the old and library-default caps.") in events
        assert requests == 56
        assert runtime.turns == 1

    asyncio.run(run())


def test_failed_stream_keeps_request_but_not_partial_response():
    async def broken(messages, info):
        yield "Partial"
        raise RuntimeError("sensitive provider body")

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=broken)))

    async def run():
        with pytest.raises(Exception):
            _ = [event async for event in runtime.stream("hello")]
        assert len(runtime.history) == 1
        assert runtime.history[0].parts[0].content == "hello"
        assert runtime.turns == 0

    asyncio.run(run())
    assert "sensitive provider body" not in error_message(RuntimeError("sensitive provider body"))


def test_ui_stream_commits_final_message_only_once():
    async def model(messages, info):
        yield "hello "
        yield "from the model"

    output = StringIO()
    app = PreviewApp(
        model="test:local",
        runtime=AgentRuntime(Agent(FunctionModel(stream_function=model))),
        console=Console(file=output, color_system=None),
    )

    async def run():
        with create_pipe_input() as pipe:
            session = create_prompt(
                app.registry, activity=app.activity, input=pipe, output=DummyOutput()
            )
            writer = TerminalOutput(app.transcript.console, session.app)
            app.transcript.output = writer
            assert await asyncio.wait_for(app.run_live(writer, "hello"), timeout=5)
            await writer.flush()
        assert not app.activity.busy
        assert output.getvalue().count("hello from the model") == 1
        assert app.runtime.turns == 1

    asyncio.run(run())


def test_ui_cancellation_cleans_up_generation_and_accepts_next_input():
    output = StringIO()
    cleaned_up = []

    async def run():
        started = asyncio.Event()

        async def model(messages, info):
            try:
                yield "unfinished answer"
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaned_up.append(True)

        app = PreviewApp(
            model="test:local",
            runtime=AgentRuntime(Agent(FunctionModel(stream_function=model))),
            console=Console(file=output, color_system=None),
        )
        with create_pipe_input() as pipe:
            session = create_prompt(
                app.registry, activity=app.activity, input=pipe, output=DummyOutput()
            )
            writer = TerminalOutput(app.transcript.console, session.app)
            app.transcript.output = writer
            task = asyncio.create_task(app.run_live(writer, "start"))
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            assert not await asyncio.wait_for(task, timeout=5)
            await writer.flush()
            assert cleaned_up
            assert not app.activity.busy
            assert len(app.runtime.history) == 1
            assert app.runtime.history[0].parts[0].content == "start"
            pipe.send_text("next input\r")
            assert await session.prompt_async() == "next input"
        assert "Run cancelled" in output.getvalue()

    asyncio.run(run())


def test_thinking_is_streamed_as_events_separate_from_answer():
    from pydantic_ai.models.function import DeltaThinkingPart

    from pcode.runtime import Thinking, ThinkingDelta

    async def model(messages, info):
        yield {0: DeltaThinkingPart(content="Visible reasoning")}
        yield {0: DeltaThinkingPart(content=" continued", signature="OPAQUE_SIGNATURE")}
        yield "Public **answer**"

    async def run():
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
        try:
            events = [event async for event in runtime.stream("hello")]
            assert (
                "".join(e.text for e in events if isinstance(e, ThinkingDelta))
                == "Visible reasoning continued"
            )
            assert Thinking("Visible reasoning continued") in events
            assert RunStatus("Thinking…") in events
            assert "OPAQUE_SIGNATURE" not in repr(events)
            assert Message("Public **answer**") in events
        finally:
            runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["done", "error", "cancel"])
@pytest.mark.parametrize("shown", [True, False])
def test_thinking_scrollback_survives_turn_end_and_is_retained_when_hidden(outcome, shown):
    from pydantic_ai.models.function import DeltaThinkingPart

    async def run():
        async def model(messages, info):
            yield {0: DeltaThinkingPart(content="REASONING_SENTINEL")}
            if outcome == "error":
                raise RuntimeError("model failed")
            if outcome == "cancel":
                raise asyncio.CancelledError
            yield "Public answer"

        output = StringIO()
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
        app = PreviewApp(
            model="test:local", runtime=runtime, console=Console(file=output, color_system=None)
        )
        app.activity.thinking_mode = "scrollback" if shown else "off"
        try:
            with create_pipe_input() as pipe:
                session = create_prompt(
                    app.registry, activity=app.activity, input=pipe, output=DummyOutput()
                )
                writer = TerminalOutput(app.transcript.console, session.app)
                app.transcript.output = writer
                assert await app.run_live(writer, "hello") == (outcome == "done")
                await writer.flush()
            assert ("REASONING_SENTINEL" in output.getvalue()) is shown
            assert "REASONING_SENTINEL" in repr(app.transcript.log.entries)
            assert not writer._thinking_tail
            app.activity.thinking_mode = "scrollback"
            assert "REASONING_SENTINEL" in repr(app.transcript.replay())
            app.activity.thinking_mode = "off"
            assert "REASONING_SENTINEL" not in repr(app.transcript.replay())
        finally:
            runtime.close()

    asyncio.run(run())


def test_thinking_block_boundaries_reach_events():
    from pydantic_ai.models.function import DeltaThinkingPart

    from pcode.runtime import Thinking

    async def model(messages, info):
        yield {0: DeltaThinkingPart(content="**First summary**")}
        yield {1: DeltaThinkingPart(content="**Second ")}
        yield {1: DeltaThinkingPart(content="summary**")}
        yield "Public answer"

    async def run():
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
        try:
            events = [event async for event in runtime.stream("hello")]
            assert [e.text for e in events if isinstance(e, Thinking)] == [
                "**First summary**",
                "**Second summary**",
            ]
        finally:
            runtime.close()

    asyncio.run(run())
