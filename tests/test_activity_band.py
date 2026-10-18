"""The activity panel's band: the thought and tool rows inside the editor box."""

import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode import theme
from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.runtime import ToolStarted, ToolSummary
from pcode.ui import (
    Activity,
    PromptLayout,
    create_prompt,
    frame_shade,
    header_shade,
)

BACKGROUND = "#2e2a29"
THOUGHT = "Checking where the config is loaded before touching the parser"
PLAN = [
    {"content": "Read the config loader", "status": "completed"},
    {"content": "Fix the parser", "status": "in_progress"},
]


def busy(activity: Activity, *, thought: str = THOUGHT, plan: bool = True) -> Activity:
    """`activity` mid-turn: a thought, a finished call and a running one."""
    activity.prompt, activity.prompt_state = "Fix it", "running"
    activity.think(thought)
    activity.tools.record(ToolStarted("read_file", "src/config.py", "one"))
    activity.tools.record(ToolSummary("read_file", "src/config.py", call_id="one"))
    activity.tools.record(ToolStarted("shell", "make test", "two"))
    if plan:
        activity.plan = [dict(item) for item in PLAN]
    return activity


def render(columns=60, *, shaded=False, configure=lambda activity: None):
    """The screen's rows, and each row's resolved cell attributes."""

    async def run():
        app = PreviewApp(console=Console(file=StringIO(), width=columns, color_system=None))
        app.transcript.theme = "auto"
        app.transcript.detected_theme = "dark"
        activity = app.activity
        activity.frame_style = "shaded" if shaded else "borders"
        busy(activity)
        configure(activity)
        style = app.transcript.prompt_style("status", activity.frame_style)
        with create_pipe_input() as pipe:
            output = Vt100_Output(
                StringIO(), lambda: Size(rows=30, columns=columns), enable_cpr=False
            )
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
            cells = [
                [screen.data_buffer[row][col] for col in range(columns)]
                for row in range(screen.height)
            ]
            lines = ["".join(cell.char for cell in row).rstrip() for row in cells]
            attrs = [[style.get_attrs_for_style_str(cell.style) for cell in row] for row in cells]
            return lines, attrs

    return asyncio.run(run())


def status_index(lines: list[str]) -> int:
    return next(i for i, line in enumerate(lines) if "Read file" in line or "Run shell" in line)


@pytest.mark.parametrize("plan", [True, False])
def test_the_thought_hangs_under_the_status_row_and_calls_sit_above_the_box(plan):
    lines, _ = render(configure=lambda activity: None if plan else activity.plan.clear())
    status = status_index(lines)
    assert lines[status].startswith("┌─ ")
    # The calls are plain rows over the box, newest last, as before.
    done, running = lines[status - 2 : status]
    assert done.startswith(" √ ⎘ src/config.py"), done
    assert "$ make test" in running, running
    thought, continued, divider = lines[status + 1 : status + 4]
    # Quoted: the mark opens every row of the thought.
    assert thought.startswith("│❝ Checking where the config")
    assert continued.startswith("│❝ ") and continued[3] != " ", continued
    assert divider.startswith("├─") and divider.endswith("─┤")
    if plan:
        assert lines[status + 4].startswith("│✔ Read the config loader")
        assert lines[status + 6].startswith("├─")
        assert lines[status + 7].startswith("│❯")
    else:
        assert lines[status + 4].startswith("│❯")
    assert not any("❝" in line for line in lines[:status])


def test_detached_activity_keeps_its_rows_above_the_box():
    def detach(activity):
        activity.attach_activity = False

    lines, _ = render(configure=detach)
    status = status_index(lines)
    assert lines[status - 4].startswith(" ❝ Checking where the config")
    assert lines[status - 3].startswith(" ❝ ")
    assert "make test" in lines[status - 1]
    assert lines[status + 1].startswith("│✔ Read the config loader")


@pytest.mark.parametrize("attached", [True, False])
def test_hiding_the_panel_hides_the_thought_and_tool_rows_too(attached):
    def hide(activity):
        activity.attach_activity = attached
        activity.show_activity = False

    lines, _ = render(configure=hide)
    status = status_index(lines)
    # The status row stays, carrying the running call's command the hidden
    # tool row would have shown.
    assert "Run shell · make test" in lines[status]
    rest = lines[:status] + lines[status + 1 :]
    assert not any("❝" in line or "make test" in line or "Read the" in line for line in rest)
    assert lines[status + 1].startswith("│❯")


def test_hiding_the_tasks_keeps_the_thought_and_tool_rows():
    def hide(activity):
        activity.show_tasks = False

    lines, _ = render(configure=hide)
    status = status_index(lines)
    assert lines[status + 1].startswith("│❝ Checking where the config")
    assert "make test" in lines[status - 1]
    assert not any("Read the" in line for line in lines)
    # The status row says what the hidden list would: the step in progress.
    assert "Fix the parser" in lines[status]


