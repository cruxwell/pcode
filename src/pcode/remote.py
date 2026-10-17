"""The terminal's side of a session host (see `pcode.host`).

`RemoteController` stands in for `SessionController` while another process runs
the conversation. What the terminal asks for goes to the host as intents
(`submit`, `command`, `cancel`, ...) or queries; what the host shows arrives as
view calls, which `PreviewApp` renders exactly as it renders its own
controller's. The session's state (model, effort, commands, the live panel's
session fields, side questions) is mirrored from what the host sends.

`HostedSession` is the controller's `runtime` for the parts of the terminal that
read the conversation itself (`/tree`, `/tools`, `/diffs`, replay): it reads the
host's journal from disk, without owning it.
"""

import asyncio
import inspect
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pcode.aside import Aside, Asides
from pcode.commands import Command, CommandRegistry
from pcode.controller import (
    MODEL_COMMANDS,
    SESSION_FIELDS,
    VIEW_CALLS,
    SessionController,
    mcp_enable,
)
from pcode.host_protocol import (
    LINE_LIMIT,
    PROTOCOL,
    HostEntry,
    dumps,
    host_dir,
    list_hosts,
    read_message,
    socket_path,
)
from pcode.jobs import Job, JobRegistry
from pcode.remote_profile import RemoteProfile, scrubbed_env
from pcode.rpc import Peer, RemoteError, decode
from pcode.runtime import EditCompleted


class HostError(RuntimeError):
    """The host could not be reached, refused this terminal, or went away."""

    # Carries no provider text, so it is shown as is.
    sanitized = True


class JobsView(JobRegistry):
    """The host's shell jobs, read from its registry directory for the jobs browser.

    Read only: nothing here launches, stops, or deletes a job (stopping is an
    intent sent to the host); `refresh` only reads what the host published.
    """

    def __init__(self, home: Path) -> None:
        super().__init__()
        self.home = home

    def refresh(self) -> list:
        try:
            record = json.loads((self.home / "registry.json").read_text(encoding="utf-8"))
        except OSError, ValueError:
            record = {}
        entries = record.get("jobs") if isinstance(record, dict) else None
        for identity, entry in (entries if isinstance(entries, dict) else {}).items():
            if identity not in self.jobs and isinstance(entry, dict):
                try:
                    self.jobs[identity] = Job(
                        id=identity,
                        command=str(entry["command"]),
                        directory=Path(entry["directory"]),
                        supervisor_pid=int(entry["supervisor_pid"]),
                        started_at=float(entry["started_at"]),
                        background=True,
                        purpose=str(entry.get("purpose") or ""),
                    )
                except KeyError, TypeError, ValueError:
                    continue
        for job in self.jobs.values():
            if job.exit_code is not None:
                continue
            status = self._read_status(job)
            if status is None:
                continue
            job.pid = job.pid or status.get("pid")
            if status.get("exit_code") is not None:
                job.exit_code = status["exit_code"]
                job.ended_at = status.get("ended_at") or time.time()
        return []


class HostedSession:
    """The runtime a terminal attached to a host sees: the host's journal, read from disk."""

    remote = True
    agent = None
    mcp = None
    recovery_blocked = None
    inspections = None

    def __init__(self, controller: "RemoteController", welcome: dict) -> None:
        self.controller = controller
        self.id = welcome["id"]
        self.pid = welcome["pid"]
        self.session = None
        self.session_id = ""
        self.jobs = None
        self.lost = False

    @property
    def tree(self):
        return self.session.tree if self.session is not None else None

    def follow(self, state: dict) -> None:
        """Keep up with the host: its journal (a new one after /new) and its jobs."""
        from pcode.sessions import SessionError, SessionJournal

        self.session_id = state.get("session_id", "")
        directory = state.get("session_directory", "")
        if not directory:
            self.session = None
        elif self.session is None or str(self.session.directory) != directory:
            try:
                self.session = SessionJournal.read(Path(directory))
            except OSError, SessionError, ValueError:
                self.session = None
        else:
            self.session.refresh()
        home = state.get("jobs_directory", "")
        if not home:
            self.jobs = None
        elif self.jobs is None or str(self.jobs.home) != home:
            self.jobs = JobsView(Path(home))

    def refresh(self) -> None:
        from pcode.sessions import SessionError

        if self.session is not None:
            try:
                self.session.refresh()
            except OSError, SessionError, ValueError:
                pass  # Keep what was read; the next refresh catches up.

    def stop(self, *, keep_worktree: bool = False) -> None:
        self.controller.peer.notify("stop", keep_worktree)

    def close(self) -> None:
        self.controller.close()


