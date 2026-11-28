"""A headless process that owns one conversation, for terminals to attach to.

The host runs a `SessionController` exactly as a terminal would in-process,
but renders nothing: its view, `HostView`, sends every call to the attached
terminals over `pcode.rpc`, and keeps the calls since the running turn began
so a terminal attaching mid-turn can catch up. Terminals come and go; the host
stops when told to, when the session ends itself, or after an idle timeout.

Started by `pcode.remote.spawn_host`, which runs `python -m pcode.host` from the
terminal that asked for it, so the host inherits that terminal's environment
(direnv credentials, `PATH`) the way a local session would.

The connection starts with one plain line each way (`hello`, then `ready` or
`error`), so a host and terminal on different protocol versions refuse each
other with a message instead of hanging. Then both ends are `rpc.Peer`s: the
terminal calls `attach` for its `welcome`, sends intents (`INTENTS`) and
queries, and receives view calls.
"""

import argparse
import asyncio
import contextlib
import itertools
import json
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from pcode import remote_profile
from pcode.controller import COMMAND_MODE, INTENTS, SESSION_FIELDS, SessionController
from pcode.error_report import error_message
from pcode.host_protocol import (
    LINE_LIMIT,
    NOTHING_TO_RESUME,
    PROTOCOL,
    HostEntry,
    code_fingerprint,
    dumps,
    forget_stopped,
    host_dir,
    new_host_id,
    read_message,
    remember_stopped,
    remove_entry,
    socket_path,
    write_entry,
)
from pcode.jobs import registry as job_registry
from pcode.rpc import Peer, broadcast
from pcode.runtime import (
    CommandOutput,
    EditPreview,
    PlanUpdated,
    PreviewRuntime,
    TextDelta,
    ThinkingDelta,
)
from pcode.ui import NOTICE_SECONDS, Activity

# Consecutive events of these kinds merge in the catch-up buffer: deltas join,
# and a newer snapshot of the same call replaces the older one.
DELTA_EVENTS = (TextDelta, ThinkingDelta)

# How long a host with nothing to do waits for a terminal before stopping, when
# `session_host_idle_minutes` is 0: long enough to /attach away and back.
IDLE_GRACE_SECONDS = 15.0
# How often drawers watching the side questions hear of changes (`pcode.drawer`).
OBSERVER_INTERVAL = 0.08
SNAPSHOT_EVENTS = (CommandOutput, EditPreview)

# Read-only transcript observer handshake version and the data-only view calls
# forwarded to it (terminal-only calls like `redraw`, `flash`, pickers are excluded).
TRANSCRIPT_OBSERVER_VERSION = 1
TRANSCRIPT_OBSERVER_CALLS = frozenset(
    {
        "turn_started",
        "turn_event",
        "cancelled",
        "after_turn",
        "user",
        "shell_result",
        "drop_output",
        "note",
        "retained_note",
        "warning",
        "error",
        "conversation_reset",
        "replay_conversation",
        "show_branch",
        "session_changed",
        "state",
        "plan_changed",
    }
)

# View calls never kept for a terminal attaching later: its welcome carries the
# current value instead (`plan`), so replaying old ones would only repeat it.
UNBUFFERED = frozenset({"plan_changed"})

# View calls that change what the conversation is, so a terminal attaching
# later reads it from the journal instead of from the buffer.
RESETS = {"conversation_reset", "replay_conversation", "show_branch"}
# Of those, the ones that redraw the conversation from the journal. A transcript
# observer has no journal reader of its own, so it gets the records instead, as
# a `history` call (`SessionHost.emit`).
REPLAYS = {"replay_conversation", "show_branch"}

# Journal records a transcript observer never draws: streaming deltas (the
# settled Message and Thinking follow them), status ticks, plan snapshots, and
# model traffic. A long session's journal is mostly these, and its whole history
# goes to an observer in one message.
OBSERVER_SKIPPED_KINDS = frozenset(
    {
        "TextDelta",
        "ThinkingDelta",
        "RunStatus",
        "PlanUpdated",
        "PlanPreview",
        "ChildText",
        "ChildPlan",
        "WorkerStatus",
        "CacheBust",
        "compaction_checkpoint",
        "request",
        "response",
    }
)
# Fields an observer never reads; a tool's arguments can be most of a journal.
# File-edit tools keep `arguments` so observers can synthesize diffs when a
# backend records ToolStarted/ToolSummary without emitting EditCompleted.
OBSERVER_DROPPED_FIELDS = {"ToolStarted": ("arguments",)}
OBSERVER_EDIT_ARGUMENT_TOOLS = frozenset({"edit_file", "write_file", "edit", "write"})
# Where a journal line names its kind: right after its version and time.
_JOURNAL_KIND = re.compile(rb'"kind": "([^"]+)"')


def observer_history(journal, limit: int | None = None) -> list[dict]:
    """The selected branch of `journal` up to byte `limit`, as a transcript observer draws it.

    Filters branches as `SessionJournal.active_records` does, so `/tree` changes it.
    """
    from pcode.sessions import side_record

    try:
        with (journal.directory / "transcript.jsonl").open("rb") as fh:
            raw = fh.read() if limit is None else fh.read(max(0, limit))
    except OSError:
        return []
    tree = journal.tree
    selected = set(tree.path(tree.active)) if tree.nodes else None
    recording = None
    records: list[dict] = []
    for line in raw.splitlines():
        if not line:
            continue
        # Skip the bulk without parsing it; anything unexpected is parsed and checked.
        sniffed = _JOURNAL_KIND.search(line, 0, 200)
        if sniffed is not None and sniffed[1].decode(errors="replace") in OBSERVER_SKIPPED_KINDS:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("kind") in OBSERVER_SKIPPED_KINDS:
            continue
        if obj.get("kind") == "turn_started" and not side_record(obj):
            recording = obj.get("run_id")
        if selected is not None and (obj.get("run_id") or recording) not in selected:
            continue
        kind = obj.get("kind")
        keep_arguments = (
            kind == "ToolStarted"
            and isinstance(obj.get("name"), str)
            and obj["name"].lower() in OBSERVER_EDIT_ARGUMENT_TOOLS
        )
        for name in OBSERVER_DROPPED_FIELDS.get(kind, ()):
            if name == "arguments" and keep_arguments:
                continue
            obj.pop(name, None)
        records.append(obj)
    return records


# What a terminal may call besides the controller's intents.
HOST_CALLS = frozenset(
    {
        "attach",
        "peek",
        "ask_aside",
        "query",
        "send",
        "run",
        "stop",
        "stop_if_idle",
        "release",
        "restart",
        "asides_read",
        "continue_interrupted",
        "manage_worker",
        "navigate_tree",
    }
)

_MISSING = object()


class MirroredActivity(Activity):
    """The host's live-panel state; each change to a session field goes to the terminals."""

    def __init__(self, send) -> None:
        object.__setattr__(self, "_send", None)
        object.__setattr__(self, "_sent", {})
        super().__init__()
        object.__setattr__(self, "_send", send)

    def __setattr__(self, name: str, value) -> None:
        object.__setattr__(self, name, value)
        if name in SESSION_FIELDS and self._send is not None:
            self.push()

    def flash(self, text: str, seconds: float = NOTICE_SECONDS) -> None:
        # Text and expiry in one push: a terminal that drew the new text with
        # the old expiry would show it twice (as a toast, `drawer.toast`).
        send = self._send
        object.__setattr__(self, "_send", None)
        try:
            super().flash(text, seconds)
        finally:
            object.__setattr__(self, "_send", send)
        if send is not None:
            self.push()

    def push(self) -> None:
        """Send the session fields that changed since the last push; lists compare by value."""
        changes = {}
        for name in SESSION_FIELDS:
            value = getattr(self, name)
            if isinstance(value, list):
                value = list(value)
            if self._sent.get(name, _MISSING) != value:
                self._sent[name] = value
                changes[name] = value
        if changes:
            self._send(changes)

    def session_fields(self) -> dict:
        return {name: getattr(self, name) for name in SESSION_FIELDS}


class _Run:
    """A command sent with `run`, until it has run."""

    def __init__(self) -> None:
        self.done = asyncio.get_running_loop().create_future()
        # The background tasks (compaction, MCP) as it began; None until it does.
        self.work: tuple | None = None


