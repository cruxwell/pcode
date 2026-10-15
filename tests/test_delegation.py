import asyncio
import json
import re
from copy import deepcopy

import pytest
from pydantic_ai import Agent, FunctionToolset
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.subagents import SubAgent, SubAgents

from pcode.agent import create_coder
from pcode.claude_sdk import ClaudeConnectionError
from pcode.delegation import DelegationReporting, _parent, stream_child_activity
from pcode.live import AgentRuntime
from pcode.runtime import ChildPlan, Message, PlanUpdated, TextDelta, ToolStarted, ToolSummary
from pcode.tool_display import assignment, delegation_detail, target
from pcode.tool_panel import ToolCall, ToolHistory, panel_fragments, task_panel_rows


def returns(messages):
    return [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


def delegate(index=0, agent_name="explorer"):
    return DeltaToolCall(
        name="delegate_task",
        json_args=json.dumps({"agent_name": agent_name, "task": f"Read sample.txt ({index})"}),
        tool_call_id=f"parent-{index}",
    )


def delegate_started(agent, task, call_id, **fields):
    """A delegation's start, as the runtime reports it."""
    return ToolStarted(
        "delegate_task", f"{agent} · {task}", call_id, agent=agent, task=task, **fields
    )


def test_real_parallel_children_are_correlated_streamed_and_inspectable(tmp_path):
    (tmp_path / "sample.txt").write_text("workspace evidence")

    async def model(messages, info):
        names = {t.name for t in info.function_tools}
        if "delegate_task" in names:
            if returns(messages):
                yield "Parent answer"
            else:
                yield {0: delegate(0, "worker"), 1: delegate(1, "worker")}
        else:
            assert "read_file" in names
            assert "shell" in names
            assert {"edit_file", "write_file"} <= names
            if returns(messages):
                yield "PRIVATE CHILD ANSWER"
            else:
                yield "PRIVATE CHILD PROSE"
                yield {
                    0: DeltaToolCall(
                        name="read_file",
                        json_args='{"path":"sample.txt"}',
                        tool_call_id="same-child-id",
                    )
                }

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        events = [e async for e in runtime.stream("Explore in parallel")]
        assert _parent.get() is None
        assert [e.markdown for e in events if isinstance(e, Message)] == ["Parent answer"]
        assert "PRIVATE" not in "".join(e.text for e in events if isinstance(e, TextDelta))
        children = [e for e in events if isinstance(e, ToolStarted) and e.parent_call_id]
        assert {e.parent_call_id for e in children} == {"parent-0", "parent-1"}
        assert {e.call_id for e in children} == {"parent-0:same-child-id", "parent-1:same-child-id"}
        for child in children:
            assert child.detail == "sample.txt"
            finished = next(
                e for e in events if isinstance(e, ToolSummary) and e.call_id == child.call_id
            )
            assert finished.parent_call_id == child.parent_call_id
            assert "workspace evidence" in finished.result
            assert finished.run_id == child.run_id
        parents = [e for e in events if isinstance(e, ToolSummary) and e.name == "delegate_task"]
        assert len(parents) == 2
        assert all(e.outcome == "ok" and not e.failed and "Completed" in e.detail for e in parents)
        assert all(e.result == "PRIVATE CHILD ANSWER" for e in parents)
        assert {
            e.activity for e in events if isinstance(e, ToolStarted) and e.name == "delegate_task"
        } >= {"Waiting for model", "Responding", "Working"}
        # The panel names the agent from its real arguments, not by parsing `detail`.
        assert {
            (e.agent, e.task)
            for e in events
            if isinstance(e, ToolStarted) and e.name == "delegate_task"
        } == {("worker", "Read sample.txt (0)"), ("worker", "Read sample.txt (1)")}
        assert len(runtime.inspections.calls) == 4  # Phase changes don't create new calls.
        assert all(c.state == "succeeded" for c in runtime.inspections.calls)

    asyncio.run(run())


DROPPED = "API Error: Connection dropped (ECONNRESET) secret-body"


def worker_runtime(tmp_path, child):
    """A real coder whose delegated workers answer from `child(messages, info)`."""

    async def model(messages, info):
        if "delegate_task" in {t.name for t in info.function_tools}:
            if returns(messages) or any(
                isinstance(p, RetryPromptPart) for m in messages for p in m.parts
            ):
                yield "Parent answer"
            else:
                yield {0: delegate(0, "worker"), 1: delegate(1, "worker")}
            return
        async for chunk in child(messages, info):
            yield chunk

    return AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )


