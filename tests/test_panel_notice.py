"""Short-lived acknowledgements belong to the live panel, never to scrollback."""

import asyncio
from io import StringIO

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.ui import NOTICE_ROWS, WAIT_GRACE_SECONDS, Activity, Transcript, create_prompt


def test_notice_wraps_to_the_pane_and_is_bounded():
    activity = Activity()
    assert activity.notice_rows(40) == []
    activity.flash("Show thinking: off. Usage: /show-thinking [on|off] (Ctrl+T)")
    rows = activity.notice_rows(30)
    assert [style for style, _ in rows] == ["class:activity.notice"] * len(rows)
    assert all(len(text) <= 30 for _, text in rows)
    assert "".join(text for _, text in rows).replace(" ", "").startswith("Showthinking:off.")
    activity.flash("\n".join(f"line {i}" for i in range(NOTICE_ROWS + 4)))
    assert len(activity.notice_rows(80)) == NOTICE_ROWS


def test_notice_expires_without_further_input():
    activity = Activity()
    activity.flash("Theme: light.", seconds=0.0)
    assert not activity.notice_shown
    assert activity.notice_rows(80) == []


def test_flash_without_a_live_panel_falls_back_to_a_printed_note():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None), activity=Activity())
    transcript.flash("Theme: light.")
    assert "Theme: light." in stream.getvalue()


def test_a_wait_shows_a_spinner_row_only_once_it_outlasts_the_grace_period():
    activity = Activity()
    with activity.waiting("Starting the session host") as wait:
        assert activity.wait_fragments("|", 80) == []
        wait.started -= WAIT_GRACE_SECONDS
        ((style, text),) = activity.wait_fragments("|", 80)
        assert style == "class:activity.system"
        assert text.startswith("| ◈ Starting the session host · ")
        assert len(activity.wait_fragments("|", 12)[0][1]) <= 12
        # The newest wait is named; the elapsed time is the oldest one's.
        wait.started -= 10
        later = activity.begin_wait("Running /model")
        later.started -= WAIT_GRACE_SECONDS
        assert activity.wait_fragments("|", 80)[0][1].startswith("| ◈ Running /model · 10s")
        # A live status row (a host job or turn) covers the same work: one spinner.
        activity.start_prompt("Merging worktree", kind="system", detail="pcode-x")
        assert activity.wait_fragments("|", 80) == []
        activity.finish_prompt("done")
        assert activity.wait_fragments("|", 80)
        activity.end_wait(later)
    assert activity.waits == []
    assert activity.wait_fragments("|", 80) == []


def test_a_wait_renders_above_the_editor():
    async def run():
        stream = StringIO()
        app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=app.activity,
                transcript=app.transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
            )
            app.activity.begin_wait("Loading saved sessions").started -= WAIT_GRACE_SECONDS
            stream.seek(0)
            stream.truncate()
            with set_app(session.app):
                session.app.renderer.render(session.app, session.app.layout)
            return stream.getvalue()

    screen = asyncio.run(run())
    assert "Loading saved sessions" in screen
    assert screen.index("Loading saved sessions") < screen.index("┌")