class _Owner:
    """A headless caller's queued message: when it starts, or that it never will.

    Told to that caller alone, in order with the view calls, so everything
    between `message_started` and `after_turn` is its turn.
    """

    def __init__(self, client: "_Client") -> None:
        self.client = client

    def started(self) -> None:
        self.client.peer.notify("message_started")

    def dropped(self) -> None:
        self.client.peer.notify("message_dropped")


class _Client:
    """One attached terminal, and the calls it may make."""

    def __init__(self, host: "SessionHost", number: int) -> None:
        self.host = host
        self.number = number
        self.peer: Peer | None = None
        # A caller with no editor (`--attach --print`): popups and side-answer
        # notices meant for "the terminal last used" skip it, and it does not
        # count as a terminal watching the session.
        self.headless = False
        # What attaching took from the terminals, given back if it turns out headless.
        self.displaced: _Client | None = None
        self.unseen_before = False

    # Intents: the controller's, with commands tagged by who sent them.

    def submit(self, text: str, mode: str) -> None:
        if mode == "shell" and remote_profile.active() is not None:
            # A `!command` runs outside the sandbox; an unattended host takes none,
            # whoever connects (its own model's commands cannot connect at all).
            raise PermissionError("Shell mode is off in a remote session.")
        self.host.touch(self)
        self.host.controller.submit(text, mode)

    def command(self, text: str, tag=None) -> None:
        self.host.touch(self)
        self.host.controller.command(text, (self.number, tag))

    # Host calls for a caller with no editor to watch

    def send(self, text: str, mode: str = "queue") -> None:
        """Send `text`; the caller hears when its turn starts (see `_Owner`).

        `queue` waits for the running turn to end; `steering` joins it at its
        next model request, and the rest of that turn is the caller's (or
        starts one, when the session is idle).
        """
        if mode not in ("queue", "steering"):
            raise ValueError(f"mode must be 'queue' or 'steering', not {mode!r}")
        self.become_headless()
        self.host.active_at = time.monotonic()
        self.host.controller.submit(text, mode, owner=_Owner(self))

    async def run(self, text: str) -> bool:
        """`command`, returning once it has run, and whether it did.

        A command dropped from the queue (a Ctrl+C elsewhere cleared it) or
        refused (one that waits for an idle session) did not. Background work
        the command started (compaction, MCP sign-in) is waited for, as its
        outcome is the point of sending, say, `/compact`.
        """
        self.become_headless()
        self.host.active_at = time.monotonic()
        tag = (self.number, f"run-{next(self.host.run_ids)}")
        run = self.host.runs[tag] = _Run()
        try:
            self.host.controller.command(text, tag)
            await run.done
        finally:
            self.host.runs.pop(tag, None)
        if run.work is None:
            return False
        controller = self.host.controller
        compaction, mcp = run.work
        if controller.compact_task not in (None, compaction):
            await controller.compact_idle.wait()
        if controller.mcp_task not in (None, mcp):
            await controller.mcp_idle.wait()
        return True

    def become_headless(self) -> None:
        """This caller has no editor: give the terminals back what attaching took."""
        if self.headless:
            return
        self.headless = True
        host = self.host
        if host._latest is self:
            host._latest = self.displaced
        host.update(
            attached=len(host.terminals()),
            unseen=host.entry.unseen or self.unseen_before,
            touch=False,
        )

    def cancel(self) -> None:
        self.host.controller.cancel()

    def set_thinking(self, mode: str) -> None:
        self.host.controller.set_thinking(mode)
        self.host.push_state()
        # Pcode.app's transcript shows or hides thinking rows to match.
        for peer in list(self.host.transcript_observers):
            peer.notify("thinking_mode", self.host.activity.thinking_mode)

    def adjust_effort(self, direction: int) -> None:
        self.host.controller.adjust_effort(direction)
        self.host.controller.view.session_changed()

    def stop_jobs(self, job_ids) -> None:
        self.host.controller.stop_jobs(job_ids)

    def manage_worker(self, action: str, worker_id: str, text: str = "") -> dict[str, object]:
        return self.host.controller.manage_worker(action, worker_id, text)

    async def navigate_tree(
        self, identity: str | None = None, edit: bool = False
    ) -> dict[str, object]:
        runtime = getattr(self.host.controller, "runtime", None)
        if runtime is None or not hasattr(runtime, "navigate"):
            return {"ok": False, "message": "Session is still starting."}
        try:
            draft = await self.host.controller.navigate_tree(identity, edit=bool(edit))
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        self.host.push_state()
        return {"ok": True, "draft": draft}

    def cancel_asides(self) -> None:
        self.host.controller.cancel_asides()

    def watch_job(self, job_id) -> None:
        self.host.controller.watch_job(job_id)

    # Host calls

    def attach(self) -> dict:
        """Everything needed to show the session now; later changes follow as view calls.

        Built and registered with no await in between, so every change arrives
        exactly once: in this welcome or after it.
        """
        return self.host.attach(self)

    async def peek(self) -> dict:
        """Read-only live snapshot without attaching, clearing `unseen`, or reading scrollback."""
        return await self.host.peek()

    async def ask_aside(self, question: str, model: str = "") -> dict:
        """One-shot headless `/btw` against this host's context; records nothing."""
        return await self.host.ask_aside(question, model)

    async def query(self, name: str, *args):
        return await self.host.controller.query(name, *args)

    def asides_read(self, identities) -> None:
        """The terminal showed these answers; no terminal needs telling they are ready."""
        for aside in self.host.controller.asides.items:
            if aside.id in identities and not aside.read:
                aside.read = True
                self.host.emit_aside(aside)

    def stop(self, keep_worktree: bool = False) -> None:
        # A remote profile keeps its worktree however the host is stopped.
        self.host.keep_worktree = bool(keep_worktree) or remote_profile.active() is not None
        self.host.stop()

    def stop_if_idle(self, require_detached: bool = False, keep_worktree: bool = False) -> str:
        """`pcode --kill-hosts idle`: "" once stopping, else what it is busy with."""
        return self.host.stop_if_idle(
            require_detached=require_detached, keep_worktree=keep_worktree
        )

    def release(self, cancel: bool = False) -> bool:
        """Pcode.app hibernated this session's pane (see `SessionHost.release`).

        `cancel` takes it back: the pane was shown again before it let go.
        """
        if cancel:
            self.host.cancel_release()
        else:
            self.host.release()
        return True

    def restart(self, interrupt: bool = False) -> dict:
        """`/restart all`: `{"id": successor}` once stopping, else `{"skipped": why not}`.

        `interrupt` restarts a busy host too, continuing its turn (see `SessionHost.restart`).
        """
        return self.host.restart(interrupt=bool(interrupt))

    def continue_interrupted(self) -> None:
        """A terminal moved its session here mid-turn (`/detach`): continue that turn.

        Sent only once the terminal has connected, ahead of what was typed
        during the move, so a host that failed to come up never runs it.
        """
        self.host.touch(self)
        self.host.continue_when_ready()


