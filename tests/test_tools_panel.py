import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import Message, TextDelta, ToolStarted, ToolSummary
from pcode.tool_panel import ToolCall, ToolHistory, panel_fragments, task_panel_rows
from pcode.ui import CursorSafeOutput, TerminalOutput


def test_results_remove_their_call_even_when_they_arrive_out_of_order():
    history = ToolHistory()
    history.record(ToolStarted("read_file", "first.py", "first"))
    history.record(ToolStarted("read_file", "second.py", "second"))
    history.record(ToolSummary("read_file", "second.py → read", call_id="second"))
    assert [call.event.call_id for call in history.calls] == ["first"]
    history.record(ToolSummary("read_file", "first.py → read", call_id="first"))
    assert history.calls == []


def test_restated_start_updates_in_place_and_the_newest_call_owns_the_status_row():
    history = ToolHistory()
    history.record(ToolStarted("read_file", "first.py", "first"))
    history.record(ToolStarted("read_file", "first.py", "first", activity="reading"))
    assert len(history.calls) == 1
    assert "reading" in history.active.line()
    history.record(ToolStarted("grep", "pattern", "second"))
    assert history.active.event.call_id == "second"
    # The older call still counts toward `Running N tools`, but gets no row.
    assert history.running == 2
    assert history.rows(5) == []


def test_settled_calls_do_not_linger_in_the_panel():
    history = ToolHistory()
    for i in range(12):
        history.record(ToolStarted("read_file", f"file_{i}.py", str(i)))
        history.record(ToolSummary("read_file", f"file_{i}.py", call_id=str(i)))
    assert history.calls == []
    assert panel_fragments(history.rows(5), 100) == []


def test_a_command_that_finishes_instantly_still_holds_the_status_row(monkeypatch):
    history = ToolHistory()
    history.record(ToolStarted("run_command", "quick", "one", command="true"))
    history.record(ToolSummary("run_command", "quick", call_id="one"))
    # Gone from the panel, but the status row keeps it long enough to read.
    assert history.calls == []
    assert history.active is not None and history.active.event.call_id == "one"
    frozen = history.active.line()
    assert history.active.line() == frozen  # Its duration stops ticking.
    # Real work always wins the row back.
    history.record(ToolStarted("grep", "pattern", "two"))
    assert history.active.event.call_id == "two"
    history.record(ToolSummary("grep", "pattern", call_id="two"))
    monkeypatch.setattr("pcode.tool_panel.STATUS_DWELL", 0.0)
    assert history.active is None
    assert history.recent is None


def test_a_wait_row_names_its_job_and_the_command_that_job_runs():
    def line(event):
        return ToolCall(event, started=0.0, settled=45.2).line()

    known = ToolStarted(
        "wait_for_job", "j3", "one", command="make e2e", purpose="running the suite"
    )
    assert line(known) == "Wait for job · 45.2s · j3 · running the suite · make e2e"
    # A job the runtime could not name still says which one is being waited on.
    assert line(ToolStarted("wait_for_job", "j9", "two")) == "Wait for job · 45.2s · j9"
    # Reading a job is not waiting on it, but names the job the same way.
    output = ToolStarted("job_output", "j3", "three", command="make e2e")
    assert line(output) == "Read job output · 45.2s · j3 · make e2e"


@pytest.mark.parametrize("width", [1, 8, 24, 80])
def test_panel_rows_are_cell_bounded_and_controls_cannot_change_layout(width):
    history = ToolHistory()
    noisy = "界e\u0301🙂\n\x1b[2J" * 20
    history.record(ToolStarted("delegate_task", "", "one", agent="worker", task=noisy))
    history.record(ToolStarted("delegate_task", "", "two", agent="reviewer", task="Review"))
    history.record(ToolStarted("grep", "pattern", "three"))
    fragments = panel_fragments(history.rows(5, nested=True), width)
    lines = "".join(text for _, text in fragments).splitlines()
    # Both sub-agents get rows; the plain call belongs to the status row.
    assert len(lines) == 2
    assert all(cell_len(line) <= width for line in lines)
    assert "\x1b" not in "".join(lines)
    agent_styles = [
        style for style, text in fragments if text != "\n" and style != "class:plan.tree"
    ]
    assert all(style.startswith("class:plan.agent,agent.hue.") for style in agent_styles)
    # Parallel sub-agents take different hues.
    assert {style.split(",")[1].split()[0] for style in agent_styles} == {
        "agent.hue.0",
        "agent.hue.1",
    }


