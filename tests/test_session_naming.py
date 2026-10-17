"""Session titles asked of the model, and the terminal tab title that shows them."""

import asyncio
import os
from copy import deepcopy
from io import StringIO

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from rich.console import Console

from pcode.agent import SideModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.preferences import save_preferences
from pcode.runtime import Message
from pcode.session_naming import (
    INSTRUCTIONS,
    RENAME_REQUEST,
    clean_title,
    request_text,
    suggest_title,
)
from pcode.sessions import SavedSession, list_sessions
from pcode.terminal_notify import RESTORE_TITLE, SAVE_TITLE, TabTitle, title


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("Fix billing outage", "Fix billing outage"),
        ('Title: "Fix billing outage."', "Fix billing outage"),
        ("\n\n**Session name:** Redis queue retries\nsecond line", "Redis queue retries"),
        ("`Theme tests`!", "Theme tests"),
        ("Bad \x1b[31m  escape", "Bad [31m escape"),
        ("", ""),
        ("  \n ", ""),
    ],
)
def test_answers_are_cleaned_to_one_short_line(answer, expected):
    assert clean_title(answer) == expected


def test_a_long_answer_is_cut_to_a_list_row():
    cleaned = clean_title("word " * 40)
    assert len(cleaned) <= 60 and cleaned.startswith("word word")


def test_the_request_carries_the_first_prompt_trimmed():
    assert request_text("  " + "x" * 5000).count("x") == 2000


def test_suggest_title_asks_the_sessions_own_model_at_low_effort(monkeypatch):
    asked = []
    model = TestModel(custom_output_text='Title: "Fix the parser."')

    def side_model(name, effort=""):
        asked.append((name, effort))
        return SideModel(name, model, None)

    monkeypatch.setattr("pcode.agent.side_model", side_model)
    assert asyncio.run(suggest_title("anthropic:x", "Fix the parser")) == "Fix the parser"
    assert asked == [("anthropic:x", "low")]


def test_a_claude_model_answers_from_a_process_it_does_not_keep(monkeypatch):
    from pcode.claude_sdk.model import KEEP_WARM_SETTING
    from pcode.claude_sdk.workspace import CWD_SETTING

    seen = {}
    model = TestModel(custom_output_text="Claude title")
    # A `claude:` model names its system "claude"; the stand-in only needs that.
    monkeypatch.setattr(TestModel, "system", property(lambda self: "claude"))

    def side_model(name, effort=""):
        return SideModel(name, model, {"anthropic_effort": "low"})

    monkeypatch.setattr("pcode.agent.side_model", side_model)

    async def run():
        import pydantic_ai.direct

        original = pydantic_ai.direct.model_request_stream

        def wrapped(chosen, messages, *, model_settings=None):
            seen["settings"] = model_settings
            seen["instructions"] = messages[0].instructions
            return original(chosen, messages)

        monkeypatch.setattr(pydantic_ai.direct, "model_request_stream", wrapped)
        return await suggest_title("claude:x", "prompt", workspace="/work")

    assert asyncio.run(run()) == "Claude title"
    expected = {"anthropic_effort": "low", CWD_SETTING: "/work", KEEP_WARM_SETTING: False}
    assert seen["settings"] == expected
    assert seen["instructions"] == INSTRUCTIONS


def saved_app(tmp_path, *, prompt="Fix the parser", reply="Patched parser.py"):
    root = tmp_path / "sessions"
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
    app.model = "test:local"
    app.runtime = AgentRuntime(Agent("test"), None)
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.append("turn_started", prompt=prompt)
    saved.event(Message(reply))
    saved.append("turn_completed")
    saved.save_info()
    app.runtime.session = saved
    return app, saved, root


def test_a_title_is_saved_without_moving_the_session_in_resume(tmp_path, monkeypatch):
    save_preferences(session_naming="on")
    asked = []

    async def suggest(model, prompt, *, workspace=None):
        asked.append((model, prompt, workspace))
        return "Parser fix"

    monkeypatch.setattr("pcode.session_naming.suggest_title", suggest)
    app, saved, root = saved_app(tmp_path)
    updated = saved.info.updated

    async def run():
        app.controller.start_naming()
        await app.controller.naming_task
        # Once per session, whatever the outcome.
        app.controller.start_naming()
        assert app.controller.naming_task.done()

    try:
        asyncio.run(run())
        # Without a prompt handed over, the first one is read from the journal.
        assert asked == [("test:local", "Fix the parser", tmp_path)]
        (info,) = list_sessions(root)
        assert (info.title, info.updated) == ("Parser fix", updated)
        assert app.controller.session_title() == "Parser fix"
        assert dict(app.controller.session_overview())["Title"] == "Parser fix"
        # /rename wins, and clearing it falls back to the title.
        app.controller.rename("Mine")
        assert app.controller.session_title() == "Mine"
        app.controller.rename("-")
        assert app.controller.session_title() == "Parser fix"
    finally:
        saved.close()


