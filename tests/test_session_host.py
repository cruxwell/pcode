"""Session hosts: the wire format, the host process's logic, and terminals attached to it.

Hosts run in-process here, over a real Unix socket: a `SessionHost` serving a
`SessionController`, and terminals that are either a `RemoteController` with a
recording view or a whole `PreviewApp`. What a separate process adds
(environment inheritance, detaching from the terminal's session) is
`spawn_host`'s, covered by its own test.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from pcode.agent import create_coder
from pcode.app import PreviewApp
from pcode.host import MirroredActivity, SessionHost
from pcode.host_protocol import (
    EVENT_TYPES,
    PROTOCOL,
    HostEntry,
    decode_event,
    encode_event,
    find_host,
    list_hosts,
    list_stopped,
    remove_entry,
    socket_path,
    write_entry,
)
from pcode.live import AgentRuntime
from pcode.remote import (
    HostError,
    HostLaunch,
    RemoteController,
    request_host,
    spawn_host,
    stop_if_idle,
)
from pcode.runtime import (
    CacheBust,
    ChildPlan,
    ChildText,
    CommandOutput,
    EditCompleted,
    EditPreview,
    JobFinished,
    Message,
    PlanPreview,
    PlanUpdated,
    RunStatus,
    TextDelta,
    Thinking,
    ThinkingDelta,
    ToolStarted,
    ToolSummary,
)
from pcode.sessions import SavedSession, is_open
from pcode.ui import Activity, create_prompt

SAMPLES = [
    Message("**done**"),
    ToolStarted("shell", "ls", "c1", command="ls", arguments='{"command": "ls"}', purpose="look"),
    ToolSummary("shell", "ls", True, "c1", 1.5, "boom", "ls", result="out", outcome="failed"),
    JobFinished("shell", "sleep 9", call_id="j1", result="finished"),
    TextDelta("partial "),
    ThinkingDelta("hmm"),
    Thinking("hmm, done"),
    RunStatus("Thinking…"),
    CacheBust("cache collapsed"),
    PlanUpdated([{"content": "Inspect", "status": "in_progress"}]),
    PlanPreview(None),
    ChildPlan("c2", [{"content": "Child step", "status": "pending"}]),
    ChildText("c2", "child prose", thinking=True, start=True),
    CommandOutput("c3", "make test", "line 1\nline 2"),
    EditCompleted("c4", "a.py", "edit", "@@ -1 +1 @@\n-a\n+b", 1, 1),
    # Its own `kind` field once overwrote the event name on the wire.
    EditPreview("c5", "a.py", "print(1)", kind="code"),
]


def test_every_event_type_round_trips_through_json():
    assert {type(event).__name__ for event in SAMPLES} == set(EVENT_TYPES)
    for event in SAMPLES:
        assert decode_event(json.loads(json.dumps(encode_event(event)))) == event


def test_job_count_round_trips_in_host_snapshot_and_updates():
    updates = []
    activity = MirroredActivity(updates.append)
    activity.job_count = 2
    terminal = RemoteController(View(), Activity())
    snapshot = json.loads(json.dumps(activity.session_fields()))
    for name, value in snapshot.items():
        terminal.apply_field(name, value)
    assert terminal.activity.job_count == 2

    for count in (1, 0):
        updates.clear()
        activity.job_count = count
        assert updates == [{"job_count": count}]
        for name, value in json.loads(json.dumps(updates[0])).items():
            terminal.apply_field(name, value)
        assert terminal.activity.job_count == count


def test_records_can_stop_where_a_running_turn_began(tmp_path):
    session = SavedSession.create("test", tmp_path, tmp_path / "sessions")
    try:
        session.append("turn_started", prompt="one", run_id="a", parent_id=None)
        session.append("Message", markdown="first answer", run_id="a")
        session.append("turn_completed", run_id="a")
        end = session.journal_size()
        session.append("turn_started", prompt="two", run_id="b", parent_id="a")
        session.append("TextDelta", text="streaming", run_id="b")
        assert [r["kind"] for r in session.records(end)] == [
            "turn_started",
            "Message",
            "turn_completed",
        ]
        settled = list(session.transcript_records(end))
        assert [r["kind"] for r in settled] == ["turn_started", "Message", "turn_completed"]
        # Without `end`, the running turn's streamed text shows as a partial.
        assert list(session.transcript_records())[-1] == {
            "kind": "partial",
            "markdown": "streaming",
        }
    finally:
        session.close()


def test_an_attached_terminal_sees_the_running_turns_edits(tmp_path):
    """/diffs reads edits from the journal by branch, so it must follow mid-turn."""
    from pcode.remote import HostedSession

    session = SavedSession.create("test", tmp_path, tmp_path / "sessions")
    try:
        session.append("turn_started", prompt="one", run_id="a", parent_id=None)
        session.append("turn_completed", run_id="a")
        terminal = RemoteController(View(), Activity())
        terminal.runtime = HostedSession(terminal, {"id": "h", "pid": 1})
        terminal.runtime.follow({"session_directory": str(session.directory)})
        # The next turn starts after the terminal last read the journal.
        session.append("turn_started", prompt="two", run_id="b", parent_id="a")

        def edited():
            return [
                record["path"]
                for record in terminal.runtime.session.active_records()
                if record["kind"] == "EditCompleted"
            ]

        edit = EditCompleted("c1", "a.py", "edit", "@@ -1 +1 @@\n-a\n+b", 1, 1)
        session.event(edit, run_id="b")
        terminal.receive("turn_event", (edit,), {})
        assert edited() == ["a.py"]
    finally:
        session.close()


@pytest.fixture
def host_dir(monkeypatch):
    # macOS caps a Unix socket path at 104 bytes, and pytest's tmp_path is longer.
    directory = Path(tempfile.mkdtemp(prefix="pch-", dir="/tmp"))
    monkeypatch.setenv("PCODE_HOST_DIR", str(directory))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def user_texts(messages) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in getattr(message, "parts", [])
        if type(part).__name__ == "UserPromptPart" and not str(part.content).startswith("<")
    ]


class Script:
    """A model that answers by prompt; `hang ...` prompts wait on `release`."""

    def __init__(self):
        self.gates: dict[str, asyncio.Event] = {}
        self.requests: list[list] = []

    def gate(self, name: str) -> asyncio.Event:
        return self.gates.setdefault(name, asyncio.Event())

    def release(self, name: str) -> None:
        self.gate(name).set()

    async def model(self, messages, info):
        self.requests.append(messages)
        texts = user_texts(messages)
        prompt = texts[-1] if texts else ""
        if prompt == "read with steering":
            yield "Reading. "
            await self.gate(prompt).wait()
            yield {1: DeltaToolCall(name="read_file", json_args='{"path": "sample.txt"}')}
        elif "also check the tests" in texts:
            yield "Steering received."
        elif prompt == "fail at once":
            raise RuntimeError("provider down")
        elif prompt.startswith("hang"):
            yield "Started. "
            await self.gate(prompt).wait()
            yield "Finished."
        else:
            yield f"Echo: {prompt}"


def scripted_agent(script: Script, workspace: Path) -> Agent:
    return Agent(
        FunctionModel(stream_function=script.model), capabilities=[create_coder(workspace)]
    )


async def start_host(
    identity: str, workspace: Path, script: Script, saved: SavedSession | None = None
) -> SessionHost:
    """A host serving a controller over a scripted model, as `pcode.host` sets one up.

    With `saved` it carries that conversation on, as `pcode.host --resume` does.
    """
    (workspace / "sample.txt").write_text("a workspace marker\n")
    entry = HostEntry(
        id=identity, pid=os.getpid(), model="function:script", workspace=str(workspace)
    )
    host = SessionHost(entry)
    controller = host.controller
    controller.model = "function:script"
    controller.workspace = workspace
    if saved is not None:
        entry.session_id = saved.info.id
        controller.runtime = AgentRuntime(scripted_agent(script, workspace), saved)
        await controller.runtime.restore()
    else:
        controller.runtime = AgentRuntime(
            scripted_agent(script, workspace),
            session_factory=lambda model=None: SavedSession.create(
                "function:script", workspace, workspace / "sessions"
            ),
        )
    await host.serve()
    host.reset_buffer()
    host.push_state()
    host.start()
    return host


async def stop_host(host: SessionHost) -> None:
    host.stop()
    await host.close()
    host.controller.runtime.close()


class View:
    """A terminal's view that records every call the host makes to it."""

    ASYNC = {"after_turn", "after_command", "run_command", "browse_jobs", "choose_model"}

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.answers = {"choose_model": None, "read_asides": None}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in self.ASYNC or name in self.answers:
                return self._answer(name)
            return None

        return call

    async def _answer(self, name):
        return self.answers.get(name)

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def count(self, name: str) -> int:
        return self.names().count(name)

    def events(self) -> list:
        return [args[0] for name, args, _ in self.calls if name == "turn_event"]


