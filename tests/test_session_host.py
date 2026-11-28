"""Session hosts: the wire format, the host process's logic, and terminals attached to it.

Hosts run in-process here, over a real Unix socket: a `SessionHost` serving a
`SessionController`, and terminals that are either a `RemoteController` with a
recording view or a whole `PreviewApp`. What a separate process adds
(environment inheritance, detaching from the terminal's session) is
`spawn_host`'s, covered by its own test.
"""

import asyncio
import contextlib
import json
import os
import shutil
import signal
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
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from pcode.agent import create_coder
from pcode.app import PreviewApp
from pcode.controller import SessionController
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
    WorkerStatus,
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
    WorkerStatus("c2", "w1", question="Which path?", fallback="both", tokens=1200),
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


def test_cancel_stays_unanswered_until_the_host_is_idle():
    class Peer:
        def notify(self, *args):
            pass

    terminal = RemoteController(View(), Activity())
    terminal.peer = Peer()
    terminal.cancel()
    first = terminal.cancel_sent
    assert first is not None
    terminal.cancel()
    assert terminal.cancel_sent == first  # Counted from the oldest unanswered cancel.
    # A host stuck in its turn can still report other things.
    terminal.receive("state", ({"job_count": 1, "busy": True},), {})
    assert terminal.cancel_sent == first
    terminal.receive("state", ({"busy": False},), {})
    assert terminal.cancel_sent is None


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
        elif prompt == "make a plan":
            if isinstance(messages[-1].parts[-1], ToolReturnPart):
                yield "Planned."
            else:
                items = '{"items": [{"content": "Inspect", "status": "in_progress"}]}'
                yield {1: DeltaToolCall(name="write_plan", json_args=items)}
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
    identity: str,
    workspace: Path,
    script: Script,
    saved: SavedSession | None = None,
    carried: dict | None = None,
) -> SessionHost:
    """A host serving a controller over a scripted model, as `pcode.host` sets one up.

    With `saved` it carries that conversation on, as `pcode.host --resume` does;
    with `carried`, what the process it replaces handed over, as `--carry` does.
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
    if carried:
        host.take_over(carried)
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


def test_an_open_side_answer_viewer_does_not_hold_other_terminals_commands(tmp_path, host_dir):
    """One terminal reading /btw answers must not stall another's commands behind it."""
    from pcode.aside import Aside, Bridge

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            host.controller.asides.items.append(
                Aside(question="why?", status="answered", answer="because")
            )
            reader, reader_view, _ = await attach(host)
            other, other_view, _ = await attach(host)
            close = asyncio.Event()
            opened = []

            async def still_reading():
                opened.append(True)
                await close.wait()
                return Bridge("thread", "summary")

            reader_view.read_asides = still_reading
            reader.command("/btw", 7)
            await until(lambda: opened)
            # The viewer runs as the command it opened for, whose tag it keeps.
            assert reader_view.calls[-1] == ("command_started", (7,), {})
            await until(lambda: reader.activity.waits == [])
            assert host.busy_with() == "a side-answer viewer is open"
            other_opened = []

            async def glance():
                other_opened.append(True)

            other_view.read_asides = glance
            other.command("/btw")
            await until(lambda: other_opened)
            # What the viewer asks for once it closes is still kept.
            kept = []

            async def keep_thread(thread, action, instructions=""):
                kept.append((thread, action))

            host.controller.keep_thread = keep_thread
            close.set()
            await until(lambda: kept == [("thread", "summary")])
            await until(lambda: not host.view.viewers)
            for terminal in (reader, other):
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


def test_kill_hosts_stops_hosts_at_once_not_one_after_another(host_dir, monkeypatch, capsys):
    """Each host can take a while to tear down; a dozen in a row looked like a hang."""
    from pcode.app import main

    for identity in ("idle0001", "idle0002", "idle0003"):
        entry = HostEntry(id=identity, pid=os.getpid(), model="test", workspace="/")
        entry.state = "idle"
        write_entry(entry)
    stopping = set()
    overlapped = []

    async def stop_if_idle(entry):
        stopping.add(entry.id)
        await asyncio.sleep(0.05)
        overlapped.append(len(stopping))
        return ""

    monkeypatch.setattr("pcode.remote.stop_if_idle", stop_if_idle)
    monkeypatch.setattr(sys, "argv", ["pcode", "--kill-hosts", "idle"])
    main()
    assert overlapped == [3, 3, 3]
    output = capsys.readouterr()
    assert output.out.count("Stopped idle000") == 3
    assert "Stopping 3 session host(s)" in output.err
    for entry in list_hosts():
        remove_entry(entry.id)


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


def _running(runtime, *jobs):
    """The real registry reporting `jobs` as running, for what `busy_with` asks."""
    return patch.object(runtime.jobs, "running", lambda: list(jobs))


def _job(identity: str, *, adopted: bool):
    from pcode.jobs import Job

    return Job(identity, "serve", Path("/j"), 1, 0.0, background=True, adopted=adopted)


def test_a_job_adopted_from_an_exited_pcode_does_not_keep_a_host_up(tmp_path, host_dir):
    """It never wakes this session, and stopping leaves it running for the next pcode."""

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        runtime = host.controller.runtime
        try:
            with _running(runtime, _job("j7", adopted=True)):
                assert host.busy_with() == ""
            with _running(runtime, _job("j7", adopted=True), _job("j8", adopted=False)):
                assert host.busy_with() == "a background command is running"
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_host_with_no_terminal_says_what_keeps_it_running(tmp_path, host_dir, capsys):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        runtime = host.controller.runtime
        try:
            terminal, _, _ = await attach(host)
            with _running(runtime, _job("j8", adopted=False)):
                watcher = asyncio.create_task(host.stop_when_idle(0, every=0.01, grace=0.05))
                await asyncio.sleep(0.1)
                assert list_hosts()[0].held == "", "an attached terminal is why it runs"
                terminal.close()
                await until(lambda: list_hosts()[0].attached == 0)
                stamped = list_hosts()[0].updated
                await until(lambda: list_hosts()[0].held == "a background command is running")
                assert not host.stopped.is_set()
                # Published without counting as activity, which /attach ages rows by.
                assert list_hosts()[0].updated == stamped
            await asyncio.wait_for(host.stopped.wait(), 5)
            await watcher
        finally:
            await stop_host(host)
        log = capsys.readouterr().err
        assert "not stopping: a background command is running" in log
        assert "nothing holds it now" in log

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

        def spawn_host(*, model, workspace, resume, session_dir, carry=None):
            assert carry is None, "idle, there is no turn to continue"
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
    """A `spawn_host` serving each session in a scripted host.

    A fork is dddd4444; anything else is cccc3333, then eeee5555.
    """

    others = iter(["cccc3333", "eeee5555"])

    def spawn_host(
        *, model, workspace, resume, session_dir, fork=False, worktree=False, carry=None
    ):
        identity = "dddd4444" if fork else next(others)
        # Opens the session as the real host does: refused if the terminal still held it.
        saved = SavedSession.open(resume, session_dir, fork=fork) if resume else None
        script = Script()
        host = asyncio.run_coroutine_threadsafe(
            start_host(identity, workspace, script, saved, carry), loop
        ).result()
        host.carry_received = carry
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
                    await seen("until it is idle")
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


@pytest.mark.in_process("moves a session running in this terminal; hosted, there is none")
def test_new_with_a_prompt_mid_turn_starts_a_session_beside_this_one(tmp_path, host_dir):
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
                    pipe.send_text("hang here\r")
                    await until(lambda: "hang here" in script.gates)
                    pipe.send_text("/new go\r")
                    await seen("Session cccc3333 is working on it in the background")
                    assert "unavailable while working" not in output.getvalue()
                    assert not app.hosted and app.activity.busy
                    script.release("hang here")
                    await seen("Moved to background session eeee5555")
                    pipe.send_text("\x1e")
                    await seen("Switched to session cccc3333")
                    pipe.send_text("/detach\r")

                with (
                    patch("pcode.app.create_prompt", prompt),
                    patch("pcode.remote.spawn_host", spawn_host),
                ):
                    await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=30)
            fresh = next(host for host in hosts if host.entry.id == "cccc3333")
            await until(lambda: any("go" in user_texts(m) for m in fresh.script.requests))
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


def run_local(app, output, drive, spawn_host, timeout=30):
    """Run `app` with `drive(pipe, seen)` typing into it and `spawn_host` patched in."""

    async def run():
        with create_pipe_input() as pipe:

            def prompt(*args, **kwargs):
                return create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)

            async def seen(text):
                await until(lambda: text in output.getvalue(), timeout=10)

            with (
                patch("pcode.app.create_prompt", prompt),
                patch("pcode.remote.spawn_host", spawn_host),
            ):
                await asyncio.wait_for(
                    asyncio.gather(app.run_async(), drive(pipe, seen)), timeout=timeout
                )

    return run()


async def hang_with_a_queued_message(app, script, pipe, seen) -> None:
    """A turn waiting on the model, with a message queued behind it."""
    pipe.send_text("hello here\r")  # Saves the session.
    await seen("Echo: hello here")
    await until(lambda: not app.activity.busy)
    app.send_mode = "queue"
    pipe.send_text("hang here\r")
    await until(lambda: "hang here" in script.gates)
    pipe.send_text("queued after\r")
    await until(lambda: "queued after" in app.activity.queued_prompts)