@pytest.mark.parametrize("case", ["off", "named", "failing"])
def test_no_title_when_off_named_or_failing(tmp_path, monkeypatch, case):
    save_preferences(session_naming="off" if case == "off" else "on")
    calls = []

    async def suggest(model, prompt, *, workspace=None):
        calls.append(prompt)
        raise RuntimeError("provider down")

    monkeypatch.setattr("pcode.session_naming.suggest_title", suggest)
    app, saved, root = saved_app(tmp_path)
    if case == "named":
        app.controller.rename("Already named")

    async def run():
        app.controller.start_naming()
        if app.controller.naming_task is not None:
            await app.controller.naming_task

    try:
        asyncio.run(run())
        assert calls == (["Fix the parser"] if case == "failing" else [])
        assert list_sessions(root)[0].title is None
    finally:
        saved.close()


def test_a_title_arriving_after_rename_is_dropped(tmp_path, monkeypatch):
    save_preferences(session_naming="on")
    app, saved, root = saved_app(tmp_path)

    async def suggest(model, prompt, *, workspace=None):
        app.controller.rename("Typed meanwhile")
        return "Late title"

    monkeypatch.setattr("pcode.session_naming.suggest_title", suggest)

    async def run():
        app.controller.start_naming()
        await app.controller.naming_task

    try:
        asyncio.run(run())
        (info,) = list_sessions(root)
        assert (info.name, info.title) == ("Typed meanwhile", None)
    finally:
        saved.close()


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_new_cancels_a_title_request_and_reopening_asks_again(tmp_path, monkeypatch):
    save_preferences(session_naming="on")
    app, saved, root = saved_app(tmp_path)
    started = []

    async def suggest(model, prompt, *, workspace=None):
        started.append(prompt)
        await asyncio.Event().wait()  # Answers only when cancelled.

    monkeypatch.setattr("pcode.session_naming.suggest_title", suggest)

    async def run():
        app.controller.start_naming()
        task = app.controller.naming_task
        await until(lambda: started)
        app.controller.new("")
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        # Opened again in this process, the session is asked again.
        app.runtime.session = saved
        app.controller.start_naming()
        await until(lambda: len(started) == 2)
        await app.controller.stop_naming()
        assert app.controller.naming_task.cancelled()

    try:
        asyncio.run(run())
        assert started == ["Fix the parser", "Fix the parser"]
        assert list_sessions(root)[0].title is None
    finally:
        saved.close()


def conversation_app(tmp_path, stream):
    """A saved session with one exchange, on a model that answers with `stream`."""
    root = tmp_path / "sessions"
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
    app.model = "test:local"
    app.runtime = AgentRuntime(Agent(FunctionModel(stream_function=stream)))
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.save_info()
    app.runtime.session = saved
    app.runtime.history = [
        ModelRequest([UserPromptPart("Fix the parser")]),
        ModelResponse([TextPart("Patched parser.py")]),
    ]
    return app, saved, root


def test_bare_rename_asks_the_conversation_for_a_name_beside_it(tmp_path, monkeypatch):
    sent = []

    async def stream(messages, info):
        sent.append(deepcopy(messages))
        yield 'Title: "Parser fix."'

    app, saved, root = conversation_app(tmp_path, stream)
    history = deepcopy(app.runtime.history)
    updated = saved.info.updated
    flashed = []
    monkeypatch.setattr(app, "flash", flashed.append)

    async def run():
        app.controller.rename("")
        # The command is done before the answer: it holds nothing back meanwhile.
        assert not app.controller.rename_task.done()
        await app.controller.rename_task

    try:
        asyncio.run(run())
        (request,) = sent
        # The whole conversation, then the request as it is, not framed as a side question.
        assert request[: len(history)] == history
        (asked,) = request[len(history) :]
        assert [part.content for part in asked.parts] == [RENAME_REQUEST]
        (info,) = list_sessions(root)
        assert (info.name, info.updated) == ("Parser fix", updated)
        assert flashed == ["Asking the model for a name…", "Session named: Parser fix"]
        # Nothing joins the conversation, and it is no /btw thread either.
        assert app.runtime.history == history
        assert app.controller.asides.items == []
    finally:
        saved.close()


def asks_for_a_name(messages) -> bool:
    return any(
        isinstance(part, UserPromptPart) and RENAME_REQUEST in str(part.content)
        for message in messages
        for part in message.parts
    )


