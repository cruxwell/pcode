"""Exercise editable input and serialized submissions with the real prompt loop."""

import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import Message, TextDelta
from pcode.ui import create_prompt


@pytest.mark.parametrize("outcome", ["success", "cancel", "failure"])
def test_edit_and_queue_during_generation(outcome):
    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()
        followup = asyncio.Event()
        calls = []
        active = 0
        output = StringIO()

        class Runtime:
            session = None
            recovery_blocked = ""

            async def stream(self, text):
                nonlocal active
                active += 1
                assert active == 1
                calls.append(text)
                try:
                    if len(calls) == 1:
                        yield TextDelta("**first**\npartial")
                        started.set()
                        await finish.wait()
                        if outcome == "failure":
                            raise RuntimeError("private provider body")
                        yield TextDelta(" done")
                        yield Message("**first**\npartial done")
                    else:
                        yield TextDelta("second answer")
                        yield Message("second answer")
                        followup.set()
                finally:
                    active -= 1

        app = PreviewApp(
            model="test:local", runtime=Runtime(), console=Console(file=output, color_system=None)
        )
        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            async def wait_for(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(0.01)

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait_for(lambda: session is not None and session.app.is_running)
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    pipe.send_text("second\rdraft text\x1b[D\x1b[D\x1b[D\x1b[D")
                    await wait_for(lambda: session.default_buffer.text == "draft text")
                    assert session.default_buffer.cursor_position == 6
                    # A host reports the queue back a moment after the send.
                    await wait_for(lambda: app.activity.queued == 1)
                    assert app.activity.queued_prompts == ["second"]
                    assert calls == ["first"]
                    assert app.activity.prompt == "first"
                    assert app.activity.prompt_state == "running"
                    if outcome == "cancel":
                        # The draft absorbs the first Ctrl+C; the run keeps going.
                        pipe.send_text("\x03")
                        await wait_for(lambda: not session.default_buffer.text)
                        assert app.activity.busy
                        pipe.send_text("\x03")
                    else:
                        finish.set()
                    await wait_for(lambda: not app.activity.busy)
                    assert app.activity.queued_prompts == []
                    if outcome == "success":
                        assert followup.is_set()
                        assert calls == ["first", "second"]
                    else:
                        assert calls == ["first"]
                        assert app.activity.queued == 0
                    assert (
                        app.activity.prompt_state
                        == {"success": "done", "failure": "failed", "cancel": "cancelled"}[outcome]
                    )
                    assert app.activity.prompt == ("second" if outcome == "success" else "first")
                    if outcome == "cancel":
                        pipe.send_text("draft text\x1b[D\x1b[D\x1b[D\x1b[D")
                        await wait_for(lambda: session.default_buffer.text == "draft text")
                    assert session.default_buffer.text == "draft text"
                    assert session.default_buffer.cursor_position == 6
                    pipe.send_text("my ")
                    await wait_for(lambda: session.default_buffer.text == "draft my text")
                    pipe.send_text("\x03\x04")  # Idle: discard draft, then exit.
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        printed = output.getvalue()
        assert "**first**" not in printed
        assert printed.count("first partial") == 1  # Rendered once, including cancellation.
        assert printed.count("partial") == 1
        assert "private provider body" not in printed
        if outcome == "cancel":
            assert "! Run cancelled" in printed
        elif outcome == "failure":
            assert "✗ Agent failed" in printed
        else:
            assert printed.count("second answer") == 1

    asyncio.run(run())


def tool_row(activity, width=80):
    """The tool rows' text, one line each, for the line the status row would show."""
    rows = activity.tool_rows(activity.status_line(), width)
    return "\n".join("".join(text for _, text in row) for row in rows)


def styled(fragments):
    """`{style: text}` for one status row, minus the unstyled padding."""
    return {style.removeprefix("class:"): text for style, text in fragments if style}


@pytest.mark.parametrize("state", ["running", "failed", "cancelled", "done", ""])
def test_status_row_shows_the_spinner_and_never_echoes_the_prompt(state):
    from pcode.ui import Activity

    activity = Activity(prompt="first\nsecond\x1b", prompt_state=state, status="Waiting for model…")
    assert activity.status_shown is (state == "running")
    parts = styled(activity.status_fragments("⠋", 80))
    assert parts["activity.spinner"] == "⠋ "
    assert parts["activity.phase"] == "Waiting for model"
    assert "first" not in "".join(parts.values())


@pytest.mark.parametrize(
    ("status", "phase", "detail"),
    [
        ("Thinking…", "Thinking", ""),
        ("Responding…", "Responding", ""),
        ("Waiting for model…", "Waiting for model", ""),
        ("Retrying · Overloaded. Retrying 1/3…", "Retrying", "Overloaded. Retrying 1/3"),
        # A sentence with no phase is detail, never one long accented word.
        (
            "Enabling MCP 'x' — complete browser sign-in…",
            "Working",
            "Enabling MCP 'x' — complete browser sign-in",
        ),
        ("", "Working", ""),
    ],
)
def test_status_text_splits_into_phase_and_detail(status, phase, detail):
    from pcode.ui import status_parts

    assert status_parts(status) == (phase, detail)


def test_status_row_keeps_one_shape_with_meta_right_aligned():
    from rich.cells import cell_len

    from pcode.ui import Activity

    activity = Activity(prompt_state="running", status="Responding…")
    fragments = activity.status_fragments("⠋", 60, "✓ 7 ✗ 1 tools")
    rendered = "".join(text for _, text in fragments)
    assert cell_len(rendered) == 60
    assert rendered.startswith("⠋ Responding ")
    assert rendered.endswith("✓ 7 ✗ 1 tools · 0s")
    assert styled(fragments)["activity.meta"] == "✓ 7 ✗ 1 tools · 0s"


def test_status_row_reports_the_newest_running_tool_call():
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running", status="Responding…")
    activity.tools.record(ToolStarted("read_file", "example.py", "one"))
    activity.tools.record(ToolStarted("grep", "pattern", "two"))
    fragments = activity.status_fragments("⠋", 80)
    assert styled(fragments)["activity.phase"] == "Running 2 tools"
    # The call's detail is never on the status row: the tool row has it, led
    # by the newest call's glyph in place of its verb.
    assert "pattern" not in "".join(text for _, text in fragments)
    # Parallel calls each get a row, in the order they started.
    assert tool_row(activity) == "· ⎘ example.py\n· ⌕ pattern"
    # A result hands the status row back to the call still running, and the
    # finished call keeps its row, marked done.
    activity.tools.record(ToolSummary("grep", "pattern", call_id="two"))
    parts = styled(activity.status_fragments("⠋", 80))
    # One call is its own phase: its label is already the verb.
    assert parts["activity.phase"] == "Read file" and "activity.detail" not in parts
    # The status row says the verb; the tool row's glyph stands in for it.
    assert tool_row(activity) == "· ⎘ example.py\n√ ⌕ pattern"
    # At one row, only the call the status row reports.
    activity.tool_max_rows = 1
    assert tool_row(activity) == "· ⎘ example.py"
    # A running call's mark is the tool spinner's frame, in the tool
    # row's muted shade, and a thought above does not move the row.
    activity.think("Checking the example")
    assert activity.tool_rows(activity.status_line(), 80, spinner="◜") == [
        [
            ("class:activity.tool", "◜"),
            ("class:activity.tool", " ⎘ example.py"),
        ]
    ]


def test_finished_call_is_held_as_done_under_the_models_phase():
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running", status="Running read_file…")
    activity.tools.record(ToolStarted("read_file", "example.py", "one"))
    activity.tools.record(ToolSummary("read_file", "example.py", call_id="one"))
    parts = styled(activity.status_fragments("⠋", 80))
    # Nothing runs, so a stale `Running` status gives the model the turn,
    # and the held call is muted chrome with its result mark, not live work.
    assert parts["activity.phase"] == "Waiting for model" and "activity.detail" not in parts
    assert activity.tool_rows(activity.status_line(), 80)[-1] == [
        ("class:activity.tool.done", "√"),
        ("class:activity.tool", " ⎘ example.py"),
    ]
    activity.tools.clear()
    parts = styled(activity.status_fragments("⠋", 80))
    assert "activity.detail" not in parts and parts["activity.meta"] == "0s"
    # The tool row keeps the turn's last call, so the editor box does not
    # jump a row with every call; a new turn starts without one.
    assert tool_row(activity) == "√ ⎘ example.py"
    activity.start_prompt("Next")
    assert tool_row(activity) == ""


def test_tool_row_spells_out_a_verb_with_no_glyph():
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running")
    activity.tools.record(ToolStarted("web_search", "pcode docs", "one"))
    # The status row names the verb while it runs, so the row need not.
    assert tool_row(activity) == "· pcode docs"
    activity.tools.record(ToolSummary("web_search", "pcode docs", call_id="one"))
    assert tool_row(activity) == "√ web_search · pcode docs"


def test_tool_row_spells_out_verbs_with_glyphs_off(monkeypatch):
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running", tool_glyphs="off")
    activity.tools.record(ToolStarted("read_file", "example.py", "one"))
    # The status row names the verb while it runs, thought or none.
    assert tool_row(activity) == "· example.py"
    activity.think("Checking the example")
    assert tool_row(activity) == "· example.py"
    activity.tools.record(ToolSummary("read_file", "example.py", call_id="one"))
    assert tool_row(activity) == "√ Read file · example.py"
    # `auto` draws the symbols except on the Linux console, which has no
    # fallback font for them.
    activity.tool_glyphs = "auto"
    monkeypatch.setenv("TERM", "linux")
    assert tool_row(activity) == "√ Read file · example.py"
    monkeypatch.setenv("TERM", "xterm-256color")
    assert tool_row(activity) == "√ ⎘ example.py"
    # `$` is ASCII, in every font: shell calls keep it with the symbols off.
    activity.tool_glyphs = "off"
    activity.tools.record(ToolStarted("shell", "make test", "two"))
    assert tool_row(activity) == "√ Read file · example.py\n· $ make test"
    activity.tools.record(ToolSummary("shell", "make test", call_id="two"))
    assert tool_row(activity) == "√ Read file · example.py\n√ $ make test"


def test_tool_rows_line_up_under_a_wide_spinner():
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running")
    activity.tools.record(ToolStarted("read_file", "a.py", "one"))
    tool_row(activity)  # Drawn while it runs.
    activity.tools.record(ToolSummary("read_file", "a.py", call_id="one"))
    activity.tools.record(ToolStarted("read_file", "b.py", "two"))
    rows = activity.tool_rows(activity.status_line(), 80, spinner="⢀⠀")
    assert ["".join(text for _, text in row) for row in rows] == ["√  ⎘ a.py", "⢀⠀ ⎘ b.py"]


def test_tool_rows_keep_the_latest_calls_and_the_status_rows_own():
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running", tool_max_rows=2)

    def run(name, detail, call_id):
        activity.tools.record(ToolStarted(name, detail, call_id))
        tool_row(activity)  # Drawn while it runs.
        activity.tools.record(ToolSummary(name, detail, call_id=call_id))

    run("read_file", "a.py", "one")
    run("read_file", "b.py", "two")
    run("read_file", "c.py", "three")
    # The oldest finished call gives its row up to the newest.
    assert tool_row(activity) == "√ ⎘ b.py\n√ ⎘ c.py"
    # A delegate has its own panel row, and its sub-agent's calls are its own.
    activity.tools.record(ToolStarted("delegate_task", "", "d", agent="worker", task="t"))
    activity.tools.record(ToolStarted("grep", "x", "d:grep", parent_call_id="d"))
    activity.tools.record(ToolStarted("read_file", "d.py", "four"))
    assert tool_row(activity) == "√ ⎘ c.py\n· ⎘ d.py"
    # The call the status row reports keeps its row, however the window slides:
    # at one row, it is the only one.
    line = activity.status_line()
    activity.tools.record(ToolStarted("read_file", "e.py", "five"))
    activity.tools.record(ToolStarted("read_file", "f.py", "six"))
    rows = activity.tool_rows(line, 80)
    assert ["".join(text for _, text in row) for row in rows] == ["· ⎘ d.py", "· ⎘ f.py"]
    assert len(activity.tool_rows(line, 80, limit=0)) == 1
    activity.tool_max_rows = 1
    assert ["".join(text for _, text in row) for row in activity.tool_rows(line, 80)] == [
        "· ⎘ d.py"
    ]


def test_tool_rows_clear_a_finished_call_after_it_lingers():
    from dataclasses import replace

    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running", tool_linger_seconds=10)
    for detail, call_id in (("a.py", "one"), ("b.py", "two")):
        activity.tools.record(ToolStarted("read_file", detail, call_id))
        tool_row(activity)
        activity.tools.record(ToolSummary("read_file", detail, call_id=call_id))
    activity.tools.record(ToolStarted("read_file", "c.py", "three"))
    assert tool_row(activity) == "√ ⎘ a.py\n√ ⎘ b.py\n· ⎘ c.py"
    # `a.py` finished long ago; `b.py` just now; `c.py` is still running.
    activity.tools.turn_calls[0].settled -= 11
    assert tool_row(activity) == "√ ⎘ b.py\n· ⎘ c.py"
    # Even the status row's own call clears once it has lingered.
    activity.tools.record(ToolSummary("read_file", "c.py", call_id="three"))
    for call in activity.tools.turn_calls:
        call.settled -= 11
    assert tool_row(activity) == ""
    # Except while the status row still names it.
    line = replace(activity.status_line(), call=activity.tools.turn_calls[-1])
    rows = activity.tool_rows(line, 80)
    assert ["".join(text for _, text in row) for row in rows] == ["√ ⎘ c.py"]
    # 0 keeps finished rows until newer calls push them out.
    activity.tool_linger_seconds = 0
    assert tool_row(activity) == "√ ⎘ a.py\n√ ⎘ b.py\n√ ⎘ c.py"


def test_tool_rows_tell_lookalike_calls_apart():
    from pcode.runtime import EditCompleted, ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running", tool_max_rows=4)

    def run(name, detail, call_id, *, failed=False, added=0, removed=0, omitted=""):
        activity.tools.record(ToolStarted(name, detail, call_id))
        tool_row(activity)  # Drawn while it runs.
        if name in ("edit_file", "write_file"):
            change = EditCompleted(call_id, detail, "modified", "", added, removed, omitted=omitted)
            activity.tools.record_edit(change)
        activity.tools.record(ToolSummary(name, detail, call_id=call_id, failed=failed))

    # Edits to one file say what each changed.
    run("edit_file", "a.py", "one", added=3, removed=1)
    run("edit_file", "a.py", "two", added=1)
    assert tool_row(activity) == "√ ✎ a.py · +3 −1\n√ ✎ a.py · +1 −0"
    # A repeated call shares the row of the one before it, counted, but a
    # failure keeps a row of its own.
    run("grep", "pattern", "three", failed=True)
    run("grep", "pattern", "four")
    run("grep", "pattern", "five")
    assert tool_row(activity) == ("√ ✎ a.py · +3 −1\n√ ✎ a.py · +1 −0\n✗ ⌕ pattern\n√ ⌕ pattern ×2")
    # An edit with no diff has no counts, rather than `+0 −0`.
    run("write_file", "img.bin", "six", omitted="Binary content")
    assert tool_row(activity).endswith("√ ✎ img.bin")
    # The counts outlast a long path cut to the pane.
    run("edit_file", "src/" + "deep/" * 20 + "b.py", "seven", added=2, removed=2)
    last = "".join(text for _, text in activity.tool_rows(activity.status_line(), 40)[-1])
    assert last.endswith("… · +2 −2") and len(last) == 40


def test_tool_rows_show_a_sub_agents_call_only_while_reported():
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running")
    activity.tools.record(ToolStarted("delegate_task", "", "d", agent="worker", task="t"))
    activity.tools.record(ToolStarted("read_file", "a.py", "one"))
    activity.tools.record(ToolStarted("grep", "needle", "d:grep", parent_call_id="d"))
    # The status row reports the sub-agent's newest call, so it gets a row.
    assert tool_row(activity) == "· ⎘ a.py\n· ⌕ needle"
    activity.tools.record(ToolSummary("grep", "needle", call_id="d:grep"))
    activity.tools.recent = None  # Its dwell is over.
    assert tool_row(activity) == "· ⎘ a.py"


def test_tool_rows_skip_system_work_and_clear_for_each_turn():
    from pcode.runtime import ToolStarted
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running")
    activity.tools.record(ToolStarted("read_file", "a.py", "one"))
    assert tool_row(activity) == "· ⎘ a.py"
    # A wake turn is pcode's own work: no tool rows, as before.
    activity.prompt_kind = "system"
    assert tool_row(activity) == ""
    activity.prompt_kind = "user"
    # A terminal attached to a host hears of a new turn only through
    # `turn_started`, never `start_prompt`; either clears the rows.
    app = PreviewApp(console=Console(file=StringIO()))
    app.activity = activity
    app.turn_started("next", echo=False)
    assert tool_row(activity) == ""


def test_tool_rows_share_a_short_pane_with_the_thought():
    from prompt_toolkit import PromptSession
    from prompt_toolkit.data_structures import Size

    from pcode.runtime import ToolStarted
    from pcode.ui import Activity, PromptLayout

    activity = Activity(prompt="Fix bug", prompt_state="running")
    for index in range(3):
        activity.tools.record(ToolStarted("read_file", f"{index}.py", str(index)))
    activity.think("Looking around " * 20)
    with create_pipe_input() as pipe:
        layout = PromptLayout(PromptSession(input=pipe, output=DummyOutput()), activity, None, None)
    with patch.object(layout, "size", return_value=Size(rows=12, columns=40)):
        # A quarter of 12 rows: two tool rows, and one for the thought.
        assert len(layout.tool_rows()) == 2
        assert len(layout.thought_rows()) == 1
    with patch.object(layout, "size", return_value=Size(rows=40, columns=40)):
        assert len(layout.tool_rows()) == 3
        # A running call spins with the tool spinner's frame.
        assert layout.tool_rows()[0][0] == ("class:activity.tool", layout.tool_spinner_frame())


def test_tool_row_marks_a_failed_call_and_fits_the_width():
    from rich.cells import cell_len

    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running")
    activity.tools.record(ToolStarted("run_shell", "make test\n" * 30, "one"))
    activity.tools.record(ToolSummary("run_shell", "make test", call_id="one", failed=True))
    (fragments,) = activity.tool_rows(activity.status_line(), 30)
    assert fragments[0] == ("class:activity.tool.failed", "✗")
    rendered = "".join(text for _, text in fragments)
    assert "\n" not in rendered and cell_len(rendered) <= 30 and rendered.endswith("…")
    # Nothing to show once the turn is over.
    activity.finish_prompt("done")
    assert activity.tool_rows(activity.status_line(), 30) == []


def test_status_row_waits_on_sub_agents_listed_in_the_panel(monkeypatch):
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    def row():
        return "".join(text for _, text in activity.status_fragments("⠋", 120))

    activity = Activity(prompt="Fix bug", prompt_state="running", status="Running delegate_task…")
    for call_id in ("a", "b"):
        activity.tools.record(
            ToolStarted("delegate_task", "", call_id, agent="worker", task=f"task {call_id}")
        )
    parts = styled(activity.status_fragments("⠋", 80))
    assert parts["activity.phase"] == "Waiting for 2 agents"
    assert "Worker" not in row()
    # A sub-agent's tool call is still reported on the row while it runs.
    activity.tools.record(ToolStarted("read_file", "x.py", "a:read", parent_call_id="a"))
    assert styled(activity.status_fragments("⠋", 80))["activity.phase"] == "Read file"
    # Held as done, it no longer reads as the model's turn.
    monkeypatch.setattr("pcode.tool_panel.STATUS_DWELL", 1e9)
    activity.tools.record(ToolSummary("read_file", "x.py", call_id="a:read"))
    parts = styled(activity.status_fragments("⠋", 80))
    assert parts["activity.phase"] == "Waiting for 2 agents"
    assert tool_row(activity) == "√ ⎘ x.py"
    monkeypatch.setattr("pcode.tool_panel.STATUS_DWELL", 0.0)
    activity.tools.record(ToolSummary("delegate_task", "done", call_id="b"))
    parts = styled(activity.status_fragments("⠋", 80))
    assert parts["activity.phase"] == "Waiting for 1 agent"
    # With the panel hidden, the row is the only place left to name one.
    activity.show_tasks = False
    assert "Worker" in row() and "task a" in row()


def test_status_row_names_the_active_step_only_while_the_panel_hides_it():
    from pcode.runtime import ToolStarted
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running", status="Thinking…")
    activity.plan = [
        {"content": "Inspect", "status": "completed"},
        {
            "content": "Add the migration",
            "active_form": "Adding the migration",
            "status": "in_progress",
        },
    ]
    # The panel already shows the step, so the row stays short.
    assert "activity.detail" not in styled(activity.status_fragments("⠋", 80))
    activity.show_tasks = False
    parts = styled(activity.status_fragments("⠋", 80))
    assert parts["activity.phase"] == "Thinking"
    assert parts["activity.detail"] == " · Adding the migration"
    # A running tool keeps its verb as the phase; the step is still the why.
    activity.tools.record(ToolStarted("read_file", "example.py", "one"))
    parts = styled(activity.status_fragments("⠋", 80))
    assert parts["activity.phase"] == "Read file"
    assert parts["activity.detail"] == " · Adding the migration"
    # A status with its own detail keeps it.
    activity.tools.clear()
    activity.status = "Retrying · Overloaded…"
    assert styled(activity.status_fragments("⠋", 80))["activity.detail"] == " · Overloaded"
    # `active_form` is optional: the step's text stands in.
    activity.status = "Thinking…"
    del activity.plan[1]["active_form"]
    assert styled(activity.status_fragments("⠋", 80))["activity.detail"] == " · Add the migration"
    # No step in progress, no detail.
    activity.plan[1]["status"] = "completed"
    assert "activity.detail" not in styled(activity.status_fragments("⠋", 80))


def test_phase_clock_restarts_when_the_phase_changes(monkeypatch):
    from pcode import ui

    now = [100.0]
    monkeypatch.setattr(ui, "monotonic", lambda: now[0])
    activity = ui.Activity(prompt_state="running", status="Thinking…")

    def clock():
        return styled(activity.status_fragments("⠋", 80))["activity.meta"]

    assert clock() == "0s"
    now[0] += 0.5
    assert clock() == "0s"
    now[0] += 0.9
    assert clock() == "1s"
    activity.status = "Responding…"
    now[0] += 0.5
    assert clock() == "0s"
    # A gap in drawing means the row went away between turns.
    now[0] += 30
    assert clock() == "0s"


def test_status_row_holds_each_line_before_changing(monkeypatch):
    from pcode import ui
    from pcode.runtime import ToolStarted, ToolSummary

    now = [100.0]
    monkeypatch.setattr(ui, "monotonic", lambda: now[0])
    activity = ui.Activity(prompt_state="running", status="Thinking…")

    def row():
        return styled(activity.status_fragments("⠋", 80, hold=2.5))

    assert row()["activity.phase"] == "Thinking"
    # Changes inside the hold are skipped; the clock keeps moving meanwhile.
    activity.status = "Responding…"
    now[0] += 0.5
    activity.status = "Compacting context…"
    now[0] += 0.5
    assert row()["activity.phase"] == "Thinking"
    now[0] += 0.5
    assert row()["activity.meta"] == "1s"
    # Once it is up, the row jumps to what is current and holds that in turn.
    now[0] += 1.0
    assert row()["activity.phase"] == "Compacting context"
    activity.status = "Thinking…"
    now[0] += 0.5
    assert row()["activity.phase"] == "Compacting context"
    # A gap in drawing drops the hold.
    now[0] += 30
    assert row()["activity.phase"] == "Thinking"
    # So does a retry: news, not churn.
    activity.status = "Retrying · Overloaded…"
    assert row()["activity.phase"] == "Retrying"
    # And a new turn, even one queued right behind the last.
    activity.finish_prompt("done")
    activity.start_prompt("next")
    activity.status = "Thinking…"
    assert row()["activity.phase"] == "Thinking"
    # A held tool call that finishes is not left reading as running.
    activity.tools.record(ToolStarted("read_file", "x.py", "r"))
    now[0] += 3
    assert row()["activity.phase"] == "Read file"
    activity.tools.record(ToolSummary("read_file", "x.py", call_id="r"))
    activity.tools.record(ToolStarted("run_shell", "ls", "s"))
    activity.tools.record(ToolSummary("run_shell", "ls", call_id="s"))
    now[0] += 0.1
    assert row()["activity.phase"] != "Read file"
    # Without a hold, every change shows at once.
    activity.status = "Responding…"
    assert styled(activity.status_fragments("⠋", 80))["activity.phase"] == "Responding"


@pytest.mark.parametrize("limit", [1, 3])
@pytest.mark.parametrize("width", [3, 4, 5, 8, 20])
@pytest.mark.parametrize("thought", ["word " * 20, "漢字" * 20, "x" * 80])
def test_thinking_marker_wraps_without_continuation_indent(width, thought, limit):
    from rich.cells import cell_len

    from pcode.ui import Activity

    activity = Activity(prompt_state="running", status="Thinking…", thought=thought)
    rows = activity.thought_fragments(width, limit)
    assert rows[0][0] == ("class:activity.thinking.icon", "∴ ")
    assert len(rows) <= limit
    assert all(cell_len("".join(text for _, text in row)) <= width for row in rows)
    assert all(row[0][1] == "" and not row[1][1].startswith(" ") for row in rows[1:])
    assert rows[-1][1][1].endswith("…")


def test_thinking_rows_hold_the_newest_thought_above_the_status_row():
    from rich.cells import cell_len

    from pcode.runtime import Message, TextDelta, Thinking, ThinkingDelta, ToolStarted
    from pcode.stream_display import present_stream_event
    from pcode.ui import Activity

    class Output:
        """Accepts every output call; `app.invalidate()` included."""

        def __getattr__(self, name):
            return self if name == "app" else lambda *args: None

    activity = Activity(prompt_state="running", status="Thinking…")

    def feed(event):
        present_stream_event(
            event, output=Output(), transcript=None, activity=activity, present=None
        )

    def rows(width=80, limit=3):
        # Each row has a marker fragment (empty on continuations), then text.
        return [text for (_, _bar), (_, text) in activity.thought_fragments(width, limit)]

    def row(width=80):
        return "\n".join(rows(width))

    assert activity.thinking_mode == "status-line" and row() == ""
    # Untitled text shows its newest line, with a marker over the spinner.
    feed(ThinkingDelta("The status row"))
    feed(ThinkingDelta(" is empty\n\n"))
    assert activity.thought_fragments(80) == [
        [
            ("class:activity.thinking.icon", "∴ "),
            ("class:activity.thinking", "The status row is empty"),
        ]
    ]
    # A long line wraps to a few rows and is cut at the end, like skimmed prose.
    feed(ThinkingDelta("so " + "word " * 40 + "newest"))
    assert len(rows(50)) == 3 and all(cell_len(text) <= 50 for text in rows(50))
    assert rows(50)[0].startswith("so word") and rows(50)[-1].endswith("…")
    assert "…" not in "".join(rows(50)[:-1]) and "newest" not in row(50)
    assert all(
        [style for style, _ in fragments]
        == ["class:activity.thinking.icon", "class:activity.thinking"]
        for fragments in activity.thought_fragments(50)
    )
    fragments = activity.thought_fragments(50)
    assert fragments[0][0][1] == "∴ "
    assert all(parts[0][1] == "" for parts in fragments[1:])
    assert all(not parts[1][1].startswith(" ") for parts in fragments[1:])
    assert all(cell_len("".join(text for _, text in parts)) <= 50 for parts in fragments)
    # A short pane gets fewer rows; a wide one needs no cut.
    assert len(rows(50, 1)) == 1 and rows(50, 1)[0].endswith("…")
    assert rows(400) == ["so " + "word " * 40 + "newest"]
    # A titled section shows its title, not the prose under it.
    feed(ThinkingDelta("\n\n**Tracing the resize path**\n\nI need to check the replay"))
    assert row() == "Tracing the resize path"
    # Its own row: a running tool takes the status row, not this one. The
    # thought explains the call, so the ended block is held through it.
    feed(Thinking("done"))
    activity.tools.record(ToolStarted("read_file", "ui.py", "one"))
    assert "Read file" in "".join(t for _, t in activity.status_fragments("⠋", 80))
    assert row() == "Tracing the resize path"
    # An empty text part says nothing yet; the answer streaming into
    # scrollback takes over, and the thought goes at once.
    feed(TextDelta(""))
    feed(TextDelta(" \n"))
    assert row() == "Tracing the resize path"
    feed(TextDelta("Answer"))
    assert row() == ""
    feed(Thinking("A whole block, with no deltas"))
    assert row() == "A whole block, with no deltas"
    feed(ThinkingDelta("Next idea"))
    assert row() == "Next idea"
    # A whole message with no deltas clears it too.
    feed(Message("Answer"))
    assert row() == ""
    feed(ThinkingDelta("Again"))
    assert row() == "Again"
    # The other modes have no row; a new turn starts empty.
    activity.thinking_mode = "scrollback"
    assert row() == ""
    activity.thinking_mode = "status-line"
    activity.start_prompt("again")
    assert row() == ""


@pytest.mark.parametrize("width", [0, 1, 2, 3, 12, 40, 100])
def test_status_row_truncates_to_terminal_width(width):
    from rich.cells import cell_len

    from pcode.runtime import ToolStarted
    from pcode.ui import Activity

    activity = Activity(prompt_state="running")
    activity.tools.record(ToolStarted("read_file", "界面/path\n" * 30, "one"))
    rendered = "".join(text for _, text in activity.status_fragments("⠋", width, "✓ 3 tools"))
    assert "\n" not in rendered
    assert cell_len(rendered) <= width
    # The path is the tool row's, never the status row's.
    assert "界面" not in rendered
    row = tool_row(activity, width)
    assert "\n" not in row and cell_len(row) <= width
    if width >= 12:
        assert "界面" in row and row.endswith("…")


def test_narrow_status_row_drops_the_tally_before_the_detail():
    from pcode.ui import Activity

    activity = Activity(
        prompt_state="running", status="Retrying · Overloaded, retrying request 1/3…"
    )
    parts = styled(activity.status_fragments("⠋", 32, "✓ 12 ✗ 3 tools"))
    assert parts["activity.meta"] == "0s"
    assert parts["activity.detail"].startswith(" · Overloaded")


def test_system_prompt_row_is_badged_and_not_an_echoed_command():
    from pcode.ui import Activity

    activity = Activity()
    activity.start_prompt("Compacting context", kind="system", detail="keep {tests}")
    fragments = activity.status_fragments("⠋", 80)
    assert styled(fragments) == {
        "activity.spinner": "⠋ ",
        "activity.badge": "◈ ",
        "activity.phase": "Compacting context",
        "activity.detail": " ▸ keep {tests}",
        "activity.meta": "0s",
    }
    rendered = "".join(text for _, text in fragments)
    assert "/compact" not in rendered and "❯" not in rendered


def test_system_prompt_row_drops_empty_detail():
    from pcode.ui import Activity

    activity = Activity()
    activity.start_prompt("Compacting context", kind="system")
    assert "activity.detail" not in styled(activity.status_fragments("⠋", 80))


@pytest.mark.parametrize("width", [0, 1, 2, 3, 5, 12, 40, 100])
def test_system_prompt_row_truncates_to_terminal_width(width):
    from rich.cells import cell_len

    from pcode.ui import Activity

    activity = Activity()
    activity.start_prompt("Compacting 界面 context\n" * 9, kind="system", detail="keep 界面\n" * 9)
    rendered = "".join(text for _, text in activity.status_fragments("⠋", width))
    assert "\n" not in rendered
    assert cell_len(rendered) <= width


def test_new_conversation_clears_the_system_prompt_kind():
    from pcode.ui import Activity

    activity = Activity()
    activity.start_prompt("Compacting context", kind="system", detail="keep tests")
    activity.reset()
    activity.start_prompt("Fix bug")
    assert "activity.badge" not in styled(activity.status_fragments("⠋", 40))
    assert activity.prompt_detail == ""


def test_queue_previews_are_ordered_bounded_and_single_line():
    from pcode.tool_panel import panel_fragments
    from pcode.ui import Activity

    activity = Activity(queued_prompts=["first\ncontinued", "second", "third", "fourth", "fifth"])
    rows = activity.queue_rows(3)
    assert [text for _, text in rows] == [
        "Queued: first\ncontinued",
        "Queued: second",
        "… 3 more queued",
    ]
    rendered = "".join(text for _, text in panel_fragments(rows, 18))
    assert rendered.splitlines() == ["Queued: first con…", "Queued: second", "… 3 more queued"]
    assert len(activity.queue_rows(1)) == 1
    assert activity.queue_rows(0) == []
    assert Activity().queue_rows(3) == []


def test_queued_system_commands_are_badged_like_the_running_row():
    from pcode.ui import Activity

    activity = Activity(
        queued_prompts=["/compact keep tests", "/compact", "write /compact docs"],
        queued_modes=["queue", "steering", "queue"],
    )
    assert activity.queue_rows(3) == [
        ("class:activity.system.detail", "Queued ◈ Compacting context ▸ keep tests"),
        ("class:activity.system.detail", "Steering ◈ Compacting context"),
        ("class:plan", "Queued: write /compact docs"),
    ]


def test_queued_and_running_system_rows_share_one_label():
    from pcode.ui import SYSTEM_COMMAND_LABELS, Activity

    activity = Activity(queued_prompts=["/compact keep tests"])
    queued = activity.queue_rows(1)[0][1]
    activity.start_prompt(SYSTEM_COMMAND_LABELS["/compact"], kind="system", detail="keep tests")
    parts = styled(activity.status_fragments("⠋", 80))
    running = parts["activity.badge"] + parts["activity.phase"] + parts["activity.detail"]
    assert queued.removeprefix("Queued ") == running


@pytest.mark.parametrize("inspector_command", ["/tools", "/tools failed"])
def test_commands_run_while_model_waits(inspector_command):
    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()
        streamed = asyncio.Event()
        printed = StringIO()
        calls = []

        from pcode.inspection import ToolArchive
        from pcode.runtime import ToolStarted, ToolSummary

        archive = ToolArchive()
        call = ToolStarted("read_file", "example.py", "call-1")
        archive.event(call)

        class Runtime:
            session = None
            recovery_blocked = ""
            inspections = archive

            async def stream(self, text):
                calls.append(text)
                yield call
                started.set()
                await finish.wait()
                archive.event(ToolSummary("read_file", "done", call_id="call-1"))
                yield TextDelta("answer while inspecting")
                yield Message("answer while inspecting")
                streamed.set()

            def reset(self):
                raise AssertionError("must not reset an active run")

        app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=printed))
        with create_pipe_input() as pipe:
            session = None
            inspector = None
            from pcode.inspector_ui import ToolInspector

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            def browser(*args, **kwargs):
                nonlocal inspector
                inspector = ToolInspector(*args, **kwargs)
                return inspector

            async def wait_for(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(0.01)

            with (
                patch("pcode.app.create_prompt", prompt),
                patch("pcode.inspector_ui.ToolInspector", browser),
                patch.object(app.transcript, "user", wraps=app.transcript.user) as user,
            ):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait_for(lambda: session is not None and session.app.is_running)
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    # The prompt's own echo, which a host sends as its turn starts.
                    await wait_for(lambda: user.called)
                    user.reset_mock()
                    pipe.send_text("/theme light\r/help\r/new\r/resume\r/nope\r/theme invalid\r")
                    # With a host, the terminal's own commands do not wait behind
                    # the session's, so wait for every answer rather than the last.
                    expected = [
                        "Usage: /theme",
                        "Unknown command",
                        "/new is unavailable while working",
                    ]
                    if not app.hosted:
                        # A hosted terminal resumes elsewhere, leaving this session working.
                        expected.append("/resume is unavailable while working")
                    await wait_for(lambda: all(text in printed.getvalue() for text in expected))
                    assert app.transcript.theme == "light"
                    user.assert_not_called()
                    assert app.activity.busy
                    assert app.activity.queued_prompts == []
                    assert calls == ["first"]
                    pipe.send_text(inspector_command + "\r")
                    await wait_for(lambda: inspector is not None and inspector.app.is_running)
                    assert inspector.failed == (inspector_command == "/tools failed")
                    assert inspector.archive is not archive
                    assert inspector.archive.calls[0].state == "running"
                    assert not finish.is_set()
                    finish.set()
                    await asyncio.wait_for(streamed.wait(), 5)
                    assert archive.calls[0].state == "succeeded"
                    assert inspector.archive.calls[0].state == "running"
                    # Streaming continues, but permanent output waits for the modal.
                    assert "answer while inspecting" not in printed.getvalue()
                    pipe.send_text("\x1b")
                    await wait_for(lambda: not inspector.app.is_running)
                    await wait_for(lambda: "answer while inspecting" in printed.getvalue())
                    pipe.send_text("/quit\r")
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_tools_lists_every_call_of_the_running_turn(tmp_path):
    """/tools mid-turn shows the turn's finished calls, not only the running one.

    The turn creates its session journal lazily, as a real first turn does. A
    terminal attached to a host (the socket transport) learns the journal's path
    only from the host, and its copy of the tree was stale until the turn ended.
    """

    async def run():
        from pcode.inspection import ToolArchive
        from pcode.inspector_ui import ToolInspector
        from pcode.runtime import ToolStarted, ToolSummary
        from pcode.sessions import SavedSession

        started = asyncio.Event()
        finish = asyncio.Event()

        class Runtime:
            session = None
            recovery_blocked = ""
            inspections = ToolArchive()

            async def stream(self, text):
                self.session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
                self.session.append("turn_started", prompt=text, run_id="run-1", sync=True)
                for index in (1, 2):
                    for event in (
                        ToolStarted("read_file", f"file{index}.py", f"call-{index}"),
                        ToolSummary("read_file", "done", call_id=f"call-{index}"),
                    ):
                        self.session.event(event, run_id="run-1")
                        yield event
                running = ToolStarted("shell", "make test", "call-3")
                self.session.event(running, run_id="run-1")
                yield running
                started.set()
                await finish.wait()
                yield Message("done")

        runtime = Runtime()
        app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))
        with create_pipe_input() as pipe:
            session = None
            inspector = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            def browser(*args, **kwargs):
                nonlocal inspector
                inspector = ToolInspector(*args, **kwargs)
                return inspector

            async def wait_for(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(0.01)

            with (
                patch("pcode.app.create_prompt", prompt),
                patch("pcode.inspector_ui.ToolInspector", browser),
            ):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait_for(lambda: session is not None and session.app.is_running)
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    await wait_for(
                        lambda: any(
                            call.event.call_id == "call-3" for call in app.activity.tools.calls
                        )
                    )
                    pipe.send_text("/tools\r")
                    await wait_for(lambda: inspector is not None and inspector.app.is_running)
                    calls = {call.call_id: call.state for call in inspector.archive.calls}
                    assert calls == {
                        "call-1": "succeeded",
                        "call-2": "succeeded",
                        "call-3": "running",
                    }
                    pipe.send_text("\x1b")
                    await wait_for(lambda: not inspector.app.is_running)
                    finish.set()
                    await wait_for(lambda: not app.activity.busy)
                    pipe.send_text("/quit\r")
                    await asyncio.wait_for(task, 5)
                finally:
                    finish.set()
                    # A failed assertion leaves the modal open, and cancelling
                    # the app underneath it never returns.
                    if inspector is not None and inspector.app.is_running:
                        inspector.app.exit()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    if runtime.session is not None:
                        runtime.session.close()

    asyncio.run(run())


@pytest.mark.in_process("a hosted /quit detaches; the turn carries on in the host")
@pytest.mark.parametrize("command", ["/quit", "/exit"])
def test_quit_cancels_active_run(command):
    async def run():
        started = asyncio.Event()
        cleaned = asyncio.Event()

        class Runtime:
            session = None
            recovery_blocked = ""

            async def stream(self, text):
                try:
                    started.set()
                    await asyncio.Event().wait()
                    yield Message("unreachable")
                finally:
                    await asyncio.sleep(0.01)
                    cleaned.set()

        app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=StringIO()))
        with create_pipe_input() as pipe:

            def prompt(*args, **kwargs):
                return create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    pipe.send_text("second\r" + command + "\r")
                    await asyncio.wait_for(task, 5)
                    assert cleaned.is_set()
                    assert not app.activity.queued_prompts
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_tool_rows_hold_calls_that_finished_between_frames():
    """A loaded terminal draws less often; a call that ran entirely between two
    frames, a failure included, must still get its row."""
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running")
    for number, call_id in ((1, "one"), (2, "two")):
        activity.tools.record(ToolStarted("read_file", f"{number}.py", call_id))
        activity.tools.record(
            ToolSummary("read_file", f"{number}.py", call_id=call_id, failed=number == 2)
        )
    activity.tools.record(ToolStarted("read_file", "3.py", "three"))
    # No frame drew while `1.py` or `2.py` ran.
    assert tool_row(activity) == "√ ⎘ 1.py\n✗ ⎘ 2.py\n· ⎘ 3.py"
    # A new turn drops finished rows; the call the status row reports stays.
    activity.forget_calls()
    assert tool_row(activity) == "· ⎘ 3.py"
