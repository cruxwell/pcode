import asyncio
import json
import os
import shlex
import signal
import sys
from unittest.mock import Mock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.shell import Shell

from pcode.agent import create_coder
from pcode.app import PreviewApp
from pcode.jobs import registry
from pcode.live import AgentRuntime
from pcode.runtime import CommandOutput, ToolSummary
from pcode.shell import preview_text
from pcode.shell_tools import JobShell


def command(source):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def runtime_for(workspace, source, timeout=5, background=False, session=None):
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: DeltaToolCall(
                    name="shell",
                    json_args=json.dumps(
                        {
                            "command": command(source),
                            "timeout": timeout,
                            "background": background,
                        }
                    ),
                )
            }
        else:
            assert calls == 2  # Output events do not add model calls.
            yield "Done."

    return AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(workspace)]), session
    )


def test_output_arrives_before_exit_and_final_result_is_unchanged(tmp_path):
    gate = tmp_path / "continue"
    runtime = runtime_for(
        tmp_path,
        "import pathlib, time, sys; print('FIRST'); print('ERROR', file=sys.stderr); "
        f"gate = pathlib.Path({str(gate)!r})\n"
        "while not gate.exists(): time.sleep(.02)\n"
        "print('LAST')",
    )
    runtime.tree.consume = Mock(wraps=runtime.tree.consume)

    async def run():
        events = []
        async with asyncio.timeout(10):
            async for event in runtime.stream("run"):
                events.append(event)
                if isinstance(event, CommandOutput) and "ERROR" in event.output:
                    assert not any(isinstance(e, ToolSummary) for e in events)
                    assert "FIRST" in event.output
                    gate.touch()
        result = next(e for e in events if isinstance(e, ToolSummary))
        preview = next(e for e in events if isinstance(e, CommandOutput))
        assert preview.call_id == result.call_id
        assert not result.failed
        # A command that finished carries its output and one status line: no
        # pid, no log paths, nothing to manage.
        assert result.result.startswith("FIRST\nERROR\nLAST\n[j1 · exit 0 · ")
        assert "wait_for_job" not in result.result
        assert result.process_id.isdecimal()
        assert all(
            call.args[0]["kind"] != "CommandOutput" for call in runtime.tree.consume.call_args_list
        )

    asyncio.run(run())


def test_preview_sanitizes_before_clipping_and_waits_for_complete_lines():
    assert preview_text("token=sk-synthetic") == ""
    assert "sk-0123456789ab" not in preview_text("token=sk-0123456789ab\n")
    assert preview_text("password = form.data\n") == "password = form.data"
    assert "private body" not in preview_text("-----BEGIN PRIVATE KEY-----\nprivate body\n")
    output = preview_text("\x1b[31m[bold]literal[/bold]\x1b[0m\n")
    assert "\x1b" not in output
    assert "[bold]literal[/bold]" in output
    assert "界" in preview_text("界\n")
    assert len(preview_text("x\n" * 100000)) <= 131072
    assert preview_text("last line", final=True) == "last line"
    assert "sk-0123456789ab" not in preview_text("token=sk-0123456789ab", final=True)


@pytest.mark.parametrize("background", [False, True])
def test_wait_limit_returns_live_job_and_clears_preview(tmp_path, background):
    runtime = runtime_for(
        tmp_path,
        "import time; print('READY'); time.sleep(60)",
        timeout=0.5,
        background=background,
    )

    async def run():
        app = PreviewApp(runtime=runtime)
        saw_preview = False
        pid = None
        try:
            async for event in runtime.stream("run"):
                if isinstance(event, CommandOutput) and event.output:
                    saw_preview = True
                    app.present_events((event,))
                    assert app.activity.command_outputs
                if isinstance(event, ToolSummary):
                    pid = int(event.process_id)
                    app.present_events((event,))
                    assert not app.activity.command_outputs
                    assert not event.failed
                    assert "still running" in event.detail
                    # A running command is the one case that needs handles.
                    assert "[j1 · running · pid " in event.result
                    assert 'wait_for_job("j1")' in event.result
                    os.kill(pid, 0)  # The wait ended, not the command.
            assert saw_preview is not background
            assert [job.id for job in runtime.jobs.running()] == ["j1"]
        finally:
            if pid is not None:
                os.killpg(pid, signal.SIGTERM)

    asyncio.run(run())


def test_coder_registers_job_tools_and_isolates_run_state(tmp_path):
    async def run():
        shell = next(c for c in create_coder(tmp_path).capabilities if isinstance(c, Shell))
        assert type(shell) is JobShell
        assert shell.tools == ["shell"]
        assert shell.default_timeout == 270
        base = shell.get_toolset()
        first = await base.for_run(None)
        second = await base.for_run(None)
        assert type(first) is type(base)
        assert first is not second
        assert set(first.tools) == {
            "shell",
            "wait_for_job",
            "job_output",
            "stop_job",
            "list_jobs",
        }
        # The registry is the one thing that must not be per-run: a job is
        # meant to outlive exactly that scope.
        assert first._jobs is second._jobs is registry()

    asyncio.run(run())