def delegations(events):
    return {
        e.call_id: e for e in events if isinstance(e, ToolSummary) and e.name == "delegate_task"
    }


def test_a_dropped_child_request_is_resent_without_rerunning_its_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("pcode.retries.RETRY_DELAY", 0)
    (tmp_path / "sample.txt").write_text("workspace evidence")
    sent = []

    async def child(messages, info):
        if "(1)" in messages[0].parts[-1].content:
            yield "Sibling answer"
            return
        sent.append(deepcopy(messages))
        if not returns(messages):
            yield {
                0: DeltaToolCall(
                    name="read_file", json_args='{"path":"sample.txt"}', tool_call_id="read"
                )
            }
        elif len(sent) == 2:
            raise ClaudeConnectionError("test", DROPPED)
        else:
            yield "Child answer"

    async def run():
        events = [e async for e in worker_runtime(tmp_path, child).stream("Explore")]
        ended = delegations(events)
        assert ended["parent-0"].outcome == "ok"
        assert ended["parent-0"].result == "Child answer"
        reads = [e for e in events if isinstance(e, ToolSummary) and e.name == "read_file"]
        assert len(reads) == 1
        assert "Reconnecting 1/3" in {
            e.activity for e in events if isinstance(e, ToolStarted) and e.call_id == "parent-0"
        }

    asyncio.run(run())
    # The failed request, which carried the tool's result, went again as sent
    # (pydantic-ai restamps a request each time it goes out).
    assert len(sent) == 3
    assert [m.parts for m in sent[2]] == [m.parts for m in sent[1]]
    assert "workspace evidence" in returns(sent[1])[-1].model_response_str()


def test_a_crashed_worker_leaves_the_turn_and_its_siblings_running(tmp_path):
    crashed = asyncio.Event()
    attempts = []

    async def child(messages, info):
        if "(0)" in messages[0].parts[-1].content:
            attempts.append(1)
            crashed.set()
            raise RuntimeError(f"private crash {DROPPED}")
        # Still waiting on its model when its sibling crashes.
        await crashed.wait()
        await asyncio.sleep(0)
        yield "Sibling answer"

    async def run():
        events = [e async for e in worker_runtime(tmp_path, child).stream("Explore")]
        assert [e.markdown for e in events if isinstance(e, Message)] == ["Parent answer"]
        ended = delegations(events)
        assert {k: e.outcome for k, e in ended.items()} == {
            "parent-0": "contained",
            "parent-1": "ok",
        }
        assert "secret-body" not in ended["parent-0"].result

    asyncio.run(run())
    # Not a dropped connection, so it is not resent.
    assert attempts == [1]


@pytest.mark.parametrize("where", ["model", "tool"])
def test_a_drop_is_resent_only_from_the_request_and_only_so_often(tmp_path, monkeypatch, where):
    """Past its retries a drop is contained; a drop inside a tool is never resent."""
    monkeypatch.setattr("pcode.retries.RETRY_DELAY", 0)
    attempts = []

    async def dropped():
        raise ClaudeConnectionError("test", DROPPED)

    async def child(messages, info):
        if "(1)" in messages[0].parts[-1].content:
            yield "Sibling answer"
            return
        attempts.append(1)
        if where == "model":
            raise ClaudeConnectionError("test", DROPPED)
        yield {0: DeltaToolCall(name="dropped", json_args="{}", tool_call_id="drop")}

    if where == "tool":
        monkeypatch.setattr("pcode.agent.WorkerRuntimeTools", lambda: FunctionToolset([dropped]))
    runtime = worker_runtime(tmp_path, child)

    async def run():
        events = [e async for e in runtime.stream("Explore")]
        ended = delegations(events)
        assert ended["parent-0"].outcome == "contained"
        assert ended["parent-1"].outcome == "ok"
        assert "secret-body" not in ended["parent-0"].result
        return {
            e.activity
            for e in events
            if isinstance(e, ToolStarted) and e.call_id == "parent-0" and e.activity
        }

    activities = asyncio.run(run())
    if where == "model":
        assert len(attempts) == 4
        assert {"Reconnecting 1/3", "Reconnecting 3/3"} <= activities
    else:
        assert len(attempts) == 1
        assert not any(a.startswith("Reconnecting") for a in activities)


