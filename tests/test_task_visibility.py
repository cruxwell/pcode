"""Hiding the task list, or the whole activity panel, never touches its data."""

import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.preferences import SETTINGS, load_preferences, save_preferences
from pcode.ui import Activity, create_prompt


def test_visibility_preference_and_command(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert SETTINGS["show_tasks"].default == "on"
    app = PreviewApp(console=Console(file=StringIO()))
    assert app.activity.show_tasks
    assert app.registry.dispatch("/show-tasks off")
    assert not app.activity.show_tasks
    assert load_preferences()["show_tasks"] == "off"
    assert not PreviewApp().activity.show_tasks
    assert app.registry.dispatch("/show-tasks")  # Bare toggles.
    assert app.activity.show_tasks
    assert app.registry.dispatch("/show-tasks on")
    assert app.activity.show_tasks
    assert PreviewApp().activity.show_tasks
    save_preferences(show_tasks="off")
    assert not PreviewApp().activity.show_tasks
    with pytest.raises(ValueError, match="Usage"):
        app.registry.dispatch("/show-tasks invalid")
    with pytest.raises(ValueError):
        SETTINGS["show_tasks"].validate("show_tasks", "invalid")


def test_hidden_widget_preserves_data_and_reset_preserves_visibility():
    activity = Activity(plan=[{"content": "Retained task", "status": "pending"}])
    assert activity.plan_rows(10)
    activity.show_tasks = False
    assert activity.plan_rows(10) == []
    activity.plan.append({"content": "New task", "status": "pending"})
    activity.show_tasks = True
    assert len(activity.plan_rows(10)) >= 2
    activity.show_tasks = False
    activity.reset()
    assert not activity.show_tasks


@pytest.mark.parametrize("editing_mode", ["emacs", "vi"])
def test_shortcut_hides_rows_without_losing_state(tmp_path, monkeypatch, editing_mode):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = PreviewApp(console=Console(file=StringIO()))
    activity = app.activity
    activity.plan = [{"id": "1", "content": "Retained task", "status": "in_progress"}]
    activity.busy = True
    rows = activity.plan_rows(10)
    assert rows
    with create_pipe_input() as pipe:
        session = create_prompt(
            app.registry,
            activity=activity,
            on_panel=app.set_show_activity,
            input=pipe,
            output=DummyOutput(),
            editing_mode=editing_mode,
        )

        # Editing the buffer schedules validation as a background task on the
        # current application, so it needs this session and a running loop.
        async def type_draft():
            with set_app(session.app):
                session.default_buffer.text = "retained draft"

        asyncio.run(type_draft())
        with set_app(session.app):
            (binding,) = [
                binding
                for binding in session.key_bindings.get_bindings_for_keys((Keys.ControlO,))
                if binding.filter()
            ]
        event = SimpleNamespace(app=session.app)
        binding.handler(event)
        assert activity.plan_rows(10) == []
        assert activity.plan[0]["content"] == "Retained task"
        assert activity.busy
        # Ctrl+O hides the whole panel; the task list's own switch stays on.
        assert load_preferences()["show_activity"] == "off"
        assert activity.show_tasks
        binding.handler(event)
        assert activity.plan_rows(10) == rows
        assert session.default_buffer.text == "retained draft"
        assert load_preferences()["show_activity"] == "on"
        activity.reset()
        assert activity.show_activity