class _Calls:
    """What the host may call on this terminal: its view methods, and the mirrors."""

    def __init__(self, controller: "RemoteController") -> None:
        self._controller = controller
        # Calls that arrive before the welcome is applied wait for it.
        self._held: list | None = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        controller = self._controller

        def call(*args, **kwargs):
            if self._held is not None:
                self._held.append((name, args, kwargs))
                return None
            return controller.receive(name, args, kwargs)

        return call


# The host's argument completers a terminal can run itself: each reads only the
# model catalog the host sends with its state, or the shared filesystem.
COMPLETERS = frozenset({"aside_completions", "model_list_completions", "path_completions"})


def _proxy(state: dict, controller: "RemoteController") -> Command:
    """A command the host runs, listed here for help and completion."""
    name = state["name"]
    # A host started before `completer` existed flags /btw with `models`.
    completer = state.get("completer") or ("aside_completions" if state.get("models") else None)

    arguments = tuple(state.get("arguments") or ())

    def refuse(argument: str) -> None:
        raise ValueError(f"{name} runs in the session host.")

    return Command(
        name,
        state["description"],
        refuse,
        arguments,
        tuple(state.get("aliases") or ()),
        free_arguments=bool(state.get("free_arguments")),
        # The host sends a list only for commands whose arguments change; the
        # rest (/worktree, /show-activity, ...) complete from their fixed list.
        argument_provider=lambda: tuple(controller.arguments.get(name, arguments)),
        group=state.get("group") or "Other",
        argument_descriptions=state.get("argument_descriptions") or None,
        argument_completer=getattr(controller, completer) if completer in COMPLETERS else None,
    )