@pytest.mark.parametrize("outcome", ["timeout", "budget", "failed", "contained"])
def test_real_soft_outcomes_are_not_reported_as_success(outcome):
    async def child_model(messages, info):
        if outcome == "timeout":
            await asyncio.sleep(60)
        elif outcome == "failed":
            raise UnexpectedModelBehavior("private error")
        else:
            raise RuntimeError("private crash")
        yield "unreachable"

    child = SubAgent(
        Agent(FunctionModel(stream_function=child_model), name="explorer"),
        timeout_seconds=0.01 if outcome == "timeout" else None,
        usage_limits=UsageLimits(request_limit=0) if outcome == "budget" else None,
        on_failure="Fallback observation" if outcome == "failed" else None,
        contain_errors=outcome == "contained",
    )
    calls = 0

    async def parent_model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: delegate()}
        else:
            yield "Recovered"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=parent_model),
            capabilities=[
                SubAgents(
                    agents=[child], agent_folders=None, event_stream_handler=stream_child_activity
                ),
                DelegationReporting(),
            ],
        )
    )

    async def run():
        events = [e async for e in runtime.stream("Explore")]
        summary = next(e for e in events if isinstance(e, ToolSummary))
        assert summary.failed
        assert summary.outcome == outcome
        assert "private" not in summary.detail
        assert (
            summary.detail
            == delegation_detail(
                {"agent_name": "explorer", "task": "Read sample.txt (0)"}, outcome
            )[0]
        )
        assert _parent.get() is None

    asyncio.run(run())


def test_delegation_display_sanitizes_and_bounds_assignment():
    detail = target(
        "delegate_task",
        {"agent_name": "explorer\x1b", "task": "token=sk-" + "x" * 80 + "\n" + "long " * 100},
    )
    assert "\x1b" not in detail and "\n" not in detail
    assert len(detail) < 225
    assert "agent unavailable" in target("delegate_task", {})


def test_a_stated_purpose_labels_the_delegation_instead_of_the_task_opening():
    args = {"agent_name": "worker", "task": "Repository: /repo\nFix it"}
    args["purpose"] = " fixing\x1b it "
    assert assignment("delegate_task", args) == ("worker", "fixing it")
    assert target("delegate_task", args) == "worker · fixing it"
    assert delegation_detail(args, "ok") == ("worker · fixing it → Completed", False)
    args["purpose"] = "  "
    assert assignment("delegate_task", args)[1].startswith("Repository: /repo")


def test_active_delegations_are_pinned_and_their_calls_stay_off_the_panel():
    history = ToolHistory()
    history.record(delegate_started("explorer", "investigate", "parent"))
    for i in range(20):
        history.record(ToolStarted("read_file", f"file-{i}", str(i)))
        history.record(ToolSummary("read_file", f"file-{i}", call_id=str(i)))
    history.record(ToolStarted("read_file", "child.py", "parent:child", parent_call_id="parent"))
    history.record(ToolStarted("read_file", "newest.py", "status-row"))
    assert [c.event.call_id for c in history.calls] == ["parent", "parent:child", "status-row"]
    for budget in range(1, 10):
        rows = task_panel_rows([], history, budget, "⟳")
        assert len(rows) <= min(3, budget)
        assert "Explorer" in rows[0][1]
        lines = "".join(text for _, text in panel_fragments(rows, 20)).split("\n")
        assert len(lines) == len(rows)
    # The delegate's own calls stay on the status row, never under it.
    assert [text for _, text in history.rows(3)] == [history.calls[0].line()]
    # Finishing the plan must not hide a still-running child agent.
    assert any(
        "Explorer" in text
        for _, text in task_panel_rows(
            [{"content": "done", "status": "completed"}], history, 4, "⟳"
        )
    )
    history.clear()
    assert history.calls == []


