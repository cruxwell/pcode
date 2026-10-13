"""Ctrl+Enter uses the normal submit pipeline with an explicit interrupt mode."""

import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.completion import Completion
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.jobs import JobRegistry
from pcode.preferences import save_preferences
from pcode.runtime import Message
from pcode.ui import Activity, Transcript, create_prompt


async def wait(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def press(session, key):
    session.app.key_processor.feed(KeyPress(key))
    session.app.key_processor.process_keys()


@pytest.mark.parametrize("editing_mode", ["emacs", "vi"])
@pytest.mark.parametrize("mode", ["steering", "queue", "interrupt"])
def test_ctrl_enter_interrupts_regardless_of_selected_mode(editing_mode, mode):
    save_preferences(editing_mode=editing_mode)

    async def run():
        started = asyncio.Event()
        calls = []
        cancelled = []

        class Runtime:
            session = None
            recovery_blocked = ""
            jobs = JobRegistry(state=None)

            async def stream(self, text):
                calls.append(text)
                if text == "first":
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.append(text)
                        raise
                yield Message("done")

        runtime = Runtime()
        app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))
        app.send_mode = mode
        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait(lambda: session is not None and session.app.is_running)
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    # An empty interrupt-send never cancels the running turn.
                    press(session, Keys.ControlF24)
                    session.default_buffer.text = "  \n "
                    press(session, Keys.ControlF24)
                    assert session.default_buffer.text == "  \n "
                    assert calls == ["first"]
                    assert not cancelled
                    # Even a one-shot queue/steering pick cannot override this chord.
                    app.send_mode_once = "queue" if mode == "steering" else "steering"
                    session.default_buffer.text = "second"
                    press(session, Keys.ControlF24)
                    await wait(lambda: calls == ["first", "second"] and not app.activity.busy)
                    assert cancelled == ["first"]
                    assert session.default_buffer.text == ""
                    assert "second" in session.default_buffer.history.get_strings()
                    assert app.send_mode == mode
                    assert app.send_mode_once is None
                    # Idle uses the same path without a cancellation or special turn.
                    session.default_buffer.text = "third"
                    press(session, Keys.ControlF24)
                    await wait(
                        lambda: calls == ["first", "second", "third"] and not app.activity.busy
                    )
                    assert cancelled == ["first"]
                    pipe.send_text("\x04")
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_ctrl_enter_sends_paste_and_bypasses_completion_without_changing_enter():
    async def run():
        sent = []
        interrupted = []
        activity = Activity()
        transcript = Transcript(Console(file=StringIO()), activity=activity)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=transcript,
                on_submit=sent.append,
                on_interrupt_submit=interrupted.append,
                input=pipe,
                output=DummyOutput(),
            )
            task = asyncio.create_task(session.app.run_async())
            try:
                await wait(lambda: session.app.is_running)
                text = "\n".join(f"line {i}" for i in range(100))
                pipe.send_text("\x1b[200~" + text + "\x1b[201~")
                await wait(lambda: bool(session.default_buffer.text))
                assert session.default_buffer.text == text
                press(session, Keys.ControlF24)
                assert interrupted == [text]
                assert not session.default_buffer.text
                buffer = session.default_buffer
                buffer.text = "hel"
                buffer.cursor_position = len(buffer.text)
                buffer._set_completions([Completion("hello", start_position=-3)])
                buffer.go_to_completion(0)
                press(session, Keys.ControlM)
                assert not sent  # Plain Enter still accepts a selected completion first.
                press(session, Keys.ControlM)
                assert sent == ["hello"]
                buffer.text = "hel"
                buffer.cursor_position = len(buffer.text)
                buffer._set_completions([Completion("hello", start_position=-3)])
                buffer.go_to_completion(0)
                press(session, Keys.ControlF24)
                assert interrupted == [text, "hello"]
                assert sent == ["hello"]
                assert not buffer.text
            finally:
                session.app.exit()
                await task

    asyncio.run(run())
