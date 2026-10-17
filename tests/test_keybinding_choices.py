"""Choices share the help overlay and never borrow the focused editor's draft."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.input import DummyInput
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import TextArea

from pcode.prefix_keys import Choice, PrefixKeys
from pcode.prompt_keys import PromptCallbacks, prompt_key_bindings


@contextmanager
def surface(shortcuts, keys=None):
    draft = TextArea(text="Keep my draft", multiline=True)
    other = TextArea(text="Other editor")
    app = Application(
        layout=Layout(HSplit([draft, other]), focused_element=draft),
        key_bindings=shortcuts.key_bindings(keys or KeyBindings()),
        input=DummyInput(),
        output=DummyOutput(),
    )
    with set_app(app):
        yield app, draft


def press(app, *keys):
    for key in keys:
        app.key_processor.feed(key if isinstance(key, KeyPress) else KeyPress(key))
        app.key_processor.process_keys()


@pytest.mark.parametrize(
    "prefix,opening", [("ctrl", (Keys.ControlT,)), ("ctrl+b", (Keys.ControlB, "t"))]
)
@pytest.mark.parametrize(
    "selection,mode", [("o", "off"), ("a", "activity-panel"), ("b", "scrollback")]
)
@pytest.mark.parametrize("callback", [False, True])
def test_thinking_chooses_explicit_mode(prefix, opening, selection, mode, callback):
    async def run():
        activity = SimpleNamespace(thinking_mode="activity-panel", panel_shown=True, busy=False)
        selected = []
        keys, shortcuts = prompt_key_bindings(
            activity,
            None,
            PromptCallbacks(on_thinking=selected.append if callback else None),
            prefix,
            lambda: None,
        )
        with surface(shortcuts, keys) as (app, draft):
            before = draft.buffer.document
            focus = app.layout.current_control
            press(app, *opening)
            assert shortcuts.visible and not shortcuts.pending
            assert shortcuts.help_title == "Thinking visibility"
            assert shortcuts.hint_rows() == [
                ("o", "Off"),
                ("a", "Activity panel (current)"),
                ("b", "Scrollback"),
            ]
            assert shortcuts.hint_footer() == [("Esc", "cancel")]
            assert selected == [] and activity.thinking_mode == "activity-panel"
            press(app, selection)
            assert not shortcuts.visible
            assert selected == ([mode] if callback else [])
            assert activity.thinking_mode == ("activity-panel" if callback else mode)
            assert draft.buffer.document == before
            assert app.layout.current_control is focus
            assert shortcuts.help_title == "Keybindings · Prompt"

    asyncio.run(run())


@pytest.mark.parametrize("prefix", ["ctrl", "ctrl+b"])
def test_chooser_cancel_unknown_and_f1_browse(prefix):
    async def run():
        shortcuts = PrefixKeys(prefix)
        fired = []
        shortcuts.add("t", "Choose")(
            lambda event: shortcuts.choose(
                "Pick one",
                [Choice("a", "Alpha", lambda event: fired.append("alpha"))],
            )
        )
        keys = KeyBindings()
        keys.add(Keys.Escape, eager=True)(lambda event: fired.append("underlying escape"))
        with surface(shortcuts, keys) as (app, draft):
            opening = (Keys.ControlT,) if prefix == "ctrl" else (Keys.ControlB, "t")
            press(app, *opening, "z")
            assert shortcuts.visible and "No choice" in shortcuts.message
            press(app, Keys.Escape)
            assert not shortcuts.visible and not fired
            press(app, *opening, Keys.ControlUnderscore, "a")
            assert shortcuts.browsing and not shortcuts.choices and not fired
            # A shortcut listed in help runs from it, here reopening the chooser.
            press(app, *opening)
            assert shortcuts.choices and not shortcuts.browsing and not fired
            press(app, Keys.Escape)
            assert not shortcuts.visible
            if shortcuts.leader:
                press(app, *opening, Keys.ControlB)
                assert not shortcuts.visible
            assert draft.text == "Keep my draft"

    asyncio.run(run())


@pytest.mark.parametrize("prefix", ["ctrl", "ctrl+b"])
def test_help_key_browses_without_submitting_but_runs_shortcuts(prefix):
    async def run():
        shortcuts = PrefixKeys(prefix)
        shortcuts.set_help(lambda: [("Enter", "Submit draft")], title="Editor")
        calls = []
        shortcuts.add("y", "Copy draft")(lambda event: calls.append("copy"))
        keys = KeyBindings()
        keys.add(Keys.Enter)(lambda event: calls.append("submit"))
        with surface(shortcuts, keys) as (app, draft):
            before = draft.buffer.document
            focus = app.layout.current_control
            press(app, Keys.ControlUnderscore)
            assert shortcuts.browsing and not shortcuts.pending
            assert shortcuts.hint_rows() == [
                ("Enter", "Submit draft"),
                (shortcuts.label("y"), "Copy draft"),
            ]
            assert shortcuts.hint_footer() == [("Esc / Ctrl+/", "close")]
            press(app, "y", Keys.Enter)
            assert shortcuts.browsing and not calls
            assert draft.buffer.document == before
            assert app.layout.current_control is focus
            press(app, Keys.ControlUnderscore)
            assert not shortcuts.visible
            assert draft.buffer.document == before
            assert app.layout.current_control is focus
            press(app, Keys.Enter)
            assert calls == ["submit"]
            # The shortcut itself works from help without closing it first.
            press(app, Keys.ControlUnderscore)
            assert shortcuts.browsing
            press(app, *((Keys.ControlY,) if prefix == "ctrl" else (Keys.ControlB, "y")))
            assert calls == ["submit", "copy"] and not shortcuts.visible
            assert draft.buffer.document == before

    asyncio.run(run())


@pytest.mark.parametrize(
    "prefix,leader",
    [
        ("f1 ctrl+b", (Keys.F1, Keys.ControlB)),
        ("f1 f2", (Keys.F1, Keys.F2)),
        ("ctrl+b f1", (Keys.ControlB, Keys.F1)),
    ],
)
def test_multikey_f_key_leader_runs_actions_and_then_browses_help(prefix, leader):
    async def run():
        shortcuts = PrefixKeys(prefix)
        calls = []
        shortcuts.add("y", "Copy draft")(lambda event: calls.append("copy"))
        with surface(shortcuts) as (app, draft):
            before = draft.buffer.document
            focus = app.layout.current_control
            press(app, leader[0])
            assert not shortcuts.visible
            press(app, *leader[1:])
            assert shortcuts.pending and not shortcuts.browsing
            press(app, "y")
            assert calls == ["copy"] and not shortcuts.visible
            press(app, *leader, Keys.ControlUnderscore)
            assert shortcuts.browsing and not shortcuts.pending
            press(app, "y")
            assert calls == ["copy"]
            # The whole multi-key leader reaches its menu from help.
            press(app, *leader)
            assert shortcuts.pending and not shortcuts.browsing
            press(app, "y")
            assert calls == ["copy", "copy"] and not shortcuts.visible
            press(app, Keys.ControlUnderscore, Keys.Escape)
            assert not shortcuts.visible
            assert draft.buffer.document == before
            assert app.layout.current_control is focus

    asyncio.run(run())


@pytest.mark.parametrize("overlay", ["browse", "choice"])
@pytest.mark.parametrize("prefix", ["ctrl", "ctrl+b"])
def test_overlay_protects_draft_and_focus_from_defaults_and_nonkeyboard_input(overlay, prefix):
    async def run():
        shortcuts = PrefixKeys(prefix)
        calls = []
        keys = KeyBindings()
        keys.add(Keys.Enter)(lambda event: calls.append("submit"))
        for key in (
            Keys.BracketedPaste,
            Keys.Vt100MouseEvent,
            Keys.WindowsMouseEvent,
            Keys.ScrollUp,
            Keys.ScrollDown,
        ):
            keys.add(key)(lambda event: calls.append(event.data))
        with surface(shortcuts, keys) as (app, draft):
            focus = app.layout.current_control
            before = draft.buffer.document
            if overlay == "browse":
                press(app, Keys.ControlUnderscore)
            else:
                shortcuts.choose("Pick", [Choice("a", "Alpha", lambda event: calls.append("a"))])
            press(
                app,
                Keys.ControlQ,
                "x",
                Keys.ControlR,
                Keys.ControlS,
                KeyPress(Keys.BracketedPaste, "pasted text"),
                KeyPress(Keys.Vt100MouseEvent, "mouse"),
                KeyPress(Keys.WindowsMouseEvent, "mouse"),
                Keys.ScrollUp,
                Keys.ScrollDown,
                Keys.Tab,
                Keys.Enter,
                Keys.Backspace,
            )
            assert shortcuts.visible
            assert draft.buffer.document == before
            assert app.layout.current_control is focus
            assert not app.quoted_insert
            assert not calls
            press(app, Keys.Escape, "!")
            assert "!" in draft.text

    asyncio.run(run())


def test_pending_leader_still_passes_paste_through_and_unknown_keys_stay_open():
    async def run():
        shortcuts = PrefixKeys("ctrl+b")
        calls = []
        keys = KeyBindings()
        keys.add(Keys.BracketedPaste)(lambda event: calls.append(event.data))
        with surface(shortcuts, keys) as (app, draft):
            press(app, Keys.ControlB, "z", KeyPress(Keys.BracketedPaste, "paste"))
            assert shortcuts.pending and "No binding" in shortcuts.message
            assert calls == ["paste"]
            assert draft.text == "Keep my draft"

    asyncio.run(run())


def test_pending_leader_blocks_eager_editor_defaults():
    async def run():
        shortcuts = PrefixKeys("ctrl+b")
        with surface(shortcuts) as (app, draft):
            before = draft.buffer.document
            focus = app.layout.current_control
            press(app, Keys.ControlB, Keys.ControlQ, "x", Keys.ControlR, Keys.ControlS)
            assert shortcuts.pending
            assert not app.quoted_insert
            assert draft.buffer.document == before
            assert app.layout.current_control is focus
            press(app, Keys.Escape)
            assert not shortcuts.visible

    asyncio.run(run())


def test_action_labels_describe_actions_and_follow_state():
    activity = SimpleNamespace(thinking_mode="off", panel_shown=True, busy=False)
    transcript = SimpleNamespace(command_mode="scrollback")
    callbacks = PromptCallbacks(
        on_send_mode=lambda: None,
        on_model=lambda: None,
        on_effort=lambda _: None,
        on_commands=lambda: None,
    )
    _, shortcuts = prompt_key_bindings(activity, transcript, callbacks, "ctrl+b", lambda: None)

    def labels():
        return {shortcut.key: shortcut.label for shortcut in shortcuts.available()}

    assert labels() == {
        "s": "Cycle send mode",
        "l": "Select model",
        "n": "Increase thinking effort",
        "p": "Decrease thinking effort",
        "o": "Hide activity panel",
        "t": "Select thinking visibility",
        "g": "Hide command output",
        "y": "Copy draft / choose response",
    }
    activity.panel_shown = False
    assert labels()["o"] == "Show activity panel"
    # Ctrl+G cycles the modes, and says where the next press takes the output.
    transcript.command_mode = "off"
    assert labels()["g"] == "Show command output in the activity panel"
    transcript.command_mode = "activity-panel"
    assert labels()["g"] == "Mirror command output into scrollback"
    del transcript.command_mode
    assert labels()["g"] == "Cycle command output"


def test_choices_reject_ambiguous_keys():
    shortcuts = PrefixKeys("ctrl")
    with pytest.raises(ValueError, match="single-character"):
        shortcuts.choose("Empty", [])
    with pytest.raises(ValueError, match="single-character"):
        shortcuts.choose("Long", [Choice("long", "Long", lambda event: None)])
    with pytest.raises(ValueError, match="unique"):
        shortcuts.choose("Duplicates", [Choice("a", "A", lambda event: None)] * 2)