def test_child_calls_never_get_panel_rows_and_the_delegate_never_takes_the_status_row():
    """Child calls settle fast, so a row for each would strobe under the delegate."""
    history = ToolHistory()
    history.record(delegate_started("explorer", "investigate", "parent"))
    # Alone, the delegate stays on the panel and leaves the status row free.
    assert history.active is None
    assert len(history.delegates) == 1
    history.record(ToolStarted("read_file", "child.py", "parent:child", parent_call_id="parent"))
    history.record(ToolStarted("grep", "newest", "status-row"))
    assert all("child.py" not in text for _, text in history.rows(3))
    history.record(ToolSummary("read_file", "child.py", call_id="parent:child"))
    assert [c.event.call_id for c in history.calls] == ["parent", "status-row"]
    # The status row keeps reporting running work, not the settled child.
    assert history.active is not None and history.active.event.call_id == "status-row"
    history.record(ToolSummary("grep", "newest", call_id="status-row"))
    history.record(ToolStarted("read_file", "next.py", "parent:next", parent_call_id="parent"))
    assert history.active.event.call_id == "parent:next"
    history.record(ToolSummary("read_file", "next.py", call_id="parent:next"))
    assert all("next.py" not in text for _, text in history.rows(3))


def test_parallel_parents_take_priority_over_child_chatter():
    history = ToolHistory()
    for i in range(4):
        history.record(delegate_started(f"explorer-{i}", "look", str(i)))
        history.record(ToolStarted("read_file", "file", f"{i}:child", parent_call_id=str(i)))
    history.record(ToolStarted("read_file", "newest.py", "status-row"))
    rows = history.rows(3)
    assert len(rows) == 3
    assert all("» Explorer-" in text and "Read" not in text for _, text in rows)


def test_timeout_settles_inflight_child_tool_before_parent_resumes():
    stopped = False

    async def hold():
        nonlocal stopped
        try:
            await asyncio.sleep(60)
        finally:
            stopped = True

    async def child_model(messages, info):
        yield {0: DeltaToolCall(name="hold", json_args="{}", tool_call_id="held")}

    async def parent_model(messages, info):
        if returns(messages):
            yield "Recovered"
        else:
            yield {0: delegate()}

    child = Agent(FunctionModel(stream_function=child_model), name="explorer", tools=[hold])
    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=parent_model),
            capabilities=[
                SubAgents(
                    agents=[SubAgent(child, timeout_seconds=0.05)],
                    agent_folders=None,
                    event_stream_handler=stream_child_activity,
                ),
                DelegationReporting(),
            ],
        )
    )

    async def run():
        events = [e async for e in runtime.stream("Explore")]
        child_end = next(e for e in events if isinstance(e, ToolSummary) and e.parent_call_id)
        assert child_end.outcome == "interrupted" and child_end.failed
        parent_end = next(e for e in events if isinstance(e, ToolSummary) and not e.parent_call_id)
        assert parent_end.outcome == "timeout"
        assert events.index(child_end) < events.index(parent_end)
        assert stopped

    asyncio.run(run())


@pytest.mark.parametrize("crash", [False, True])
def test_cancel_or_propagated_crash_cleans_up_real_child_and_panel(crash):
    from io import StringIO
    from types import SimpleNamespace

    from prompt_toolkit.output import DummyOutput
    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.ui import CursorSafeOutput, TerminalOutput

    async def run():
        gate = asyncio.Event()
        stopped = asyncio.Event()

        async def hold():
            try:
                await gate.wait()
                raise RuntimeError("child crashed")
            finally:
                stopped.set()

        async def child_model(messages, info):
            yield {0: DeltaToolCall(name="hold", json_args="{}", tool_call_id="held")}

        async def parent_model(messages, info):
            yield {0: delegate()}

        child = Agent(FunctionModel(stream_function=child_model), name="explorer", tools=[hold])
        runtime = AgentRuntime(
            Agent(
                FunctionModel(stream_function=parent_model),
                capabilities=[
                    SubAgents(
                        agents=[SubAgent(child)],
                        agent_folders=None,
                        event_stream_handler=stream_child_activity,
                    ),
                    DelegationReporting(),
                ],
            )
        )
        app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))
        output = TerminalOutput(
            app.transcript.console,
            SimpleNamespace(
                output=CursorSafeOutput(DummyOutput()),
                invalidate=lambda: None,
            ),
        )
        task = asyncio.create_task(app.run_live(output, "Explore"))
        try:
            async with asyncio.timeout(3):
                while not any(c.event.parent_call_id for c in app.activity.tools.calls):
                    await asyncio.sleep(0.001)
            if crash:
                gate.set()
            else:
                task.cancel()
            assert not await task
            assert stopped.is_set()
            assert app.activity.tools.calls == []
        finally:
            if not task.done():
                task.cancel()
                await task

    asyncio.run(run())