class RemoteController:
    """A session host's controller, as seen from an attached terminal."""

    aside_completions = SessionController.aside_completions
    model_list_completions = SessionController.model_list_completions

    def path_completions(self, argument: str):
        """An extension command's path argument, resolved against the host's workspace."""
        from pcode.path_completion import complete_paths

        return complete_paths(argument, self.workspace)

    def __init__(self, view, activity) -> None:
        self.view = view
        self.activity = activity
        self.peer: Peer | None = None
        self.runtime: HostedSession | None = None
        self.running = True
        self.registry = CommandRegistry()
        self.asides = Asides()
        self.activity.asides = self.asides.items
        self.arguments: dict[str, list[str]] = {}
        self.state: dict = {}
        self.model: str | None = None
        self.pending_model: str | None = None
        self.workspace = Path.cwd()
        self.session_dir: Path | None = None
        self.save_sessions = True
        self.resuming = False
        self.extensions = None
        self.skill_command_names: list[str] = []
        self._session_id = None
        self._saved_session = None
        self._needs_runtime = False
        self.startup_pending = False
        self.startup_error: Exception | None = None
        self.interactive = True
        self._calls = _Calls(self)
        self._serving: asyncio.Task | None = None
        self.on_closed = lambda: None
        # The host said it was stopping, rather than just going away.
        self.host_stopped = False
        # One per command sent and not yet answered by `after_command`, oldest
        # first: the host runs them in order and reports each exactly once.
        self._command_waits: list = []

    # Connecting

    @classmethod
    async def connect(cls, path: Path, view, activity) -> tuple["RemoteController", dict]:
        """Attach to the host at `path`; returns the controller and the host's welcome."""
        try:
            reader, writer = await asyncio.open_unix_connection(str(path), limit=LINE_LIMIT)
        except OSError as error:
            raise HostError(f"Cannot reach the session host at {path}: {error}") from error
        try:
            writer.write(dumps({"type": "hello", "protocol": PROTOCOL}))
            await writer.drain()
            reply = await read_message(reader)
        except (OSError, ValueError) as error:
            writer.close()
            raise HostError(f"The session host at {path} did not answer: {error}") from error
        if reply is None or reply.get("type") != "ready":
            writer.close()
            message = (reply or {}).get("message") or "The session host closed the connection."
            raise HostError(message)
        controller = cls(view, activity)
        controller.peer = Peer(reader, writer, controller._calls, allowed=VIEW_CALLS)
        controller.peer.on_close = controller._closed
        controller._serving = asyncio.create_task(controller.peer.serve())
        try:
            welcome = await controller.peer.request("attach")
        except ConnectionError as error:
            raise HostError("The session host closed the connection.") from error
        controller.runtime = HostedSession(controller, welcome)
        controller.apply_state(welcome["session"])
        for name, value in welcome["activity"].items():
            controller.apply_field(name, value)
        for state in welcome["asides"]:
            controller.apply_aside(state)
        return controller, welcome

    async def start(self, welcome: dict) -> None:
        """Deliver the calls buffered in `welcome`, then whatever arrived since."""
        for name, args, kwargs in welcome["calls"]:
            await self._deliver(name, decode(args), decode(kwargs))
        while self._calls._held:
            name, args, kwargs = self._calls._held.pop(0)
            await self._deliver(name, args, kwargs)
        self._calls._held = None

    async def _deliver(self, name: str, args, kwargs) -> None:
        result = self.receive(name, args, kwargs)
        if inspect.isawaitable(result):
            await result

    @property
    def id(self) -> str:
        return self.runtime.id if self.runtime is not None else ""

    def close(self) -> None:
        """Detach: the host keeps running."""
        self.running = False
        self._end_command_waits()
        if self.peer is not None:
            self.peer.on_close = None
            self.peer.close()

    async def detach(self) -> None:
        """`close`, then wait until what was sent has gone out."""
        self.close()
        if self._serving is not None:
            await asyncio.gather(self._serving, return_exceptions=True)

    def _end_command_waits(self) -> None:
        while self._command_waits:
            self.activity.end_wait(self._command_waits.pop())

    def _closed(self) -> None:
        self._end_command_waits()
        if self.runtime is not None:
            self.runtime.lost = True
        self.on_closed()

    # What the host sends

    def receive(self, name: str, args, kwargs):
        if name == "state":
            for field, value in args[0].items():
                self.apply_field(field, value)
            self.view.redraw()
            return None
        if name == "session_state":
            self.apply_state(args[0])
            self.view.commands_changed()
            self.view.redraw()
            return None
        if name == "aside_changed":
            self.apply_aside(args[0])
            self.view.redraw()
            return None
        if name == "aside_answered":
            self.view.aside_answered(self.apply_aside(args[0]))
            return None
        if name == "host_closed":
            self.host_stopped = True
            return None
        if name == "read_asides":
            return self._read_asides()
        if name == "after_command" and self._command_waits:
            self.activity.end_wait(self._command_waits.pop(0))
        if self.runtime and (
            name in ("turn_ended", "replay_conversation", "show_branch")
            # The host journals an edit before sending it. /diffs reads the
            # running turn's edits from the journal, mid-turn too.
            or (name == "turn_event" and isinstance(args[0], EditCompleted))
        ):
            self.runtime.refresh()
        return getattr(self.view, name)(*args, **kwargs)

    def apply_field(self, name: str, value) -> None:
        if name not in SESSION_FIELDS:
            return
        if name == "prompt_state" and value != getattr(self.activity, name):
            # Autohiding the task list is this terminal's preference, applied here.
            if value == "running":
                self.activity.tasks_autohidden = False
            elif self.activity.autohide_tasks:
                self.activity.tasks_autohidden = True
        setattr(self.activity, name, value)

    def apply_state(self, state: dict) -> None:
        self.state = state
        self.model = state.get("model")
        self.pending_model = state.get("pending_model")
        self.workspace = Path(state.get("workspace") or self.workspace)
        self.startup_pending = bool(state.get("startup_pending"))
        error = state.get("startup_error")
        self.startup_error = HostError(error) if error else None
        self.arguments = state.get("arguments") or {}
        self.skill_command_names = list(state.get("skills") or ())
        self.save_sessions = bool(state.get("saving"))
        directory = state.get("session_directory")
        self.session_dir = Path(directory).parent if directory else self.session_dir
        commands = state.get("commands") or []
        if [command.name for command in self.registry.commands] != [c["name"] for c in commands]:
            self.registry = CommandRegistry()
            for command in commands:
                self.registry.register(_proxy(command, self))
        if self.runtime is not None:
            self.runtime.follow(state)

    def apply_aside(self, state: dict) -> Aside:
        aside = next((item for item in self.asides.items if item.id == state["id"]), None)
        new = aside is None
        if new:
            aside = Aside(question=state["question"], id=state["id"])
            self.asides.items.append(aside)
        for name in (
            "model",
            "label",
            "effort",
            "status",
            "answer",
            "activity",
            "error",
            "started",
            "finished",
            "thread",
            "conversation",
            "base",
            "bridged",
        ):
            if name in state:
                setattr(aside, name, state[name])
        # The reply lives in the host; a follow-up only needs to know there is one.
        aside.reply = True if state.get("replied") else None
        # Read here and not yet reported stays read.
        aside.read = aside.read or bool(state.get("read"))
        if new:
            # The host dropped its oldest settled threads the same way.
            self.asides._trim()
        return aside

    async def _read_asides(self):
        """The viewer; then the host learns what was read, so no terminal shows it as ready."""
        try:
            return await self.view.read_asides()
        finally:
            read = [aside.id for aside in self.asides.items if aside.read]
            if read and self.peer is not None:
                self.peer.notify("asides_read", read)

    # What the terminal asks for

    def submit(self, text: str, mode: str) -> None:
        # Set now so Enter + Ctrl+C in one input batch cancels the pending
        # request rather than clearing the draft; the host's state follows.
        self.activity.busy = True
        self.peer.notify("submit", text, mode)

    def command(self, text: str, tag=None) -> None:
        if text.split()[0] in MODEL_COMMANDS or mcp_enable(text):
            self.activity.busy = True
        self._command_waits.append(self.activity.begin_wait(f"Running {text.split()[0]}"))
        self.peer.notify("command", text, tag)

    def cancel(self) -> None:
        # The host drops queued commands, some without an `after_command`.
        self._end_command_waits()
        self.peer.notify("cancel")

    def set_thinking(self, mode: str) -> None:
        self.peer.notify("set_thinking", mode)

    def adjust_effort(self, direction: int) -> None:
        self.peer.notify("adjust_effort", direction)

    def stop_jobs(self, job_ids) -> None:
        self.peer.notify("stop_jobs", job_ids)

    def cancel_asides(self) -> None:
        # The answers run in the host; this terminal's records only mirror them.
        self.peer.notify("cancel_asides")

    def asides_seen(self, identities: list[str]) -> None:
        """Answers this terminal showed outside the viewer, read as `_read_asides` reports."""
        if self.peer is not None:
            self.peer.notify("asides_read", identities)

    def watch_job(self, job_id) -> None:
        self.peer.notify("watch_job", job_id)

    async def query(self, name: str, *args):
        with self.activity.waiting("Waiting for the session host"):
            return await self.peer.request("query", name, *args)

    def current_effort(self) -> str:
        return self.state.get("effort", "n/a") if self.model else "n/a"

    def context_label(self) -> str:
        return self.state.get("context", "")

    def session_title(self) -> str:
        return self.state.get("session_title") or ""

    def model_suggestions(self) -> list[str]:
        return self.state.get("models") or []

    def check_bridge(self, thread: str):
        """What the side-answer viewer checks before offering to merge; the host checks again."""
        if self.activity.busy or self.activity.queued:
            raise ValueError("Adding to the conversation waits for the running turn")
        return self.asides.follows(thread)

    def follow_up_aside(self, thread: str, question: str) -> None:
        self.asides.follows(thread)  # Refuses here what the host would refuse.

        async def ask() -> None:
            try:
                await self.query("follow_up_aside", thread, question)
            except ValueError as error:
                self.view.warning(str(error))

        asyncio.create_task(ask())

    async def navigate_tree(self, identity: str | None, *, edit: bool = False) -> str:
        return await self.query("navigate_tree", identity, edit)

    async def keep_thread(self, thread: str, action: str, instructions: str = "") -> None:
        await self.query("keep_thread", thread, action, instructions)