def test_a_sub_agent_keeps_its_hue_when_an_earlier_one_finishes():
    history = ToolHistory()
    for call_id in ("one", "two"):
        history.record(ToolStarted("delegate_task", "", call_id, agent="worker", task=call_id))
    history.record(ToolSummary("delegate_task", "done", call_id="one"))
    assert [c.hue for c in history.delegates] == [1]
    # The freed slot goes to the next sub-agent.
    history.record(ToolStarted("delegate_task", "", "three", agent="worker", task="three"))
    assert [c.hue for c in history.delegates] == [1, 0]


def test_a_delegate_rows_whole_head_goes_bold_whether_or_not_it_names_the_agent():
    for head in ("» reviewing the fix", "» Reviewer: checking the fix"):
        rows = [("class:plan.agent,agent.hue.0", f"└── {head} · 1.0s · Working")]
        assert panel_fragments(rows, 80)[1:] == [
            ("class:plan.agent,agent.hue.0 bold", head),
            ("class:plan.agent,agent.hue.0", " · 1.0s · Working"),
        ]


def test_row_parts_color_guides_and_icons_apart_from_the_text():
    rows = [
        ("class:plan.completed,agent.hue.1", "    ├── ✓ Read it"),
        ("class:plan.agent,agent.hue.1", "└── » Worker · 1.0s · Working · Fix it"),
        ("class:plan", "Queued: keep whole"),
    ]
    assert panel_fragments(rows, 80) == [
        ("class:plan.tree", "    ├── "),
        ("class:plan.icon.completed", "✓"),
        ("class:plan.completed,agent.hue.1", " Read it"),
        ("", "\n"),
        ("class:plan.tree", "└── "),
        ("class:plan.agent,agent.hue.1 bold", "» Worker"),
        ("class:plan.agent,agent.hue.1", " · 1.0s · Working · Fix it"),
        ("", "\n"),
        ("class:plan", "Queued: keep whole"),
    ]


def test_planning_calls_never_reach_the_panel():
    history = ToolHistory()
    history.record(ToolStarted("read_file", "path", "one"))
    history.record(ToolStarted("write_plan", "", "plan"))
    history.record(ToolSummary("write_plan", "Invalid task", failed=True, call_id="plan"))
    assert [call.event.call_id for call in history.calls] == ["one"]
    history.clear()
    assert history.calls == []


def test_tools_do_not_commit_model_tail_but_summaries_reach_scrollback():
    class Runtime:
        session = None

        async def stream(self, prompt):
            yield TextDelta("model ")
            yield ToolStarted("read_file", "hidden_tool_target", "one")
            assert output.tail == "model "
            yield ToolSummary("read_file", "hidden_tool_target → read", call_id="one")
            yield TextDelta("answer")
            yield Message("model answer")

    stream = StringIO()
    app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=stream))
    terminal_app = SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None)
    output = TerminalOutput(app.transcript.console, terminal_app)
    app.transcript.output = output

    async def run():
        assert await app.run_live(output, "go")
        await output.flush()
        printed = stream.getvalue()
        # A settled call commits the prose before it, then summarizes itself.
        assert printed.index("model") < printed.index("hidden_tool_target")
        assert printed.index("hidden_tool_target") < printed.index("answer")
        assert printed.count("hidden_tool_target") == 1
        assert app.activity.tools.calls == []

    asyncio.run(run())


@pytest.mark.parametrize("name", ["wait_for_job", "job_output"])
def test_job_inspection_does_not_split_streaming_prose(name):
    class Runtime:
        session = None

        async def stream(self, prompt):
            yield TextDelta("model ")
            yield ToolStarted(name, "j14", "one")
            yield ToolSummary(name, "j14 · exit 2", failed=True, call_id="one", outcome="success")
            assert output.tail == "model "
            assert app.activity.tools.calls == []
            yield TextDelta("answer")
            yield Message("model answer")

    stream = StringIO()
    app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=stream))
    terminal_app = SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None)
    output = TerminalOutput(app.transcript.console, terminal_app)
    app.transcript.output = output

    async def run():
        assert await app.run_live(output, "go")
        await output.flush()
        assert "model answer" in stream.getvalue()
        assert name not in stream.getvalue()

    asyncio.run(run())


