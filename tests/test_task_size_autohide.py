"""The task widget hides in a pane below `tasks_min_rows`/`tasks_min_columns`.

Each terminal measures its own pane, so one preference suits a full screen,
a stacked split (short) and a side-by-side split (narrow) alike.
"""

import asyncio
from io import StringIO

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.preferences import SETTINGS, load_preferences, save_preferences, screen_rows
from pcode.ui import Activity, PromptLayout, Transcript, create_prompt

PLAN = [{"content": "TASK", "status": "pending"}]


def small_pane_activity(**kwargs) -> Activity:
    activity = Activity(tasks_min_rows=30, tasks_min_columns=100, plan=list(PLAN), **kwargs)
    activity.fit_screen(80, 24)
    return activity


def test_thresholds_are_whole_numbers():
    # 0 is allowed: it never hides. (conftest zeroes both for the rest of the suite.)
    assert SETTINGS["tasks_min_rows"].validate("tasks_min_rows", "0") is None
    with pytest.raises(ValueError):
        SETTINGS["tasks_min_rows"].validate("tasks_min_rows", "0.5")


@pytest.mark.parametrize(
    "columns,rows,shown",
    [(200, 50, True), (200, 24, False), (90, 50, False), (100, 30, True)],
)
def test_short_or_narrow_panes_hide_the_widget(columns, rows, shown):
    activity = Activity(tasks_min_rows=30, tasks_min_columns=100, plan=list(PLAN))
    activity.fit_screen(columns, rows)
    assert activity.panel_shown is shown
    assert bool(activity.plan_rows(10)) is shown
    assert activity.show_tasks  # The preference is never touched.


def test_zero_never_hides():
    activity = Activity(plan=list(PLAN))
    activity.fit_screen(20, 5)
    assert activity.panel_shown


def test_toggle_in_a_small_pane_overrides_without_turning_tasks_off():
    activity = small_pane_activity()
    assert not activity.panel_shown
    assert activity.toggle_tasks() is True  # Persisted as on: nothing changes.
    assert activity.panel_shown
    assert activity.toggle_tasks() is True  # Hiding again keeps the preference on.
    assert not activity.panel_shown and activity.show_tasks


def test_toggle_in_a_small_pane_turns_a_disabled_widget_on():
    activity = small_pane_activity(show_tasks=False)
    assert activity.toggle_tasks() is True
    assert activity.panel_shown


def test_crossing_the_threshold_drops_the_override():
    activity = small_pane_activity()
    activity.toggle_tasks()
    activity.fit_screen(80, 20)  # Still small: the override stands.
    assert activity.panel_shown
    activity.fit_screen(200, 50)
    assert activity.panel_shown
    activity.fit_screen(80, 24)  # A new split decides afresh.
    assert not activity.panel_shown


def test_autohide_after_a_turn_still_applies_in_a_large_pane():
    activity = Activity(tasks_min_rows=30, autohide_tasks=True, plan=list(PLAN))
    activity.fit_screen(200, 50)
    activity.finish_prompt("done")
    assert not activity.panel_shown


def render_tasks(size: list[Size]) -> list[int]:
    """TASK rows drawn at each size, resizing one live prompt between frames."""

    async def run():
        stream = StringIO()
        activity = Activity(tasks_min_rows=30, tasks_min_columns=100, plan=list(PLAN))
        view = Transcript(Console(file=stream), activity=activity)
        current = [size[0]]
        with create_pipe_input() as pipe:
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=view,
                on_submit=lambda text: None,
                input=pipe,
                output=Vt100_Output(stream, lambda: current[0], enable_cpr=False),
            )
            app = session.app
            counts = []
            with set_app(app):
                try:
                    for each in size:
                        current[0] = each
                        app.renderer.render(app, app.layout)
                        counts.append(
                            sum(
                                fragment_list_to_text(to_formatted_text(w.content.text)).count(
                                    "TASK"
                                )
                                for w in app.layout.find_all_windows()
                                if w.render_info is not None
                                and isinstance(w.content, FormattedTextControl)
                            )
                        )
                    return counts
                finally:
                    await app.cancel_and_wait_for_background_tasks()

    return asyncio.run(run())


def test_the_drawn_widget_follows_resizes():
    full = Size(rows=50, columns=200)
    stacked = Size(rows=24, columns=200)
    side = Size(rows=50, columns=90)
    assert render_tasks([full, stacked, full, side, full]) == [1, 0, 1, 0, 1]


def test_config_applies_thresholds_at_once():
    app = PreviewApp(console=Console(file=StringIO()))
    app.activity.plan = list(PLAN)
    app.activity.fit_screen(80, 24)
    assert app.activity.panel_shown
    app.registry.dispatch("/config set tasks_min_rows 30")
    app.activity.fit_screen(80, 24)
    assert not app.activity.panel_shown
    assert PreviewApp().activity.tasks_min_rows == 30


def test_show_tasks_command_in_a_small_pane_never_saves_off():
    app = PreviewApp(console=Console(file=StringIO()))
    activity = app.activity
    activity.plan = list(PLAN)
    activity.tasks_min_rows = 30
    activity.fit_screen(80, 24)
    assert not activity.panel_shown
    app.registry.dispatch("/show-tasks")  # Bare: show it here.
    assert activity.panel_shown and load_preferences().get("show_tasks", "on") == "on"
    app.registry.dispatch("/show-tasks")  # And hide it again, still saved as on.
    assert not activity.panel_shown and load_preferences()["show_tasks"] == "on"
    app.registry.dispatch("/show-tasks on")
    assert activity.panel_shown
    app.registry.dispatch("/show-tasks off")
    assert not activity.panel_shown and load_preferences()["show_tasks"] == "off"


def test_a_running_turn_still_counts_tasks_hidden_by_size():
    async def run():
        stream = StringIO()
        activity = Activity(tasks_min_rows=30, plan=list(PLAN))
        activity.start_prompt("go")
        view = Transcript(Console(file=stream), activity=activity)
        with create_pipe_input() as pipe:
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=view,
                on_submit=lambda text: None,
                input=pipe,
                output=Vt100_Output(stream, lambda: Size(rows=24, columns=120), enable_cpr=False),
            )
            layout = PromptLayout(session, activity, view, session.shortcuts)
            border = layout.status_border().content.text()
            return fragment_list_to_text(to_formatted_text(border)), activity.panel_shown

    border, shown = asyncio.run(run())
    assert not shown
    assert "Tasks 0/1" in border


@pytest.mark.parametrize("value,rows", [(0.25, 10), (0.5, 20), (12.0, 12), (60.0, 40)])
def test_screen_rows(value, rows):
    assert screen_rows(value, 40) == rows


@pytest.mark.parametrize(
    "setting,screen,rows",
    # A fixed count is capped at a quarter of the pane, less the tool row.
    [("10", 80, 10), ("10", 24, 6), ("0.5", 40, 20), ("0.1", 5, 1)],
)
def test_thinking_rows_scale_with_the_pane(setting, screen, rows):
    save_preferences(thinking_max_lines=setting)
    thought = " ".join(f"word{i}" for i in range(2000))
    activity = Activity(prompt_state="running", status="Thinking…", thought=thought)
    with create_pipe_input() as pipe:
        session = create_prompt(
            CommandRegistry(),
            activity=activity,
            input=pipe,
            output=Vt100_Output(
                StringIO(), lambda: Size(rows=screen, columns=60), enable_cpr=False
            ),
        )
        layout = PromptLayout(session, activity, None, session.shortcuts)
        assert len(layout.thought_rows()) == rows