@dataclass
class HostLaunch:
    """A host to connect to once the terminal's event loop runs: just spawned, or running."""

    id: str
    process: subprocess.Popen | None = None
    log: Path | None = None
    directory: Path | None = None

    async def connect(self, view, activity) -> tuple[RemoteController, dict]:
        return await wait_for_host(
            self.id, view, activity, self.process, self.log, directory=self.directory
        )

    @classmethod
    def running(cls, entry: HostEntry) -> "HostLaunch":
        return cls(entry.id, log=Path(entry.log) if entry.log else None)


def spawn_host(
    *,
    model: str,
    workspace: Path,
    resume: str | None = None,
    session_dir: Path | None = None,
    no_save: bool = False,
    worktree=None,
    no_worktree: bool = False,
    fork: bool = False,
    directory: Path | None = None,
    profile: RemoteProfile | None = None,
) -> tuple[str, subprocess.Popen, Path]:
    """Start a host from this process, so it inherits this terminal's environment.

    With a remote `profile` it inherits only the allowlisted part of it, always
    works in a fresh worktree, and runs under the profile's limits.
    """
    directory = directory or host_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = uuid4().hex[:8]
    socket_path(identity, directory)  # Fail here, not in the child, on a too-long path.
    log = directory / f"{identity}.log"
    argv = [
        sys.executable,
        "-m",
        "pcode.host",
        "--id",
        identity,
        "--model",
        model,
        "--workspace",
        str(workspace),
    ]
    if resume:
        argv += ["--resume", resume]
        if fork:
            argv.append("--fork")
    if session_dir:
        argv += ["--session-dir", str(session_dir)]
    if no_save:
        argv.append("--no-save")
    if profile is not None:
        # The profile always makes a worktree; only its name may be chosen.
        argv += ["--remote-profile", profile.to_json()]
        if isinstance(worktree, str):
            argv += ["--worktree", worktree]
    elif no_worktree:
        argv.append("--no-worktree")
    elif isinstance(worktree, str):
        argv += ["--worktree", worktree]
    elif worktree:
        argv.append("--worktree")
    inherited = os.environ if profile is None else scrubbed_env(os.environ)
    env = {**inherited, "PCODE_HOST_LOG": str(log), "PCODE_HOST_DIR": str(directory)}
    fd = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "ab") as output:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            cwd=workspace,
            env=env,
            # Its own session: closing this terminal's tab does not signal it.
            start_new_session=True,
        )
    return identity, process, log