def asked(script: Script) -> list[str]:
    return [user_texts(messages)[-1] for messages in script.requests]


@pytest.mark.in_process("moves a session running in this terminal; hosted, there is none")
def test_detach_mid_turn_continues_the_turn_in_the_host_ahead_of_the_queue(tmp_path, host_dir):
    async def run():
        output = StringIO()
        script = Script()
        app = local_app(tmp_path, script, output)
        hosts: list[SessionHost] = []

        async def drive(pipe, seen):
            await hang_with_a_queued_message(app, script, pipe, seen)
            pipe.send_text("/detach\r")
            await seen("Moved mid-turn; continuing in the background host cccc3333")
            host = hosts[0]
            # Continued once this terminal had the host, not from a carry at spawn.
            assert host.carry_received is None
            await until(lambda: "hang here" in host.script.gates)
            host.script.release("hang here")
            await seen("Echo: queued after")
            # Asked again from its checkpoint, then the queued message: none lost or doubled.
            assert asked(host.script) == ["hang here", "queued after"]
            assert asked(script) == ["hello here", "hang here"]
            assert host.controller.runtime.session.info.id == app.runtime.session_id
            pipe.send_text("/detach\r")

        try:
            spawn = spawning(tmp_path, asyncio.get_running_loop(), hosts)
            await run_local(app, output, drive, spawn)
            assert "keeps running in the background" in output.getvalue()
        finally:
            for host in hosts:
                await stop_host(host)

    asyncio.run(run())


@pytest.mark.in_process("moves a session running in this terminal; hosted, there is none")
def test_a_move_that_fails_letting_go_here_still_continues_in_the_host(tmp_path, host_dir):
    """Once the host is up it has the turn and the queue, whatever happens after."""

    async def run():
        output = StringIO()
        script = Script()
        app = local_app(tmp_path, script, output)
        hosts: list[SessionHost] = []
        left = []
        leave = app.leave_controller

        async def failing_leave(controller, **kwargs):
            # The move's, not the exit's.
            if isinstance(controller, SessionController) and not left:
                left.append(controller)
                raise RuntimeError("could not let go")
            await leave(controller, **kwargs)

        app.leave_controller = failing_leave

        async def drive(pipe, seen):
            await hang_with_a_queued_message(app, script, pipe, seen)
            app.request_app_restart()

        try:
            spawn = spawning(tmp_path, asyncio.get_running_loop(), hosts)
            await run_local(app, output, drive, spawn)
            shown = " ".join(output.getvalue().replace("▎", "").split())
            assert "reattach with: pcode --attach cccc3333" in shown
            host = hosts[0]
            await until(lambda: "hang here" in host.script.gates)
            host.script.release("hang here")
            await until(lambda: asked(host.script) == ["hang here", "queued after"])
            # Nothing ran here, and nothing here waits to.
            assert asked(script) == ["hello here", "hang here"]
            (previous,) = left
            assert previous.superseded and not previous.running
        finally:
            for host in hosts:
                await stop_host(host)
            app.runtime.close()

    asyncio.run(run())


@pytest.mark.in_process("moves a session running in this terminal; hosted, there is none")
def test_the_app_restart_signal_moves_a_local_session_to_a_host_then_exits(tmp_path, host_dir):
    """SIGUSR2: the process exits only once the host serves the session, and leaves it running."""

    async def run():
        output = StringIO()
        script = Script()
        app = local_app(tmp_path, script, output)
        hosts: list[SessionHost] = []

        async def drive(pipe, seen):
            await hang_with_a_queued_message(app, script, pipe, seen)
            # A terminal command waiting its turn: the exit would cancel it unrun.
            app.controller.handoff_idle.clear()
            app.controller.command("/help")
            await until(lambda: app.controller.held_commands)
            os.kill(os.getpid(), signal.SIGUSR2)

        try:
            spawn = spawning(tmp_path, asyncio.get_running_loop(), hosts)
            await run_local(app, output, drive, spawn)
            host = hosts[0]
            shown = " ".join(output.getvalue().replace("▎", "").split())
            assert "Not run, as this terminal is exiting; run again in the new one" in shown
            assert "still wanted: /help" in shown
            assert host.carry_received is None and app.detach_requested
            assert "keeps running in the background. Reattach: pcode --attach cccc3333" in (
                output.getvalue()
            )
            await until(lambda: not host.clients)
            assert not host.stopped.is_set()
            # The host carries on with the turn and then the queue, with nobody attached.
            await until(lambda: "hang here" in host.script.gates)
            host.script.release("hang here")
            await until(lambda: asked(host.script) == ["hang here", "queued after"])
        finally:
            for host in hosts:
                await stop_host(host)

    asyncio.run(run())


@pytest.mark.in_process("moves a session running in this terminal; hosted, there is none")
def test_the_app_restart_signal_exits_without_resuming_here_when_the_move_fails(tmp_path, host_dir):
    async def run():
        output = StringIO()
        script = Script()
        app = local_app(tmp_path, script, output)

        def spawn_host(**kwargs):
            raise OSError("no room for a host")

        async def drive(pipe, seen):
            await hang_with_a_queued_message(app, script, pipe, seen)
            app.request_app_restart()

        await run_local(app, output, drive, spawn_host)
        text = output.getvalue()
        assert "Could not move this session to a background host" in text
        # Neither the interrupted turn nor the queued message ran here on the way out.
        assert "Continuing the interrupted turn" not in text
        assert asked(script) == ["hello here", "hang here"]
        assert f"Continue with: pcode --continue {app.runtime.session.info.id}" in text
        app.runtime.close()

    asyncio.run(run())


@pytest.mark.in_process("exits a session running in this terminal")
def test_the_app_restart_signal_exits_past_an_open_popup(tmp_path, host_dir):
    async def run():
        output = StringIO()
        app = local_app(tmp_path, Script(), output)
        opened = asyncio.Event()

        async def hold_a_popup():
            async with app.popup(app.output, app.prompt_session):
                opened.set()
                await asyncio.Event().wait()

        async def drive(pipe, seen):
            await until(
                lambda: app.prompt_session is not None and app.prompt_session.app.is_running
            )
            app.prompt_session.app.create_background_task(hold_a_popup())
            await opened.wait()
            app.request_app_restart()

        def spawn_host(**kwargs):
            raise AssertionError("nothing saved yet, so nothing to move")

        await run_local(app, output, drive, spawn_host)
        assert app.app_restart_requested and not app.running
        app.runtime.close()

    asyncio.run(run())


def test_the_app_restart_handler_is_handed_back_without_the_default_action(monkeypatch):
    """The default kills the process, and another thread could take the signal then."""
    from pcode.app import restore_signal_handler

    def previous(signum, frame):
        pass

    installed = []
    real = signal.signal

    def recording(signum, handler):
        installed.append(handler)
        return real(signum, handler)

    async def run():
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGUSR2, lambda: None)
        monkeypatch.setattr(signal, "signal", recording)
        restore_signal_handler(loop, signal.SIGUSR2, previous)
        monkeypatch.undo()
        assert installed == [previous]
        assert signal.getsignal(signal.SIGUSR2) is previous
        assert not loop.remove_signal_handler(signal.SIGUSR2)  # No longer the loop's.

    before = signal.getsignal(signal.SIGUSR2)
    try:
        asyncio.run(run())
    finally:
        signal.signal(signal.SIGUSR2, before)


@pytest.mark.in_process("exits a session running in this terminal")
def test_an_app_restart_signal_from_startup_is_acted_on_and_exit_cleanup_stays_covered(
    tmp_path, host_dir, monkeypatch
):
    """Before the editor runs, `pcode.cli` only notes the signal; after, its handler is back."""
    from pcode import cli

    async def run():
        output = StringIO()
        app = local_app(tmp_path, Script(), output)
        monkeypatch.setattr(cli, "app_restart_signalled", False)
        previous = signal.signal(signal.SIGUSR2, cli.remember_app_restart)
        try:
            os.kill(os.getpid(), signal.SIGUSR2)  # As during the slow imports.
            await until(lambda: cli.app_restart_signalled)

            async def drive(pipe, seen):
                pass  # Nobody types: the noted signal alone ends it.

            def spawn_host(**kwargs):
                raise AssertionError("nothing saved yet, so nothing to move")

            await run_local(app, output, drive, spawn_host)
            assert app.app_restart_requested and not cli.app_restart_signalled
            # Exit cleanup is past, and a late signal is noted rather than fatal.
            assert signal.getsignal(signal.SIGUSR2) is cli.remember_app_restart
        finally:
            signal.signal(signal.SIGUSR2, previous)
            app.runtime.close()

    asyncio.run(run())


def test_the_app_restart_signal_restarts_a_stale_host_then_detaches(host_dir, monkeypatch):
    async def run():
        class Host:
            remote = True
            id, pid, session_id, lost = "aaaa1111", 4242, "session-1", False

        monkeypatch.setattr("pcode.host_protocol.code_fingerprint", lambda: "now")
        restarted = []

        async def restart_host():
            restarted.append(True)

        for code in ("old", "now"):
            write_entry(
                HostEntry(id="aaaa1111", pid=os.getpid(), model="m", workspace="/", code=code)
            )
            app = PreviewApp(model="m", runtime=Host(), console=Console(file=StringIO()))
            app._restart_host = restart_host
            app._startup_done.set()
            await app._app_restart()
            # Left running either way: the terminal detaches rather than stopping it.
            assert app.detach_requested and not app.running
        assert restarted == [True], "only a host on older code restarts"

    asyncio.run(run())