def test_replay_keeps_child_identity_and_interrupted_delegation(tmp_path):
    from io import StringIO
    from types import SimpleNamespace

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.sessions import SavedSession

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        saved.append("turn_started", prompt="Explore")
        saved.event(delegate_started("explorer", "Explore", "parent", activity="Thinking"))
        saved.event(ToolStarted("read_file", "file.py", "child", parent_call_id="parent"))
        saved.append("turn_cancelled")
        app = PreviewApp(
            model="test:local",
            runtime=SimpleNamespace(session=saved),
            console=Console(file=StringIO()),
        )
        app.replay()
        # A cancelled turn leaves nothing running, so the panel starts empty.
        assert app.activity.tools.calls == []
    finally:
        saved.close()


def test_rejected_delegation_has_no_false_success():
    async def child_model(messages, info):
        raise AssertionError("A refused child must not run")
        yield "unreachable"

    async def parent_model(messages, info):
        if returns(messages):
            yield "Use existing evidence"
        else:
            yield {0: delegate()}

    child = Agent(FunctionModel(stream_function=child_model), name="explorer")
    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=parent_model),
            capabilities=[
                SubAgents(
                    agents=[SubAgent(child, max_calls=0)],
                    agent_folders=None,
                    event_stream_handler=stream_child_activity,
                ),
                DelegationReporting(),
            ],
        )
    )

    async def run():
        events = [e async for e in runtime.stream("Explore")]
        summary = next(e for e in events if isinstance(e, ToolSummary))
        assert summary.failed and summary.outcome == "not_started"
        assert "Not started" in summary.detail

    asyncio.run(run())


def test_an_interrupted_delegate_leaves_the_panel():
    history = ToolHistory()
    history.record(delegate_started("explorer", "investigate", "parent"))
    history.record(ToolStarted("read_file", "child.py", "parent:child", parent_call_id="parent"))
    history.clear()
    assert history.calls == []
    assert history.rows(3) == []


@pytest.mark.parametrize("failed", [False, True])
def test_a_finished_delegate_leaves_the_panel_without_taking_the_status_row(failed, monkeypatch):
    """Scrollback keeps its outcome; the status row only ever reports tool calls."""
    # The hold is timed; a loaded run must not outlast it before the assertion.
    monkeypatch.setattr("pcode.tool_panel.STATUS_DWELL", 1e9)
    history = ToolHistory()
    history.record(delegate_started("explorer", "investigate", "parent"))
    history.record_plan("parent", [{"content": "Look", "status": "completed"}])
    history.record(ToolStarted("read_file", "child.py", "parent:child", parent_call_id="parent"))
    history.record(ToolSummary("delegate_task", "explorer → Done", call_id="parent", failed=failed))
    # Its plan and its own calls go with it.
    assert history.calls == [] and history.plans == {}
    assert task_panel_rows([], history, 10, "○") == []
    assert not history.animating
    # The child call it took with it never held the status row either.
    assert history.active is None
    state = "Failed" if failed else "Done"
    finished = ToolCall(delegate_started("explorer", "investigate", "x"), settled=1.0)
    finished.failed = failed
    assert re.fullmatch(rf"» Explorer · {state} · investigate", finished.line(timed=False))


def test_a_finished_delegate_leaves_its_siblings_running():
    history = ToolHistory()
    for call_id in ("done", "live"):
        history.record(delegate_started("worker", call_id, call_id))
        history.record_plan(call_id, [{"content": "Look", "status": "in_progress"}])
        history.record(
            ToolStarted("read_file", f"{call_id}.py", f"{call_id}:child", parent_call_id=call_id)
        )
    history.record(ToolSummary("delegate_task", "done", call_id="done"))
    assert [c.event.call_id for c in history.calls] == ["live", "live:child"]
    assert list(history.plans) == ["live"]
    # A late plan for the finished one does not bring it back.
    history.record_plan("done", [{"content": "Late", "status": "pending"}])
    assert list(history.plans) == ["live"]