async def wait_for_host(
    identity: str,
    view,
    activity,
    process: subprocess.Popen | None = None,
    log: Path | None = None,
    *,
    directory: Path | None = None,
    timeout: float = 300.0,
) -> tuple[RemoteController, dict]:
    """Connect once the host is listening. Worktree setup can make that a while."""
    path = socket_path(identity, directory)
    deadline = time.monotonic() + timeout
    while True:
        if process is not None and process.poll() is not None:
            raise HostError(_exit_message(process.returncode, log))
        if process is None and not path.exists() and not path.with_suffix(".json").exists():
            # Neither socket nor entry: it stopped (both go together), as
            # opposed to starting up, which writes the entry long before the socket.
            raise HostError(f"Session host {identity} is no longer running.")
        if path.exists():
            try:
                return await RemoteController.connect(path, view, activity)
            except HostError:
                if process is None:
                    raise
        if time.monotonic() > deadline:
            raise HostError(f"The session host did not start within {timeout:.0f}s; see {log}.")
        await asyncio.sleep(0.05)


def _exit_message(code: int, log: Path | None) -> str:
    tail = ""
    if log is not None:
        try:
            lines = log.read_text(errors="replace").strip().splitlines()
            tail = "\n".join(lines[-8:])
        except OSError:
            pass
    message = f"The session host exited during startup (status {code})."
    if tail:
        message += f"\n{tail}"
    if log is not None:
        message += f"\nLog: {log}"
    return message