def test_idle_editor_border_names_the_session_once_it_has_a_name():
    title = ""

    def tops(activity, columns=40):
        async def run():
            stream = StringIO()
            app = PreviewApp(console=Console(file=stream, width=columns, color_system=None))
            with create_pipe_input() as pipe:
                output = Vt100_Output(
                    stream, lambda: Size(rows=24, columns=columns), enable_cpr=False
                )
                session = create_prompt(
                    CommandRegistry(),
                    activity=activity,
                    transcript=app.transcript,
                    on_submit=lambda text: None,
                    session_title=lambda: title,
                    input=pipe,
                    output=output,
                )
                with set_app(session.app):
                    session.app.renderer.render(session.app, session.app.layout)
                screen = session.app.renderer._last_screen
                return [
                    "".join(screen.data_buffer[row][col].char for col in range(columns)).rstrip()
                    for row in range(screen.height)
                ]

        return [line for line in asyncio.run(run()) if line.startswith("┌")]

    def editor_top(activity, columns=40):
        # The editor box is the lowest box; a detached task box sits above it.
        return tops(activity, columns)[-1]

    # No name yet (a new session): a plain rule.
    assert editor_top(Activity()) == "┌" + "─" * 38 + "┐"
    title = "Rework\nthe status row"
    # No outcome known yet (a resumed session): a muted dot leads the name.
    assert editor_top(Activity()) == "┌─ • Rework the status row " + "─" * 12 + "┐"
    # A long name is cut to the pane, leaving the rule its corners.
    title = "x" * 100
    top = editor_top(Activity())
    assert len(top) == 40 and top.endswith("… ─┐"), top
    # While a turn runs, the status row has the border.
    running = Activity(prompt="Fix", prompt_state="running", status="Thinking…")
    assert "x" not in editor_top(running)
    assert "Thinking" in editor_top(running)
    # Attached tasks head the idle box, and the name rides along.
    title = "Rework the status row"
    planned = Activity()
    planned.plan = [{"content": "Inspect", "status": "completed"}]
    assert editor_top(planned, 60).startswith("┌─ Tasks 1/1 · Rework the status row ─")
    # Detached, the task box has its own heading and the editor keeps the name.
    planned.attach_tasks = False
    tasks, editor = tops(planned, 60)
    assert tasks.startswith("┌─ Tasks 1/1 ─")
    assert editor.startswith("┌─ • Rework the status row ─")
    # The last turn's outcome leads the name, and shows before there is one.
    for state, mark in [("done", "✓"), ("cancelled", "⊘"), ("failed", "✗")]:
        assert editor_top(Activity(turn_outcome=state)).startswith(f"┌─ {mark} Rework the status ")
    title = ""
    assert editor_top(Activity(turn_outcome="failed")).startswith("┌─ ✗ ─")
    title = "Rework the status row"
    planned.attach_tasks = True
    planned.turn_outcome = "done"
    assert editor_top(planned, 60).startswith("┌─ Tasks 1/1 · ✓ Rework the status row ─")


def test_session_label_colours_the_name_and_a_failure_in_red():
    from pcode.ui import PALETTES, PromptLayout

    layout = PromptLayout.__new__(PromptLayout)
    layout.activity = Activity(turn_outcome="failed")
    layout.session_title = lambda: "Fix login"
    assert layout.session_label() == [
        ("class:session.failed", "✗"),
        ("", " "),
        ("class:session.name", "Fix login"),
    ]
    palette = PALETTES["dark"]
    style = dict(palette.prompt_style().style_rules)
    assert "ansired" in style["session.failed"]
    # Coloured like the task heading, neither dimmed nor bold.
    assert palette.task_heading in style["session.name"]
    assert "bold" not in style["session.name"]


def test_toggle_renders_above_the_editor_instead_of_entering_scrollback():
    async def run():
        stream = StringIO()
        app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=app.activity,
                transcript=app.transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
            )
            app.transcript.output = type(
                "Stub",
                (),
                {
                    "app": session.app,
                    "print": lambda *a, **k: None,
                    "typing_fragments": lambda self: [],
                },
            )()
            app.show_thinking("off")
            stream.seek(0)
            stream.truncate()
            with set_app(session.app):
                session.app.renderer.render(session.app, session.app.layout)
            return stream.getvalue()

    screen = asyncio.run(run())
    assert "Thinking: off" in screen
    # The frame below it proves the notice is chrome above the editor.
    assert screen.index("Thinking: off") < screen.index("┌")


def test_typed_row_sits_directly_under_scrollback_above_the_status():
    async def run():
        stream = StringIO()
        app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
        app.activity.prompt_state = "running"
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=app.activity,
                transcript=app.transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
            )
            app.transcript.output = type(
                "Stub",
                (),
                {
                    "app": session.app,
                    "print": lambda *a, **k: None,
                    "typing_fragments": lambda self: [("bold", "Half a sen")],
                },
            )()
            with set_app(session.app):
                session.app.renderer.render(session.app, session.app.layout)
            screen = session.app.renderer._last_screen
            return [
                "".join(screen.data_buffer[row][col].char for col in range(80)).rstrip()
                for row in range(screen.height)
            ]

    lines = asyncio.run(run())
    # The layout's first row is the one under the cursor, where scrollback
    # ends, so the typed text continues it flush left without a gap.
    assert lines[0] == "Half a sen"
    status = next(i for i, line in enumerate(lines) if "Working" in line)
    # The status rides the editor box's top border.
    assert lines[status].startswith("┌─ ")
    # Scrollback's own gap sits between the typed row and the box.
    assert lines[status - 1] == ""