def test_child_command_carries_what_it_ran(tmp_path):
    async def model(messages, info):
        names = {t.name for t in info.function_tools}
        if "delegate_task" in names:
            yield "Done" if returns(messages) else {0: delegate(0, "worker")}
        elif returns(messages):
            yield "Child done"
        else:
            yield {
                0: DeltaToolCall(
                    name="shell",
                    json_args='{"command":"echo child-ran","purpose":"say hello"}',
                    tool_call_id="child-shell",
                )
            }

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        events = [e async for e in runtime.stream("Delegate a command")]
        started, finished = (
            next(e for e in events if isinstance(e, kind) and e.call_id == "parent-0:child-shell")
            for kind in (ToolStarted, ToolSummary)
        )
        for event in (started, finished):
            assert event.command == "echo child-ran"
            assert event.purpose == "say hello"
        assert started.execution == "foreground"

    asyncio.run(run())


def test_child_calls_are_written_indented_under_their_delegate():
    from io import StringIO

    from rich.console import Console

    from pcode.ui import Transcript

    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    for event in (
        ToolSummary("read_file", "a.py → 3 lines", call_id="p:r", parent_call_id="p"),
        ToolSummary(
            "shell",
            "echo hi",
            call_id="p:s",
            command="echo hi",
            parent_call_id="p",
        ),
        ToolSummary("delegate_task", "worker · look → Completed", call_id="p"),
    ):
        transcript.tool_result(event)
    lines = stream.getvalue().splitlines()
    assert lines == [
        "✓ Delegate task  worker · look → Completed",
        "    ✓ Read file  a.py → 3 lines",
        "    ✓ Run shell · echo hi",
    ]
    # A redraw rebuilds the same grouping from the retained log.
    assert [
        line.rstrip()
        for objects, _, _ in transcript.replay()
        for line in _render(objects).splitlines()
    ] == lines


def test_orphaned_child_calls_are_not_lost_when_the_turn_is_cancelled():
    from io import StringIO

    from rich.console import Console

    from pcode.ui import Transcript

    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    transcript.tool_result(
        ToolSummary("read_file", "a.py → 3 lines", call_id="p:r", parent_call_id="p")
    )
    assert stream.getvalue() == ""
    transcript.cancelled()
    assert "✓ Read file  a.py → 3 lines" in stream.getvalue()


def _render(objects):
    from io import StringIO

    from rich.console import Console

    console = Console(file=StringIO(), width=80, color_system=None)
    console.print(*objects)
    return console.file.getvalue()


def test_a_child_plan_reaches_the_parent_without_touching_its_plan(tmp_path):
    async def model(messages, info):
        names = {t.name for t in info.function_tools}
        if "delegate_task" in names:
            yield "Done" if returns(messages) else {0: delegate(0, "worker")}
        elif returns(messages):
            yield "Child done"
        else:
            items = [
                {"content": "Read the code", "status": "completed"},
                {"content": "Fix the bug", "status": "in_progress"},
            ]
            yield {
                0: DeltaToolCall(
                    name="write_plan",
                    json_args=json.dumps({"items": items}),
                    tool_call_id="child-plan",
                )
            }

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        events = [e async for e in runtime.stream("Delegate with a plan")]
        plans = [e for e in events if isinstance(e, ChildPlan)]
        assert [p.call_id for p in plans] == ["parent-0"]
        assert [(i["content"], i["status"]) for i in plans[0].items] == [
            ("Read the code", "completed"),
            ("Fix the bug", "in_progress"),
        ]
        assert not any(isinstance(e, PlanUpdated) for e in events)
        assert await runtime.plan_store.get_items() == []

    asyncio.run(run())