class HostView:
    """The controller's view in a host: calls go to the attached terminals.

    Plain calls go to every terminal and into the catch-up buffer. Popups and
    commands handed back go to the terminal whose command is running (a
    command's tag carries its sender), or else the one that sent the last
    thing; with no terminal attached they are answered with nothing.
    """

    def __init__(self, host: "SessionHost") -> None:
        self._host = host
        # The running command's tag as its terminal gave it (see `read_asides`).
        self._command_tag = None
        # Side-answer viewers open in a terminal (see `read_asides`), and
        # what they asked for while it is kept.
        self.viewers: set[asyncio.Task] = set()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self._host.emit(name, args, kwargs)

        return call

    # Turns: the buffer restarts where a turn begins.

    def turn_started(self, text: str, *, echo: bool) -> None:
        self._host.turn_began(text)
        self._host.emit("turn_started", (text,), {"echo": echo})

    async def after_turn(self) -> None:
        self._host.turn_finished()
        self._host.emit("after_turn", (), {})

    async def after_command(self, tag=None) -> None:
        # Terminals get it untagged, as ever; a `run` caller is waiting for its own.
        self._host.emit("after_command", (), {})
        run = self._host.run_for(tag)
        if run is not None and not run.done.done():
            run.done.set_result(None)
        if not self._host.controller.running:
            # Only the terminal that sent it: `sender` would guess for an untagged one.
            client = self._host.sender(tag)[0] if isinstance(tag, (list, tuple)) else None
            if client is not None:
                # Ahead of `host_closed`, which can overtake the reply to `run`:
                # ending the session was what this caller asked for, so its
                # terminal exits as an in-process one would, not as after a crash.
                client.peer.notify("session_ended")
            self._host.stop()

    def session_changed(self) -> None:
        self._host.push_state()
        for peer in list(self._host.transcript_observers):
            peer.notify("session_changed", self._host.entry.session_id)
        # A resume or a new runtime brings its own plan store.
        self._host.refresh_plan()

    def turn_event(self, event) -> None:
        self._host.emit("turn_event", (event,), {})
        if isinstance(event, PlanUpdated):
            # The turn's authoritative snapshot, read from the store after
            # every settled tool (`stream_events`), so planning calls need no hook.
            self._host.push_plan(event.items)

    def commands_changed(self) -> None:
        self._host.push_state()

    def preview_reply(self, text: str) -> None:
        self._host.emit("preview_reply", (text,), {})
        reply = getattr(self._host.controller.runtime, "reply", None)
        if reply is None:
            return
        events = reply(text)
        for peer in list(self._host.transcript_observers):
            peer.notify("user", text)
            for event in events:
                peer.notify("turn_event", event)

    def show_events(self, events) -> None:
        # Pcode.app's transcript takes events one at a time (merged side threads
        # replay as `user` then this, and need their answers beside the prompt).
        events = tuple(events)
        self._host.emit("show_events", (events,), {})
        for peer in list(self._host.transcript_observers):
            for event in events:
                peer.notify("turn_event", event)

    def redraw(self) -> None:
        pass  # Terminals repaint on what they receive.

    def aside_changed(self, aside) -> None:
        self._host.emit_aside(aside)

    def aside_answered(self, aside) -> None:
        self._host.emit_aside(aside)
        client = self._host.latest_client()
        if client is not None:
            client.peer.notify("aside_answered", self._host.aside_state(aside))

    # Requests to one terminal

    async def run_command(self, text: str, *, idle: bool, tag) -> None:
        self._host.run_began(tag)
        client, own_tag = self._host.sender(tag)
        if client is None:
            return
        try:
            await client.peer.request("run_command", text, idle=idle, tag=own_tag)
        except ConnectionError:
            pass

    def command_started(self, tag) -> None:
        self._host.run_began(tag)
        client, own_tag = self._host.sender(tag)
        self._host.running_command = client
        self._command_tag = own_tag
        if client is not None:
            client.peer.notify("command_started", own_tag)

    def command_finished(self) -> None:
        client, self._host.running_command = self._host.running_command, None
        self._command_tag = None
        if client is not None and client.peer is not None:
            client.peer.notify("command_finished")

    async def _ask(self, method: str, *args):
        client = self._host.running_command or self._host.latest_client()
        if client is None:
            self._host.emit("note", (f"{method} needs an attached terminal.",), {})
            return None
        try:
            return await client.peer.request(method, *args)
        except ConnectionError:
            return None

    async def choose_model(self, values, providers, current):
        return await self._ask("choose_model", list(values), sorted(providers), current)

    async def read_asides(self):
        """Open the side-answer viewer in the asking terminal, without waiting for it.

        The viewer stays open as long as its reader likes, and the host runs
        one command at a time: awaited here, one terminal's open viewer would
        stall every other terminal's commands behind it. So the command ends
        at once, and a thread the viewer asks to bring into the conversation
        is kept from outside the queue, as a thread in the drawer is (`keep_thread`).
        """
        client = self._host.running_command
        if client is None or client.headless:
            # A headless caller only says it cannot show the viewer, at once
            # and while its command still runs, so `run` reports it failed.
            return await self._ask("read_asides")
        task = asyncio.create_task(self._read_asides(client, self._command_tag))
        self.viewers.add(task)
        task.add_done_callback(self.viewers.discard)
        return None

    async def _read_asides(self, client: "_Client", tag) -> None:
        # The tag goes along: the command has finished by the time the viewer
        # opens, and the terminal needs it to tell an auto-open (see `PreviewApp.read_asides`).
        try:
            request = await client.peer.request("read_asides", tag)
        except ConnectionError:
            return
        if request is None:
            return
        controller = self._host.controller
        try:
            await controller.keep_thread(request.thread, request.action, request.instructions)
        except Exception as error:
            controller.command_failed("/btw", error)

    async def browse_jobs(self) -> None:
        await self._ask("browse_jobs")

    async def read_secret(self, title: str, prompt: str, detail: str = "") -> str | None:
        return await self._ask("read_secret", title, prompt, detail)