def test_a_busy_host_restarts_mid_turn_and_its_successor_continues_it(tmp_path, host_dir):
    """`restart(interrupt=True)`: the turn stops at its checkpoint, the successor asks again."""

    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        successor = None
        try:
            terminal, _, _ = await attach(host)
            terminal.submit("hello", "queue")
            await until(lambda: asked(script) == ["hello"] and not host.busy)
            terminal.submit("hang here", "queue")
            await until(lambda: "hang here" in script.gates)
            terminal.submit("queued after", "queue")
            await until(lambda: "queued after" in host.controller.activity.queued_prompts)
            # A command sent once the move holds the queue goes nowhere until it lands.
            host.controller.handoff_idle.clear()
            terminal.command("/effort high")
            await until(lambda: host.controller.held_commands)
            # Busy, so a plain restart says why not.
            assert "skipped" in await request_host(host.entry, "restart")
            answer = await request_host(host.entry, "restart", True)
            assert answer == {"id": host.successor}
            await asyncio.wait_for(host.stopped.wait(), 10)
            assert host.carry == {
                "continue": True,
                "messages": [("queued after", "queue")],
                "commands": ["/effort high"],
            }
            # Asked twice: already on its way.
            assert host.restart(interrupt=True) == {"id": host.successor}
            session_id = host.entry.session_id
            await host.close()
            host.controller.runtime.close()
            saved = SavedSession.open(session_id, tmp_path / "sessions")
            next_script = Script()
            successor = await start_host(host.successor, tmp_path, next_script, saved, host.carry)
            # It names the command it did not run for anyone.
            warnings = [args[0] for name, args, _ in successor.buffer if name == "warning"]
            assert any("/effort high" in text for text in warnings), warnings
            await until(lambda: "hang here" in next_script.gates)
            next_script.release("hang here")
            await until(lambda: asked(next_script) == ["hang here", "queued after"])
            assert not script.gate("hang here").is_set()
        finally:
            await stop_host(host)
            if successor is not None:
                await stop_host(successor)

    asyncio.run(run())


def test_a_carried_hand_over_is_read_once(tmp_path):
    from pcode.host import read_carry

    path = tmp_path / "x.carry"
    path.write_text(json.dumps({"continue": True, "messages": [["hi", "queue"]]}))
    assert read_carry(path) == {"continue": True, "messages": [["hi", "queue"]]}
    assert not path.exists()
    path.write_text("not json")
    assert read_carry(path) is None and not path.exists()
    assert read_carry(None) is None


@pytest.mark.in_process("moves a session running in this terminal; hosted, there is none")
def test_a_detach_whose_host_never_connects_continues_the_turn_here_once(tmp_path, host_dir):
    """The host is not told to continue until this terminal has it, so a failed
    `wait_for_host` (host killed) never leaves the turn running in two places."""

    async def run():
        output = StringIO()
        script = Script()
        app = local_app(tmp_path, script, output)
        hosts: list[SessionHost] = []

        async def wait_for_host(*args, **kwargs):
            await asyncio.sleep(0.2)  # Long enough for a carried continue to start.
            await stop_host(hosts[0])  # As the terminal's kill does.
            raise HostError("the host never answered")

        async def drive(pipe, seen):
            await hang_with_a_queued_message(app, script, pipe, seen)
            pipe.send_text("/detach\r")
            await seen("so it carries on here")
            await until(lambda: asked(script).count("hang here") == 2)
            script.release("hang here")
            await seen("Echo: queued after")
            await until(lambda: not app.activity.busy)
            pipe.send_text("\x04")

        spawn = spawning(tmp_path, asyncio.get_running_loop(), hosts)
        try:
            with patch("pcode.remote.wait_for_host", wait_for_host):
                await run_local(app, output, drive, spawn)
            assert hosts[0].script.requests == [], "the host never ran the turn"
            assert asked(script) == ["hello here", "hang here", "hang here", "queued after"]
        finally:
            app.runtime.close()

    asyncio.run(run())