async def attach(host: SessionHost) -> tuple[RemoteController, View, dict]:
    view = View()
    controller, welcome = await RemoteController.connect(host.socket, view, Activity())
    await controller.start(welcome)
    return controller, view, welcome


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def text_of(events) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


def test_terminal_sees_a_turn_the_host_runs(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("hello", "queue")
            await until(lambda: view.count("after_turn"))
            assert Message("Echo: hello") in view.events()
            assert view.names()[:1] == ["state"] or "turn_started" in view.names()
            # The first turn creates the session; the host says so.
            saved = host.controller.runtime.session
            await until(lambda: terminal.runtime.session_id == saved.info.id)
            assert terminal.runtime.session.directory == saved.directory
            (entry,) = list_hosts()
            assert (entry.id, entry.state, entry.title) == ("aaaa1111", "idle", "hello")
            assert entry.session_id == saved.info.id
            assert find_host(entry.session_id[:8]).id == "aaaa1111"
            # The live panel's session fields are mirrored as they change.
            assert not terminal.activity.busy and terminal.activity.prompt_state == "done"
            terminal.close()
        finally:
            await stop_host(host)
        assert list_hosts() == []

    asyncio.run(run())


def test_a_session_is_titled_beside_its_first_turn(tmp_path, host_dir, monkeypatch):
    from pcode.preferences import save_preferences
    from pcode.sessions import list_sessions

    save_preferences(session_naming="on")
    asked = []

    async def suggest(model, prompt, *, workspace=None):
        asked.append((model, prompt))
        return "Greeting the host"

    monkeypatch.setattr("pcode.session_naming.suggest_title", suggest)

    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("hang hello", "queue")
            # Asked from the prompt alone, while the first turn is still running.
            # The title reaches the attached terminal (its tab), /switch, and /resume.
            await until(lambda: terminal.session_title() == "Greeting the host")
            assert not view.count("after_turn")
            script.release("hang hello")
            await until(lambda: view.count("after_turn"))
            assert asked == [("function:script", "hang hello")]
            (entry,) = list_hosts()
            assert entry.title == "Greeting the host"
            (info,) = list_sessions(tmp_path / "sessions")
            assert (info.title, info.name) == ("Greeting the host", None)
            # Asked once per session, not per turn.
            terminal.submit("again", "queue")
            await until(lambda: view.count("after_turn") == 2)
            assert len(asked) == 1
            # A /rename name wins over the title everywhere it shows.
            host.controller.rename("Custom name")
            await until(lambda: terminal.session_title() == "Custom name")
            assert list_hosts()[0].title == "Custom name"
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_later_turn_titles_an_untitled_session_from_its_first_prompt(
    tmp_path, host_dir, monkeypatch
):
    from pcode.preferences import save_preferences

    save_preferences(session_naming="off")
    asked = []

    async def suggest(model, prompt, *, workspace=None):
        asked.append(prompt)
        return "Greeting the host"

    monkeypatch.setattr("pcode.session_naming.suggest_title", suggest)

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("hello", "queue")
            await until(lambda: view.count("after_turn"))
            save_preferences(session_naming="on")
            terminal.submit("again", "queue")
            await until(lambda: terminal.session_title() == "Greeting the host")
            assert asked == ["hello"]
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_first_turn_that_fails_before_any_output_is_still_titled(tmp_path, host_dir, monkeypatch):
    from pcode.preferences import save_preferences

    save_preferences(session_naming="on")
    asked = []

    async def suggest(model, prompt, *, workspace=None):
        asked.append(prompt)
        return "Failing provider"

    monkeypatch.setattr("pcode.session_naming.suggest_title", suggest)

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("fail at once", "queue")
            await until(lambda: terminal.session_title() == "Failing provider")
            terminal.submit("again", "queue")
            await until(lambda: view.count("after_turn") == 2)
            assert asked == ["fail at once"]
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_resumed_conversation_is_drawn_once_the_host_has_loaded_it(tmp_path, host_dir):
    """`pcode --continue` and /restart attach while the host is still reading the journal."""

    async def run():
        script = Script()
        first = await start_host("aaaa1111", tmp_path, script)
        terminal, view, _ = await attach(first)
        terminal.submit("remember the parser", "queue")
        await until(lambda: view.count("after_turn"))
        identity = first.controller.runtime.session.info.id
        terminal.close()
        await stop_host(first)

        entry = HostEntry(
            id="bbbb2222", pid=os.getpid(), model="function:script", workspace=str(tmp_path)
        )
        host = SessionHost(entry)
        controller = host.controller
        controller.model = "function:script"
        controller.workspace = tmp_path
        saved = SavedSession.open(identity, tmp_path / "sessions", tmp_path)
        controller._saved_session = saved
        controller.resuming = controller._needs_runtime = controller.startup_pending = True
        controller._create_runtime = lambda: AgentRuntime(
            Agent(FunctionModel(stream_function=script.model)), saved
        )
        await host.serve()
        output = StringIO()
        app = PreviewApp(
            model="function:script",
            workspace=tmp_path,
            host=HostLaunch("bbbb2222"),
            console=Console(file=output, color_system=None, width=140),
        )
        booting = None
        try:
            with create_pipe_input() as pipe:

                def prompt(*args, **kwargs):
                    return create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)

                async def drive():
                    nonlocal booting
                    await until(lambda: "Attached to session host bbbb2222" in output.getvalue())
                    assert "remember the parser" not in output.getvalue()
                    booting = asyncio.create_task(host.boot())
                    await until(lambda: "Echo: remember the parser" in output.getvalue())
                    pipe.send_text("/quit\r")

                with patch("pcode.app.create_prompt", prompt):
                    await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=20)
            assert output.getvalue().count("Echo: remember the parser") == 1
            # Drawn under the note that said how this terminal got here.
            assert f"Resumed {identity}" not in output.getvalue()
        finally:
            if booting is not None:
                await booting
            await stop_host(host)

    asyncio.run(run())


