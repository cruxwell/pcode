"""Command bindings travel through the live prompt, not the editable draft."""

import asyncio
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import Command, CommandRegistry, SlashCompleter
from pcode.jobs import JobRegistry
from pcode.keymap import read_bindings, save_binding
from pcode.preferences import save_preferences
from pcode.runtime import Message
from pcode.ui import create_prompt


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


class HeldRuntime:
    session = None
    recovery_blocked = ""

    def __init__(self):
        self.jobs = JobRegistry(state=None)
        self.calls = []
        self.release = asyncio.Event()

    async def stream(self, text):
        self.calls.append(text)
        await self.release.wait()
        yield Message("done")


@asynccontextmanager
async def running_app():
    runtime = HeldRuntime()
    app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))
    with create_pipe_input() as pipe:
        session = None
        processed = asyncio.Event()

        def prompt(*args, **kwargs):
            nonlocal session
            session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
            session.shortcuts.bindings.add("f12", eager=True)(lambda event: processed.set())
            return session

        async def press(text):
            processed.clear()
            pipe.send_text(text + "\x1b[24~")
            await asyncio.wait_for(processed.wait(), 5)

        with patch("pcode.app.create_prompt", prompt):
            task = asyncio.create_task(app.run_async())
            try:
                await wait_for(lambda: session is not None and session.app.is_running)
                yield SimpleNamespace(app=app, runtime=runtime, session=session, press=press)
            finally:
                runtime.release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
@pytest.mark.parametrize("prefix", [None, "ctrl+b"], ids=["default-ctrl", "saved-leader"])
def test_bound_terminal_command_preserves_multiline_draft_and_history(busy, prefix):
    save_preferences(show_thinking="status-line")
    if prefix is not None:
        save_preferences(key_prefix=prefix)

    async def run():
        async with running_app() as view:
            if busy:
                await view.press("first\r")
                await wait_for(lambda: view.app.activity.busy and view.runtime.calls == ["first"])
            await view.press("/bind e /show-thinking off\r")
            await wait_for(lambda: read_bindings().get("e") == "/show-thinking off")
            buffer = view.session.default_buffer
            draft = Document("unfinished first line\nsecond line", cursor_position=7)
            buffer.set_document(draft)
            history = list(view.session.history.get_strings())
            await view.press("\x05" if prefix is None else "\x02e")
            await wait_for(lambda: view.app.activity.thinking_mode == "off")
            assert buffer.document == draft
            assert list(view.session.history.get_strings()) == history
            assert "/show-thinking off" not in history
            assert view.runtime.calls == (["first"] if busy else [])
            assert not view.app.activity.queued_prompts
            assert view.app.activity.busy is busy
            if busy:
                view.runtime.release.set()
                await wait_for(lambda: not view.app.activity.busy)
                assert view.runtime.calls == ["first"]
                assert buffer.document == draft

    asyncio.run(run())


def test_unbind_and_reset_take_effect_without_restarting_prompt():
    save_preferences(show_thinking="status-line")

    async def run():
        async with running_app() as view:
            buffer = view.session.default_buffer
            await view.press("/bind e /show-thinking off\r")
            await wait_for(lambda: read_bindings().get("e") == "/show-thinking off")
            for command in ("/unbind e", "/bind reset e"):
                buffer.reset()
                await view.press(command + "\r")
                await wait_for(
                    lambda: (
                        ("e" in read_bindings() and read_bindings()["e"] is None)
                        if command == "/unbind e"
                        else "e" not in read_bindings()
                    )
                )
                buffer.set_document(Document("first line\nsecond line", cursor_position=3))
                history = list(view.session.history.get_strings())
                await view.press("\x05")
                # Without the override, Emacs Ctrl+E moves to the end of this line.
                assert buffer.text == "first line\nsecond line"
                assert buffer.cursor_position == len("first line")
                assert view.app.activity.thinking_mode == "status-line"
                assert list(view.session.history.get_strings()) == history
                assert view.runtime.calls == []
            assert view.session.command_bindings.manage("e") == "e: unbound"

    asyncio.run(run())


def test_native_action_labels_and_copy_reset_follow_live_bindings():
    async def run():
        async with running_app() as view:
            await view.press("/unbind y\r")
            await wait_for(lambda: view.app.shortcut("y") == "unbound")
            await view.press("/bind e @copy\r")
            await wait_for(lambda: view.app.shortcut("y") == "Ctrl+E")
            await view.press("/bind reset y\r")
            await wait_for(lambda: view.app.shortcut("y") == "Ctrl+Y")
            assert "@copy" in view.session.command_bindings.manage("y")
            assert "[default]" in view.session.command_bindings.manage("y")
            assert view.runtime.calls == []

    asyncio.run(run())


@pytest.mark.parametrize("saved", [False, True], ids=["live-remap", "saved-remap"])
def test_command_descriptions_do_not_advertise_stale_shortcuts(saved):
    if saved:
        save_binding("e", "@model")
        save_binding("l", None)

    async def run():
        async with running_app() as view:
            if not saved:
                view.session.command_bindings.manage("e @model")
                view.session.command_bindings.manage("l", unbind=True)
            assert view.app.shortcut("l") == "Ctrl+E"
            assert ("Ctrl+E", "Select model") in view.session.shortcuts.hint_rows()
            registry = view.session.command_bindings.registry
            for name in ("/model", "/effort", "/show-activity", "/show-thinking", "/show-commands"):
                # These descriptions feed both the /help table and completions.
                command = registry.find(name)
                assert command is not None
                assert "Ctrl+" not in command.description
                completions = list(
                    SlashCompleter(registry).get_completions(Document(name), CompleteEvent())
                )
                matching = next(item for item in completions if item.text == name)
                assert matching.display_meta_text == command.description
            assert view.runtime.calls == []

    asyncio.run(run())


def test_bind_completion_uses_registered_target_arguments():
    async def run():
        async with running_app() as view:
            registry = view.session.command_bindings.registry
            command = registry.find("/bind")
            assert command is not None and command.argument_completer is not None
            assert [
                (item.text, item.start_position)
                for item in command.argument_completer("e /show-thinking of")
            ] == [("off", -2)]
            completions = list(
                SlashCompleter(registry).get_completions(
                    Document("/bind e /show-thinking of"), CompleteEvent()
                )
            )
            assert [(item.text, item.start_position) for item in completions] == [("off", -2)]
            assert view.runtime.calls == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "text", ["unfinished draft\nsecond line", "/show-thinking off\nstill editing"]
)
def test_saved_command_removed_from_registry_reports_without_submitting_draft(text):
    save_binding("e", "/temporary off")

    async def run():
        registry = CommandRegistry()
        registry.register(Command("/temporary", "Temporary command", lambda arg: None, ("off",)))
        submitted, executed, reports = [], [], []
        with create_pipe_input() as pipe:
            session = create_prompt(
                registry,
                input=pipe,
                output=DummyOutput(),
                on_submit=submitted.append,
                on_command=executed.append,
            )
            session.command_bindings.report = reports.append
            task = asyncio.create_task(session.prompt_async())
            try:
                await wait_for(lambda: session.app.is_running)
                registry.unregister("/temporary")
                draft = Document(text, cursor_position=5)
                session.default_buffer.set_document(draft)
                history = list(session.history.get_strings())
                pipe.send_text("\x05")
                await wait_for(lambda: bool(reports))
                assert reports == ["Command unavailable: /temporary"]
                assert executed == []
                assert submitted == []
                assert session.default_buffer.document == draft
                assert list(session.history.get_strings()) == history
                assert not task.done()
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