def test_failed_commands_keep_only_a_summary_line_without_command_mirroring():
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream))
    event = ToolSummary(
        "run_command",
        "pytest -q → exit 1",
        failed=True,
        call_id="one",
        command="pytest -q",
        error="FAILED test_example: missing module",
    )
    app.present_events((event,))
    assert "✗ Run shell" in stream.getvalue()
    assert "FAILED test_example" not in stream.getvalue()
    assert app.activity.tools.calls == []
    assert app.registry.find("/tools") is not None
    with pytest.raises(ValueError, match="Usage: /tools"):
        app.registry.dispatch("/tools 1")


def test_new_clears_task_panel_and_previous_prompt_row():
    app = PreviewApp(console=Console(file=StringIO()))
    app.activity.plan = [{"id": "one", "content": "A task", "status": "completed"}]
    app.activity.tools.record(ToolStarted("read_file", "file.py", "one"))
    app.activity.prompt = "previous prompt"
    app.activity.prompt_state = "done"
    app.activity.status = "Responding…"
    app.controller.new("")
    assert app.activity.plan == []
    assert not app.activity.tools.calls
    assert app.activity.prompt == ""
    assert app.activity.prompt_state == ""
    assert app.activity.status == ""
    assert task_panel_rows(app.activity.plan, app.activity.tools, 10, "⠋") == []


def test_cancelled_run_drops_outstanding_tools_from_the_panel():
    class Runtime:
        session = None

        async def stream(self, prompt):
            yield ToolStarted("run_command", "sleep 30", "one", command="sleep 30")
            raise asyncio.CancelledError

    app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=StringIO()))
    terminal_app = SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None)
    output = TerminalOutput(app.transcript.console, terminal_app)
    assert not asyncio.run(app.run_live(output, "go"))
    assert app.activity.tools.calls == []


def test_resume_leaves_no_stale_running_tools(tmp_path):
    from pcode.sessions import SavedSession

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        saved.append("turn_started", prompt="go")
        saved.event(ToolStarted("read_file", "one", "one"))
        saved.event(ToolStarted("read_file", "two", "two"))
        saved.event(ToolSummary("read_file", "two → read", call_id="two"))
        saved.event(ToolStarted("run_command", "waiting", "three", command="sleep 30"))
        saved.event(ToolSummary("read_file", "one → read", call_id="one"))
        saved.append("turn_cancelled")
        for _ in range(50):
            saved.append("Message", markdown="later conversation")
        assert any(record["kind"] == "ToolSummary" for record in saved.transcript_records())
        app = PreviewApp(
            model="test:local",
            runtime=SimpleNamespace(session=saved),
            console=Console(file=StringIO()),
        )
        app.replay()
        assert app.activity.tools.calls == []
    finally:
        saved.close()


@pytest.mark.parametrize("status", ["pending", "in_progress", "blocked"])
def test_parallel_tool_calls_never_get_task_panel_rows(status):
    """Fast calls would strobe in and out; the status row and its tally cover them."""
    history = ToolHistory()
    history.record(ToolStarted("read_file", "example.py", "one"))
    history.record(ToolStarted("grep", "pattern", "status-row"))
    history.record(ToolStarted("read_file", "child.py", "x:one", parent_call_id="x"))
    items = [
        {"id": "one", "content": "Inspect", "status": "completed"},
        {"id": "two", "content": "Implement", "status": status},
    ]
    icon = "⟳" if status == "in_progress" else {"pending": "○", "blocked": "!"}[status]
    text = [text for _, text in task_panel_rows(items, history, 10, "⟳")]
    assert text == ["✔ Inspect", f"{icon} Implement"]
    assert task_panel_rows([], history, 10, "⟳") == []


@pytest.mark.parametrize("budget", [1, 2, 4, 6, 10])
def test_running_tool_calls_leave_the_whole_budget_to_the_tasks(budget):
    history = ToolHistory()
    for i in range(10):
        history.record(ToolStarted("read_file", f"file_{i}.py", str(i)))
    items = [{"id": str(i), "content": f"Task {i}", "status": "pending"} for i in range(12)]
    items[8]["status"] = "in_progress"
    text = [text for _, text in task_panel_rows(items, history, budget, "⟳")]
    assert len(text) == min(budget, 5)
    assert "⟳ Task 8" in text
    assert not any("Read file" in line for line in text)