def test_commands_wait_while_the_session_moves_and_go_with_it(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        controller = host.controller
        try:
            # Held, then taken by the move.
            controller.handoff_idle.clear()
            controller.command("/effort high")
            await until(lambda: controller.held_commands)
            await asyncio.sleep(0.05)
            assert controller.held_commands and not controller.command_idle.is_set()
            assert controller.take_typed() == [("/effort high", "command")]
            assert controller.command_idle.is_set()
            # Waiting, then run here once the move did not happen.
            controller.command("/effort high")
            await asyncio.sleep(0.05)
            assert not controller.command_idle.is_set()
            controller.resume_here(False)
            await until(lambda: controller.command_idle.is_set() and not controller.held_commands)
            assert controller.commands.empty()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_move_puts_back_a_prompt_cancelled_before_its_turn_was_journaled(tmp_path, host_dir):
    """Stopped before `run_turn` recorded the turn, there is nothing to `/continue`."""

    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        controller = host.controller
        reached = asyncio.Event()

        async def slow_start(text, **kwargs):
            # As `stream_on` awaits a side model's options before journaling.
            reached.set()
            await asyncio.Event().wait()

        class Owner:
            def __init__(self):
                self.told = []

            def started(self):
                self.told.append("started")

            def dropped(self):
                self.told.append("dropped")

        owner = Owner()
        controller.run_turn = slow_start
        try:
            controller.submit("first prompt", "queue", owner=owner)
            await asyncio.wait_for(reached.wait(), 5)
            assert await controller.settle_for_handoff() is False
            # Its sender had its result (started, then cancelled): put back without it.
            assert controller.prompts._fetched is None
            queued = list(controller.prompts._items._queue)
            assert [(text, who) for _g, text, _m, who, _s in queued] == [("first prompt", None)]
            assert owner.told == ["started"]
            assert controller.take_typed() == [("first prompt", "queue")]
            assert script.requests == []
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_move_keeps_a_prompt_waiting_on_a_reload_from_starting(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        controller = host.controller
        reloading, release = asyncio.Event(), asyncio.Event()

        async def reload_extensions():
            reloading.set()
            await release.wait()
            controller.reload_pending = False

        controller.reload_extensions = reload_extensions
        controller.reload_pending = True
        try:
            controller.submit("after the reload", "queue")
            await asyncio.wait_for(reloading.wait(), 5)
            settling = asyncio.create_task(controller.settle_for_handoff())
            await asyncio.sleep(0.05)
            assert not settling.done(), "waits for the reload"
            release.set()
            assert await asyncio.wait_for(settling, 5) is False
            assert controller.take_typed() == [("after the reload", "queue")]
            assert script.requests == []
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_the_app_restart_signal_starts_nothing_once_the_editor_has_exited():
    from types import SimpleNamespace

    async def run():
        app = PreviewApp(model="m", console=Console(file=StringIO()))
        started = []

        def create_background_task(work):
            work.close()
            started.append(work)

        loop = asyncio.get_running_loop()
        done = loop.create_future()
        done.set_result(None)
        for future, expected in ((None, 0), (done, 0), (loop.create_future(), 1)):
            app.app_restart_requested = False
            fake = SimpleNamespace(future=future, create_background_task=create_background_task)
            app.prompt_session = SimpleNamespace(app=fake)
            app.request_app_restart()
            assert app.app_restart_requested and len(started) == expected

    asyncio.run(run())


def test_a_busy_restart_with_no_host_entry_warns_rather_than_stopping_the_turn(host_dir):
    async def run():
        stopped = []

        class Host:
            remote = True
            id, pid, session_id, lost = "aaaa1111", 4242, "session-1", False

            def stop(self, **kwargs):
                stopped.append(kwargs)

        output = StringIO()
        app = PreviewApp(model="m", runtime=Host(), console=Console(file=output, width=200))
        app.activity.busy = True
        await app._restart_host()
        assert stopped == []
        assert "The session host did not restart" in output.getvalue()

    asyncio.run(run())


def test_a_hand_over_whose_host_cannot_spawn_is_removed(tmp_path, host_dir):
    with (
        patch("pcode.remote.subprocess.Popen", side_effect=OSError("no exec")),
        pytest.raises(OSError),
    ):
        spawn_host(model="m", workspace=tmp_path, directory=host_dir, carry={"continue": True})
    assert list(host_dir.glob("*.carry")) == []


def test_orphaned_hand_overs_are_swept_with_stale_entries(host_dir):
    from pcode.host_protocol import ORPHAN_CARRY_SECONDS

    old, fresh = host_dir / "aaaa1111.carry", host_dir / "bbbb2222.carry"
    for path in (old, fresh):
        path.write_text("{}")
    long_ago = time.time() - ORPHAN_CARRY_SECONDS - 60
    os.utime(old, (long_ago, long_ago))
    list_hosts(host_dir)
    assert not old.exists() and fresh.exists()
    # A dead host's own hand-over goes with its entry.
    write_entry(HostEntry(id="bbbb2222", pid=2**22 + 12345, model="m", workspace="/"))
    list_hosts(host_dir)
    assert not fresh.exists()


def test_a_restart_whose_successor_never_starts_says_what_it_was_carrying(host_dir):
    from pcode.host import start_successor
    from pcode.remote import restart_entry

    async def run():
        carry = {
            "continue": True,
            "messages": [["queued after", "queue"]],
            "commands": ["/effort high"],
        }

        def spawn_fails(**kwargs):
            raise OSError("no exec")

        # The old host could not spawn it: the hand-over is left to report.
        with patch("pcode.remote.spawn_host", spawn_fails):
            start_successor({"identity": "bbbb2222", "carry": carry})
        assert (host_dir / "bbbb2222.carry").exists()

        async def request(entry, method, *args):
            return {"id": "bbbb2222"}

        async def exited(pid, timeout=None):
            return True

        entry = HostEntry(id="aaaa1111", pid=4242, model="m", workspace="/")
        with (
            patch("pcode.remote.request_host", request),
            patch("pcode.remote.wait_for_exit", exited),
            pytest.raises(HostError) as raised,
        ):
            await restart_entry(entry, host_dir, interrupt=True)
        message = str(raised.value)
        assert "interrupted turn" in message
        assert "queued after" in message and "/effort high" in message
        assert not (host_dir / "bbbb2222.carry").exists()

    asyncio.run(run())


def test_an_interrupting_restart_falls_back_for_a_host_too_old_to_take_it(host_dir):
    from pcode.remote import restart_entry
    from pcode.rpc import RemoteError

    async def run():
        calls = []

        async def request(entry, method, *args):
            calls.append(args)
            if args:
                raise RemoteError("TypeError", "restart() takes 1 positional argument")
            return {"skipped": "a turn is running"}

        entry = HostEntry(id="aaaa1111", pid=4242, model="m", workspace="/")
        with patch("pcode.remote.request_host", request):
            assert await restart_entry(entry, host_dir, interrupt=True) == (
                "",
                "a turn is running",
            )
        assert calls == [(True,), ()]

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
            for noise in ("▎", "exited", "Run cancelled", "Stopped the session host"):
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


def test_a_full_session_id_finds_its_host_without_reading_sessions(host_dir, monkeypatch, tmp_path):
    import pcode.sessions
    from pcode.app import _running_host

    identity = "0123abcd-0000-4000-8000-000000000000"
    write_entry(
        HostEntry(id="live0000", pid=os.getpid(), model="m", workspace="/repo", session_id=identity)
    )

    def unread(*args, **kwargs):
        raise AssertionError("listed sessions")

    monkeypatch.setattr(pcode.sessions, "list_sessions", unread)
    assert _running_host(identity, None, tmp_path).id == "live0000"


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


def test_spawned_host_leaves_out_the_app_panes_terminal_env(host_dir, tmp_path):
    """Pcode.app's editor flags are for its pane's terminal, not the host or its children."""
    app_env = {
        "PCODE_EDITOR_ONLY": "1",
        "PCODE_NATIVE_TRANSCRIPT": "1",
        "PCODE_DOCK": "1",
        "PCODE_HOST_ID": "0123abcd",
    }
    with patch("pcode.remote.subprocess.Popen") as popen:
        with patch.dict(os.environ, app_env):
            spawn_host(model="m", workspace=tmp_path, no_worktree=True)
    env = popen.call_args.kwargs["env"]
    assert not set(app_env) & set(env)
    assert env["PCODE_HOST_DIR"] == str(host_dir)


def test_app_host_id_is_used_once_and_never_over_a_live_host(host_dir, monkeypatch):
    from pcode.remote import app_host_id

    monkeypatch.setenv("PCODE_HOST_ID", "0123abcd")
    assert app_host_id() == "0123abcd"
    assert app_host_id() is None  # Consumed: a later /new gets its own id.
    monkeypatch.setenv("PCODE_HOST_ID", "../etc")
    assert app_host_id() is None
    (host_dir / "4567cdef.json").write_text("{}")
    monkeypatch.setenv("PCODE_HOST_ID", "4567cdef")
    assert app_host_id() is None


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
    # Running with no terminal and unable to stop itself: the row says why.
    held = HostEntry("held0000", 1, "m", "/w/g", state="idle", code="2", held="a job")
    assert host_row(held, None, now=1, code="2").endswith("· held: a job")
    held.attached = 1
    assert "held" not in host_row(held, None, now=1, code="2")


def test_picker_nests_forks_under_their_original():
    from pcode.host_ui import host_row, nested, ordered

    original = HostEntry("orig0000", 1, "m", "/w", session_id="s-orig", title="orig", updated=1)
    # The fork is working, so its family leads; it keeps its place under the original.
    fork = HostEntry(
        "fork0000",
        1,
        "m",
        "/w",
        session_id="s-fork",
        forked_from="s-orig",
        title="fork",
        state="working",
        updated=2,
    )
    other = HostEntry("othr0000", 1, "m", "/w", session_id="s-othr", title="other", updated=3)
    # Its original is not running: a root, like any other.
    orphan = HostEntry(
        "orph0000", 1, "m", "/w", session_id="s-orph", forked_from="gone", title="orphan"
    )
    fresh = HostEntry("frsh0000", 1, "m", "/w", title="no session yet", updated=0)
    rows = nested(ordered([original, fork, other, orphan, fresh]))
    assert [(e.id, guide) for e, guide in rows] == [
        ("orig0000", ""),
        ("fork0000", "└─ "),
        ("orph0000", ""),
        ("othr0000", ""),
        ("frsh0000", ""),
    ]
    assert "└─ fork" in host_row(fork, None, now=2, guide="└─ ")


@pytest.mark.parametrize("action", ["attach", "stop"])
def test_switch_resumes_or_forgets_a_stopped_session(host_dir, monkeypatch, action):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from pcode import host_ui
    from pcode.host_protocol import remember_stopped
    from pcode.pick_ui import Picked

    remember_stopped(
        HostEntry("stop0000", 1, "m", "/w", session_id="sess-1", title="done", unseen=True)
    )
    output = StringIO()
    app = PreviewApp(model="function:script", console=Console(file=output, width=140))
    shown: list[dict] = []

    class Surface:
        async def pick(self, items, **options):
            shown.extend(item.id for item in items)
            assert options["actions"] == (host_ui.NEW_SESSION, host_ui.STOP)
            return Picked("stop0000", "x" if action == "stop" else None)

    @asynccontextmanager
    async def popup(output, session, **options):
        assert options["pick"]
        yield Surface()

    opened: list = []

    async def open_saved(identity, active_id):
        opened.append(identity)

    monkeypatch.setattr(app, "popup", popup)
    monkeypatch.setattr(app, "open_saved_session", open_saved)
    asyncio.run(
        app.switch_session(None, SimpleNamespace(app=SimpleNamespace(output=None, style=None)))
    )
    assert shown == ["stop0000"]
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


def test_restart_stops_a_host_keeping_its_worktree_and_names_the_successor(tmp_path, host_dir):
    """`/restart all` from another terminal: attached terminals hear where the session went."""

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, _, _ = await attach(host)
            closed = asyncio.Event()
            terminal.on_closed = closed.set
            # Nothing saved yet, so nothing for a successor to resume.
            assert await request_host(host.entry, "restart") == {
                "skipped": "it has no saved conversation to resume"
            }
            host.entry.session_id = "session-1"
            host.controller.command_idle.clear()
            assert await request_host(host.entry, "restart") == {"skipped": "a command is running"}
            assert not host.stopped.is_set()
            host.controller.command_idle.set()
            # An attached terminal does not hold it back.
            answer = await request_host(host.entry, "restart")
            assert answer == {"id": host.successor} and len(host.successor) == 8
            assert host.stopped.is_set() and host.keep_worktree and not host.idle_stopped
            await host.close()
            await asyncio.wait_for(closed.wait(), 5)
            assert terminal.successor == host.successor
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_successor_starts_from_the_host_with_its_session_and_profile(monkeypatch):
    from pcode import host as host_module

    spawned = []
    monkeypatch.setattr(
        "pcode.remote.spawn_host",
        lambda **kwargs: spawned.append(kwargs) or ("bbbb2222", None, "l"),
    )
    successor = {"identity": "bbbb2222", "model": "m", "resume": "session-1", "profile": None}
    host_module.start_successor(successor)
    assert spawned == [successor]


def test_restart_entry_reports_a_successor_that_never_started(tmp_path, host_dir):
    from pcode.remote import restart_entry

    async def run():
        for identity, log in (("aaaa1111", False), ("aaaa2222", True)):
            host = await start_host(identity, tmp_path, Script())
            host.entry.session_id = "session-1"
            original = host.restart

            def restart(interrupt=False, original=original, log=log):
                answer = original(interrupt)
                if log:  # Spawned, but its new code never got as far as its entry.
                    (host_dir / f"{answer['id']}.log").touch()
                return answer

            host.restart = restart
            try:
                with patch("pcode.remote.wait_for_exit", return_value=True):
                    match = "did not start" if log else "could not start its successor"
                    with pytest.raises(HostError, match=match):
                        await restart_entry(host.entry, timeout=0.2)
            finally:
                await stop_host(host)

    asyncio.run(run())


# A model that fails at startup leaves no runtime to release the session's lock.
@pytest.mark.parametrize("model", ["test", "no-such-provider:model"])
def test_a_real_host_restarts_into_a_successor_on_the_same_session(tmp_path, host_dir, model):
    """End to end: the successor resumes the very session (no fork) with the old host's env."""
    from pcode.remote import restart_entry, wait_for_exit_sync

    workspace, sessions = tmp_path / "ws", tmp_path / "sessions"
    workspace.mkdir()
    saved = SavedSession.create("test", workspace, sessions)
    session_id = saved.info.id
    saved.close()
    with patch.dict(os.environ, {"PCODE_RESTART_CHECK": "the-old-hosts"}):
        identity, process, log = spawn_host(
            model=model,
            workspace=workspace,
            resume=session_id,
            session_dir=sessions,
            no_worktree=True,
        )
    successor = None
    try:
        deadline = time.monotonic() + 60
        while True:
            entry = next((e for e in list_hosts() if e.id == identity), None)
            # Its entry is written before it listens: wait for the socket too.
            if entry is not None and entry.socket.exists():
                successor, skipped = asyncio.run(restart_entry(entry))
                if successor or skipped != "it is starting up":
                    break
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.2)
        assert successor, skipped
        process.wait(10)
        deadline = time.monotonic() + 60
        while not (new := next((e for e in list_hosts() if e.id == successor), None)) or (
            new.state != "idle"
        ):
            assert time.monotonic() < deadline, (host_dir / f"{successor}.log").read_text()
            time.sleep(0.2)
        assert new.session_id == session_id and not new.forked_from
        assert "restarted as" in log.read_text()
        assert ("startup failed" in log.read_text()) == (model != "test")
        # Spawned by the old host, so with its environment, not this caller's.
        environment = subprocess.run(
            ["ps", "eww", "-p", str(new.pid)], capture_output=True, text=True
        ).stdout
        if "PCODE_HOST_DIR" in environment:  # `ps` shows environments here.
            assert "PCODE_RESTART_CHECK=the-old-hosts" in environment
    finally:
        # Read from the files, not `list_hosts`: one that hung half-started is there too.
        for name in (identity, successor):
            path = host_dir / f"{name}.json"
            if name and path.exists():
                pid = json.loads(path.read_text())["pid"]
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
                wait_for_exit_sync(pid)
        if process.poll() is None:
            process.kill()


def test_a_host_older_than_restart_is_skipped_not_restarted_from_here(tmp_path, host_dir):
    """Restarting it from the caller would hand it the caller's environment."""
    from pcode.remote import restart_entry

    old_calls = frozenset({"attach", "query", "send", "run", "stop", "stop_if_idle", "asides_read"})

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            with patch("pcode.host.HOST_CALLS", old_calls):
                successor, skipped = await restart_entry(host.entry)
            assert successor == "" and "/restart it in its own terminal" in skipped
            assert not host.stopped.is_set()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_restart_hosts_restarts_stale_hosts_and_reports_each(host_dir, monkeypatch, capsys):
    from pcode.app import main

    for identity, code in (("old00001", "old"), ("busy0001", "old"), ("gone0001", "old")):
        entry = HostEntry(id=identity, pid=os.getpid(), model="test", workspace="/", code=code)
        entry.title = identity
        write_entry(entry)
    current = HostEntry(id="new00001", pid=os.getpid(), model="test", workspace="/")
    current.code = "now"
    write_entry(current)
    monkeypatch.setattr("pcode.host_protocol.code_fingerprint", lambda: "now")
    asked = []

    async def restart_entry(entry, directory=None, *, interrupt=False):
        assert interrupt
        asked.append(entry.id)
        if entry.id == "gone0001":
            raise ConnectionRefusedError("refused")
        return ("", "a turn or command is running") if entry.id == "busy0001" else ("cafe0001", "")

    monkeypatch.setattr("pcode.remote.restart_entry", restart_entry)
    monkeypatch.setattr(sys, "argv", ["pcode", "--restart-hosts", "stale"])
    main()
    assert sorted(asked) == ["busy0001", "gone0001", "old00001"]
    output = capsys.readouterr()
    assert "Restarted old00001 as cafe0001  old00001" in output.out
    assert "Skipped busy0001  busy0001: a turn or command is running" in output.out
    assert "Could not restart gone0001: refused" in output.err

    asked.clear()
    monkeypatch.setattr(sys, "argv", ["pcode", "--restart-hosts", "all"])
    main()
    assert "new00001" in asked


def test_restart_all_restarts_other_stale_hosts_then_this_one(tmp_path, host_dir, monkeypatch):
    async def run():
        class Host:
            remote = True
            id, pid, session_id, lost = "mine0001", 4242, "session-1", False

            def stop(self, *, keep_worktree=False):
                pass

        for identity in ("mine0001", "othr0001"):
            write_entry(
                HostEntry(id=identity, pid=os.getpid(), model="m", workspace="/", code="old")
            )
        write_entry(HostEntry(id="new00001", pid=os.getpid(), model="m", workspace="/", code="now"))
        monkeypatch.setattr("pcode.host_protocol.code_fingerprint", lambda: "now")
        restarted = []

        async def restart_and_report(entry, *, interrupt=False):
            assert interrupt, "busy hosts restart too"
            restarted.append(entry.id)
            return f"Restarted {entry.id}", False

        monkeypatch.setattr("pcode.remote.restart_and_report", restart_and_report)
        output = StringIO()
        app = PreviewApp(model="m", runtime=Host(), console=Console(file=output, width=200))
        own = []

        async def restart_host():
            own.append(True)

        app.restart_host = restart_host
        app.restart("all")
        assert app.restart_all_requested
        await app.restart_stale_hosts()
        # Current hosts are left alone; this terminal's own host goes last, the usual way.
        assert restarted == ["othr0001"] and own == [True]
        assert "Restarted othr0001" in output.getvalue()

    asyncio.run(run())


def test_a_terminal_whose_host_restarted_elsewhere_says_where_it_went():
    class Host:
        remote = True
        id, pid, session_id, lost = "aaaa1111", 4242, "session-1", False

    output = StringIO()
    app = PreviewApp(model="m", runtime=Host(), console=Console(file=output, width=200))
    app.controller.successor = "bbbb2222"
    app.host_closed()
    assert "restarting on the current pcode as bbbb2222" in output.getvalue()
    assert "/attach bbbb2222" in output.getvalue()


def test_an_app_pane_exits_when_its_host_is_stopped_elsewhere():
    """`pcode --kill-hosts` stops a host an app pane shows: the pane's pcode exits so it closes."""

    class Host:
        remote = True
        id, pid, session_id, lost = "aaaa1111", 4242, "session-1", False

    app = PreviewApp(model="m", runtime=Host(), console=Console(file=StringIO(), width=200))
    app.editor_only = True
    app.controller.host_stopped = True
    app.host_closed()
    assert not app.running and app.session_over


def test_an_app_pane_stays_open_while_restart_replaces_its_host(monkeypatch, host_dir):
    """`/restart` with nothing to resume stops the host itself; the pane must not exit."""

    async def run():
        class Host:
            remote = True
            id, pid, session_id, lost = "aaaa1111", 4242, "", False

            def stop(self, *, keep_worktree=False):
                # The host answers its stop with `host_closed` and goes away.
                app.controller.host_stopped = True
                app.host_closed()

        async def wait_for_exit(pid, timeout=30.0):
            return True

        started = []

        async def start_host_session(resume, note=None):
            started.append(resume)

        monkeypatch.setattr("pcode.remote.wait_for_exit", wait_for_exit)
        app = PreviewApp(model="m", runtime=Host(), console=Console(file=StringIO(), width=200))
        app.editor_only = True
        app.start_host_session = start_host_session
        await app.restart_host()
        assert app.running and not app.session_over
        assert started == [""]

    asyncio.run(run())


def test_an_app_pane_whose_host_vanished_says_so_in_the_editor():
    """Its transcript goes nowhere, so a crashed host is reported where the pane shows text."""

    class Host:
        remote = True
        id, pid, session_id, lost = "aaaa1111", 4242, "session-1", False

    app = PreviewApp(model="m", runtime=Host(), console=Console(file=StringIO(), width=200))
    app.editor_only = True
    app.activity.busy = True
    app.host_closed()
    assert app.running and not app.activity.busy
    assert "aaaa1111 has exited" in app.activity.notice


def test_restart_lets_the_host_start_its_successor_and_attaches_to_it(monkeypatch, host_dir):
    """`/restart` keeps the host's environment: this terminal only attaches to the successor."""
    from pcode.remote import TOO_OLD_TO_RESTART

    async def run():
        class Host:
            remote = True
            id, pid, session_id, lost = "aaaa1111", 4242, "session-1", False
            stopped_with = None

            def stop(self, *, keep_worktree=False):
                self.stopped_with = keep_worktree

        write_entry(HostEntry(id="aaaa1111", pid=os.getpid(), model="m", workspace="/"))
        answers = iter(
            [("bbbb2222", ""), ("", "a background command is running"), ("", TOO_OLD_TO_RESTART)]
        )

        async def restart_entry(entry, *, interrupt=False):
            assert interrupt  # A busy host's turn carries over; an idle one just restarts.
            assert app.restarting  # Its host going away is expected, not a warning.
            return next(answers)

        async def wait_for_host(identity, view, activity):
            return f"controller {identity}", {"welcome": identity}

        adopted, started = [], []

        async def adopt_controller(controller, welcome, note):
            adopted.append((controller, note))

        async def start_host_session(prompt="", *, resume=None, note=None):
            started.append(resume)

        monkeypatch.setattr("pcode.remote.restart_entry", restart_entry)
        monkeypatch.setattr("pcode.remote.wait_for_host", wait_for_host)
        output = StringIO()
        runtime = Host()
        app = PreviewApp(model="m", runtime=runtime, console=Console(file=output, width=200))
        app.adopt_controller = adopt_controller
        app.start_host_session = start_host_session
        await app.restart_host()
        assert adopted == [("controller bbbb2222", "Restarted on the current pcode (bbbb2222)")]
        assert runtime.stopped_with is None and not app.restarting
        # Busy: it says why and stays as it is.
        await app.restart_host()
        assert "did not restart: a background command is running" in output.getvalue()
        assert runtime.stopped_with is None and started == []
        # Older than the call: stopped and resumed from here, as before.
        with patch("pcode.remote.wait_for_exit"):
            await app.restart_host()
        assert runtime.stopped_with is True and started == ["session-1"]

    asyncio.run(run())


def test_a_drawer_watches_side_questions_read_only_and_never_holds_the_host(tmp_path, host_dir):
    """`pcode.drawer`: an observer gets the side questions, may call nothing, is no terminal."""
    import json

    from pcode.aside import Aside

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            aside = Aside(question="why?", status="answered", answer="because")
            host.controller.asides.items.append(aside)
            reader, writer = await asyncio.open_unix_connection(str(host.socket), limit=2**24)

            async def message():
                return json.loads(await asyncio.wait_for(reader.readline(), 5))

            writer.write(b'{"type": "observe", "version": 2}\n')
            assert (await message()) == {"type": "ready", "version": 2}
            [state] = (await message())["args"][0]
            assert state["answer"] == "because" and state["thread"] == aside.thread
            await until(lambda: host.connections == 0)
            assert host.idle() and not host.clients, "an open drawer is not a terminal"

            aside.answer += " more"
            host.emit_aside(aside)
            changed = await message()
            assert changed["method"] == "aside_changed"
            assert changed["args"][0]["answer"] == "because more"

            writer.write(
                b'{"type": "request", "id": 1, "method": "submit", "args": ["ls", "shell"]}\n'
            )
            refused = await message()
            assert refused["error"]["type"] == "PermissionError"

            host.stop()
            await host.close()
            assert (await message())["method"] == "host_closed"
            writer.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_the_drawer_viewer_draws_a_hosts_thread_in_its_own_terminal(tmp_path, host_dir):
    """`python -m pcode.btw_tail`, as the terminal app runs it, on a pty."""
    import pty
    import select
    import subprocess
    import sys

    from pcode.aside import Aside

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        master, slave = pty.openpty()
        process = None
        try:
            aside = Aside(question="why is it blue?", status="answered")
            aside.answer = "Rayleigh scattering."
            host.controller.asides.items.append(aside)
            process = subprocess.Popen(
                [sys.executable, "-m", "pcode.btw_tail", "aaaa1111", aside.thread],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env={**os.environ, "TERM": "xterm-256color"},
            )
            seen = b""
            deadline = asyncio.get_running_loop().time() + 10
            while b"Rayleigh" not in seen and asyncio.get_running_loop().time() < deadline:
                ready = await asyncio.to_thread(select.select, [master], [], [], 0.2)
                if ready[0]:
                    seen += os.read(master, 4096)
            assert b"why is it blue?" in seen and b"Rayleigh scattering." in seen, seen
            assert host.idle() and not host.clients
        finally:
            if process is not None:
                process.terminate()
                process.wait(5)
            os.close(master)
            os.close(slave)
            await stop_host(host)

    asyncio.run(run())


def test_observe_transcript_streams_history_catchup_and_live_data_only(tmp_path, host_dir):
    """Read-only transcript observers get history, catch-up, and live data calls."""
    from pcode.host import TRANSCRIPT_OBSERVER_VERSION
    from pcode.host_protocol import dumps, read_message

    async def run():
        host = await start_host("obs11111", tmp_path, Script())
        try:
            host.entry.unseen = True
            # Buffer a data call and a terminal-only call before the observer connects.
            host.emit("turn_event", (Message("buffered prose"),), {})
            host.emit("redraw", (), {})

            # Version mismatch returns an error cleanly.
            bad_r, bad_w = await asyncio.open_unix_connection(str(host.socket))
            bad_w.write(dumps({"type": "observe_transcript", "version": 999}))
            await bad_w.drain()
            err = await read_message(bad_r)
            assert err["type"] == "error"
            bad_w.close()

            # Valid handshake receives ready snapshot without changing attached/unseen.
            reader, writer = await asyncio.open_unix_connection(str(host.socket))
            hello = {"type": "observe_transcript", "version": TRANSCRIPT_OBSERVER_VERSION}
            writer.write(dumps(hello))
            await writer.drain()
            ready = await read_message(reader)
            assert ready["type"] == "ready"
            assert ready["version"] == TRANSCRIPT_OBSERVER_VERSION
            assert ready["paced_scrollback"] in ("rows", "typed", "off")
            assert "effort" in ready
            assert "context" in ready
            assert not ready["context"].startswith(" · ")
            methods = [c[0] for c in ready["calls"]]
            assert "turn_event" in methods
            assert "redraw" not in methods
            assert host.entry.attached == 0
            assert host.entry.unseen is True

            # Live data calls forward; terminal-only calls (`redraw`, `flash`) are filtered out.
            host.emit("redraw", (), {})
            host.emit("turn_event", (TextDelta("hello live"),), {})
            msg = await asyncio.wait_for(read_message(reader), timeout=2.0)
            assert msg["type"] == "notify"
            assert msg["method"] == "turn_event"
            assert msg["args"][0]["$event"]["kind"] == "TextDelta"
            assert msg["args"][0]["$event"]["fields"]["text"] == "hello live"

            # A merged side thread replays as `user` then `show_events`; the
            # observer gets the answer as a `turn_event` beside the prompt.
            host.controller.view.user("side question")
            host.controller.view.show_events((Message("side answer"),))
            msg = await asyncio.wait_for(read_message(reader), timeout=2.0)
            assert (msg["method"], msg["args"]) == ("user", ["side question"])
            msg = await asyncio.wait_for(read_message(reader), timeout=2.0)
            assert msg["method"] == "turn_event"
            assert msg["args"][0]["$event"]["fields"]["markdown"] == "side answer"

            # With nothing saved to redraw from, a replay leaves the observer's view alone
            # (`test_a_booting_host_...` covers the `history` it sends otherwise).
            host.emit("replay_conversation", (), {})
            host.emit("note", ("after replay",), {})
            msg = await asyncio.wait_for(read_message(reader), timeout=2.0)
            assert (msg["method"], msg["args"]) == ("note", ["after replay"])

            # Changing model/effort/context notifies transcript observers with `session_metadata`.
            host.controller.model = "anthropic:claude-sonnet"
            host.controller.current_effort = lambda: "high"
            host.controller.context_label = lambda: " · 12k/200k"
            host.push_state()
            msg = await asyncio.wait_for(read_message(reader), timeout=2.0)
            assert msg["method"] == "session_metadata"
            assert msg["args"][0] == {
                "model": "anthropic:claude-sonnet",
                "effort": "high",
                "context": "12k/200k",
            }

            # Repeating push_state with identical metadata does not re-emit `session_metadata`,
            # while `conversation_reset` clears the dedup cache so the next push always resends.
            host.push_state()
            host.emit("note", ("dedup sentinel",), {})
            msg = await asyncio.wait_for(read_message(reader), timeout=2.0)
            assert (msg["method"], msg["args"]) == ("note", ["dedup sentinel"])

            host.emit("conversation_reset", (), {})
            msg = await asyncio.wait_for(read_message(reader), timeout=2.0)
            assert msg["method"] == "conversation_reset"
            host.push_state()
            msg = await asyncio.wait_for(read_message(reader), timeout=2.0)
            assert msg["method"] == "session_metadata"
            assert msg["args"][0]["context"] == "12k/200k"

            # Observer is read-only: RPC requests are refused with PermissionError.
            req = {"type": "request", "id": 1, "method": "submit", "args": ["hi", "queue"]}
            writer.write(dumps(req))
            await writer.drain()
            refused = await asyncio.wait_for(read_message(reader), timeout=2.0)
            assert refused["error"]["type"] == "PermissionError"

            writer.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_plan_changes_reach_terminals_and_transcript_observers(tmp_path, host_dir):
    """Pcode.app shows the plan from `plan_changed` and the welcome's `plan`, not tool calls."""
    from pcode.host import TRANSCRIPT_OBSERVER_VERSION
    from pcode.host_protocol import dumps, read_message

    async def observe(host):
        reader, writer = await asyncio.open_unix_connection(str(host.socket))
        writer.write(dumps({"type": "observe_transcript", "version": TRANSCRIPT_OBSERVER_VERSION}))
        await writer.drain()
        return reader, writer, await read_message(reader)

    async def next_plan(reader):
        while True:
            msg = await asyncio.wait_for(read_message(reader), timeout=5.0)
            if msg.get("method") == "plan_changed":
                return msg["args"][0]

    async def run():
        host = await start_host("plan1111", tmp_path, Script())
        try:
            reader, writer, ready = await observe(host)
            assert ready["plan"] == []
            client, view, welcome = await attach(host)
            assert welcome["plan"] == []

            client.submit("make a plan", "queue")
            items = await next_plan(reader)
            assert [(item["content"], item["status"]) for item in items] == [
                ("Inspect", "in_progress")
            ]
            assert {"id", "content", "status", "size", "active_form"} <= items[0].keys()
            await until(lambda: view.count("plan_changed"))
            assert [args[0] for name, args, _ in view.calls if name == "plan_changed"] == [items]
            await until(lambda: view.count("after_turn"))

            # Connecting mid-session gets the current plan in the welcome, and
            # it is not replayed as a buffered call.
            late_reader, late_writer, late_ready = await observe(host)
            assert late_ready["plan"] == items
            assert "plan_changed" not in [call[0] for call in late_ready["calls"]]
            late_client, _, late_welcome = await attach(host)
            assert late_welcome["plan"] == items
            assert "plan_changed" not in [call[0] for call in late_welcome["calls"]]

            # A new conversation drops the plan.
            host.controller.new("")
            assert await next_plan(reader) == []
            assert host.plan == []

            for w in (writer, late_writer):
                w.close()
            await late_client.detach()
            await client.detach()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_observer_history_drops_what_the_app_never_draws(tmp_path):
    from pcode.host import observer_history

    records = [
        {"version": 1, "kind": "turn_started", "prompt": "hi"},
        {"version": 1, "kind": "ThinkingDelta", "text": "hm"},
        {"version": 1, "kind": "RunStatus", "text": "working"},
        {"version": 1, "kind": "ToolStarted", "call_id": "c1", "arguments": {"x": "y" * 50}},
        {
            "version": 1,
            "kind": "ToolStarted",
            "call_id": "c2",
            "name": "edit_file",
            "arguments": '{"path":"a.py","target":"x","replacement":"y"}',
        },
        {"version": 1, "kind": "compaction_checkpoint", "messages": []},
        {"version": 1, "kind": "Message", "markdown": "done"},
    ]
    from types import SimpleNamespace

    path = tmp_path / "transcript.jsonl"
    lines = [json.dumps(record).encode() + b"\n" for record in records]
    path.write_bytes(b"".join(lines) + b"not json\n")
    journal = SimpleNamespace(directory=tmp_path, tree=SimpleNamespace(nodes={}))

    history = observer_history(journal)
    assert [r["kind"] for r in history] == ["turn_started", "ToolStarted", "ToolStarted", "Message"]
    assert "arguments" not in history[1] and history[1]["call_id"] == "c1"
    assert history[2]["call_id"] == "c2" and "target" in history[2]["arguments"]
    # Up to the settled end only.
    assert [r["kind"] for r in observer_history(journal, len(lines[0]))] == ["turn_started"]
    missing = SimpleNamespace(directory=tmp_path / "missing", tree=journal.tree)
    assert observer_history(missing) == []


def test_observer_history_shows_only_the_selected_branch(tmp_path):
    """As the terminal's `/tree` redraw does (`SessionJournal.active_records`)."""
    from types import SimpleNamespace

    from pcode.host import observer_history

    records = [
        {"kind": "turn_started", "prompt": "root", "run_id": "a"},
        {"kind": "Message", "markdown": "root answer"},
        {"kind": "turn_started", "prompt": "abandoned", "run_id": "b"},
        {"kind": "Message", "markdown": "abandoned answer", "run_id": "b"},
        {"kind": "turn_started", "prompt": "chosen", "run_id": "c"},
        {"kind": "Message", "markdown": "chosen answer"},
    ]
    (tmp_path / "transcript.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    tree = SimpleNamespace(nodes={"a": 1, "b": 1, "c": 1}, active="c", path=lambda _: ["a", "c"])
    history = observer_history(SimpleNamespace(directory=tmp_path, tree=tree))
    assert [r.get("prompt") or r.get("markdown") for r in history] == [
        "root",
        "root answer",
        "chosen",
        "chosen answer",
    ]


def test_a_booting_host_shows_observers_the_conversation_it_is_resuming(tmp_path):
    """The socket opens before the runtime loads; history comes from the saved session."""
    from types import SimpleNamespace

    (tmp_path / "transcript.jsonl").write_text(
        json.dumps({"version": 1, "kind": "Message", "markdown": "earlier"}) + "\n"
    )
    journal = tmp_path / "transcript.jsonl"
    saved = SimpleNamespace(
        directory=tmp_path,
        tree=SimpleNamespace(nodes={}),
        journal_size=lambda: journal.stat().st_size,
    )
    booting = SimpleNamespace(
        controller=SimpleNamespace(runtime=None, _saved_session=saved),
        settled_end=None,
        boot_history_end=None,
    )
    history = SessionHost._read_settled_history(booting)
    assert [r["markdown"] for r in history] == ["earlier"]
    assert booting.boot_history_end == journal.stat().st_size
    booting.controller._saved_session = None
    assert SessionHost._read_settled_history(booting) == []

    # Once loaded, boot's replay skips observers already shown that journal...
    sent = []
    booting.controller.runtime = SimpleNamespace(session=saved)
    booting.transcript_observers = [SimpleNamespace(_send_encoded=sent.append)]
    booting._read_settled_history = lambda: SessionHost._read_settled_history(booting)
    SessionHost._replay_to_observers(booting, "replay_conversation")
    assert sent == [] and booting.boot_history_end is None
    # ...and any later replay sends it.
    SessionHost._replay_to_observers(booting, "replay_conversation")
    assert len(sent) == 1 and b'"history"' in sent[0] and b"earlier" in sent[0]


def test_host_starts_without_a_model_and_activates_one_later(tmp_path, host_dir, monkeypatch):
    """A host started with no model runs in preview mode until /model activates an agent."""
    from pcode.host import TRANSCRIPT_OBSERVER_VERSION, SessionHost
    from pcode.host_protocol import HostEntry, dumps, read_message, write_entry
    from pcode.remote import RemoteController

    script = Script()

    def fake_create_agent(model, workspace, capabilities=(), subagents=(), advisor=None):
        return Agent(FunctionModel(stream_function=script.model))

    monkeypatch.setattr("pcode.agent.create_agent", fake_create_agent)
    loaded, mcp_started = [], []
    monkeypatch.setattr(
        SessionController, "_load_extensions", lambda self, *args: loaded.append(args)
    )
    monkeypatch.setattr(SessionController, "start_mcp_defaults", lambda self: mcp_started.append(1))

    async def run():
        from pcode.host import plan_startup

        entry = HostEntry(id="nomod111", pid=os.getpid(), model="", workspace=str(tmp_path))
        write_entry(entry, host_dir)
        host = SessionHost(entry, host_dir)
        controller = host.controller
        plan_startup(controller, None, None)
        assert not controller.startup_pending and controller.startup_error is None
        controller.workspace = tmp_path
        controller.session_dir = tmp_path / "sessions"
        controller.save_sessions = True
        await host.serve()
        await host.boot()
        try:
            view = View()
            client, welcome = await RemoteController.connect(host.socket, view, Activity())
            await client.start(welcome)
            try:
                assert client.model is None
                # Connect a transcript observer and send a preview prompt before any model is set.
                obs_r, obs_w = await asyncio.open_unix_connection(str(host.socket))
                obs_w.write(
                    dumps({"type": "observe_transcript", "version": TRANSCRIPT_OBSERVER_VERSION})
                )
                await obs_w.drain()
                ready = await read_message(obs_r)
                assert ready["type"] == "ready" and ready["model"] == ""
                assert ready["show_thinking"] == host.activity.thinking_mode

                client.submit("hello preview", "queue")
                await until(lambda: view.count("preview_reply"))
                seen = []
                while len(seen) < 2:
                    msg = await asyncio.wait_for(read_message(obs_r), timeout=2.0)
                    if msg["method"] in ("user", "turn_event"):
                        seen.append(msg)
                assert seen[0]["method"] == "user" and seen[0]["args"] == ["hello preview"]
                assert seen[1]["method"] == "turn_event"
                assert "local UI preview" in seen[1]["args"][0]["$event"]["fields"]["markdown"]

                # `/show-thinking` reaches the app's transcript as `thinking_mode`.
                client.set_thinking("off")
                while True:
                    msg = await asyncio.wait_for(read_message(obs_r), timeout=2.0)
                    if msg.get("method") == "thinking_mode":
                        break
                assert msg["args"] == ["off"]

                # Nothing to load or start until a model is picked.
                assert not loaded and not mcp_started
                # Activating a model upgrades the host from PreviewRuntime to AgentRuntime,
                # finishing startup as a launch with a model would have.
                await controller.activate_model("function:script")
                assert isinstance(controller.runtime, AgentRuntime)
                assert len(loaded) == 1
                await controller.follow_up()  # What the host runs after /model returns.
                assert mcp_started == [1]
                host.push_state()
                await until(lambda: client.model == "function:script")
                assert host.entry.model == "function:script"
                obs_w.close()
            finally:
                await client.detach()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_in_process_preview_model_only_swaps_the_runtime(tmp_path, monkeypatch):
    """Outside a host, /model from the canned preview is unchanged: no extensions or MCP."""
    from pcode.runtime import PreviewRuntime

    script = Script()
    monkeypatch.setattr(
        "pcode.agent.create_agent",
        lambda *args, **kwargs: Agent(FunctionModel(stream_function=script.model)),
    )
    monkeypatch.setattr(
        SessionController,
        "_load_extensions",
        lambda self, *args: pytest.fail("extensions loaded in an in-process preview"),
    )

    async def run():
        controller = SessionController(View(), Activity(), PreviewRuntime())
        controller.workspace = tmp_path
        assert not controller.starts_on_model
        await controller.activate_model("function:script")
        try:
            assert isinstance(controller.runtime, AgentRuntime)
            assert controller.model == "function:script"
            assert not controller.mcp_defaults_requested
            assert controller.extensions is None
        finally:
            controller.runtime.close()

    asyncio.run(run())


def test_resumed_host_without_a_model_waits_for_model_to_restore(tmp_path, host_dir, monkeypatch):
    """A saved session with no model to resume with: startup settles, and /model restores it."""
    from pcode.host import plan_startup

    script = Script()
    monkeypatch.setattr(
        "pcode.agent.create_agent",
        lambda *args, **kwargs: Agent(FunctionModel(stream_function=script.model)),
    )
    monkeypatch.setattr(SessionController, "_load_extensions", lambda self, *args: None)
    saved = SavedSession.create("", tmp_path, tmp_path / "sessions")

    async def run():
        entry = HostEntry(
            id="nomod222",
            pid=os.getpid(),
            model="",
            workspace=str(tmp_path),
            session_id=saved.info.id,
        )
        write_entry(entry, host_dir)
        host = SessionHost(entry, host_dir)
        controller = host.controller
        plan_startup(controller, None, saved)
        controller.workspace = tmp_path
        controller.session_dir = tmp_path / "sessions"
        controller.save_sessions = True
        assert not controller.startup_pending
        await host.serve()
        await host.boot()
        try:
            assert not controller.startup_pending
            assert "/model" in str(controller.startup_error)
            terminal, _, _ = await attach(host)
            try:
                assert not terminal.startup_pending
                assert "pick one with /model" in str(terminal.startup_error)
                await controller.activate_model("function:script")
                assert controller.startup_error is None
                assert isinstance(controller.runtime, AgentRuntime)
                assert controller.runtime.session is saved
            finally:
                await terminal.detach()
        finally:
            host.stop()
            await host.close()
            close = getattr(controller.runtime, "close", None)
            if close is not None:
                close()

    asyncio.run(run())


def test_move_into_follows_the_workspace_and_survives_a_missing_one(monkeypatch, tmp_path):
    from pcode.host import move_into

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    workspace = tmp_path / "tree"
    workspace.mkdir()
    move_into(workspace)
    assert Path.cwd() == workspace.resolve()
    assert os.environ["PWD"] == str(workspace)
    move_into(tmp_path / "gone")  # Not there: stays put rather than failing.
    assert Path.cwd() == workspace.resolve()


def test_connect_or_respawn_host_spawns_fresh_host_when_resumed_host_vanished(
    monkeypatch, tmp_path
):
    """If `--continue` routed to a host that vanished before attach, spawn a fresh one."""
    import tempfile
    from types import SimpleNamespace

    from pcode.app import PreviewApp
    from pcode.remote import HostError, HostLaunch

    spawned: list[dict] = []
    fresh_controller = SimpleNamespace(id="newhost1")

    with tempfile.TemporaryDirectory(prefix="pc-", dir="/tmp") as raw_dir:
        host_dir = Path(raw_dir)

        spawned: list[dict] = []
        waited_pids: list[int] = []
        fresh_controller = SimpleNamespace(id="newhost1")
        successor_controller = SimpleNamespace(id="succ2222")

        def fake_spawn_host(**kwargs):
            spawned.append(kwargs)
            return "newhost1", SimpleNamespace(poll=lambda: None), host_dir / "newhost1.log"

        async def fake_wait_for_host(identity, view, activity, process=None, log=None, **kwargs):
            if identity == "deadhost":
                raise HostError("Session host deadhost is no longer running.")
            if identity == "succ2222":
                return successor_controller, {"id": "succ2222"}
            assert identity == "newhost1"
            return fresh_controller, {"id": "newhost1"}

        async def fake_wait_for_exit(pid, timeout=30.0):
            waited_pids.append(pid)
            return True

        monkeypatch.setattr("pcode.remote.spawn_host", fake_spawn_host)
        monkeypatch.setattr("pcode.remote.wait_for_host", fake_wait_for_host)
        monkeypatch.setattr("pcode.remote.wait_for_exit", fake_wait_for_exit)
        monkeypatch.setattr("pcode.host_protocol.list_hosts", lambda directory=None: [])
        monkeypatch.setenv("PCODE_HOST_ID", "deadbeef")

        app = SimpleNamespace(
            model="function:script",
            workspace=tmp_path,
            session_dir=tmp_path / "sessions",
            activity=SimpleNamespace(),
        )
        launch = HostLaunch(
            "deadhost", process=None, directory=host_dir, resume="sess-123", pid=4242
        )
        controller, welcome = asyncio.run(PreviewApp._connect_or_respawn_host(app, launch))
        assert controller is fresh_controller
        assert welcome == {"id": "newhost1"}
        assert waited_pids == [4242]
        assert len(spawned) == 1
        assert spawned[0]["resume"] == "sess-123"
        assert spawned[0]["identity"] == "deadbeef"

        # If a live successor host already owns the session (e.g. /restart), attach to it instead.
        successor_entry = HostEntry(
            id="succ2222",
            pid=9999,
            model="function:script",
            workspace=str(tmp_path),
            session_id="sess-123",
        )
        monkeypatch.setattr(
            "pcode.host_protocol.list_hosts", lambda directory=None: [successor_entry]
        )
        controller2, welcome2 = asyncio.run(PreviewApp._connect_or_respawn_host(app, launch))
        assert controller2 is successor_controller
        assert welcome2 == {"id": "succ2222"}
        assert len(spawned) == 1

        # Explicit --attach (no resume) does not silently spawn a new host.
        attach_only = HostLaunch("deadhost", process=None, directory=host_dir, resume=None)
        with pytest.raises(HostError, match="no longer running"):
            asyncio.run(PreviewApp._connect_or_respawn_host(app, attach_only))


def test_controller_manage_worker_direct_actions():
    from types import SimpleNamespace

    from pcode.controller import SessionController
    from pcode.host import HOST_CALLS
    from pcode.worker_channel import Question, WorkerHandle, WorkerHub

    assert "manage_worker" in HOST_CALLS

    hub = WorkerHub()
    cancelled: list[str] = []
    handle = WorkerHandle(id="w1", call_id="call-1", agent="worker", task="check tests")
    handle.cancel = lambda: cancelled.append(handle.id)
    hub._handles[handle.id] = handle

    notes: list[str] = []
    cap = SimpleNamespace(hub=hub)
    ctrl = SimpleNamespace(
        runtime=SimpleNamespace(
            agent=SimpleNamespace(root_capability=SimpleNamespace(capabilities=(cap,)))
        ),
        view=SimpleNamespace(note=notes.append),
    )

    # Message a running worker directly
    res = SessionController.manage_worker(ctrl, "message", handle.id, "focus on edge cases")
    assert res["ok"] is True
    assert list(handle.inbox) == ["focus on edge cases"]

    # Answer a pending question directly
    question = Question(text="Proceed?", fallback="abort")
    handle.question = question
    res = SessionController.manage_worker(ctrl, "answer", handle.id, "yes, proceed")
    assert res["ok"] is True
    assert question.answer == "yes, proceed"
    assert question.answered.is_set()

    # Second answer while question is already answered is rejected
    dup = SessionController.manage_worker(ctrl, "answer", handle.id, "changed my mind")
    assert dup["ok"] is False
    assert question.answer == "yes, proceed"

    # Stop a background worker directly
    res = SessionController.manage_worker(ctrl, "stop", handle.id)
    assert res["ok"] is True
    assert handle.stop_requested is True
    assert cancelled == [handle.id]

    # Unknown worker returns ok=False
    bad = SessionController.manage_worker(ctrl, "message", "w999", "hi")
    assert bad["ok"] is False


def test_host_client_navigate_tree_rpc():
    from types import SimpleNamespace

    from pcode.host import HOST_CALLS, _Client

    assert "navigate_tree" in HOST_CALLS

    calls: list[tuple[str | None, bool]] = []
    pushed: list[bool] = []

    async def fake_navigate(identity: str | None, edit: bool = False) -> str:
        calls.append((identity, edit))
        if identity == "busy-turn":
            raise ValueError("/tree is unavailable while working or messages are queued.")
        return "edited prompt" if edit else ""

    host = SimpleNamespace(
        controller=SimpleNamespace(
            navigate_tree=fake_navigate,
            runtime=SimpleNamespace(navigate=lambda *a, **kw: None),
        ),
        push_state=lambda: pushed.append(True),
    )
    client = _Client.__new__(_Client)
    client.host = host

    async def run():
        res = await client.navigate_tree("t2", False)
        assert res == {"ok": True, "draft": ""}
        assert calls[-1] == ("t2", False)
        assert len(pushed) == 1

        res_edit = await client.navigate_tree("t1", True)
        assert res_edit == {"ok": True, "draft": "edited prompt"}
        assert calls[-1] == ("t1", True)
        assert len(pushed) == 2

        res_err = await client.navigate_tree("busy-turn", False)
        assert res_err["ok"] is False
        assert "unavailable" in str(res_err["message"])
        assert len(pushed) == 2

        host.controller.runtime = None
        res_boot = await client.navigate_tree("t1", False)
        assert res_boot == {"ok": False, "message": "Session is still starting."}

    asyncio.run(run())