def test_a_terminal_attaching_mid_turn_catches_up_exactly(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            first, first_view, _ = await attach(host)
            first.submit("settled turn", "queue")
            await until(lambda: first_view.count("after_turn") == 1)
            first.submit("hang here", "queue")
            await until(lambda: any(name == "turn_event" for name, _, _ in host.buffer))
            view = View()
            late, welcome = await RemoteController.connect(host.socket, view, Activity())
            # History is the journal up to the running turn; the turn itself
            # comes from the host's buffer, so nothing shows twice.
            journal = late.runtime.session
            records = journal.transcript_records(welcome["settled_end"])
            prompts = [record["prompt"] for record in records if record["kind"] == "turn_started"]
            assert prompts == ["settled turn"]
            assert welcome["calls"][0][:2] == ["turn_started", ["hang here"]]
            assert late.activity.busy and late.activity.prompt == "hang here"
            await late.start(welcome)
            script.release("hang here")
            await until(lambda: view.count("after_turn") and first_view.count("after_turn") == 2)
            second_turn = first_view.events()[
                first_view.events().index(Message("Echo: settled turn")) + 1 :
            ]
            assert text_of(second_turn) == text_of(view.events()) == "Started. Finished."
            first.close()
            late.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_cancel_stops_the_host_turn_and_the_next_prompt_is_not_lost(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("hang forever", "queue")
            await until(lambda: host.buffer)
            terminal.cancel()
            # Sent while the cancelled turn is still unwinding in the host.
            terminal.submit("next", "queue")
            await until(lambda: Message("Echo: next") in view.events())
            assert "cancelled" in view.names()
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_steering_reaches_the_hosts_next_model_request(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("read with steering", "queue")
            await until(lambda: host.buffer)
            terminal.submit("also check the tests", "steering")
            await until(lambda: terminal.activity.queued_modes == ["steering"])
            script.release("read with steering")
            await until(lambda: Message("Steering received.") in view.events())
            assert "also check the tests" in user_texts(script.requests[-1])
            # Shown when the model took it, like a local session's steering.
            assert ("user", ("also check the tests",), {}) in view.calls
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_session_commands_run_in_the_host_and_ask_the_terminal_that_sent_them(
    tmp_path, host_dir, monkeypatch
):
    async def run():
        monkeypatch.setattr("pcode.models.active_providers", lambda model: {"anthropic"})
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            sender, view, _ = await attach(host)
            watcher, other, _ = await attach(host)
            sender.command("/model", 7)
            await until(lambda: view.count("choose_model"))
            # The picker opens only where the command was typed, with its tag.
            assert ("command_started", (7,), {}) in view.calls
            assert not other.count("choose_model")
            await until(lambda: view.count("command_finished"))
            sender.command("/effort")
            await until(lambda: view.count("flash") and other.count("flash"))
            # A command the terminal owns comes back to it, in order with the rest.
            sender.command("/theme dark", 8)
            await until(lambda: view.count("run_command"))
            assert ("run_command", ("/theme dark",), {"idle": True, "tag": 8}) in view.calls
            assert not other.count("run_command")
            assert "/effort" in [command.name for command in sender.registry.commands]
            sender.close()
            watcher.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_command_waits_with_a_spinner_until_the_host_has_run_it(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, view, _ = await attach(host)
            terminal.command("/effort")
            terminal.command("/effort")
            assert [wait.label for wait in terminal.activity.waits] == ["Running /effort"] * 2
            await until(lambda: view.count("after_command") == 2)
            assert terminal.activity.waits == []
            # Ctrl+C drops queued commands, some of which never report back.
            terminal.command("/effort")
            terminal.cancel()
            assert terminal.activity.waits == []
            terminal.command("/effort")
            terminal.close()
            assert terminal.activity.waits == []
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_btw_model_names_complete_as_soon_as_a_terminal_attaches(tmp_path, host_dir, monkeypatch):
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from pcode.commands import SlashCompleter

    catalog = ["anthropic:claude-opus", "openai-codex:gpt-6"]
    monkeypatch.setattr("pcode.models.active_providers", lambda model: {"anthropic"})
    monkeypatch.setattr("pcode.models.model_catalog", lambda providers, current: list(catalog))

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            view = View()
            # No await after connecting: the welcome carries the catalog.
            controller, welcome = await RemoteController.connect(host.socket, view, Activity())
            completer = SlashCompleter(controller.registry)
            found = completer.get_completions(Document("/btw $opus"), CompleteEvent())
            assert [item.text for item in found] == ["$anthropic:claude-opus"]
            found = completer.get_completions(Document("/subagents gpt"), CompleteEvent())
            assert [item.text for item in found] == ["openai-codex:gpt-6"]
            # Commands with a fixed argument list and no provider complete too.
            found = completer.get_completions(Document("/worktree fin"), CompleteEvent())
            assert [item.text for item in found] == ["finish"]
            await controller.start(welcome)
            # A new model changes the catalog; the terminal's follows with the state.
            catalog.append("anthropic:claude-sonnet")
            host.controller._model_suggestions = None
            host.controller.model = "anthropic:claude-sonnet"
            host.push_state()
            await until(lambda: "anthropic:claude-sonnet" in controller.model_suggestions())
            controller.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_an_answer_read_in_one_terminal_is_read_in_every_terminal(tmp_path, host_dir):
    from pcode.aside import Aside

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            answered = Aside(question="why?", status="answered", answer="because")
            host.controller.asides.items.append(answered)
            reader, view, _ = await attach(host)
            other, _, _ = await attach(host)
            assert reader.asides.unread == other.asides.unread == 1

            async def read_asides():
                for aside in reader.asides.items:
                    aside.read = True

            view.read_asides = read_asides
            reader.command("/btw")
            await until(lambda: answered.read and other.asides.unread == 0)
            late, _, _ = await attach(host)
            assert late.asides.unread == 0
            for terminal in (reader, other, late):
                terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_terminal_stops_side_questions_and_sees_them_settle(tmp_path, host_dir):
    """The questions run in the host; the terminal's records must follow them to the end."""

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:

            async def forever(aside):
                await asyncio.Event().wait()

            host.controller.asides.start("still going?", forever)
            terminal, _, _ = await attach(host)
            await until(lambda: terminal.asides.running == 1)
            terminal.cancel_asides()
            await until(lambda: host.controller.asides.running == 0)
            await until(lambda: terminal.asides.running == 0)
            assert [aside.status for aside in terminal.asides.items] == ["cancelled"]
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_stopping_an_idle_host_tells_terminals_it_closed_and_nothing_else(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        terminal, view, _ = await attach(host)
        closed = asyncio.Event()
        terminal.on_closed = closed.set
        await stop_host(host)
        await asyncio.wait_for(closed.wait(), 5)
        # Not a "Run cancelled" for a turn nobody was running.
        assert not view.count("cancelled")

    asyncio.run(run())


def test_kill_hosts_idle_asks_each_idle_host_and_reports_what_it_did(host_dir, monkeypatch, capsys):
    from pcode.app import main

    for identity, state in (
        ("idle0001", "idle"),
        ("busy0001", "idle"),
        ("work0001", "working"),
        ("gone0001", "idle"),
    ):
        entry = HostEntry(id=identity, pid=os.getpid(), model="test", workspace="/")
        entry.state, entry.title = state, identity
        write_entry(entry)
    asked = []

    async def stop_if_idle(entry):
        asked.append(entry.id)
        if entry.id == "gone0001":
            raise ConnectionRefusedError("refused")
        return "a side question is running" if entry.id == "busy0001" else ""

    monkeypatch.setattr("pcode.remote.stop_if_idle", stop_if_idle)
    monkeypatch.setattr(sys, "argv", ["pcode", "--kill-hosts", "idle"])
    main()
    # One running a turn says so in its entry, so it is not even asked.
    assert sorted(asked) == ["busy0001", "gone0001", "idle0001"]
    output = capsys.readouterr()
    assert "Skipped work0001  work0001: a turn is running" in output.out
    assert "Stopped idle0001  idle0001" in output.out
    assert "Skipped busy0001  busy0001: a side question is running" in output.out
    assert "Could not stop gone0001: refused" in output.err

    for entry in list_hosts():
        remove_entry(entry.id)
    # The flag's old name still works.
    monkeypatch.setattr(sys, "argv", ["pcode", "--stop-hosts", "idle"])
    main()
    assert "No session hosts are running." in capsys.readouterr().out


def test_a_host_stops_as_if_idle_with_a_terminal_attached_unless_busy(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, _, _ = await attach(host)
            host.controller.command_idle.clear()
            assert await request_host(host.entry, "stop_if_idle") == "a command is running"
            assert not host.stopped.is_set()
            host.controller.command_idle.set()
            assert await request_host(host.entry, "stop_if_idle") == ""
            # Stopped as it stops itself once idle: unmerged work and an
            # unread reply are kept for resuming.
            assert host.stopped.is_set() and host.idle_stopped and not host.keep_worktree
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_host_says_what_it_is_busy_with(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        controller = host.controller
        try:
            assert host.busy_with() == ""
            controller.startup_pending = True
            assert host.busy_with() == "it is starting up"
            controller.startup_pending = False
            # A title asked for beside the first turn, or a name a bare /rename asked for.
            for attribute in ("naming_task", "rename_task"):
                task = asyncio.get_running_loop().create_future()
                setattr(controller, attribute, task)
                assert host.busy_with() == "it is naming the session"
                task.cancel()
                assert host.busy_with() == ""
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_host_that_never_answers_hello_times_out(host_dir):
    from pcode.remote import _host_peer

    async def run():
        entry = HostEntry(id="mute0001", pid=os.getpid(), model="test", workspace="/")

        async def mute(reader, writer):
            await asyncio.sleep(5)  # Accepts, then never says ready.

        server = await asyncio.start_unix_server(mute, path=entry.socket)
        try:
            with pytest.raises(HostError, match="did not answer"):
                async with _host_peer(entry, timeout=0.1):
                    pass
        finally:
            server.close()

    asyncio.run(run())


def test_a_host_older_than_stop_if_idle_is_judged_by_its_entry(tmp_path, host_dir):
    old_calls = frozenset({"attach", "query", "send", "run", "stop", "asides_read"})

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            with (
                patch("pcode.host.HOST_CALLS", old_calls),
                patch("pcode.remote.wait_for_exit", return_value=True),
            ):
                host.entry.state = "working"
                assert await stop_if_idle(host.entry) == "a turn is running"
                assert not host.stopped.is_set()
                host.entry.state = "idle"
                assert await stop_if_idle(host.entry) == ""
                await asyncio.wait_for(host.stopped.wait(), 5)
            # Nobody said it was idle, so its worktree is left alone.
            assert host.keep_worktree and not host.idle_stopped
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_terminal_on_another_protocol_is_refused(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            with patch("pcode.remote.PROTOCOL", PROTOCOL + 1):
                with pytest.raises(HostError, match="protocol"):
                    await RemoteController.connect(host.socket, View(), Activity())
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_switch_says_what_it_cannot_switch_to(host_dir):
    output = StringIO()
    app = PreviewApp(model="function:script", console=Console(file=output, width=140))
    with pytest.raises(ValueError, match="No previous session"):
        app.switch("-")
    app.switch("zzzz")
    asyncio.run(app.switch_session(None, None))
    assert "No running session host matches 'zzzz'" in output.getvalue()
    app.previous_host = "gone1234"
    app.switch("-")
    asyncio.run(app.switch_session(None, None))
    assert "The previous session (gone1234) is no longer running" in output.getvalue()
    # Knowing its conversation, the terminal carries that on instead.
    opened = []

    async def open_saved(identity, active_id):
        opened.append(identity)

    app.open_saved_session = open_saved
    app.previous_session = "sess-1"
    app.switch("-")
    asyncio.run(app.switch_session(None, None))
    assert opened == ["sess-1"]


def test_switch_leaves_a_running_turn_in_its_host_and_comes_back_to_it(tmp_path, host_dir):
    """The whole terminal: attach, switch away mid-turn, switch back, then /detach."""

    async def run():
        script_a, script_b = Script(), Script()
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        host_a = await start_host("aaaa1111", tmp_path / "a", script_a)
        host_b = await start_host("bbbb2222", tmp_path / "b", script_b)
        output = StringIO()
        app = PreviewApp(
            model="function:script",
            workspace=tmp_path / "a",
            host=HostLaunch("aaaa1111"),
            console=Console(file=output, color_system=None, width=140),
        )
        try:
            with create_pipe_input() as pipe:
                session = None

                def prompt(*args, **kwargs):
                    nonlocal session
                    session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                    return session

                async def seen(text):
                    await until(lambda: text in output.getvalue(), timeout=10)

                async def drive():
                    await seen("Attached to session host aaaa1111")
                    pipe.send_text("hello a\r")
                    await seen("Echo: hello a")
                    pipe.send_text("hang in a\r")
                    await until(lambda: host_a.buffer)
                    pipe.send_text("/switch bbbb2222\r")
                    await seen("Switched to session bbbb2222")
                    assert app.runtime.id == "bbbb2222"
                    assert host_a.busy, "switching away must not stop A's turn"
                    await until(lambda: [e.id for e in app.hosts] == ["aaaa1111"])
                    pipe.send_text("hello b\r")
                    await seen("Echo: hello b")
                    # Ctrl+^ is `/switch -`: back to the session shown before.
                    pipe.send_text("\x1e")
                    await seen("Switched to session aaaa1111")
                    assert app.previous_host == "bbbb2222"
                    # Back mid-turn: the terminal shows A's turn again.
                    await until(lambda: app.activity.busy)
                    script_a.release("hang in a")
                    await seen("Finished.")
                    await until(lambda: not app.activity.busy)
                    pipe.send_text("/detach\r")

                with patch("pcode.app.create_prompt", prompt):
                    await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=30)
            assert "keeps running in the background" in output.getvalue()
            # Detached: both hosts still serve, nobody attached.
            await until(lambda: not host_a.clients and not host_b.clients)
            assert not host_a.stopped.is_set() and not host_b.stopped.is_set()
        finally:
            await stop_host(host_a)
            await stop_host(host_b)

    asyncio.run(run())


def local_app(tmp_path: Path, script: Script, output: StringIO) -> PreviewApp:
    """A session running in this terminal (`session_host off`) that saves its conversation."""
    sessions = tmp_path / "sessions"
    runtime = AgentRuntime(
        scripted_agent(script, tmp_path),
        session_factory=lambda model=None: SavedSession.create(
            "function:script", tmp_path, sessions
        ),
    )
    return PreviewApp(
        model="function:script",
        workspace=tmp_path,
        runtime=runtime,
        save=True,
        session_dir=sessions,
        console=Console(file=output, color_system=None, width=160),
    )


@pytest.mark.in_process("moves a session running in this terminal; hosted, there is none")
def test_detach_moves_a_session_running_here_into_a_host_and_stays_on_it(tmp_path, host_dir):
    async def run():
        output = StringIO()
        script = Script()
        app = local_app(tmp_path, script, output)
        hosts: list[SessionHost] = []
        loop = asyncio.get_running_loop()
        typed = threading.Event()

        def spawn_host(*, model, workspace, resume, session_dir):
            # Typed while it moves: lands in this terminal's own queue first.
            loop.call_soon_threadsafe(pipe.send_text, "typed meanwhile\r")
            loop.call_soon_threadsafe(
                lambda: loop.call_later(0.2, typed.set)  # Time to reach the queue.
            )
            typed.wait(5)
            # Opens the session as the real host does: refused if the terminal still held it.
            saved = SavedSession.open(resume, session_dir)
            host = asyncio.run_coroutine_threadsafe(
                start_host("cccc3333", workspace, Script(), saved), loop
            ).result()
            hosts.append(host)
            return "cccc3333", None, tmp_path / "host.log"

        try:
            with create_pipe_input() as pipe:

                def prompt(*args, **kwargs):
                    return create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)

                async def seen(text):
                    await until(lambda: text in output.getvalue(), timeout=10)

                async def drive():
                    pipe.send_text("/detach\r")
                    await seen("Nothing to move to a background host yet")
                    pipe.send_text("hello here\r")
                    await seen("Echo: hello here")
                    await until(lambda: not app.activity.busy)
                    pipe.send_text("hang here\r")
                    await until(lambda: "hang here" in script.gates)
                    pipe.send_text("/detach\r")
                    await seen("so what it is doing can't move")
                    script.release("hang here")
                    await seen("Finished.")
                    await until(lambda: not app.activity.busy)
                    session_id = app.runtime.session.info.id
                    pipe.send_text("/detach\r")
                    await seen("Moved to background session cccc3333")
                    # What was typed during the move went to the host, not lost.
                    await seen("Echo: typed meanwhile")
                    assert app.hosted and app.runtime.session_id == session_id
                    assert "left " not in output.getvalue(), "the host runs that same session"
                    pipe.send_text("hello there\r")
                    await seen("Echo: hello there")
                    # The host carried the conversation on, not a copy of it.
                    assert hosts[0].controller.runtime.session.info.id == session_id
                    pipe.send_text("/detach\r")

                with (
                    patch("pcode.app.create_prompt", prompt),
                    patch("pcode.remote.spawn_host", spawn_host),
                ):
                    await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=30)
            assert "keeps running in the background" in output.getvalue()
            await until(lambda: not hosts[0].clients)
            assert not hosts[0].stopped.is_set()
        finally:
            for host in hosts:
                await stop_host(host)

    asyncio.run(run())


def spawning(tmp_path: Path, loop, hosts: list) -> callable:
    """A `spawn_host` serving each session in a scripted host: a fork as dddd4444, else cccc3333."""

    def spawn_host(*, model, workspace, resume, session_dir, fork=False, worktree=False):
        identity = "dddd4444" if fork else "cccc3333"
        # Opens the session as the real host does: refused if the terminal still held it.
        saved = SavedSession.open(resume, session_dir, fork=fork)
        script = Script()
        host = asyncio.run_coroutine_threadsafe(
            start_host(identity, workspace, script, saved), loop
        ).result()
        host.script = script
        hosts.append(host)
        return identity, None, tmp_path / f"{identity}.log"

    return spawn_host


@pytest.mark.in_process("moves a session running in this terminal; hosted, there is none")
def test_a_fork_made_mid_turn_moves_this_session_into_a_host_once_idle(tmp_path, host_dir):
    """`/fork PROMPT` mid-turn: both run, and switching between them stops neither."""

    async def run():
        output = StringIO()
        script = Script()
        app = local_app(tmp_path, script, output)
        hosts: list[SessionHost] = []
        spawn_host = spawning(tmp_path, asyncio.get_running_loop(), hosts)
        try:
            with create_pipe_input() as pipe:

                def prompt(*args, **kwargs):
                    return create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)

                async def seen(text):
                    await until(lambda: text in output.getvalue(), timeout=10)

                async def drive():
                    pipe.send_text("hello here\r")
                    await seen("Echo: hello here")
                    await until(lambda: not app.activity.busy)
                    session_id = app.runtime.session.info.id
                    pipe.send_text("hang here\r")
                    await until(lambda: "hang here" in script.gates)
                    pipe.send_text("/fork go\r")
                    await seen("working on it in the background")
                    await seen("This session moves into a background host")
                    # Mid-turn it stays here: a turn cannot move.
                    assert not app.hosted and app.activity.busy
                    pipe.send_text("\x1e")
                    await seen("until its turn ends")
                    script.release("hang here")
                    await seen("Finished.")
                    await seen("Moved to background session cccc3333")
                    assert app.hosted and app.runtime.session_id == session_id
                    # Ctrl+^ goes to the copy just started, then back.
                    pipe.send_text("\x1e")
                    await seen("Switched to session dddd4444")
                    pipe.send_text("\x1e")
                    await seen("Switched to session cccc3333")
                    assert app.previous_host == "dddd4444"
                    pipe.send_text("/detach\r")

                with (
                    patch("pcode.app.create_prompt", prompt),
                    patch("pcode.remote.spawn_host", spawn_host),
                ):
                    await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=30)
            fork = next(host for host in hosts if host.entry.id == "dddd4444")
            # The copy works on its prompt meanwhile.
            await until(lambda: any("go" in user_texts(m) for m in fork.script.requests))
        finally:
            for host in hosts:
                await stop_host(host)

    asyncio.run(run())


@pytest.mark.in_process("leaves a session running in this terminal; hosted, there is none")
def test_ctrl_caret_resumes_a_session_left_from_this_terminal(tmp_path, host_dir):
    """A bare `/fork` ends the session running here; Ctrl+^ carries it on in a host."""

    async def run():
        output = StringIO()
        app = local_app(tmp_path, Script(), output)
        hosts: list[SessionHost] = []
        spawn_host = spawning(tmp_path, asyncio.get_running_loop(), hosts)
        try:
            with create_pipe_input() as pipe:

                def prompt(*args, **kwargs):
                    return create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)

                async def seen(text):
                    await until(lambda: text in output.getvalue(), timeout=10)

                async def drive():
                    pipe.send_text("hello here\r")
                    await seen("Echo: hello here")
                    await until(lambda: not app.activity.busy)
                    session_id = app.runtime.session.info.id
                    pipe.send_text("/fork\r")
                    await seen("Forked into session dddd4444")
                    await seen(f"left {session_id}")
                    assert app.previous_host is None and app.previous_session == session_id
                    pipe.send_text("\x1e")
                    await seen(f"Resumed {session_id} in session host cccc3333")
                    assert app.runtime.session_id == session_id
                    pipe.send_text("\x1e")
                    await seen("Switched to session dddd4444")
                    pipe.send_text("/detach\r")

                with (
                    patch("pcode.app.create_prompt", prompt),
                    patch("pcode.remote.spawn_host", spawn_host),
                ):
                    await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=30)
        finally:
            for host in hosts:
                await stop_host(host)

    asyncio.run(run())


@pytest.mark.in_process("moves a session running in this terminal; hosted, there is none")
def test_a_session_that_cannot_move_to_a_host_carries_on_here(tmp_path, host_dir):
    async def run():
        output = StringIO()
        app = local_app(tmp_path, Script(), output)

        def spawn_host(**kwargs):
            raise OSError("no room for a host")

        with create_pipe_input() as pipe:

            def prompt(*args, **kwargs):
                return create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)

            async def seen(text):
                await until(lambda: text in output.getvalue(), timeout=10)

            async def drive():
                pipe.send_text("hello\r")
                await seen("Echo: hello")
                await until(lambda: not app.activity.busy)
                pipe.send_text("/detach\r")
                await seen("so it carries on here")
                assert not app.hosted
                # Still its own: the lock is held again and turns keep saving.
                assert is_open(app.runtime.session.directory)
                pipe.send_text("still here\r")
                await seen("Echo: still here")
                # The reply prints before the turn ends, and Ctrl+D during a
                # turn cancels it rather than quitting.
                await until(lambda: not app.activity.busy)
                pipe.send_text("\x04")

            with (
                patch("pcode.app.create_prompt", prompt),
                patch("pcode.remote.spawn_host", spawn_host),
            ):
                await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=30)
        app.runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("keys", ["\x04", "/quit\r"])