def test_a_hidden_panel_leaves_parallel_calls_the_plans_step():
    def hide(activity):
        activity.tools.record(ToolStarted("shell", "make lint", "three"))
        activity.show_activity = False

    lines, _ = render(configure=hide)
    status = next(line for line in lines if "Running 2 tools" in line)
    assert "Fix the parser" in status and "make" not in status


@pytest.mark.usefixtures("true_color")
def test_the_band_takes_the_header_shade_and_its_divider_the_box(monkeypatch):
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    lines, attrs = render(shaded=True)
    status = status_index(lines)
    box = frame_shade(BACKGROUND, "dark").removeprefix("#")
    header = header_shade(BACKGROUND, "dark").removeprefix("#")
    shades = [{cell.bgcolor for cell in row} for row in attrs]
    # The calls above the box keep the terminal's own background, the status
    # row and the thought read as one header block, and the divider, tasks
    # and prompt keep the box's shade.
    assert header not in shades[status - 1] and box not in shades[status - 1], shades
    assert all(shades[row] == {header} for row in range(status, status + 3)), shades
    assert all(shades[row] == {box} for row in range(status + 3, status + 8)), shades


@pytest.fixture
def true_color(monkeypatch):
    monkeypatch.delenv("PROMPT_TOOLKIT_COLOR_DEPTH", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")


def test_the_header_step_looks_alike_on_either_theme():
    # Dark, halfway back to the background, even on pure black.
    assert header_shade("#000000", "dark") == "#080808"
    assert frame_shade("#292929", "dark") == "#363636"
    assert header_shade("#292929", "dark") == "#303030"
    # Light, a small step darker than the box.
    assert frame_shade("#ffffff", "light") == "#f4f4f4"
    assert header_shade("#ffffff", "light") == "#ededed"


@pytest.fixture
def layout():
    activity = busy(Activity(), plan=False)
    with create_pipe_input() as pipe:
        layout = PromptLayout(PromptSession(input=pipe, output=DummyOutput()), activity, None, None)
    with patch.object(layout, "size", return_value=Size(rows=40, columns=40)):
        yield layout


def test_the_band_follows_the_thought_without_padding(layout):
    activity = layout.activity
    assert len(layout.band_rows()) == 2  # The thought wraps to two rows here.
    # A shorter thought shrinks the band at once: no blank rows held under it.
    activity.forget_thought()
    activity.think("Short")
    assert len(layout.band_rows()) == 1 and all(layout.band_rows())
    activity.forget_thought()
    activity.think(THOUGHT)
    assert len(layout.band_rows()) == 2


def test_an_empty_band_goes_at_once(layout):
    assert layout.band_rows()
    layout.activity.show_activity = False
    assert layout.band_rows() == []
    assert layout.band_height() == 0


def test_a_height_cap_gives_the_thought_up_first(layout):
    activity = layout.activity
    # Status, prompt and bottom rows, the band's divider, and one thought row.
    activity.tasks_max_height = 5
    assert layout.band_budget() == 1
    (row,) = layout.band_rows()
    assert row[1][1].endswith("…")
    assert layout.band_height() == 2
    activity.tasks_max_height = 4
    assert layout.band_rows() == []


@pytest.mark.parametrize("attached", [True, False])
def test_a_sub_agent_without_a_plan_keeps_the_box_under_its_cap(attached):
    def delegate(activity):
        activity.attach_tasks = attached
        activity.plan.clear()
        activity.tasks_max_height = 10
        activity.tools.record(
            ToolStarted(
                "delegate_task",
                "explorer · look around",
                "agent",
                activity="Working",
                agent="explorer",
                task="look around",
            )
        )

    lines, _ = render(configure=delegate)
    top = next(i for i, line in enumerate(lines) if line.startswith("┌"))
    bottom = max(i for i, line in enumerate(lines) if line.startswith("└"))
    assert "» Explorer" in "\n".join(lines), lines
    # The cap covers the editor box and a detached task box together.
    assert bottom - top + 1 <= 10, lines


def test_show_activity_and_show_tasks_switch_the_panel_and_its_list_apart():
    app = PreviewApp(console=Console(file=StringIO()))
    activity = app.activity
    assert app.registry.dispatch("/show-tasks off")
    assert activity.panel_shown and not activity.tasks_shown
    assert app.registry.dispatch("/show-activity off")
    assert not activity.panel_shown
    assert app.registry.dispatch("/show-tasks on")
    # The list's own switch is on, but the panel it lives in is still hidden.
    assert activity.show_tasks and not activity.tasks_shown
    assert app.registry.dispatch("/show-activity on")
    assert activity.tasks_shown
