"""Ctrl+L takes what the editor offers before it opens the model picker."""

import asyncio
from types import SimpleNamespace

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.auto_suggest import Suggestion
from prompt_toolkit.buffer import CompletionState
from prompt_toolkit.completion import Completion
from prompt_toolkit.input import DummyInput
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import TextArea

from pcode.prompt_keys import PromptCallbacks, prompt_key_bindings


def run(text, setup, *keys, prefix="ctrl"):
    """Type ``keys`` into a prompt holding ``text``; return (text, menu open, picker opened)."""

    async def go():
        activity = SimpleNamespace(thinking_mode="status-line", panel_shown=True, busy=False)
        opened = []
        bindings, shortcuts = prompt_key_bindings(
            activity, None, PromptCallbacks(on_model=lambda: opened.append(True)), prefix, None
        )
        editor = TextArea(text=text, multiline=True)
        buffer = editor.buffer
        buffer.cursor_position = len(text)
        app = Application(
            layout=Layout(editor),
            key_bindings=shortcuts.key_bindings(bindings),
            input=DummyInput(),
            output=DummyOutput(),
        )
        with set_app(app):
            setup(buffer)
            for key in keys:
                app.key_processor.feed(KeyPress(key))
                app.key_processor.process_keys()
            return buffer.text, buffer.complete_state is not None, bool(opened)

    return asyncio.run(go())


def ghost(text, cursor=None):
    def setup(buffer):
        buffer.suggestion = Suggestion(text)
        if cursor is not None:
            buffer.cursor_position = cursor

    return setup


def menu(*words, index=None):
    def setup(buffer):
        start = -len(buffer.document.text_before_cursor)
        completions = [Completion(word, start_position=start) for word in words]
        buffer.complete_state = CompletionState(buffer.document, completions)
        if index is not None:
            buffer.go_to_completion(index)

    return setup


def test_opens_model_picker_with_nothing_offered():
    assert run("hello", lambda buffer: None, Keys.ControlL) == ("hello", False, True)


def test_accepts_ghost_text():
    assert run("git st", ghost("atus"), Keys.ControlL) == ("git status", False, False)


def test_ghost_text_needs_cursor_at_end():
    assert run("git st", ghost("atus", cursor=0), Keys.ControlL) == ("git st", False, True)


def test_accepts_first_completion_when_none_selected():
    assert run("/mo", menu("/model", "/mode"), Keys.ControlL) == ("/model", False, False)


def test_keeps_selected_completion():
    assert run("/mo", menu("/model", "/mode", index=1), Keys.ControlL) == ("/mode", False, False)


def test_leader_letter_still_opens_picker():
    result = run("git st", ghost("atus"), Keys.ControlB, "l", prefix="ctrl+b")
    assert result == ("git st", False, True)
