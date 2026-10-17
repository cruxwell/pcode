"""The thinking shortcut selects a mode rather than blindly cycling it."""

import asyncio
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.input import DummyInput
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import TextArea

from pcode.prompt_keys import PromptCallbacks, prompt_key_bindings


def press(app, *keys):
    for key in keys:
        app.key_processor.feed(KeyPress(key))
        app.key_processor.process_keys()


@pytest.mark.parametrize(
    "prefix,opening", [("ctrl", (Keys.ControlT,)), ("ctrl+b", (Keys.ControlB, "t"))]
)
@pytest.mark.parametrize("callback", [False, True])
def test_thinking_chooser_refreshes_current_selection(prefix, opening, callback):
    async def run():
        activity = SimpleNamespace(thinking_mode="off", panel_shown=True, busy=False)
        selected = []

        def set_mode(mode):
            selected.append(mode)
            activity.thinking_mode = mode

        draft = TextArea(text="Keep this draft", multiline=True)
        keys, shortcuts = prompt_key_bindings(
            activity,
            None,
            PromptCallbacks(on_thinking=set_mode if callback else None),
            prefix,
            lambda: draft.buffer,
        )
        app = Application(
            layout=Layout(draft),
            key_bindings=shortcuts.key_bindings(keys),
            input=DummyInput(),
            output=DummyOutput(),
        )
        choices = [
            ("o", "off", "Off"),
            ("s", "status-line", "Status line"),
            ("b", "scrollback", "Scrollback"),
        ]
        with set_app(app):
            document = draft.buffer.document
            focus = app.layout.current_control
            expected_calls = []
            for key, mode, _ in choices:
                before = activity.thinking_mode
                press(app, *opening)
                assert shortcuts.help_title == "Thinking visibility"
                assert shortcuts.hint_rows() == [
                    (letter, label + (" (current)" if value == before else ""))
                    for letter, value, label in choices
                ]
                assert shortcuts.hint_footer() == [("Esc", "cancel")]
                assert activity.thinking_mode == before
                assert selected == expected_calls
                press(app, key)
                if callback:
                    expected_calls.append(mode)
                assert selected == expected_calls
                assert activity.thinking_mode == mode
                assert not shortcuts.visible
                assert draft.buffer.document == document
                assert app.layout.current_control is focus

            press(app, *opening, Keys.Escape)
            assert activity.thinking_mode == "scrollback"
            assert selected == expected_calls
            assert not shortcuts.visible
            assert draft.buffer.document == document
            assert app.layout.current_control is focus

    asyncio.run(run())


def test_main_shortcut_labels():
    activity = SimpleNamespace(thinking_mode="off", panel_shown=True, busy=False)
    callbacks = PromptCallbacks(
        on_send_mode=lambda: None,
        on_model=lambda: None,
        on_effort=lambda direction: None,
        on_commands=lambda: None,
    )
    _, shortcuts = prompt_key_bindings(activity, None, callbacks, "ctrl+b", lambda: None)
    labels = {shortcut.key: shortcut.label for shortcut in shortcuts.available()}
    assert labels == {
        "s": "Cycle send mode",
        "l": "Select model",
        "n": "Increase thinking effort",
        "p": "Decrease thinking effort",
        "o": "Hide activity panel",
        "t": "Select thinking visibility",
        "g": "Toggle command output",
        "y": "Copy draft / choose response",
    }
    activity.panel_shown = False
    assert {shortcut.key: shortcut.label for shortcut in shortcuts.available()}["o"] == (
        "Show activity panel"
    )