@pytest.mark.parametrize("commands", [False, True])
@pytest.mark.parametrize("tasks", [False, True])
@pytest.mark.parametrize("attached", [False, True])
def test_main_status_rides_the_editor_box_and_jobs_only_appear_in_footer(commands, tasks, attached):
    from pcode.aside import Aside
    from pcode.runtime import CommandOutput

    async def run():
        stream = StringIO()
        app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
        activity = app.activity
        activity.prompt_state = "running"
        activity.asides = [Aside(question="why?")]
        activity.job_count = 2
        activity.thought = "Latest thought"
        activity.flash("Notice above status")
        activity.show_tasks = tasks
        activity.attach_tasks = attached
        activity.plan = [{"content": "Example task", "status": "in_progress"}]
        app.transcript.command_scrollback = commands
        activity.command_outputs["one"] = CommandOutput("one", "example", "Tool output")
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=app.transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
                bottom_toolbar=app.toolbar,
            )
            with set_app(session.app):
                session.app.renderer.render(session.app, session.app.layout)
            screen = session.app.renderer._last_screen
            return [
                "".join(screen.data_buffer[row][col].char for col in range(80)).rstrip()
                for row in range(screen.height)
            ]

    lines = [line for line in asyncio.run(run()) if line.strip()]
    status = next(line for line in lines if "Working" in line)
    aside = next(line for line in lines if " btw " in line)
    assert status.startswith("┌─ ") and status.endswith("─┐"), status
    assert len(status) == 80, status
    assert len(aside) - len(aside.lstrip()) == 1, aside
    status_index = lines.index(status)
    # Everything else live sits above the box: side questions, the thought,
    # notices, a command's output, and tasks drawn in a frame of their own.
    assert lines.index(aside) < status_index
    assert next(i for i, line in enumerate(lines) if "Latest thought" in line) < status_index
    assert next(i for i, line in enumerate(lines) if "Notice above status" in line) < status_index
    if commands:
        assert next(i for i, line in enumerate(lines) if "Tool output" in line) < status_index
    if tasks:
        task = next(i for i, line in enumerate(lines) if "Example task" in line)
        if attached:
            # Under the status, with no heading row of their own: its count
            # rides the status instead.
            assert task == status_index + 1
            assert "Tasks 0/1 · " in status
            # The step turns with the running commands' arc spinner.
            assert lines[task][0] == "│" and lines[task][1] in "◜◠◝◞◡◟"
            assert lines[task][2:].startswith(" Example task")
        else:
            assert task < status_index
            assert "Tasks" not in status
    else:
        # Hidden tasks give the status no count either.
        assert "Tasks" not in status
        assert lines[status_index + 1].startswith("│❯")
    assert "2 jobs" in lines[-1]
    assert sum("jobs" in line for line in lines) == 1


@pytest.mark.parametrize("columns", [4, 5, 8, 12, 20, 33, 80])
def test_status_border_fills_the_pane_exactly_at_any_width(columns):
    async def run():
        stream = StringIO()
        app = PreviewApp(console=Console(file=stream, width=columns, color_system=None))
        activity = app.activity
        activity.prompt_state = "running"
        activity.plan = [{"content": "Example task", "status": "in_progress"}]
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=columns), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=app.transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
            )
            with set_app(session.app):
                session.app.renderer.render(session.app, session.app.layout)
            screen = session.app.renderer._last_screen
            return [
                "".join(screen.data_buffer[row][col].char for col in range(columns))
                for row in range(screen.height)
            ]

    lines = asyncio.run(run())
    cursor = next(i for i, line in enumerate(lines) if line.startswith("│❯"))
    top = max(i for i, line in enumerate(lines[:cursor]) if line.startswith("┌"))
    assert len(lines[top].rstrip()) == columns, lines[top]
    assert lines[top].endswith("┐"), lines[top]
    if columns >= 20:
        assert "Working" in lines[top]


@pytest.mark.parametrize(
    "task_style, row", [("status", "│{} Example task"), ("icons", "│ {} Example task")]
)
def test_icon_task_style_sets_task_rows_off_the_frame(task_style, row):
    async def run():
        stream = StringIO()
        app = PreviewApp(console=Console(file=stream, width=40, color_system=None))
        activity = app.activity
        activity.task_style = task_style
        activity.plan = [{"content": "Example task", "status": "in_progress"}]
        activity.prompt_state = "running"
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=40), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=app.transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
            )
            with set_app(session.app):
                session.app.renderer.render(session.app, session.app.layout)
            screen = session.app.renderer._last_screen
            return [
                "".join(screen.data_buffer[r][c].char for c in range(40))
                for r in range(screen.height)
            ]

    lines = asyncio.run(run())
    task = next(line for line in lines if "Example task" in line)
    # The step in progress turns with the running commands' arc spinner.
    frames = tuple(row.format(frame) for frame in "◜◠◝◞◡◟")
    assert task.startswith(frames) and task.endswith("│"), task