def test_a_delegate_shows_its_plan_but_not_its_calls():
    history = ToolHistory()
    history.record(delegate_started("worker", "fix it", "parent"))
    history.record_plan(
        "parent",
        [
            {"content": "Read the code", "status": "completed"},
            {"content": "Fix the bug", "status": "in_progress"},
            {"content": "Run the tests", "status": "pending"},
            {"content": "Report back", "status": "pending"},
        ],
    )
    history.record(ToolStarted("read_file", "child.py", "parent:child", parent_call_id="parent"))
    history.record(ToolStarted("grep", "newest", "status-row"))
    rows = [text for _, text in task_panel_rows([], history, 10, "*")]
    assert rows[0].startswith("» Worker · ") and " · Starting · fix it" in rows[0]
    assert rows[1:] == [
        "├── ✔ Read the code",
        "├── * Fix the bug",
        "└── ○ Run the tests",
    ]
    # The plan stays, and keeps updating, once the calls have settled.
    history.record(ToolSummary("read_file", "child.py", call_id="parent:child"))
    history.record(ToolSummary("grep", "newest", call_id="status-row"))
    history.record_plan(
        "parent",
        [
            {"content": "Read the code", "status": "completed"},
            {"content": "Fix the bug", "status": "completed"},
            {"content": "Run the tests", "status": "in_progress"},
            {"content": "Report back", "status": "pending"},
        ],
    )
    assert [text for _, text in task_panel_rows([], history, 10, "*")][1:] == [
        "├── ✔ Fix the bug",
        "├── * Run the tests",
        "└── ○ Report back",
    ]


def test_a_delegate_row_leads_with_its_purpose_and_names_only_other_agents():
    history = ToolHistory()
    for agent, call_id in (("worker", "w"), ("reviewer", "r"), ("", "old")):
        purpose = f"{call_id} purpose"
        history.record(delegate_started(agent, purpose, call_id, purpose=purpose))
    rows = [text for _, text in task_panel_rows([], history, 10, "*")]
    assert rows == [
        "» w purpose · 0.0s · Starting",
        "» Reviewer: r purpose · 0.0s · Starting",
        # Saved before delegates recorded their agent: only workers existed.
        "» old purpose · 0.0s · Starting",
    ]


def test_a_long_purpose_leaves_the_delegate_rows_clock_and_state_in_80_columns():
    history = ToolHistory()
    purpose = "checking every discount edge case against the main branch" + " x" * 4
    history.record(delegate_started("worker", purpose, "w", purpose=purpose[:60]))
    rows = task_panel_rows([{"content": "Parent task", "status": "in_progress"}], history, 5, "*")
    text = "".join(part for _, part in panel_fragments(rows, 80))
    assert "0.0s · Starting" in text
    # A ` · ` of its own would cut the bold head short.
    history = ToolHistory()
    history.record(delegate_started("worker", "fix · test", "w", purpose="fix · test"))
    assert [t for _, t in task_panel_rows([], history, 5, "*")] == [
        "» fix – test · 0.0s · Starting"
    ]


def test_a_short_panel_keeps_the_delegate_before_its_plan():
    history = ToolHistory()
    history.record(delegate_started("worker", "fix it", "parent"))
    history.record_plan("parent", [{"content": f"step {i}", "status": "pending"} for i in range(5)])
    rows = task_panel_rows([{"content": "Parent task", "status": "in_progress"}], history, 3, "*")
    assert rows[0] == ("class:plan.in_progress", "* Parent task")
    assert rows[1][1].startswith("└── » Worker")
    assert rows[2] == ("class:plan.pending,agent.hue.0", "    └── ○ step 0")


def test_an_extension_delegate_opts_in_to_showing_its_plan():
    from pcode.planning import IdentifiedPlanning

    async def child_model(messages, info):
        if returns(messages):
            yield "Reviewed"
        else:
            yield {
                0: DeltaToolCall(
                    name="write_plan",
                    json_args=json.dumps({"items": [{"content": "Read the diff"}]}),
                    tool_call_id="child-plan",
                )
            }

    async def parent_model(messages, info):
        yield "Done" if returns(messages) else {0: delegate(0, "reviewer")}

    reviewer = Agent(
        FunctionModel(stream_function=child_model),
        name="reviewer",
        capabilities=[IdentifiedPlanning()],
    )
    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=parent_model),
            capabilities=[
                SubAgents(
                    agents=[SubAgent(reviewer)],
                    agent_folders=None,
                    event_stream_handler=stream_child_activity,
                ),
                DelegationReporting(),
            ],
        )
    )

    async def run():
        events = [e async for e in runtime.stream("Review")]
        plans = [e for e in events if isinstance(e, ChildPlan)]
        assert [(p.call_id, [i["content"] for i in p.items]) for p in plans] == [
            ("parent-0", ["Read the diff"])
        ]

    asyncio.run(run())
