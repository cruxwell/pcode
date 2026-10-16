import asyncio
from io import StringIO

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode import theme
from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.ui import PALETTES, create_prompt, frame_shade

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BACKGROUND = "#2e2a29"


@pytest.fixture
def true_color(monkeypatch):
    monkeypatch.delenv("PROMPT_TOOLKIT_COLOR_DEPTH", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")


def test_a_background_report_is_kept_with_its_theme():
    assert theme.background_theme(b"\x1b]11;rgb:2e2e/2a2a/2929\x1b\\") == "dark"
    assert theme.terminal_background() == (BACKGROUND, "dark")


def test_the_shade_steps_away_from_the_background():
    assert frame_shade("#000000", "dark") == "#0f0f0f"
    assert frame_shade("#ffffff", "light") < "#ffffff"


def transcript(detected="dark"):
    app = PreviewApp(console=Console(file=StringIO(), width=40, color_system=None))
    app.transcript.theme = "auto"
    app.transcript.detected_theme = detected
    return app.transcript


@pytest.mark.usefixtures("true_color")
def test_the_shade_follows_the_reported_background(monkeypatch):
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    assert transcript().frame_shade() == frame_shade(BACKGROUND, "dark")


@pytest.mark.usefixtures("true_color")
def test_a_report_from_another_theme_is_not_mixed_from(monkeypatch):
    # A light/dark flip reported without a color leaves the old dark one behind.
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    light = transcript("light")
    # `/syntax terminal` (the default) has no surface to fall back on.
    assert light.frame_shade() is None
    light.syntax_themes = {**light.syntax_themes, "light": "default"}
    assert light.frame_shade() == frame_shade(PALETTES["light"].surface, "light")


def test_without_true_color_the_borders_stay(monkeypatch):
    monkeypatch.delenv("PROMPT_TOOLKIT_COLOR_DEPTH", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    assert transcript().frame_shade() is None


def render(columns, *, running, task_style="status", plan=True, outcome=None, note=None):
    """The shaded layout's rows, and the resolved cell attributes of the top row and the next."""

    async def run():
        app = PreviewApp(console=Console(file=StringIO(), width=columns, color_system=None))
        app.transcript.theme = "auto"
        app.transcript.detected_theme = "dark"
        activity = app.activity
        activity.frame_style = "shaded"
        activity.task_style = task_style
        if plan:
            activity.plan = [{"content": "Example task", "status": "in_progress"}]
        if running:
            activity.prompt_state = "running"
        activity.turn_outcome = outcome
        if note is not None:
            app.transcript.retained_note(note)
        style = app.transcript.prompt_style(task_style, "shaded")
        with create_pipe_input() as pipe:
            output = Vt100_Output(
                StringIO(), lambda: Size(rows=24, columns=columns), enable_cpr=False
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
            rows = [
                [screen.data_buffer[row][col] for col in range(columns)]
                for row in range(screen.height)
            ]
            lines = ["".join(cell.char for cell in row) for row in rows]
            # The editor box is the last on screen; search up so a box above can't match.
            top = max(i for i, line in enumerate(lines) if line.endswith("┐"))
            attrs = [
                [style.get_attrs_for_style_str(cell.style) for cell in rows[row]]
                for row in (top, top + 1)
            ]
            return lines, top, attrs

    return asyncio.run(run())


@pytest.mark.usefixtures("true_color")
@pytest.mark.parametrize("columns", [20, 40, 80])
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("task_style, lead", [("status", 1), ("icons", 2)])
def test_shaded_headings_line_up_with_the_task_icons(
    monkeypatch, columns, running, task_style, lead
):
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    lines, top, (attrs, below) = render(columns, running=running, task_style=task_style)
    heading, task = lines[top], lines[top + 1]
    # The heading starts in the task icon's column, after the invisible side.
    assert heading[:lead] == " " * lead and heading[lead] != " ", heading
    assert task[:lead].strip("│ ") == "" and task[lead] != " ", task
    if running:
        assert heading[lead] in SPINNER
    else:
        assert heading[lead:].startswith("Tasks")
    # Same geometry as borders: the row fills the pane and keeps its corner.
    assert len(heading) == columns and heading.endswith("┐")
    shade = frame_shade(BACKGROUND, "dark").removeprefix("#")
    # Border glyphs, rule included, vanish into the row's shade instead of drawing a line.
    corner = attrs[-1]
    assert corner.color == corner.bgcolor
    assert not corner.dim
    # The heading row sits a touch darker than the box below it.
    assert corner.bgcolor < shade
    assert below[-1].bgcolor == shade


@pytest.mark.usefixtures("true_color")
def test_a_new_sessions_empty_top_row_keeps_the_box_shade(monkeypatch):
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    lines, top, (attrs, below) = render(40, running=False, plan=False)
    assert lines[top].strip(" ─┐") == "", lines[top]
    shade = frame_shade(BACKGROUND, "dark").removeprefix("#")
    assert attrs[-1].bgcolor == below[-1].bgcolor == shade


@pytest.mark.usefixtures("true_color")
def test_a_finished_turns_title_row_darkens_without_tasks(monkeypatch):
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    lines, top, (attrs, below) = render(40, running=False, plan=False, outcome="done")
    assert "✓" in lines[top], lines[top]
    shade = frame_shade(BACKGROUND, "dark").removeprefix("#")
    assert attrs[-1].bgcolor < shade == below[-1].bgcolor


@pytest.mark.usefixtures("true_color")
@pytest.mark.parametrize("running", [False, True])
def test_the_prompt_drops_a_row_below_a_filled_top_row(monkeypatch, running):
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    lines, top, _ = render(40, running=running, plan=False, outcome="done")
    # The box keeps its three rows: heading, a blank shaded row, then the prompt.
    assert lines[top + 1].strip(" └─┘") == "", lines[top + 1]
    assert lines[top + 2].startswith("│❯"), lines[top + 2]
    assert not any(line.startswith("└") for line in lines[top + 2 :]), lines


@pytest.mark.usefixtures("true_color")
def test_attached_tasks_keep_the_prompt_under_their_divider(monkeypatch):
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    lines, top, _ = render(40, running=False, plan=True, outcome="done")
    assert lines[top + 2].startswith("├"), lines
    assert lines[top + 3].startswith("│❯") and lines[top + 4].startswith("└"), lines


def test_drawn_borders_keep_the_prompt_under_the_title(monkeypatch):
    # Without true color the borders stay, and their line already separates the two.
    monkeypatch.delenv("PROMPT_TOOLKIT_COLOR_DEPTH", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    lines, top, _ = render(40, running=False, plan=False, outcome="done")
    assert lines[top + 1].startswith("│❯") and lines[top + 2].startswith("└"), lines


@pytest.mark.usefixtures("true_color")
def test_a_new_sessions_prompt_stays_centred(monkeypatch):
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    lines, top, _ = render(40, running=False, plan=False)
    assert lines[top + 1].startswith("│❯"), lines[top + 1]
    assert lines[top + 2].startswith("└"), lines[top + 2]


@pytest.mark.usefixtures("true_color")
def test_the_idle_editor_keeps_a_blank_row_below_startup_notes(monkeypatch):
    monkeypatch.setattr(theme, "_background", (BACKGROUND, "dark"))
    # Scrollback ending blank needs no row of the editor's own...
    lines, top, _ = render(40, running=False, plan=False)
    assert top == 0, lines
    # ...but ending on a note, as startup's repository and skill lines do, it does.
    lines, top, _ = render(40, running=False, plan=False, note="Skill commands: /skill:x")
    assert top == 1 and lines[0].strip() == "", lines