def child_pid_of(runtime, tmp_path, policy):
    """Cancel a stream mid-command under `policy`; return the command's pid."""

    async def run():
        pid = None
        ready = asyncio.Event()

        async def consume():
            nonlocal pid
            async for event in runtime.stream("run"):
                if isinstance(event, CommandOutput) and "CHILD_PID=" in event.output:
                    pid = int(event.output.split("CHILD_PID=")[1].splitlines()[0])
                    ready.set()

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), 10)
        finally:
            runtime.jobs.cancel_policy = policy
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert pid is not None
        return pid

    return asyncio.run(run())


def source_reporting_pid():
    return "import os, time; print(f'CHILD_PID={os.getpid()}'); time.sleep(60)"


def test_interrupting_a_wait_leaves_the_command_running(tmp_path):
    """A typed follow-up abandons the wait; the command is not its casualty."""
    runtime = runtime_for(tmp_path, source_reporting_pid())
    pid = child_pid_of(runtime, tmp_path, "detach")
    try:
        os.kill(pid, 0)
        job = runtime.jobs.get("j1")
        assert job.running and job.detached
        # Detaching is what makes the job worth announcing later: the model is
        # holding a handle it never asked for.
        assert runtime.jobs.take_announcements("model") == []
    finally:
        # The command's pid is not its group leader; the registry knows which
        # session to signal.
        runtime.jobs.stop_all()


def test_explicit_cancellation_stops_the_command(tmp_path):
    """Ctrl+C means stop working, so the command the turn waited on stops too."""
    runtime = runtime_for(tmp_path, source_reporting_pid())
    pid = child_pid_of(runtime, tmp_path, "stop")

    async def wait_for_exit():
        async with asyncio.timeout(10):
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                await asyncio.sleep(0.02)

    asyncio.run(wait_for_exit())
    assert runtime.jobs.get("j1").stopped


def test_parallel_commands_keep_their_own_call_identity(tmp_path):
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                i: DeltaToolCall(
                    name="shell",
                    tool_call_id=f"shell-{i}",
                    json_args=json.dumps(
                        {"command": command(f"import time; print('{name}'); time.sleep(.2)")}
                    ),
                )
                for i, name in enumerate(("ALPHA", "BETA"))
            }
        else:
            yield "Done."

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        events = [event async for event in runtime.stream("run both")]
        previews = [event for event in events if isinstance(event, CommandOutput)]
        results = {event.call_id: event for event in events if isinstance(event, ToolSummary)}
        assert {event.call_id for event in previews} == {"shell-0", "shell-1"}
        for event in previews:
            result = results[event.call_id]
            assert event.command == result.command
            assert result.result.startswith(event.output.strip())
        assert calls == 2

    asyncio.run(run())


def test_preview_cap_and_nonzero_status_come_from_upstream_events(tmp_path):
    runtime = runtime_for(tmp_path, "import sys; print('x' * 20000); sys.exit(7)")

    async def run():
        events = [event async for event in runtime.stream("run")]
        result = next(e for e in events if isinstance(e, ToolSummary))
        assert result.failed and "exit 7" in result.detail
        assert "preview capped" in result.detail
        assert "· exit 7 ·" in result.error
        previews = [e for e in events if isinstance(e, CommandOutput)]
        assert "Live preview capped" in previews[-1].output
        assert len(previews[-1].output) < 17000

    asyncio.run(run())


def test_result_marker_supersedes_an_earlier_running_event():
    """The event is a snapshot; the marker is written after the wait ended."""
    from pcode.tool_display import job_status

    running = "starting\n[j4 · running · pid 12 · 1.0s] Started in the background."
    assert job_status(running) == ("j4 · still running", False)
    # A command that exits between the event and the result must not be
    # reported as running just because the event said so.
    assert job_status("boom\n[j4 · exit 9 · 2.0s]") == ("j4 · exit 9", True)
    assert job_status("[j4 · exit 0 · 2.0s]") == ("", False)
    assert job_status("no marker here") == ("", False)


def test_a_key_is_masked_in_the_preview_and_its_clipped_tail_in_the_result(tmp_path):
    from pcode.sessions import SavedSession

    # The result keeps only the log's tail, past the key's header; only its end
    # marker identifies what is left. A middle cut by the spill preview has no
    # marker at all and is shown, as the model saw it.
    # Synthetic content, generated without putting its body in the command text.
    marker = "SYNTHETICPRIVATEBODY"
    source = (
        "import sys; print('-----BEGIN PRIVATE KEY-----'); "
        f"print(''.join(map(chr, {list(map(ord, marker))!r})) * 1000); "
        "print('-----END PRIVATE KEY-----'); sys.exit(3)"
    )
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = runtime_for(tmp_path, source, session=saved)

    async def run():
        try:
            events = [event async for event in runtime.stream("run")]
            result = next(e for e in events if isinstance(e, ToolSummary))
            assert result.failed
            assert "[private key redacted]\n\n[j1 · exit 3" in result.result
            assert f"{marker}\n-----END" not in result.result
            assert all(marker not in e.output for e in events if isinstance(e, CommandOutput))
        finally:
            runtime.close()

    asyncio.run(run())