def test_quitting_stops_the_host_as_stop_does(tmp_path, host_dir, keys):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        output = StringIO()
        app = PreviewApp(
            model="function:script",
            workspace=tmp_path,
            host=HostLaunch("aaaa1111"),
            console=Console(file=output, color_system=None, width=140),
        )
        try:
            with create_pipe_input() as pipe:

                def prompt(*args, **kwargs):
                    return create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)

                async def drive():
                    await until(lambda: "Attached to session host" in output.getvalue())
                    pipe.send_text(keys)

                with patch("pcode.app.create_prompt", prompt):
                    await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=30)
            assert "Stopped the session host." in output.getvalue()
            await asyncio.wait_for(host.stopped.wait(), 5)
            # Left for the terminal to tidy, asking, as after /stop.
            assert app.host_stopped and host.keep_worktree
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_command_that_ends_the_session_reads_as_done_not_as_a_crash(tmp_path, host_dir):
    """`/worktree finish` stops its host on purpose: the terminal exits without a crash warning."""
    from types import SimpleNamespace

    linked = SimpleNamespace(path=tmp_path, main=tmp_path, branch="pcode-1234")

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        output = StringIO()
        app = PreviewApp(
            model="function:script",
            workspace=tmp_path,
            host=HostLaunch("aaaa1111"),
            console=Console(file=output, color_system=None, width=140),
        )
        try:
            with (
                create_pipe_input() as pipe,
                patch("pcode.worktree.describe", return_value=linked),
                patch("pcode.worktree.finish", return_value="merged pcode-1234 into master"),
            ):

                def prompt(*args, **kwargs):
                    return create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)

                async def drive():
                    await until(lambda: "Attached to session host" in output.getvalue())
                    pipe.send_text("/worktree finish\r")
                    # As `pcode.host` does once stopped: the terminal exits on its own.
                    await host.stopped.wait()
                    await host.close()

                with patch("pcode.app.create_prompt", prompt):
                    await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=30)
            text = output.getvalue()
            assert "✓ merged pcode-1234 into master" in text
            for noise in ("Warning", "exited", "Run cancelled", "Stopped the session host"):
                assert noise not in text, text
            # The terminal left without stopping the host a second time or tidying.
            assert app.session_over and not app.host_stopped
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_host_counts_turns_and_marks_ones_finished_unwatched_as_unseen(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal, _, _ = await attach(host)
            await until(lambda: list_hosts()[0].attached == 1)
            terminal.submit("hang unwatched", "queue")
            await until(lambda: host.buffer)
            terminal.close()
            await until(lambda: list_hosts()[0].attached == 0)
            script.release("hang unwatched")
            await until(lambda: list_hosts()[0].turns == 1)
            (entry,) = list_hosts()
            assert (entry.outcome, entry.unseen, entry.state) == ("done", True, "idle")
            # Looking at it again is what clears it.
            again, _, _ = await attach(host)
            await until(lambda: not list_hosts()[0].unseen)
            again.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_idle_host_stops_itself_and_a_busy_one_does_not(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal, _, _ = await attach(host)
            terminal.submit("hang long", "queue")
            await until(lambda: host.buffer)
            terminal.close()
            watcher = asyncio.create_task(host.stop_when_idle(0.0005, every=0.01))
            await asyncio.sleep(0.2)
            assert not host.stopped.is_set(), "a running turn is not idle"
            script.release("hang long")
            await asyncio.wait_for(host.stopped.wait(), 5)
            await watcher
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_zero_minutes_stops_a_host_a_grace_period_after_its_last_terminal(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, _, _ = await attach(host)
            watcher = asyncio.create_task(host.stop_when_idle(0, every=0.01, grace=0.1))
            await asyncio.sleep(0.3)
            assert not host.stopped.is_set(), "an attached terminal keeps it"
            terminal.close()
            await asyncio.wait_for(host.stopped.wait(), 5)
            await watcher
            assert host.idle_stopped
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_handshake_or_a_running_command_keeps_a_host_from_idling(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            assert host.idle()
            reader, writer = await asyncio.open_unix_connection(str(host.socket))
            await until(lambda: host.connections == 1)
            assert not host.idle(), "a terminal saying hello is about to attach"
            writer.close()
            await until(lambda: host.connections == 0)
            assert host.idle()
            host.controller.command_idle.clear()
            assert not host.idle(), "a slash command is still running"
            host.controller.command_idle.set()
        finally:
            await stop_host(host)

    asyncio.run(run())


@pytest.mark.parametrize(
    "stored,minutes", [(None, 0), ("0", 0), ("5", 5), ("off", None), ("soon", 0)]
)
def test_idle_minutes_setting(stored, minutes):
    from pcode.host import _idle_minutes
    from pcode.preferences import SETTINGS

    preferences = {} if stored is None else {"session_host_idle_minutes": stored}
    assert _idle_minutes(preferences, SETTINGS) == minutes


def test_attach_to_a_stopped_host_finds_its_session(host_dir, tmp_path, monkeypatch):
    from pcode.app import _stopped_session
    from pcode.host_protocol import remember_stopped

    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    remember_stopped(HostEntry("stop0000", 1, "m", "/w", session_id="sess-1", unseen=True))
    assert _stopped_session("stop") == "sess-1"
    assert _stopped_session("zzzz") is None
    write_entry(HostEntry("live0000", os.getpid(), "m", "/w", session_id="sess-2"))
    assert _stopped_session("live") is None, "a running host is attached to, not resumed"
    write_entry(HostEntry("sto10000", os.getpid(), "m", "/w"))
    assert _stopped_session("sto") is None, "ambiguous with a running host: find_host says so"
    saved = SavedSession.create("function:script", tmp_path, tmp_path / "sessions")
    try:
        assert _stopped_session(saved.info.id[:8]) == saved.info.id
    finally:
        saved.close()


def test_connecting_to_a_listed_host_waits_for_its_socket_but_not_for_a_gone_one(host_dir):
    from pcode.remote import wait_for_host

    async def run():
        with pytest.raises(HostError, match="no longer running"):
            await wait_for_host("gone0000", View(), Activity(), timeout=5)
        # Starting up: listed, but not listening yet.
        write_entry(HostEntry("boot0000", os.getpid(), "m", "/w"))
        with pytest.raises(HostError, match="did not start"):
            await wait_for_host("boot0000", View(), Activity(), timeout=0.2)

    asyncio.run(run())


async def finish_unwatched(host: SessionHost, script: Script) -> str:
    """Run a turn that finishes after its terminal left; the session's ID."""
    terminal, _, _ = await attach(host)
    terminal.submit("hang unwatched", "queue")
    await until(lambda: host.buffer)
    terminal.close()
    await until(lambda: list_hosts()[0].attached == 0)
    script.release("hang unwatched")
    await until(lambda: list_hosts()[0].turns == 1 and list_hosts()[0].session_id)
    return list_hosts()[0].session_id


def test_a_host_stopped_idle_keeps_its_unseen_turn_listed_until_it_runs_again(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            session_id = await finish_unwatched(host, script)
            await host.stop_when_idle(0, every=0.01, grace=0.05)
            await host.close()
            assert list_hosts() == []
            (entry,) = list_stopped()
            assert (entry.session_id, entry.state, entry.unseen) == (session_id, "stopped", True)
            # A host running the session again takes its place.
            again = HostEntry(
                id="bbbb2222",
                pid=os.getpid(),
                model="m",
                workspace=str(tmp_path),
                session_id=session_id,
            )
            write_entry(again)
            assert list_stopped() == []
        finally:
            host.idle_stopped = False
            await stop_host(host)

    asyncio.run(run())


def test_a_host_stopped_on_request_forgets_its_unseen_turn(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            await finish_unwatched(host, script)
        finally:
            await stop_host(host)
        assert list_stopped() == []

    asyncio.run(run())


def test_stop_can_leave_the_worktree_for_the_terminal(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, _, _ = await attach(host)
            terminal.runtime.stop(keep_worktree=True)
            await asyncio.wait_for(host.stopped.wait(), 5)
            assert host.keep_worktree
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_registry_forgets_hosts_whose_process_is_gone(host_dir):
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    write_entry(HostEntry(id="dead0000", pid=process.pid, model="m", workspace="/"))
    write_entry(HostEntry(id="live0000", pid=os.getpid(), model="m", workspace="/"))
    assert [entry.id for entry in list_hosts()] == ["live0000"]
    assert not (host_dir / "dead0000.json").exists()
    with pytest.raises(LookupError):
        find_host("nothing")


def test_hosts_json_lists_running_hosts_for_scripts(host_dir, monkeypatch, capsys):
    from pcode.app import main

    write_entry(
        HostEntry(
            id="live0000",
            pid=os.getpid(),
            model="m",
            workspace="/repo",
            session_id="abc123",
            state="idle",
            title="Plan",
        )
    )
    monkeypatch.setattr(sys, "argv", ["pcode", "--hosts", "--json"])
    main()
    (entry,) = json.loads(capsys.readouterr().out)
    assert entry == {
        "id": "live0000",
        "session_id": "abc123",
        "state": "idle",
        "workspace": "/repo",
        "title": "Plan",
        "last_prompt": "",
        "attached": 0,
    }
    remove_entry("live0000")
    main()
    assert json.loads(capsys.readouterr().out) == []
    monkeypatch.setattr(sys, "argv", ["pcode", "--json"])
    with pytest.raises(SystemExit):
        main()
    assert "--json applies to --hosts" in capsys.readouterr().err


def test_socket_paths_that_are_too_long_fail_with_advice(tmp_path):
    with pytest.raises(OSError, match="PCODE_HOST_DIR"):
        socket_path("abcd1234", tmp_path / ("x" * 120))


def test_spawned_host_inherits_the_terminal_environment_and_its_own_session(host_dir, tmp_path):
    """The host is started from the terminal, so direnv credentials follow it there."""
    with patch("pcode.remote.subprocess.Popen") as popen:
        with patch.dict(os.environ, {"PCODE_SPAWN_CHECK": "from-this-terminal"}):
            identity, _, log = spawn_host(model="m", workspace=tmp_path, no_worktree=True)
    options = popen.call_args.kwargs
    assert options["env"]["PCODE_SPAWN_CHECK"] == "from-this-terminal"
    assert options["start_new_session"] is True
    assert options["cwd"] == tmp_path
    argv = popen.call_args.args[0]
    assert argv[:3] == [sys.executable, "-m", "pcode.host"]
    assert "--no-worktree" in argv and identity in argv
    assert log.parent == host_dir


def test_a_turn_is_announced_on_the_desktop_once_however_many_terminals_see_it(host_dir):
    from pcode.host_protocol import claim

    assert claim("aaaa1111-3")
    assert not claim("aaaa1111-3")
    assert claim("aaaa1111-4")
    from pcode.host_protocol import remove_entry

    remove_entry("aaaa1111")
    assert not list(host_dir.glob("*.claim"))


def test_background_finish_notifies_once_per_turn(tmp_path, host_dir):
    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=200), workspace=tmp_path)
    sent = []
    app._emulator = sent.append
    watched = HostEntry("aaaa1111", 1, "m", "/", title="fix auth", turns=1, attached=1)
    unwatched = HostEntry("bbbb2222", 1, "m", "/", title="add tests", turns=2, outcome="failed")
    app.background_finished(watched)
    app.background_finished(unwatched)
    app.background_finished(unwatched)  # Another terminal, or a second look: no repeat.
    # Another session's turn never lands in this transcript.
    assert "Background session" not in output.getvalue()
    # A watched session is announced too; the same turn is not announced twice.
    assert sent == [
        "\x1b]9;pcode: fix auth — finished\x07",
        "\x1b]9;pcode: add tests — failed\x07",
    ]


def test_notifications_cannot_break_out_of_their_escape(monkeypatch):
    from pcode.terminal_notify import notification

    monkeypatch.delenv("TMUX", raising=False)
    assert notification("done\x07\x1b]52;c;evil\x07") == "\x1b]9;done  ]52;c;evil\x07"
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    assert notification("hi") == "\x1bPtmux;\x1b\x1b]9;hi\x07\x1b\\"


def test_picker_puts_unseen_sessions_first_and_flags_old_code():
    from pcode.host_ui import host_row, ordered

    idle = HostEntry("idle0000", 1, "m", "/w/a", title="idle", updated=3)
    working = HostEntry("work0000", 1, "m", "/w/b", title="busy", state="working", updated=2)
    fresh = HostEntry(
        "new00000", 1, "m", "/w/c", title="done", state="idle", unseen=True, updated=1
    )
    assert [e.id for e in ordered([idle, working, fresh])] == ["new00000", "work0000", "idle0000"]
    old = HostEntry("old00000", 1, "m", "/w/d", title="old", state="idle", code="1")
    assert host_row(old, None, now=0, code="2").endswith("old code")
    assert "✓ new" in host_row(fresh, None, now=1, code="2")
    # No fingerprint at all: a host from before fingerprints, so older still.
    assert host_row(fresh, None, now=1, code="2").endswith("old code")
    current = HostEntry("cur00000", 1, "m", "/w/e", title="current", code="2")
    assert not host_row(current, None, now=1, code="2").endswith("old code")
    # A stopped one starts again on today's code: never "old", but stopped.
    stopped = HostEntry("stop0000", 1, "m", "/w/f", state="stopped", unseen=True, code="1")
    row = host_row(stopped, None, now=1, code="2")
    assert "✓ new" in row and row.endswith("· stopped")


@pytest.mark.parametrize("action", ["attach", "stop"])
def test_switch_resumes_or_forgets_a_stopped_session(host_dir, monkeypatch, action):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from pcode.host_protocol import remember_stopped

    remember_stopped(
        HostEntry("stop0000", 1, "m", "/w", session_id="sess-1", title="done", unseen=True)
    )
    output = StringIO()
    app = PreviewApp(model="function:script", console=Console(file=output, width=140))
    shown: list[dict] = []

    class Dialog:
        async def run_async(self):
            return (action, "stop0000")

    def dialog(entries, **kwargs):
        shown.extend({"id": entry.id, "state": entry.state} for entry in entries)
        return Dialog()

    @asynccontextmanager
    async def popup(output, session):
        yield None

    opened: list = []

    async def open_saved(identity, active_id):
        opened.append(identity)

    monkeypatch.setattr("pcode.host_ui.hosts_dialog", dialog)
    monkeypatch.setattr(app, "popup", popup)
    monkeypatch.setattr(app, "open_saved_session", open_saved)
    asyncio.run(
        app.switch_session(None, SimpleNamespace(app=SimpleNamespace(output=None, style=None)))
    )
    assert shown == [{"id": "stop0000", "state": "stopped"}]
    assert opened == (["sess-1"] if action == "attach" else [])
    assert list_stopped() == []


def test_status_describes_the_host_process():
    from pcode.host_ui import status_rows

    entry = HostEntry(
        "abcd1234", 42, "m", "/w", started=0, attached=2, log="/h/abcd1234.log", code="1"
    )
    rows = dict(status_rows(entry, now=7200, code="1"))
    assert rows["Host"] == "abcd1234 · pid 42 · started 2h ago"
    assert rows["Attached"] == "2 terminals attached"
    assert rows["Host log"] == "/h/abcd1234.log"
    assert "Host code" not in rows
    assert "/restart" in dict(status_rows(entry, now=0, code="newer"))["Host code"]


def test_restart_stops_keeping_the_worktree_and_resumes_the_session(tmp_path):
    async def run():
        class Host:
            remote = True
            id, pid, session_id, lost = "aaaa1111", 4242, "session-1", False
            stopped_with = None

            def stop(self, *, keep_worktree=False):
                self.stopped_with = keep_worktree

        runtime = Host()
        app = PreviewApp(model="m", runtime=runtime, console=Console(file=StringIO()))
        started = []

        async def start_host_session(prompt="", *, resume=None, note=None):
            started.append((prompt, resume, note))

        app.start_host_session = start_host_session
        app.restart("")
        # An async function patches as an AsyncMock, awaitable as it is.
        with patch("pcode.remote.wait_for_exit") as waited:
            await app.restart_host()
        waited.assert_awaited_once_with(4242)
        assert runtime.stopped_with is True
        assert started == [("", "session-1", "Restarted on the current pcode")]

    asyncio.run(run())


def test_waiting_for_a_spawned_host_does_not_wait_out_its_zombie():
    """A host this terminal spawned exits as its child: the wait must reap it, not time out."""
    from pcode.remote import wait_for_exit, wait_for_exit_sync

    for wait in (wait_for_exit_sync, lambda pid, timeout: asyncio.run(wait_for_exit(pid, timeout))):
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        started = time.monotonic()
        wait(process.pid, timeout=10)
        assert time.monotonic() - started < 5
