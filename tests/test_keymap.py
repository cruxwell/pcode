"""User-owned keymaps and live shortcut changes without restarting the prompt."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completion
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import Layout
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import TextArea

from pcode import preferences
from pcode.commands import Command, CommandRegistry
from pcode.keymap import DEFAULT_ACTIONS, PromptKeymap, bindings_path, read_bindings, save_binding
from pcode.prefix_keys import PrefixKeys


def run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return run


@pytest.fixture
def keymap_factory():
    def make(prefix="ctrl+b", *, vi_prefix="off"):
        native, executed, reports = [], [], []
        available = [True]
        shortcuts = PrefixKeys(prefix, vi_prefix=vi_prefix)
        for key, name in DEFAULT_ACTIONS.items():
            shortcuts.add(key, name, filter=Condition(lambda: available[0]))(
                lambda event, key=key: native.append(key)
            )
        registry = CommandRegistry()
        registry.register(Command("/model", "Select model", lambda arg: None, ("fast", "slow")))
        registry.register(Command("/note", "Add note", lambda arg: None, free_arguments=True))
        manager = PromptKeymap(shortcuts, registry, executed.append, reports.append)
        return SimpleNamespace(
            manager=manager,
            shortcuts=shortcuts,
            registry=registry,
            native=native,
            executed=executed,
            reports=reports,
            available=available,
        )

    return make


@asynccontextmanager
async def running_surface(view):
    """F12 acknowledges input processing, rather than guessing with a sleep."""
    with create_pipe_input() as pipe:
        ready, processed = asyncio.Event(), asyncio.Event()
        editor = TextArea(multiline=True)
        keys = KeyBindings()
        controls = []

        @keys.add("f12", eager=True)
        def acknowledge(event):
            processed.set()

        for key in ("c-c", "c-j", "enter"):
            keys.add(key)(lambda event, key=key: controls.append(key))

        app = Application(
            layout=Layout(editor),
            key_bindings=view.shortcuts.key_bindings(keys),
            input=pipe,
            output=DummyOutput(),
            full_screen=True,
        )
        # This marker must work even with a leader waiting for a selection.
        view.shortcuts.bindings.add("f12", eager=True)(acknowledge)
        task = asyncio.create_task(app.run_async(pre_run=ready.set))
        await asyncio.wait_for(ready.wait(), 2)

        async def press(text):
            processed.clear()
            pipe.send_text(text + "\x1b[24~")
            await asyncio.wait_for(processed.wait(), 2)

        try:
            yield SimpleNamespace(press=press, editor=editor, controls=controls, app=app)
        finally:
            if not task.done():
                app.exit()
            await asyncio.wait_for(task, 2)


def test_storage_roundtrip_reset_and_private_user_location(tmp_path):
    assert read_bindings() == {}
    assert bindings_path() == tmp_path / "config" / "pcode" / "bindings.json"
    assert save_binding("1", '/note two  words "quoted"') == {"1": '/note two  words "quoted"'}
    assert save_binding("y", None) == {"1": '/note two  words "quoted"', "y": None}
    assert bindings_path().stat().st_mode & 0o777 == 0o600
    assert save_binding("y", reset=True) == {"1": '/note two  words "quoted"'}
    assert save_binding(None, reset=True) == {}
    assert json.loads(bindings_path().read_text()) == {}


def test_storage_concurrent_updates_do_not_lose_other_keys():
    keys = "12345678"
    barrier = Barrier(len(keys))

    def save(key):
        barrier.wait(timeout=5)
        save_binding(key, f"/note {key}")

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        list(pool.map(save, keys))
    assert read_bindings() == {key: f"/note {key}" for key in keys}


def test_storage_failed_atomic_replace_preserves_original(monkeypatch):
    save_binding("y", "/note old")
    original = bindings_path().read_bytes()
    entries = set(bindings_path().parent.iterdir())

    def fail_replace(source, destination):
        assert Path(destination) == bindings_path()
        assert Path(source).parent == bindings_path().parent
        assert json.loads(Path(source).read_text()) == {"y": "/note new"}
        assert bindings_path().read_bytes() == original
        raise OSError("replace failed")

    monkeypatch.setattr(preferences.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        save_binding("y", "/note new")
    assert bindings_path().read_bytes() == original
    assert set(bindings_path().parent.iterdir()) == entries


@pytest.mark.parametrize(
    "contents",
    [
        "{",
        "[]",
        '{"y": 42}',
        '{"long": "/note"}',
        '{"y": "@future-action"}',
        '{"y": "plain text"}',
        '{"y": "/note\\nsecond line"}',
    ],
)
def test_corrupt_or_unknown_config_is_not_overwritten(contents, keymap_factory):
    path = bindings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    with pytest.raises(ValueError):
        read_bindings()
    for operation in (
        lambda: save_binding("l", "/model fast"),
        lambda: save_binding(None, reset=True),
    ):
        with pytest.raises(ValueError):
            operation()
        assert path.read_text() == contents
    view = keymap_factory()
    assert view.reports and "Could not load" in view.reports[0]
    assert {shortcut.key for shortcut in view.shortcuts.shortcuts} == set(DEFAULT_ACTIONS)
    assert path.read_text() == contents


def test_management_listing_actions_inspection_disable_and_reset(keymap_factory):
    view = keymap_factory()
    manager = view.manager
    assert manager.manage("") == manager.manage("list")
    assert "@copy" in manager.manage("actions")
    assert "[default]" in manager.manage("y")
    assert "[custom]" in manager.manage("y /model fast")
    assert read_bindings() == {"y": "/model fast"}
    manager.manage("y", unbind=True)
    assert manager.manage("y") == "y: disabled"
    manager.manage("reset y")
    assert "@copy" in manager.manage("y")
    manager.manage("1 /note hello")
    manager.manage("reset")
    assert manager.manage("1") == "1: unbound"
    assert read_bindings() == {}


@run_async
async def test_default_command_keys_run_only_where_the_command_exists(keymap_factory):
    view = keymap_factory("ctrl")
    async with running_surface(view) as surface:
        # Without the commands, Ctrl+V and Ctrl+] stay the editor's, silently;
        # Emacs character search after Ctrl+] consumes the next key, here x.
        await surface.press("draft\x16")
        await surface.press("\x1dx")
        assert view.executed == [] and view.reports == []
        assert all(shortcut.key not in "vz]" for shortcut in view.shortcuts.available())
        for name in ("/paste-image", "/show-edits", "/group-tools"):
            view.registry.register(Command(name, name, lambda arg: None, ("on", "off")))
        labels = {shortcut.key: shortcut.label for shortcut in view.shortcuts.available()}
        # Ctrl+V pastes an image, as in Claude Code and Codex; edit diffs moved to Ctrl+Z.
        assert labels["v"] == "Paste an image from the clipboard"
        assert labels["z"] == "Show / hide edit diffs"
        await surface.press("\x16\x1a\x1d")
        assert view.executed == ["/paste-image", "/show-edits", "/group-tools"]
        assert surface.editor.text == "draft"
        assert "/paste-image" in view.manager.manage("v") and "[default]" in view.manager.manage(
            "v"
        )
        assert view.manager.action_label("v") == "Ctrl+V"
        assert view.manager.action_label("z") == "Ctrl+Z"
        view.manager.manage("v", unbind=True)
        await surface.press("\x16")
        assert view.executed == ["/paste-image", "/show-edits", "/group-tools"]
        assert view.manager.action_label("v") == "unbound"
        view.manager.manage("reset v")
        await surface.press("\x16")
        assert view.executed[-1] == "/paste-image"


@pytest.mark.parametrize(
    "argument",
    [
        "y /missing",
        "y /model invalid",
        "y @unknown",
        "y plain",
        "y /",
        "long /note",
        "y /note\nsecond line",
    ],
)
def test_invalid_binding_never_executes_or_changes_storage(argument, keymap_factory):
    view = keymap_factory()
    save_binding("l", "/model fast")
    before = bindings_path().read_bytes()
    with pytest.raises(ValueError):
        view.manager.manage(argument)
    assert bindings_path().read_bytes() == before
    assert view.executed == []
    assert view.native == []


@run_async
async def test_arguments_preserved_and_reloaded_command_revalidated(keymap_factory):
    view = keymap_factory()
    target = '/note two  words "quoted" /nested'
    view.manager.manage("1 " + target)
    async with running_surface(view) as surface:
        await surface.press("draft\x021")
        assert view.executed == [target]
        assert surface.editor.text == "draft"
        view.registry.unregister("/note")
        await surface.press("\x021")
        assert view.executed == [target]
        assert "unavailable" in view.reports[-1].lower()
        assert "unavailable" in view.manager.manage("1")
        view.registry.register(Command("/note", "Changed arguments", lambda arg: None))
        await surface.press("\x021")
        assert view.executed == [target]
        assert "Usage: /note" in view.reports[-1]
        assert surface.editor.text == "draft"


@run_async
async def test_unavailable_saved_command_never_becomes_prompt_text(keymap_factory):
    save_binding("1", "/gone preserved argument")
    view = keymap_factory()
    async with running_surface(view) as surface:
        await surface.press("draft\x021")
        assert view.executed == []
        assert view.reports and "unavailable" in view.reports[-1].lower()
        assert surface.editor.text == "draft"
    assert read_bindings() == {"1": "/gone preserved argument"}


@pytest.mark.parametrize(
    "prefix,press_y,press_l",
    [
        ("ctrl", "\x19", "\x0c"),
        ("ctrl+b", "\x02y", "\x02l"),
    ],
)
@run_async
async def test_live_override_disable_restore_and_native_action_reuse(
    keymap_factory,
    prefix,
    press_y,
    press_l,
):
    view = keymap_factory(prefix)
    async with running_surface(view) as surface:
        await surface.press(press_y)
        assert view.native == ["y"]
        view.manager.manage("y /model fast")
        await surface.press(press_y)
        assert view.executed == ["/model fast"]
        assert view.native == ["y"]
        view.manager.manage("l @copy")
        await surface.press(press_l)
        assert view.native == ["y", "y"]
        view.manager.manage("y /model slow")
        await surface.press(press_y)
        assert view.executed == ["/model fast", "/model slow"]
        view.manager.manage("y", unbind=True)
        await surface.press(press_y)
        assert view.executed == ["/model fast", "/model slow"]
        # An unbound leader selection keeps the menu open; reset it explicitly.
        view.shortcuts.dismiss()
        view.manager.manage("reset y")
        await surface.press(press_y)
        assert view.native == ["y", "y", "y"]
        view.manager.manage("reset")
        await surface.press(press_l)
        assert view.native[-1] == "l"


@pytest.mark.parametrize("key", ["1", "A", "/", "c", "j", "m", "d"])
@run_async
async def test_custom_leader_keys_do_not_hijack_reserved_controls(keymap_factory, key):
    view = keymap_factory()
    view.manager.manage(f"{key} /note selected")
    async with running_surface(view) as surface:
        await surface.press("draft\x02" + key)
        assert view.executed == ["/note selected"]
        assert surface.editor.text == "draft"
        await surface.press("\x03\x0a\x0d")
        assert surface.controls == ["c-c", "c-j", "enter"]
        assert view.executed == ["/note selected"]


@run_async
async def test_direct_ctrl_custom_reserved_letters_do_not_claim_controls(keymap_factory):
    view = keymap_factory("ctrl")
    for key in "cjm":
        view.manager.manage(f"{key} /note {key}")
    async with running_surface(view) as surface:
        await surface.press("\x03\x0a\x0d")
        assert surface.controls == ["c-c", "c-j", "enter"]
        assert view.executed == []


@run_async
async def test_reused_native_action_retains_its_availability_filter(keymap_factory):
    view = keymap_factory()
    view.manager.manage("1 @copy")
    view.available[0] = False
    async with running_surface(view) as surface:
        await surface.press("\x021")
        assert view.native == []
        view.shortcuts.dismiss()
        view.available[0] = True
        await surface.press("\x021")
        assert view.native == ["y"]


@run_async
async def test_vi_leader_supports_custom_keys_and_live_remapping(keymap_factory):
    view = keymap_factory("ctrl", vi_prefix="<space>")
    async with running_surface(view) as surface:
        surface.app.editing_mode = EditingMode.VI
        surface.app.vi_state.input_mode = InputMode.NAVIGATION
        view.manager.manage("1 /model fast")
        await surface.press(" 1")
        assert view.executed == ["/model fast"]
        view.manager.manage("1 /model slow")
        await surface.press(" 1")
        assert view.executed == ["/model fast", "/model slow"]
        view.manager.manage("1", unbind=True)
        await surface.press(" 1")
        assert view.executed == ["/model fast", "/model slow"]
        assert surface.editor.text == ""


def test_action_label_follows_native_action_remap_and_disable(keymap_factory):
    view = keymap_factory()
    assert view.manager.action_label("y") == view.shortcuts.label("y")
    view.manager.manage("y /model fast")
    assert view.manager.action_label("y") == "unbound"
    view.manager.manage("1 @copy")
    assert view.manager.action_label("y") == view.shortcuts.label("1")
    view.manager.manage("1", unbind=True)
    assert view.manager.action_label("y") == "unbound"
    view.manager.manage("reset")
    assert view.manager.action_label("y") == view.shortcuts.label("y")


def test_completion_management_keys_targets_and_nested_arguments(keymap_factory):
    view = keymap_factory()
    view.manager.manage("1 /note hello")

    def completed(argument):
        return [(item.text, item.start_position) for item in view.manager.complete(argument)]

    assert ("actions", -2) in completed("ac")
    assert ("1", -1) in completed("reset 1")
    assert ("@copy", -3) in completed("y @co")
    assert ("/model", -3) in completed("y /mo")
    assert completed("y /model fa") == [("fast", -2)]
    view.registry.register(
        Command(
            "/nested",
            "Nested completion",
            lambda arg: None,
            free_arguments=True,
            argument_completer=lambda argument: [Completion(argument + "-done", -len(argument))],
        )
    )
    assert completed("y /nested first second") == [("first second-done", -12)]
