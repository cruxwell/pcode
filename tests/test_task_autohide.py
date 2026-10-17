"""The widget can hide itself once a turn ends, without forgetting /show-tasks."""

from io import StringIO

import pytest
from rich.console import Console

from pcode.app import PreviewApp
from pcode.preferences import SETTINGS, load_preferences, save_preferences
from pcode.ui import Activity


def test_autohide_defaults_off():
    assert not Activity().autohide_tasks


def test_autohide_hides_and_restores_on_the_next_turn():
    activity = Activity(autohide_tasks=True, plan=[{"content": "Task", "status": "pending"}])
    assert activity.plan_rows(10)
    activity.finish_prompt("done")
    assert activity.prompt_state == "done"
    assert activity.show_tasks  # The preference itself is untouched.
    assert activity.plan_rows(10) == []
    activity.start_prompt("next")
    assert activity.plan_rows(10)


def test_autohide_off_keeps_the_widget_after_a_turn():
    activity = Activity(autohide_tasks=False, plan=[{"content": "Task", "status": "pending"}])
    activity.finish_prompt("failed")
    assert activity.plan_rows(10)


def test_toggle_after_autohide_shows_the_widget_again():
    activity = Activity(autohide_tasks=True, plan=[{"content": "Task", "status": "pending"}])
    activity.finish_prompt("done")
    assert activity.toggle_panel() is True
    assert activity.plan_rows(10)
    assert activity.toggle_panel() is False
    assert activity.plan_rows(10) == []


def test_reset_clears_the_auto_hidden_state():
    activity = Activity()
    activity.finish_prompt("done")
    activity.reset()
    assert not activity.tasks_autohidden


def test_command_and_preference(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert SETTINGS["autohide_tasks"].default == "off"
    app = PreviewApp(console=Console(file=StringIO()))
    assert not app.activity.autohide_tasks
    assert app.registry.dispatch("/autohide-tasks on")
    app.activity.finish_prompt("done")
    assert app.registry.dispatch("/autohide-tasks off")
    assert not app.activity.autohide_tasks
    assert not app.activity.tasks_autohidden  # Disabling reveals it immediately.
    assert load_preferences()["autohide_tasks"] == "off"
    assert not PreviewApp().activity.autohide_tasks
    assert app.registry.dispatch("/autohide-tasks on")
    assert app.activity.autohide_tasks
    with pytest.raises(ValueError, match="Usage"):
        app.registry.dispatch("/autohide-tasks invalid")
    save_preferences(autohide_tasks="off")
    assert not PreviewApp().activity.autohide_tasks


@pytest.mark.parametrize("plan", [False, True])
def test_toggle_after_autohide_hides_a_panel_with_nothing_auto_hidden(plan):
    # Autohide hid nothing on screen (no list, or no tasks), so the first
    # Ctrl+O hides the panel rather than "showing" what is already there.
    items = [{"content": "Task", "status": "pending"}] if plan else []
    activity = Activity(autohide_tasks=True, show_tasks=not plan, plan=items)
    activity.start_prompt("go")
    activity.finish_prompt("done")
    assert activity.panel_looks_shown
    assert activity.toggle_panel() is False
    assert not activity.panel_shown