class SessionHost:
    """Serves one controller to the terminals attached to it."""

    def __init__(self, entry: HostEntry, directory: Path | None = None) -> None:
        self.entry = entry
        self.directory = directory or host_dir()
        self.clients: dict[int, _Client] = {}
        self._numbers = itertools.count(1)
        self._latest: _Client | None = None
        self.running_command: _Client | None = None
        # Commands sent with `run`, by tag, until they have run.
        self.runs: dict[tuple, _Run] = {}
        self.run_ids = itertools.count(1)
        self.view = HostView(self)
        self.activity = MirroredActivity(lambda changes: self.emit("state", (changes,), {}))
        # Started with no model, it previews until /model finishes startup. With
        # one, no preview stands in: a model that fails to start fails loudly.
        runtime = None if entry.model else PreviewRuntime()
        self.controller = SessionController(self.view, self.activity, runtime)
        self.controller.starts_on_model = True
        self.controller.interactive = True
        # A loop task cancelled while the host shuts down is not a Ctrl+C.
        self.controller.closing = lambda: not self.controller.running
        # Everything shown since the journal offset `settled_end`: a terminal
        # attaching reads the journal up to there and replays these after it.
        self.buffer: list[tuple[str, tuple, dict]] = []
        self.settled_end: int | None = None
        self._state: dict = {}
        self._observer_metadata: dict[str, str] = {}
        self.stopped = asyncio.Event()
        # A restart resumes this session in its worktree, so stopping must not tidy it away.
        self.keep_worktree = False
        self.server: asyncio.AbstractServer | None = None
        self.tasks: list[asyncio.Task] = []
        # When the host last had a terminal or work; the idle clock starts here.
        self.active_at = time.monotonic()
        # Stopped for being idle, not asked to: an unseen result is remembered.
        self.idle_stopped = False
        # The host `restart` asked to carry the session on once this one has gone.
        self.successor: str | None = None
        # What that successor needs to pick up a turn the restart stopped
        # (`hand_over`), and what the host this one replaced left it (`take_over`).
        self.carry: dict | None = None
        self.carried: dict | None = None
        self._handing_over: asyncio.Task | None = None
        # Open connections, counted from hello on: a terminal mid-handshake is not
        # a client yet, and must not find the host gone.
        self.connections = 0
        # In-flight headless `ask_aside` RPCs, counted in `busy_with()`.
        self._host_asides = 0
        # A remote profile's per-turn wall clock, while a turn runs.
        self._turn_clock: asyncio.TimerHandle | None = None
        # Drawers watching the side questions (`pcode.drawer`): not terminals,
        # not connections, and allowed to call nothing.
        self.observers: set[Peer] = set()
        # Read-only transcript observers (e.g. Pcode.app live transcript pane):
        # receive settled history + catch-up calls + live data-only view calls
        # without affecting `attached`, `unseen`, `_latest`, or the idle clock.
        self.transcript_observers: set[Peer] = set()
        # Journal size behind the history observers got while the host booted.
        self.boot_history_end: int | None = None
        # Side questions changed since observers last heard, sent together
        # shortly after, so a streaming answer is not rendered per token.
        self._observed: dict[str, object] = {}
        self._observe_flush: asyncio.TimerHandle | None = None
        # Waiting to stop for Pcode.app's hibernated pane (`release`), and the
        # `keep_worktree` it overrode, restored if a terminal attaches first.
        self._release: asyncio.Task | None = None
        self._keep_worktree_before_release = False
        # The conversation's plan as last sent (`plan_changed`), and the read
        # of the store that will update it, if one is pending.
        self.plan: list[dict] = []
        self._plan_read: asyncio.Task | None = None
        # Something replaced the plan store while a read was in flight.
        self._plan_stale = False

    @property
    def socket(self) -> Path:
        return socket_path(self.entry.id, self.directory)

    @property
    def busy(self) -> bool:
        return bool(self.activity.busy or self.controller.working())

    # Serving

    async def serve(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.socket
        path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._accept, path=path, limit=LINE_LIMIT)
        os.chmod(path, 0o600)
        # Not activity: a resumed session keeps the age `main` gave it from its last turn.
        self.update(state="idle", touch=False)
        if self.entry.session_id:
            # Running again, so /attach lists the host instead of the stopped one.
            forget_stopped(self.entry.session_id, self.directory)

    async def boot(self) -> None:
        """Build the runtime, say what a new session says, then start the loops.

        Terminals may attach while this runs: they see the startup notes as
        they come, and anything they send waits for the loops.
        """
        controller = self.controller
        try:
            if (preset := controller.startup_error) is not None:
                raise preset  # Found before boot (`_serve`); /model recovers from it.
            await controller.initialize_runtime()
        except Exception as error:  # noqa: BLE001 - reported to the terminals.
            controller.startup_error = error
            self.view.error(error_message(error), title="Agent startup failed")
            print(f"startup failed: {error_message(error)}", file=sys.stderr, flush=True)
        finally:
            controller.startup_pending = False
        # A resumed conversation is in the journal already; what follows is not.
        self.reset_buffer()
        if controller.startup_error is None:
            if controller.resuming:
                # Terminals attached while it loaded draw it now (the state
                # first, so they know which journal to read).
                self.push_state()
                self.view.replay_conversation()
            controller.show_startup_context()
            controller.warn_without_credentials()
            # Optional, and it can hang; the session must not wait for it.
            self.tasks.append(asyncio.create_task(self._refresh_context()))
            self.tasks.append(asyncio.create_task(controller.warn_outdated()))
            profile = remote_profile.active()
            if controller.model and (profile is None or profile.mcp):
                controller.start_mcp_defaults()
        if self.carried is not None:
            # Before the loops start: the continued turn goes ahead of anything
            # a terminal sent while this host booted.
            self.take_over(self.carried)
            self.carried = None
        self.push_state()
        self.start()

    async def _refresh_context(self) -> None:
        refresh = getattr(self.controller.runtime, "refresh_context", None)
        if refresh is None:
            return
        try:
            await refresh()
        except Exception as error:  # noqa: BLE001 - metadata only.
            print(f"context metadata unavailable: {error}", file=sys.stderr, flush=True)
        self.push_state()

    def start(self) -> None:
        """Run the controller's loops: turns, commands, and background jobs."""
        controller = self.controller
        for work in (controller.consume(), controller.consume_commands(), controller.watch_jobs()):
            self.tasks.append(asyncio.create_task(work))
        controller.ready.set()
        # A resumed conversation restored its plan before anything was sent.
        self.refresh_plan()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            await self._handshake(reader, writer)
        finally:
            self.connections -= 1

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            hello = await read_message(reader)
        except ValueError:
            hello = None
        if hello is not None and hello.get("type") == "observe":
            await self._observe(hello, reader, writer)
            return
        if hello is not None and hello.get("type") == "observe_transcript":
            await self._observe_transcript(hello, reader, writer)
            return
        if hello is None or hello.get("type") != "hello":
            writer.close()
            return
        if hello.get("protocol") != PROTOCOL:
            writer.write(
                dumps(
                    {
                        "type": "error",
                        "message": f"This session host speaks protocol {PROTOCOL}, the terminal "
                        f"{hello.get('protocol')}. Restart one of them on the same pcode.",
                    }
                )
            )
            await writer.drain()
            writer.close()
            return
        writer.write(dumps({"type": "ready", "protocol": PROTOCOL}))
        client = _Client(self, next(self._numbers))
        client.peer = Peer(reader, writer, client, allowed=INTENTS | HOST_CALLS)
        try:
            await client.peer.serve()
        except ValueError, ConnectionError:
            pass
        finally:
            client.peer.close()
            if self.clients.pop(client.number, None) is not None:
                self.active_at = time.monotonic()
                if self._latest is client:
                    self._latest = next(reversed(self.clients.values()), None)
                if self.running_command is client:
                    self.running_command = None
                if not self.stopped.is_set():
                    self.update(attached=len(self.terminals()), touch=False)

    async def _observe(
        self, hello: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """A drawer showing the side questions (`pcode.drawer`), until it goes.

        It gets every side question, then each change. It may call nothing,
        and it is not a terminal: it neither keeps an idle host running nor
        marks a finished turn seen.
        """
        from pcode import drawer

        if hello.get("version") != drawer.OBSERVER_VERSION:
            message = "This session host's pcode does not speak this drawer's version."
            writer.write(dumps({"type": "error", "message": message}))
            await writer.drain()
            writer.close()
            return
        writer.write(dumps({"type": "ready", "version": drawer.OBSERVER_VERSION}))
        peer = Peer(reader, writer, object(), allowed=frozenset())
        counted = False
        try:
            self.observers.add(peer)
            asides = self.controller.asides.items
            peer.notify("asides", [self.aside_state(aside) for aside in asides])
            # Counted from hello on, as every connection; no longer once it is a drawer.
            self.connections -= 1
            counted = True
            await peer.serve()
        except ValueError, ConnectionError:
            pass
        finally:
            if counted:
                self.connections += 1
            self.observers.discard(peer)
            peer.close()

    def _read_settled_history(self) -> list[dict]:
        """The settled journal (up to `settled_end`) as a transcript observer draws it."""
        session = getattr(self.controller.runtime, "session", None)
        if session is not None:
            if not self.settled_end:
                return []
            return observer_history(session, self.settled_end)
        # Still booting: the conversation being resumed is on disk already. Without
        # this, an observer that connects first is told the session is empty.
        saved = getattr(self.controller, "_saved_session", None)
        if saved is None:
            return []
        with contextlib.suppress(OSError):
            self.boot_history_end = saved.journal_size()
        return observer_history(saved)

    def _replay_to_observers(self, method: str) -> None:
        """Send transcript observers the conversation a replay just redrew, as `history`."""
        session = getattr(self.controller.runtime, "session", None)
        boot_end, self.boot_history_end = self.boot_history_end, None
        if session is None or not self.transcript_observers:
            return  # Nothing saved to read: keep what they show.
        if method == "replay_conversation" and boot_end is not None:
            with contextlib.suppress(OSError):
                if session.journal_size() == boot_end:
                    # Boot loaded what observers were shown while it ran; don't
                    # make them rebuild a long transcript a second time.
                    return
        broadcast(list(self.transcript_observers), "history", self._read_settled_history())

    async def _observe_transcript(
        self, hello: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Read-only live transcript observer (settled history + catch-up + live data calls).

        Like `_observe`, does not count as an attached terminal, clear `unseen`, or
        reset the idle clock. Builds the snapshot and registers before any `await` so
        the merge of settled history, catch-up buffer, and live notifications is
        exactly-once by construction.
        """
        from pcode.preferences import load_preferences
        from pcode.rpc import encode

        if hello.get("version") != TRANSCRIPT_OBSERVER_VERSION:
            message = (
                f"This session host speaks transcript observer version "
                f"{TRANSCRIPT_OBSERVER_VERSION}, not {hello.get('version')}."
            )
            writer.write(dumps({"type": "error", "message": message}))
            await writer.drain()
            writer.close()
            return

        history = self._read_settled_history()
        calls = [
            [name, encode(list(args)), encode(kwargs)]
            for name, args, kwargs in self.buffer
            if name in TRANSCRIPT_OBSERVER_CALLS
        ]
        paced = load_preferences().get("paced_scrollback", "rows")
        welcome = {
            "type": "ready",
            "version": TRANSCRIPT_OBSERVER_VERSION,
            "id": self.entry.id,
            "session_id": self.entry.session_id,
            "title": self.entry.title,
            "workspace": self.entry.workspace,
            "model": self.controller.model or self.entry.model,
            "effort": self.controller.current_effort(),
            "context": self.controller.context_label().removeprefix(" · "),
            "state": self.entry.state,
            "paced_scrollback": paced,
            "show_thinking": self.activity.thinking_mode,
            "queued_prompts": list(self.activity.queued_prompts),
            "queued_modes": list(self.activity.queued_modes),
            "plan": self.plan,
            "history": history,
            "calls": calls,
        }
        writer.write(dumps(welcome))
        peer = Peer(reader, writer, object(), allowed=frozenset())
        counted = False
        try:
            self.transcript_observers.add(peer)
            self.connections -= 1
            counted = True
            await peer.serve()
        except ValueError, ConnectionError:
            pass
        finally:
            if counted:
                self.connections += 1
            self.transcript_observers.discard(peer)
            peer.close()

    def _notify_observers(self) -> None:
        self._observe_flush = None
        changed, self._observed = list(self._observed.values()), {}
        for aside in changed:
            state = self.aside_state(aside)
            for peer in list(self.observers):
                peer.notify("aside_changed", state)

    def attach(self, client: _Client) -> dict:
        from pcode.rpc import encode

        self.active_at = time.monotonic()
        self.clients[client.number] = client
        client.displaced, self._latest = self._latest, client
        client.unseen_before = self.entry.unseen
        # Shown now, so whatever finished while nobody watched has been seen.
        self.update(attached=len(self.terminals()), unseen=False, touch=False)
        self._state = self.controller.session_state()
        return {
            "id": self.entry.id,
            "pid": os.getpid(),
            "session": self._state,
            "activity": self.activity.session_fields(),
            "asides": [self.aside_state(aside) for aside in self.controller.asides.items],
            "settled_end": self.settled_end,
            "forked_from": getattr(
                getattr(self.controller.runtime, "session", None), "forked_from", None
            )
            or "",
            "fork_requested": getattr(
                getattr(self.controller.runtime, "session", None), "fork_requested", False
            ),
            "plan": self.plan,
            # Already encoded: a buffered call's arguments are whatever the view got.
            "calls": [
                [name, encode(list(args)), encode(kwargs)] for name, args, kwargs in self.buffer
            ],
        }

    async def peek(self) -> dict:
        """Read-only live snapshot: state, plan, running jobs, asides, and activity.

        Does not register as a terminal, change `attached`, clear `unseen`, or
        serialize the scrollback catch-up buffer.
        """
        controller = self.controller
        runtime = controller.runtime
        plan_store = getattr(runtime, "plan_store", None)
        plan = (
            [item.model_dump(mode="json") for item in await plan_store.get_items()]
            if plan_store is not None
            else []
        )
        jobs_reg = getattr(runtime, "jobs", None)
        jobs = (
            [
                {
                    "id": job.id,
                    "command": job.command,
                    "purpose": job.purpose,
                    "adopted": job.adopted,
                }
                for job in jobs_reg.running()
            ]
            if jobs_reg is not None
            else []
        )
        asides = controller.asides
        return {
            "id": self.entry.id,
            "session_id": self.entry.session_id,
            "workspace": self.entry.workspace,
            "model": controller.model or self.entry.model,
            "state": self.entry.state,
            "title": self.entry.title,
            "last_prompt": self.entry.last_prompt,
            "turns": self.entry.turns,
            "outcome": self.entry.outcome,
            "attached": len(self.terminals()),
            "unseen": self.entry.unseen,
            "held": self.entry.held,
            "busy_with": self.busy_with(),
            "startup_pending": controller.startup_pending,
            "startup_error": (
                error_message(controller.startup_error) if controller.startup_error else ""
            ),
            "activity": {
                "busy": self.activity.busy,
                "status": self.activity.status,
                "queued": self.activity.queued,
                "queued_prompts": list(self.activity.queued_prompts),
                "prompt": self.activity.prompt,
                "prompt_state": self.activity.prompt_state,
                "turn_outcome": self.activity.turn_outcome,
                "job_count": self.activity.job_count,
            },
            "plan": plan,
            "jobs": jobs,
            "asides": {
                "total": len(asides.items),
                "running": asides.running + self._host_asides,
                "unread": asides.unread,
                "recent": [
                    {
                        "id": aside.id,
                        "question": aside.question,
                        "status": aside.status,
                        "read": aside.read,
                    }
                    for aside in asides.items[-5:]
                ],
            },
        }

    async def ask_aside(self, question: str, model: str = "") -> dict:
        """Answer `question` beside this conversation without recording an aside."""
        from pcode.aside import ASIDE_TIMEOUT_SECONDS, SideTarget, split_effort

        cleaned = question.strip()
        if not cleaned:
            raise ValueError("Provide a non-empty question.")
        controller = self.controller
        self._host_asides += 1
        try:
            async with asyncio.timeout(ASIDE_TIMEOUT_SECONDS):
                if controller.startup_pending:
                    await controller.ready.wait()
                if controller.startup_error is not None:
                    raise RuntimeError(
                        f"Agent startup failed: {error_message(controller.startup_error)}"
                    )
                runtime = controller.runtime
                if getattr(runtime, "aside", None) is None:
                    raise RuntimeError("This session host cannot answer side questions.")
                self.active_at = time.monotonic()
                chosen_model = model.strip()
                options = {}
                if chosen_model:
                    name, effort = split_effort(chosen_model)
                    target = controller.qualified(SideTarget(name, effort))
                    controller.check_effort(target)
                    options = await controller.target_options(target)
                reply = await runtime.aside(cleaned, **options)
        finally:
            self._host_asides -= 1
        self.active_at = time.monotonic()
        return {
            "host_id": self.entry.id,
            "session_id": self.entry.session_id,
            "state": self.entry.state,
            "model": chosen_model or controller.model or self.entry.model,
            "answer": reply.answer,
        }

    def touch(self, client: _Client) -> None:
        self._latest = client
        self.active_at = time.monotonic()

    def terminals(self) -> list[_Client]:
        """The attached clients with an editor, in attach order: not headless callers."""
        return [client for client in self.clients.values() if not client.headless]

    def latest_client(self) -> _Client | None:
        """The terminal last used, for what goes to one terminal; never a headless caller."""
        latest = self._latest
        if latest is not None and self.clients.get(latest.number) is latest and not latest.headless:
            return latest
        # Attach order: the newest terminal that has an editor.
        return next(reversed(self.terminals()), None)

    def sender(self, tag) -> tuple[_Client | None, object]:
        """The terminal a command came from, and the tag it gave it."""
        if isinstance(tag, (list, tuple)) and len(tag) == 2:
            return self.clients.get(tag[0]), tag[1]
        return self.latest_client(), None

    def run_for(self, tag) -> _Run | None:
        # Only `run` tags are strings: a terminal's own tag may not even be hashable.
        if isinstance(tag, tuple) and len(tag) == 2 and isinstance(tag[1], str):
            return self.runs.get(tag)
        return None

    def run_began(self, tag) -> None:
        """A `run` command is being handled: note the background work already going."""
        if (run := self.run_for(tag)) is not None:
            run.work = (self.controller.compact_task, self.controller.mcp_task)

    # Showing

    def emit(self, method: str, args: tuple, kwargs: dict) -> None:
        """Send a view call to every terminal and keep it for the ones that attach later."""
        if method in RESETS:
            self.reset_buffer()
        if method == "conversation_reset":
            self._observer_metadata = {}
            self.update(session_id="", forked_from="", title="", last_prompt="", outcome="")
            self.own_jobs()
        for client in list(self.clients.values()):
            client.peer.notify(method, *args, **kwargs)
        if method in REPLAYS:
            # After `reset_buffer`, so this is everything shown until now.
            self._replay_to_observers(method)
        elif method in TRANSCRIPT_OBSERVER_CALLS:
            for peer in list(self.transcript_observers):
                peer.notify(method, *args, **kwargs)
        if method == "conversation_reset":
            self.push_plan([])  # The runtime's reset dropped the plan with the rest.
        elif method in REPLAYS:
            self.refresh_plan()  # A branch or resumed conversation has its own plan.
        if method in RESETS or method in UNBUFFERED:
            return
        self._buffer(method, args, kwargs)

    def push_plan(self, items: list[dict]) -> None:
        """Send the plan to terminals and transcript observers, if it changed."""
        if items == self.plan:
            return
        self.plan = list(items)
        self.emit("plan_changed", (self.plan,), {})

    def refresh_plan(self) -> None:
        """Re-read the plan store soon, after something that may have replaced it."""
        if self.stopped.is_set():
            return
        if self._plan_read is not None and not self._plan_read.done():
            self._plan_stale = True  # The pending read goes again once it finishes.
            return
        self._plan_read = asyncio.get_running_loop().create_task(self._read_plan())

    async def _read_plan(self) -> None:
        while True:
            self._plan_stale = False
            store = getattr(self.controller.runtime, "plan_store", None)
            if store is not None:
                items = await store.get_items()
                self.push_plan([item.model_dump(mode="json") for item in items])
            if not self._plan_stale or self.stopped.is_set():
                return

    def _buffer(self, method: str, args: tuple, kwargs: dict) -> None:
        previous = self.buffer[-1] if self.buffer else None
        if method == "turn_event" and previous is not None and previous[0] == "turn_event":
            event, before = args[0], previous[1][0]
            if type(event) is type(before) and isinstance(event, DELTA_EVENTS):
                merged = type(event)(before.text + event.text)
                self.buffer[-1] = ("turn_event", (merged,), previous[2])
                return
            if (
                type(event) is type(before)
                and isinstance(event, SNAPSHOT_EVENTS)
                and event.call_id == before.call_id
            ):
                self.buffer[-1] = (method, args, kwargs)
                return
        self.buffer.append((method, args, kwargs))

    def reset_buffer(self) -> None:
        """The journal now holds everything shown so far; start the buffer again."""
        session = getattr(self.controller.runtime, "session", None)
        self.settled_end = session.journal_size() if session is not None else None
        self.buffer = []

    def aside_state(self, aside) -> dict:
        """A side question as a terminal needs it: everything but the reply it continues from."""
        return {
            "id": aside.id,
            "question": aside.question,
            "model": aside.model,
            "label": aside.label,
            "effort": aside.effort,
            "status": aside.status,
            "answer": aside.answer,
            "activity": aside.activity,
            "error": aside.error,
            # Monotonic clock readings, which are system-wide: comparable in the terminal.
            "started": aside.started,
            "finished": aside.finished,
            "thread": aside.thread,
            "conversation": aside.conversation,
            "base": aside.base,
            "bridged": aside.bridged,
            # Whether it can be followed up: a reply held here, or a saved node.
            "replied": aside.continuable,
            "read": aside.read,
        }

    def emit_aside(self, aside) -> None:
        for client in list(self.clients.values()):
            client.peer.notify("aside_changed", self.aside_state(aside))
        if self.observers:
            self._observed[aside.id] = aside
            if self._observe_flush is None:
                self._observe_flush = asyncio.get_running_loop().call_later(
                    OBSERVER_INTERVAL, self._notify_observers
                )

    def push_state(self) -> None:
        """Send the session state if it changed since the last one sent."""
        state = self.controller.session_state()
        if state != self._state:
            self._state = state
            for client in list(self.clients.values()):
                client.peer.notify("session_state", state)
        metadata = {
            "model": state.get("model") or "",
            "effort": state.get("effort") or "",
            "context": (state.get("context") or "").removeprefix(" · "),
        }
        if metadata != self._observer_metadata:
            self._observer_metadata = metadata
            for peer in list(self.transcript_observers):
                peer.notify("session_metadata", metadata)
        session = getattr(self.controller.runtime, "session", None)
        if session is not None and session.info.id != self.entry.session_id:
            # A /resume ages from the resumed session's last turn, not from now.
            self.update(
                session_id=session.info.id,
                forked_from=session.info.forked_from or "",
                updated=_activity_time(session.info.updated),
                touch=False,
            )
            forget_stopped(session.info.id, self.directory)
            self.own_jobs()
        # /attach lists the session by its name once it has one, not its first prompt.
        if (title := state.get("session_title")) and title != self.entry.title:
            self.update(title=title, touch=False)
        if (model := state.get("model") or "") != self.entry.model:
            self.update(model=model, touch=False)

    def own_jobs(self) -> None:
        """Tag the jobs this host launches with its session, and take back that session's.

        Jobs a /detach or an earlier host of this session left running are this
        conversation's, not adopted strangers: they keep the host up and wake it.
        """
        jobs = getattr(self.controller.runtime, "jobs", None)
        (job_registry() if jobs is None else jobs).session = self.entry.session_id

    # The entry other terminals read

    def update(self, *, touch: bool = True, **changes) -> None:
        for key, value in changes.items():
            setattr(self.entry, key, value)
        write_entry(self.entry, self.directory, touch=touch)

    def turn_began(self, prompt: str) -> None:
        self.reset_buffer()
        changes: dict[str, str] = {"state": "working"}
        if prompt:
            changes["last_prompt"] = prompt
            changes["title"] = self.entry.title or prompt
        self.update(**changes)
        profile = remote_profile.active()
        if profile is not None:
            remote_profile.BUDGET.start()
            if profile.turn_minutes:
                self._turn_clock = asyncio.get_running_loop().call_later(
                    profile.turn_minutes * 60, self.turn_expired
                )

    def turn_expired(self) -> None:
        """The remote profile's wall clock ran out: stop the turn and say why."""
        self._turn_clock = None
        controller = self.controller
        task = controller.live_task
        if task is None or task.done():
            return
        self.view.warning(remote_profile.BUDGET.expire())
        # As Ctrl+C does: commands the turn is waiting on stop with it.
        controller.set_cancel_policy("stop")
        task.cancel()

    def turn_finished(self) -> None:
        if self._turn_clock is not None:
            self._turn_clock.cancel()
            self._turn_clock = None
        outcome = {"done": "done", "failed": "failed", "cancelled": "cancelled"}.get(
            self.activity.prompt_state, "done"
        )
        self.active_at = time.monotonic()
        self.push_state()
        self.update(
            state="idle",
            outcome=outcome,
            turns=self.entry.turns + 1,
            # A headless `--attach -p` caller printed the reply, so it was seen.
            unseen=not self.clients,
        )

    # Stopping

    def idle(self) -> bool:
        """Nothing would be lost by stopping: no terminal, no work, no running job."""
        return not self.clients and not self.connections and not self.busy_with()

    def busy_with(self) -> str:
        """What stopping now would interrupt, terminals aside; "" for nothing."""
        controller = self.controller
        jobs = getattr(controller.runtime, "jobs", None)
        work = (
            (self.busy, "a turn or command is running"),
            (not controller.command_idle.is_set(), "a command is running"),
            # A job outlives its host, but its finish would wake nobody. Not one
            # adopted from an exited pcode: it never wakes this session, and
            # stopping leaves it for the next pcode to adopt. Counting it let a
            # daemon some other session started keep a host up for days.
            (
                jobs is not None and any(not job.adopted for job in jobs.running()),
                "a background command is running",
            ),
            (controller.startup_pending, "it is starting up"),
            (
                getattr(controller.runtime, "has_provider_wake", lambda: False)(),
                "Antigravity is continuing background work",
            ),
            (controller.asides.running or self._host_asides, "a side question is running"),
            # A thread brought into the conversation from it would be lost.
            (self.view.viewers, "a side-answer viewer is open"),
            # A title asked for beside the first turn, or a name /rename asked for, would be lost.
            (controller.naming(), "it is naming the session"),
        )
        return next((reason for busy, reason in work if busy), "")

    def stop_if_idle(self, require_detached: bool = False, keep_worktree: bool = False) -> str:
        """Stop as an idle host stops itself; else say what it is busy with.

        When `require_detached` is True, attached terminals also refuse the stop.
        When `keep_worktree` is True, the session's worktree is left untouched.
        Checked and stopped with no await in between, so nothing starts in the gap.
        """
        if require_detached and (attached := len(self.terminals())):
            return f"{attached} terminal(s) attached"
        if reason := self.busy_with():
            return reason
        if keep_worktree:
            self.keep_worktree = True
        self.idle_stopped = True
        self.stop()
        return ""

    def restart(self, interrupt: bool = False) -> dict:
        """Stop, leaving a successor to resume this session on the pcode installed now.

        `main` starts the successor once this host has let go of the session,
        from this process, so it keeps this host's environment and remote
        profile rather than those of whoever asked. Attached terminals do not
        stop it; work in progress does. Checked and stopped with no await in
        between, so nothing starts in the gap.

        With `interrupt`, work in progress does not stop it either, short of
        a host still starting up: a running turn stops at its checkpoint and
        the successor continues it, ahead of the messages queued behind it,
        which carry over (`hand_over`). Compaction finishes first; side
        questions stop. Background commands outlive any host, and the
        successor takes them back as this session's.
        """
        reason = self.busy_with()
        if self.successor is not None:
            return {"id": self.successor}  # Asked twice: already on its way.
        if reason and (not interrupt or self.controller.startup_pending):
            return {"skipped": reason}
        if not self.entry.session_id:
            return {"skipped": NOTHING_TO_RESUME}
        self.successor = new_host_id()
        # The successor resumes in this worktree.
        self.keep_worktree = True
        if reason:
            self._handing_over = asyncio.create_task(self.hand_over())
        else:
            self.stop()
        return {"id": self.successor}

    async def hand_over(self) -> None:
        """Stop mid-turn for a restart, leaving the successor what it needs to carry on."""
        controller = self.controller
        print(f"restarting mid-work as {self.successor}", file=sys.stderr, flush=True)
        try:
            interrupted = await controller.settle_for_handoff()
            typed = controller.take_typed()
            # A command is its sender's, and that terminal goes with this host:
            # the successor names them rather than running them for nobody.
            commands = [text for text, mode in typed if mode == COMMAND_MODE]
            messages = [(text, mode) for text, mode in typed if mode != COMMAND_MODE]
            self.carry = {"continue": interrupted, "messages": messages, "commands": commands}
        finally:
            self.stop()

    def continue_when_ready(self) -> None:
        """`/continue` the interrupted turn now, or as `boot` finishes if it is still starting."""
        if self.controller.startup_pending:
            self.carried = {**(self.carried or {}), "continue": True}
        else:
            self.take_over({"continue": True})

    def take_over(self, carry: dict) -> None:
        """Carry on from the host this one replaced mid-turn: its turn first, then its queue."""
        controller = self.controller
        messages = [tuple(message) for message in carry.get("messages") or ()]
        if commands := carry.get("commands"):
            controller.view.warning(
                "Not run, as the host restarted before them; send again if still wanted: "
                + ", ".join(commands)
            )
        if controller.startup_error is not None:
            if messages:
                controller.view.warning(
                    "Not sent, as the agent did not start: "
                    + " · ".join(text for text, _mode in messages)
                )
            return
        if carry.get("continue"):
            controller.continue_interrupted()
        for text, mode in messages:
            controller.submit(text, mode)

    async def stop_when_idle(
        self, minutes: float, *, every: float = 2.0, grace: float = IDLE_GRACE_SECONDS
    ) -> None:
        """Stop once idle for `minutes`, or for `grace` seconds when `minutes` is 0.

        Polled rather than told: jobs and side questions end without a hook,
        and reading this state every couple of seconds costs nothing. The
        journal keeps the conversation, and a result nobody saw stays listed
        in /attach (`remember_stopped`).
        """
        limit = minutes * 60 if minutes else grace
        while not self.stopped.is_set():
            await asyncio.sleep(every)
            self.held_by("" if self.clients or self.connections else self.busy_with())
            if not self.idle():
                self.active_at = time.monotonic()
            elif time.monotonic() - self.active_at >= limit:
                print(f"idle for {limit:g} s; stopping", file=sys.stderr, flush=True)
                self.idle_stopped = True
                self.stop()
                return

    def release(self) -> None:
        """Stop once nobody is attached and nothing runs, leaving the session as it is.

        Pcode.app hibernates a pane it is not showing to save memory, and still
        lists the session as open: so unlike an idle stop, this keeps the
        worktree (an untouched one would be removed, a turnless session
        deleted) and waits out the pane's own terminal letting go and any
        work, whatever `session_host_idle_minutes` says. A terminal attaching
        meanwhile (the pane reopened) calls it off.
        """
        if self._release is not None and not self._release.done():
            return
        self._keep_worktree_before_release = self.keep_worktree
        self.keep_worktree = True
        self._release = asyncio.create_task(self._stop_when_released(set(self.clients)))

    def cancel_release(self) -> None:
        """Call off a pending `release`, as a terminal attaching would."""
        if self._release is None or self._release.done():
            return
        self._release.cancel()
        self.keep_worktree = self._keep_worktree_before_release
        print("release called off by Pcode.app", file=sys.stderr, flush=True)

    async def _stop_when_released(self, attached: set[int], every: float = 0.25) -> None:
        # The pane's terminal is on its way out as this arrives; anyone else is new.
        while not self.stopped.is_set():
            if set(self.clients) - attached:
                self.keep_worktree = self._keep_worktree_before_release
                print("release called off: a terminal attached", file=sys.stderr, flush=True)
                return
            if self.idle():
                print("released by Pcode.app; stopping", file=sys.stderr, flush=True)
                self.idle_stopped = True
                self.stop()
                return
            await asyncio.sleep(every)

    def held_by(self, reason: str) -> None:
        """Publish what keeps a host with no terminal from stopping, when it changes.

        A host nobody is attached to is invisible otherwise: /attach shows the
        reason on its row, and the log keeps when it started and ended.
        """
        if reason == self.entry.held:
            return
        note = f"not stopping: {reason}" if reason else "nothing holds it now"
        print(note, file=sys.stderr, flush=True)
        self.entry.held = reason
        # Not activity: /attach ages and orders hosts by `updated`.
        write_entry(self.entry, self.directory, touch=False)

    def stop(self) -> None:
        # At once, not in close(): a prompt an attached terminal sends before
        # then would otherwise start a turn only for close() to cancel it.
        self.controller.running = False
        self.stopped.set()

    async def close(self) -> None:
        controller = self.controller
        controller.running = False
        # Terminals hear that the host is going, not the teardown after it
        # (a "Run cancelled" for a host stopped while idle).
        clients, self.clients = list(self.clients.values()), {}
        for client in clients:
            # Older terminals ignore the argument; newer ones point at the successor.
            client.peer.notify("host_closed", self.successor)
            client.peer.close()
        if self._observe_flush is not None:
            self._observe_flush.cancel()
            self._notify_observers()
        for peer in list(self.observers):
            peer.notify("host_closed")
            peer.close()
        for peer in list(self.transcript_observers):
            peer.notify("host_closed")
            peer.close()
        controller.cancel_all()
        viewers = list(self.view.viewers)
        plan_read = [self._plan_read] if self._plan_read is not None else []
        for task in (*self.tasks, *viewers, *plan_read):
            task.cancel()
        await asyncio.gather(
            *self.tasks, *viewers, *plan_read, *controller.tasks(), return_exceptions=True
        )
        await controller.asides.close()
        # Stopping them settled them: their journal entries go in before the session closes.
        await controller.side_records_settled()
        await controller.stop_naming()
        if self.server is not None:
            self.server.close()
        if controller.extensions is not None:
            await controller.extensions.close()
        remove_entry(self.entry.id, self.directory)
        if self.idle_stopped and self.entry.unseen and self.successor is None:
            # After the entry goes, so /attach never sees both. Only a saved
            # session has an ID, so only one /resume could reopen.
            remember_stopped(self.entry, self.directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one pcode conversation headless.")
    parser.add_argument("--id", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--resume")
    parser.add_argument("--fork", action="store_true")
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--worktree", nargs="?", const=True)
    parser.add_argument("--no-worktree", action="store_true")
    # JSON from `RemoteProfile.to_json`: an unattended host's fixed limits.
    parser.add_argument("--remote-profile")
    # A file `spawn_host(carry=...)` wrote: a turn to continue and messages to
    # queue, from the process this host replaces mid-turn. Read once and removed.
    parser.add_argument("--carry", type=Path)
    return parser


def _activity_time(stamp: str) -> float:
    """A session's ISO `updated` as epoch seconds, or now if it doesn't parse."""
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except TypeError, ValueError:
        return time.time()


def read_carry(path: Path | None) -> dict | None:
    """The hand-over `--carry` names, removing the file; None without one (or unreadable)."""
    if path is None:
        return None
    try:
        carry = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        print(f"could not read the hand-over {path}: {error}", file=sys.stderr, flush=True)
        return None
    finally:
        path.unlink(missing_ok=True)
    return carry if isinstance(carry, dict) else None


def remote_resume_workspace(info, workspace: Path, profile) -> Path:
    """Where a remote session resumes: its own worktree, else a fresh one.

    A local resume whose worktree was merged or cleaned away falls back to the
    project checkout. An unattended one must never work in the shared
    checkout, so it gets a new worktree instead.
    """
    from pcode.app import _enter_worktree

    if Path(info.workspace).is_dir():
        return Path(info.workspace).resolve()
    print(f"workspace: {info.workspace} is gone; continuing in a new worktree", file=sys.stderr)
    created, _ = _enter_worktree(workspace, True, always=True, base=profile.base, owner=info.id)
    return created.resolve()


def fork_worktree(saved, workspace: Path) -> Path:
    """A forked session's own worktree, started from the commit its original is on.

    Named after the copy's ID, as any session worktree is, so `pcode -c PREFIX`
    finds it. Outside git there is nothing to branch, so the copy shares the
    original's directory as `--fork` always has.
    """
    from pcode import worktree
    from pcode.app import _enter_worktree

    base = worktree.head_commit(workspace)
    if base is None:
        return workspace
    created, _ = _enter_worktree(
        workspace, saved.info.id[:8], always=True, base=base, owner=saved.info.id
    )
    created = created.resolve()
    saved.info.workspace = str(created)
    # Its own work starts here: /diffs and tidying on exit measure from it.
    saved.info.start_commit = base
    try:
        saved.save_info(touch=False)
    except BaseException:
        linked = worktree.describe(created)
        if linked is not None:
            worktree.remove(linked, force=True)
            worktree.delete_branch(linked, force=True)
        raise
    return created


def plan_startup(controller: SessionController, model: str | None, saved) -> None:
    """What `SessionHost.boot` builds: the model's runtime, or a preview until /model."""
    controller.model = model
    controller._saved_session = saved
    controller.resuming = saved is not None
    controller._needs_runtime = bool(model)
    controller.startup_pending = bool(model)
    if saved is not None and not model:
        # Nothing to restore it with: /model builds the agent and restores it
        # then, the same recovery as after a failed launch.
        from pcode.sessions import SessionError

        controller.startup_error = SessionError(
            "This session has no model to resume with: pick one with /model."
        )


def move_into(workspace: Path) -> None:
    """Make `workspace` this process's directory, as well as the session's.

    `spawn_host` starts a host in the caller's directory, which can be another
    session's worktree. Once that session removes it, a host left there fails
    every `getcwd()`, and with it anything that asks (sub-agents, tool display).
    """
    with contextlib.suppress(OSError):
        os.chdir(workspace)
        os.environ["PWD"] = str(workspace)  # getcwd is right; an inherited PWD is not


async def _serve(args: argparse.Namespace) -> dict | None:
    """Run the host until it stops; `spawn_host` arguments for its successor, if any."""
    from pcode.app import _enter_worktree, _resume_workspace
    from pcode.preferences import (
        SETTINGS,
        load_preferences,
        set_project_root,
        thinking_mode_preference,
    )
    from pcode.project_trust import prompt_trust
    from pcode.sessions import SavedSession, first_prompt
    from pcode.worktree import leave_worktree

    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    profile = (
        remote_profile.RemoteProfile.from_json(args.remote_profile) if args.remote_profile else None
    )
    # Before anything reads it: extensions, the sandbox policy, the agent.
    remote_profile.activate(profile)
    workspace = args.workspace.resolve()
    entry = HostEntry(
        id=args.id,
        pid=os.getpid(),
        model=args.model,
        workspace=str(workspace),
        log=os.environ.get("PCODE_HOST_LOG", ""),
        code=code_fingerprint(),
        python=sys.executable,
    )
    write_entry(entry)
    set_project_root(workspace)
    # The terminal that started this host asked already; nobody is here to ask.
    prompt_trust(workspace, ask=None)
    saved = None
    session_id = None
    if args.resume:
        saved = SavedSession.open(
            args.resume, args.session_dir, workspace, fork_if_open=True, fork=args.fork
        )
        try:
            if profile is not None:
                workspace = remote_resume_workspace(saved.info, workspace, profile)
            else:
                workspace = Path(_resume_workspace(saved.info, workspace)).resolve()
                if args.fork and args.worktree:
                    workspace = fork_worktree(saved, workspace)
        except BaseException:
            saved.abandon()  # A copy made for this resume is removed, not left listed.
            raise
        entry.session_id = saved.info.id
        entry.forked_from = saved.info.forked_from or ""
        prompt = first_prompt(saved.info, saved.directory.parent)
        first = "" if prompt.startswith("(") else prompt
        entry.title = saved.info.name or saved.info.title or first
        entry.updated = _activity_time(saved.info.updated)
    elif profile is not None:
        # Never the shared checkout, even from inside a linked worktree.
        workspace, session_id = _enter_worktree(
            workspace, args.worktree or True, always=True, base=profile.base
        )
        workspace = workspace.resolve()
    elif not args.no_worktree:
        workspace, session_id = _enter_worktree(workspace, args.worktree)
        workspace = workspace.resolve()
    entry.workspace = str(workspace)
    write_entry(entry, touch=saved is None)
    move_into(workspace)

    host = SessionHost(entry)
    host.carried = read_carry(args.carry)
    # An unattended session's worktree is left for its owner to inspect.
    host.keep_worktree = profile is not None
    controller = host.controller
    plan_startup(controller, args.model or None, saved)
    controller.workspace = workspace
    controller.session_dir = saved.directory.parent if saved is not None else args.session_dir
    controller.save_sessions = not args.no_save
    controller._session_id = session_id
    controller.worktree_owner = session_id
    controller.activity.thinking_mode = thinking_mode_preference()
    controller.register_skills()
    await host.serve()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, host.stop)
    print(f"serving {host.socket}", file=sys.stderr, flush=True)
    # Before boot adopts what an earlier pcode left running.
    host.own_jobs()
    await host.boot()
    watcher = None
    try:
        idle_minutes = _idle_minutes(load_preferences(), SETTINGS)
        host.active_at = time.monotonic()  # A slow boot is not idle time.
        if idle_minutes is not None:
            watcher = asyncio.create_task(host.stop_when_idle(idle_minutes))
        await host.stopped.wait()
    finally:
        if watcher is not None:
            watcher.cancel()
        from pcode.claude_sdk import shutdown

        runtime = controller.runtime
        try:
            try:
                await host.close()
            finally:
                from pcode import antigravity

                try:
                    await shutdown()
                finally:
                    await antigravity.shutdown()
        finally:
            # Lets go of the session, which a successor is about to open.
            if runtime is not None and hasattr(runtime, "close"):
                runtime.close()
            elif saved is not None:
                saved.close()  # Startup failed before a runtime took it.
        # A stop that came after `restart` must not tidy away the successor's worktree.
        if not host.keep_worktree and host.successor is None:
            # Out of the worktree first, so removing it leaves no process inside.
            move_into(Path.home())
            # A terminal that asked to keep it tidies it itself, and can ask first.
            leave_worktree(
                controller.workspace,
                getattr(runtime, "session", None),
                ask=None,
                notify=lambda text: print(text, file=sys.stderr, flush=True),
                # Nobody chose to leave a session that stopped for being idle,
                # so its branch waits to be resumed rather than merged.
                merge=not host.idle_stopped,
                owner=session_id or entry.session_id or None,
                session_dir=controller.session_dir,
            )
    if host.successor is None:
        return None
    return {
        "identity": host.successor,
        "model": controller.model,
        "workspace": controller.workspace,
        "resume": entry.session_id,
        "session_dir": controller.session_dir,
        "profile": profile,
        "carry": host.carry,
    }


def start_successor(successor: dict) -> None:
    """Start the host a `restart` asked for, now this one has let go of the session.

    Spawned from here, it inherits this host's environment (a remote profile's
    is scrubbed already) and its remote profile.
    """
    from pcode.remote import spawn_host, write_carry

    try:
        identity, _, log = spawn_host(**successor)
    except Exception as error:  # noqa: BLE001 - the log is the only reader.
        print(f"could not start the successor: {error_message(error)}", file=sys.stderr)
        if carry := successor.get("carry"):
            print(f"not carried on: {json.dumps(carry)}", file=sys.stderr, flush=True)
            # Left for whoever asked for the restart to report (`remote.undelivered`).
            with contextlib.suppress(OSError):
                write_carry(carry, successor["identity"], host_dir())
        return
    print(f"restarted as {identity} (log: {log})", file=sys.stderr, flush=True)


def _idle_minutes(preferences: dict, settings: dict) -> int | None:
    """`session_host_idle_minutes` as minutes; None for off. A bad value is the default."""
    key = "session_host_idle_minutes"
    for value in (preferences.get(key), settings[key].default):
        if value == "off":
            return None
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
    return 0


def _ignore_signal(signum, frame) -> None:
    """A signal a host neither acts on nor dies of."""


def main(argv: list[str] | None = None) -> None:
    import faulthandler

    args = _parser().parse_args(argv)
    # `kill -USR1 PID` writes every thread's stack to the log, for a host that hangs.
    faulthandler.register(signal.SIGUSR1)
    # The terminal that started the host may close; the host outlives it.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    # The terminal's "detach for an app restart" signal (dev/app-restart.md):
    # meant for terminals, and a host's default action would be to die. A
    # handler, not SIG_IGN, which the commands a host runs would inherit.
    signal.signal(signal.SIGUSR2, _ignore_signal)
    try:
        successor = asyncio.run(_serve(args))
    except Exception as error:  # noqa: BLE001 - the log is the only reader.
        print(f"session host failed: {error_message(error)}", file=sys.stderr, flush=True)
        remove_entry(args.id)
        raise SystemExit(1) from error
    remove_entry(args.id)
    if successor is not None:
        start_successor(successor)


if __name__ == "__main__":
    main()