def test_bare_rename_answers_while_a_turn_is_still_running(tmp_path):
    main_started = []
    release = None

    async def stream(messages, info):
        if asks_for_a_name(messages):
            yield "Parser fix"
            return
        main_started.append(True)
        await release.wait()
        yield "Main answer."

    app, saved, root = conversation_app(tmp_path, stream)
    app.runtime.history = []  # The first turn is the one in flight.

    async def run():
        nonlocal release
        release = asyncio.Event()

        async def turn():
            return [event async for event in app.runtime.stream("Fix the parser")]

        running = asyncio.create_task(turn())
        await until(lambda: main_started)
        app.controller.rename("")
        await app.controller.rename_task
        # Named while the turn waits, and the turn then finishes as it would have.
        assert saved.info.name == "Parser fix" and not running.done()
        release.set()
        await running
        assert not asks_for_a_name(app.runtime.history)

    try:
        asyncio.run(run())
        assert list_sessions(root)[0].name == "Parser fix"
    finally:
        saved.close()


@pytest.mark.parametrize("how", ["typed", "new", "ctrl-c", "shutdown"])
def test_a_typed_name_new_conversation_ctrl_c_or_exit_stops_the_request(tmp_path, monkeypatch, how):
    started = []

    async def stream(messages, info):
        started.append(True)
        await asyncio.Event().wait()  # Answers only when cancelled.
        yield "Never"

    app, saved, root = conversation_app(tmp_path, stream)
    flashed, notes = [], []
    monkeypatch.setattr(app, "flash", flashed.append)
    monkeypatch.setattr(app, "note", notes.append)

    async def run():
        app.controller.rename("")
        task = app.controller.rename_task
        await until(lambda: started)
        # One request at a time.
        app.controller.rename("")
        assert app.controller.rename_task is task
        assert flashed[-1] == "Already asking the model for a name."
        if how == "typed":
            app.controller.rename("Typed meanwhile")
        elif how == "new":
            app.controller.new("")
        elif how == "ctrl-c":
            # No turn is running, so this is what Ctrl+C stops.
            app.controller.cancel()
            assert notes == ["Stopped asking the model for a name."]
        else:
            await app.controller.stop_naming()
            assert task.done()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() and not app.controller.naming()

    try:
        asyncio.run(run())
        (info,) = list_sessions(root)
        assert info.name == ("Typed meanwhile" if how == "typed" else None)
    finally:
        saved.close()


@pytest.mark.parametrize("failure", ["error", "timeout", "empty"])
def test_a_failed_name_request_warns_and_keeps_the_name(tmp_path, monkeypatch, failure):
    monkeypatch.setattr("pcode.aside.ASIDE_TIMEOUT_SECONDS", 0.1)

    async def stream(messages, info):
        if failure == "error":
            raise RuntimeError("provider down")
        if failure == "timeout":
            await asyncio.Event().wait()
        yield "**"  # Nothing left once cleaned.

    app, saved, root = conversation_app(tmp_path, stream)
    app.controller.rename("Before")
    warnings = []
    monkeypatch.setattr(app, "warning", warnings.append)

    async def run():
        app.controller.rename("")
        await app.controller.rename_task

    try:
        asyncio.run(run())
        (warning,) = warnings
        assert warning.startswith("Could not name the session: ")
        # A provider's error text is redacted the way a turn's is, so its
        # frames go where a turn's do.
        reason = {"error": "RuntimeError", "timeout": "no answer within", "empty": "no name"}
        assert reason[failure] in warning
        errors = saved.directory / "errors.log"
        if failure == "empty":
            assert not errors.exists()
        else:
            assert "Session name request" in errors.read_text()
        assert list_sessions(root)[0].name == "Before"
        assert not app.controller.naming()
    finally:
        saved.close()


def read_all(fd: int) -> str:
    os.set_blocking(fd, False)
    try:
        return os.read(fd, 65536).decode()
    except BlockingIOError:
        return ""


def test_tab_title_follows_the_name_and_restores_the_shells():
    current = {"name": ""}
    reader, writer = os.pipe()
    try:
        tab = TabTitle(lambda: current["name"], writer)
        tab.tick()
        assert read_all(reader) == ""  # No name yet: the shell keeps its title.
        current["name"] = "Fix\x07 the\nparser"
        tab.tick()
        assert read_all(reader) == SAVE_TITLE + title("Fix the parser")
        assert title("Fix the parser") == "\x1b]0;Fix the parser\x07"
        tab.tick()
        assert read_all(reader) == ""  # Unchanged: nothing resent.
        current["name"] = "Mine"
        tab.tick()
        assert read_all(reader) == title("Mine")
        current["name"] = ""  # /new: the shell's title comes back, saved again.
        tab.tick()
        assert read_all(reader) == RESTORE_TITLE + SAVE_TITLE
        current["name"] = "Next"
        tab.tick()
        tab.close()
        assert read_all(reader) == title("Next") + RESTORE_TITLE
        tab.close()
        assert read_all(reader) == ""
    finally:
        os.close(reader)
        os.close(writer)


def test_tab_title_sends_nothing_when_off_or_not_a_terminal():
    reader, writer = os.pipe()
    try:
        for mode in ("off", "on"):  # A pipe is no terminal either.
            asyncio.run(TabTitle(lambda: "Name", writer, mode).run())
        assert read_all(reader) == ""
    finally:
        os.close(reader)
        os.close(writer)