def others(current: str | None, directory: Path | None = None) -> list[HostEntry]:
    return [entry for entry in list_hosts(directory) if entry.id != current]


def _running(pid: int) -> bool:
    # A host this terminal spawned is its child: until reaped it lingers as a
    # zombie that still answers kill(pid, 0), so a wait would run to its timeout.
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass  # Not our child (attached to a running host): kill() tells.
    else:
        if reaped:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def wait_for_exit(pid: int, timeout: float = 30.0) -> bool:
    """Until the host has let go of its session: the next host can then open it.

    False if it was still running when `timeout` ran out.
    """
    deadline = time.monotonic() + timeout
    while _running(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    return not _running(pid)


def wait_for_exit_sync(pid: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while _running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)


async def stop_entry(entry: HostEntry, *, keep_worktree: bool = False) -> bool:
    """Stop another host without attaching to it; False if it has not exited yet."""
    await notify_host(entry, "stop", keep_worktree)
    return await wait_for_exit(entry.pid)


async def stop_if_idle(entry: HostEntry) -> str:
    """Stop a host as it would stop itself once idle, terminals or not.

    Returns "" once it has exited, else what it is busy with. The host checks
    for itself: its entry only says whether a turn runs, not a command,
    compaction, side question or background command.
    """
    try:
        reason = await request_host(entry, "stop_if_idle")
    except RemoteError as error:
        if error.type_name != "PermissionError":
            raise
        # A host older than the call can only be judged by its entry, and is
        # stopped leaving its worktree alone: nobody chose to finish it.
        if entry.state != "idle":
            return "a turn is running"
        await notify_host(entry, "stop", True)
        reason = ""
    if reason:
        return reason
    if not await wait_for_exit(entry.pid):
        raise HostError("it was told to stop but is still running after 30 s")
    return ""


@asynccontextmanager
async def _host_peer(entry: HostEntry, timeout: float = 5.0):
    """A connection to a host for one call, not an attached terminal."""

    async def hello():
        reader, writer = await asyncio.open_unix_connection(str(entry.socket), limit=LINE_LIMIT)
        try:
            writer.write(dumps({"type": "hello", "protocol": PROTOCOL}))
            await writer.drain()
            reply = await read_message(reader)
            if reply is None or reply.get("type") != "ready":
                raise HostError((reply or {}).get("message") or "The session host refused.")
        except BaseException:
            writer.close()  # Timed out too: a long-running caller must not leak it.
            raise
        return reader, writer

    try:
        # A host whose loop is wedged accepts the connection and never answers.
        reader, writer = await asyncio.wait_for(hello(), timeout)
    except TimeoutError:
        raise HostError(f"The session host did not answer within {timeout:g} s.") from None
    peer = Peer(reader, writer, object(), allowed=frozenset())
    serving = asyncio.create_task(peer.serve())
    try:
        yield peer
    finally:
        peer.close()
        await asyncio.gather(serving, return_exceptions=True)


async def notify_host(entry: HostEntry, method: str, *args) -> None:
    """Make one call on a host without attaching to it (`stop`, `cancel`)."""
    async with _host_peer(entry) as peer:
        peer.notify(method, *args)


async def request_host(entry: HostEntry, method: str, *args, timeout: float = 10.0):
    """Make one call on a host without attaching to it, and return its answer."""
    async with _host_peer(entry) as peer:
        try:
            return await asyncio.wait_for(peer.request(method, *args), timeout)
        except TimeoutError:
            raise HostError(f"The session host did not answer within {timeout:g} s.") from None
