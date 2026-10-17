"""What runs a conversation: the `SessionController`, in the terminal's process or a host's.

By default the terminal runs one in-process; in a session host (`--host`, or
after `/detach`) the terminal only renders and attaches over the socket. See
dev/development.md#session-hosts.
"""

import asyncio
import inspect
import threading
from collections.abc import Callable
from contextlib import aclosing, suppress
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from pcode.aside import (
    EFFORT_MARK,
    MODEL_MARK,
    Aside,
    Asides,
    Bridge,
    SideReply,
    SideTarget,
    effort_fragment,
    exchanges,
    model_fragment,
    model_labels,
    parse_models,
    prompt_target,
    summary_request,
)
from pcode.commands import Command, CommandRegistry
from pcode.error_report import error_message
from pcode.jobs import OUTPUT_TAIL_BYTES, WATCHED_PREFIX, format_duration
from pcode.logins import Logins
from pcode.preferences import (
    EFFORTS,
    SETTINGS,
    THINKING_MODES,
    apply_effort,
    apply_thinking,
    effort_for,
    effort_setting,
    effort_unavailable,
    from_project,
    load_preferences,
    save_model_effort,
    save_preferences,
    subagent_models,
)
from pcode.rpc import transportable
from pcode.runtime import CommandOutput, JobFinished, Message, ToolSummary
from pcode.shell_mode import execute, shell_command
from pcode.tool_display import command_text

# What the terminal handles itself. A skill or extension command may not take
# one of these names (nor a session command's): built-ins win.
TERMINAL_COMMANDS = frozenset(
    {
        "/help",
        "/commands",
        "/config",
        "/bind",
        "/unbind",
        "/quit",
        "/exit",
        "/status",
        "/tools",
        "/diffs",
        "/links",
        "/copy",
        "/tree",
        "/agents",
        "/resume",
        "/switch",
        "/restart",
        "/stop",
        "/detach",
        "/show-tasks",
        "/autohide-tasks",
        "/show-thinking",
        "/show-edits",
        "/show-commands",
        "/group-tools",
        "/theme",
        "/syntax",
        "/theme-preview",
        "/redraw",
    }
)

WORKTREE_ACTIONS = {
    "status": "Branch, mainline, and what is unmerged",
    "merge": "Merge the mainline into this branch, then fast-forward the mainline",
    "resolve": "Ask the model to resolve the conflicts a merge stopped on",
    "finish": "Merge, remove the worktree and its branch, and quit",
    "remove": "Delete the merged worktree; the branch stays",
    "list": "Every worktree of this repository",
    "clean": "Delete every other worktree with nothing uncommitted or unmerged",
}


# The fields of `Activity` the session sets. Everything else in it is derived
# from the events the terminal renders (tools, workers, plan, previews).
SESSION_FIELDS = (
    "busy",
    "status",
    "queued",
    "queued_prompts",
    "queued_modes",
    "prompt",
    "prompt_state",
    "turn_outcome",
    "prompt_kind",
    "prompt_detail",
    "user_command",
    "job_count",
    "watched_job",
    "notice",
    "notice_expires",
)

# What a terminal attached to a host may call on its controller. Intents are
# sent without waiting; `query` names one of QUERIES and returns its value.
INTENTS = frozenset(
    {
        "submit",
        "command",
        "cancel",
        "set_thinking",
        "adjust_effort",
        "stop_jobs",
        "watch_job",
        "cancel_asides",
    }
)
QUERIES = frozenset(
    {
        "session_overview",
        "follow_up_aside",
        "check_bridge",
        "keep_thread",
        "navigate_tree",
    }
)

# Commands that change the conversation itself, refused while a turn runs or
# prompts wait (bar /compact and /resend sent while idle, which go first).
IDLE_COMMANDS = frozenset({"/resend", "/new", "/resume", "/login", "/logout", "/compact"})

# Commands that make a model request or change the toolset. Each holds the
# session busy from Enter until its handler starts, so a Ctrl+C in the same
# input batch cancels it instead of clearing the draft.
MODEL_COMMANDS = frozenset({"/compact", "/resend"})

# Commands that only change the display. They run even before the runtime is
# ready and are never dropped by a Ctrl+C clearing the queue.
FRONTEND_COMMANDS = frozenset(
    {
        "/quit",
        "/exit",
        "/help",
        "/commands",
        "/theme",
        "/theme-preview",
        "/syntax",
        "/show-tasks",
        "/autohide-tasks",
        "/show-thinking",
        "/show-edits",
        "/show-commands",
        "/group-tools",
        "/redraw",
        "/config",
        "/bind",
        "/unbind",
    }
)

# Session commands that can recover from a failed startup: picking a model that
# builds starts the agent the bad one could not, and signing in is often the fix.
RECOVERY_COMMANDS = frozenset({"/model", "/login"})

# One queued command: the queue generation it was sent in, its text, whether
# the session was idle when it was sent, and the popup generation the terminal
# stamped on it.
QueuedCommand = tuple[int, str, bool, object]

STARTUP_FAILED = "Agent startup failed; choose another model with /model or restart pcode."


class MessageOwner(Protocol):
    """Whoever waits on one queued message (a headless caller): told when its turn
    starts, or that it never will. Either is said exactly once."""

    def started(self) -> None: ...
    def dropped(self) -> None: ...


# One queued message: the queue generation it was sent in, its text, how it is
# sent ("steering", "queue", "interrupt", "shell", "resend", "wake"), and its
# owner, if anyone waits on it.
Item = tuple[int, str, str, MessageOwner | None]


class PromptQueue:
    """Messages waiting for the model, in order, mirrored into the live panel.

    The panel lists them from `activity.queued_prompts` and `queued_modes`, so
    every change goes through here and the two cannot drift. `generation`
    moves on at every `clear`: an item fetched before a clear, while a command
    or popup held the queue up, is recognised as stale and skipped. Commands
    are stamped with it too, for the same reason.
    """

    def __init__(self, activity) -> None:
        self.activity = activity
        self.generation = 0
        self._items: asyncio.Queue[Item] = asyncio.Queue()
        # What `get` handed out and nobody has acted on yet (see `take_all`).
        self._fetched: Item | None = None

    def __len__(self) -> int:
        return len(self.activity.queued_prompts)

    def put(
        self, text: str, mode: str, *, first: bool = False, owner: MessageOwner | None = None
    ) -> None:
        """Queue `text`; `first` puts it ahead of everything already waiting."""
        if first:
            waiting = self._drain()
            self._items.put_nowait((self.generation, text, mode, owner))
            for item in waiting:
                self._items.put_nowait(item)
            self.activity.queued_prompts.insert(0, text)
            self.activity.queued_modes.insert(0, mode)
        else:
            self._items.put_nowait((self.generation, text, mode, owner))
            self.activity.queued_prompts.append(text)
            self.activity.queued_modes.append(mode)
        self._sync()

    async def get(self) -> Item:
        """The next item, stale or not; check `current` before acting on it."""
        self._fetched = await self._items.get()
        return self._fetched

    def current(self, item: Item) -> bool:
        return item[0] == self.generation

    def taken(self) -> None:
        """The item just fetched is being acted on: drop it from the panel."""
        self._fetched = None
        self.activity.queued_prompts.pop(0)
        self.activity.queued_modes.pop(0)
        self._sync()

    def clear(self) -> int:
        """Drop everything waiting and start a new generation. Returns how many were dropped."""
        self.generation += 1
        count = len(self.activity.queued_prompts)
        for *_, owner in self._drain():
            if owner is not None:
                owner.dropped()
        self.activity.queued_prompts.clear()
        self.activity.queued_modes.clear()
        self._sync()
        return count

    def steering(self) -> int:
        """How many current steering messages are waiting."""
        items = self._drain()
        for item in items:
            self._items.put_nowait(item)
        return sum(
            1
            for generation, _text, mode, _owner in items
            if generation == self.generation and mode == "steering"
        )

    def take_steering(self, limit: int | None = None) -> list[str]:
        """Remove and return the current steering messages, the first `limit` of
        them if given; everything else keeps its place."""
        messages = []
        for item in self._drain():
            generation, text, mode, _owner = item
            if (
                generation == self.generation
                and mode == "steering"
                and (limit is None or len(messages) < limit)
            ):
                messages.append(text)
                index = next(
                    i
                    for i, queued in enumerate(
                        zip(self.activity.queued_prompts, self.activity.queued_modes)
                    )
                    if queued == (text, mode)
                )
                self.activity.queued_prompts.pop(index)
                self.activity.queued_modes.pop(index)
            else:
                self._items.put_nowait(item)
        self._sync()
        return messages

    def take_all(self) -> list[tuple[str, str]]:
        """Remove and return every waiting message and its mode, oldest first, without dropping it.

        For a session moving into a host. The panel is left alone: by then it
        shows the host's queue. The new generation makes stale the one `get`
        fetched but has not acted on, which is taken here too.
        """
        fetched = [self._fetched] if self._fetched is not None else []
        items = [*fetched, *self._drain()]
        self._fetched = None
        self.generation += 1
        return [
            (text, mode)
            for generation, text, mode, _owner in items
            if generation == self.generation - 1
        ]

    def _drain(self) -> list[Item]:
        items = []
        while not self._items.empty():
            items.append(self._items.get_nowait())
        return items

    def _sync(self) -> None:
        self.activity.queued = len(self.activity.queued_prompts)


class SessionView(Protocol):
    """What the controller shows, implemented by whatever is displaying the session.

    In-process that is `PreviewApp`; in a session host it will be the socket,
    so arguments stay plain values and runtime events. It grows as logic moves
    here from the terminal.
    """

    # Scrollback
    def user(self, text: str) -> None: ...
    def note(self, text: str) -> None: ...
    def retained_note(self, text: str) -> None: ...
    def flash(self, text: str) -> None: ...
    def warning(self, text: str) -> None: ...
    def error(self, text: str, *, title: str = "Error") -> None: ...
    def cancelled(self) -> None: ...
    def tool_result(self, event) -> None: ...
    def shell_result(
        self, command: str, output: str, *, failed: bool, elapsed_seconds: float
    ) -> None: ...

    # The live panel's own state (queue, busy, prompt row) changed; repaint.
    def redraw(self) -> None: ...

    # A turn, in order: started, its events, retries, ended. `drop_output`
    # removes a live command preview once its command is done.
    def turn_started(self, text: str, *, echo: bool) -> None: ...
    def turn_event(self, event) -> None: ...
    def turn_retry(self, text: str) -> None: ...
    def turn_ended(self) -> None: ...
    def finish_text(self) -> None: ...
    def show_output(self, event) -> None: ...
    def drop_output(self, call_id: str) -> None: ...
    async def after_turn(self) -> None: ...

    # A command the controller does not handle itself, run by the terminal
    # that sent it; `after_command` follows every command, with its tag.
    async def run_command(self, text: str, *, idle: bool, tag: object) -> None: ...
    async def after_command(self, tag: object = None) -> None: ...

    # One of the session's own commands is running (`tag` as for run_command),
    # and has finished. Its popups, below, return None when dismissed.
    def command_started(self, tag: object) -> None: ...
    def command_finished(self) -> None: ...

    # The session's commands changed (skills, extension commands), or the
    # conversation was replaced by a new one. `session_changed` says anything
    # else in `session_state` may have (model, effort, context, completions).
    def commands_changed(self) -> None: ...
    def session_changed(self) -> None: ...
    def conversation_reset(self, title: str) -> None: ...
    def replay_conversation(self) -> None: ...
    def show_branch(self) -> None: ...
    def preview_reply(self, text: str) -> None: ...
    def show_events(self, events) -> None: ...

    # A side question's answer moved (it streams), or it arrived.
    def aside_changed(self, aside) -> None: ...
    def aside_answered(self, aside) -> None: ...

    # Popups the session's commands open, in the terminal that sent them.
    async def browse_jobs(self) -> None: ...
    async def choose_model(self, values, providers, current: str | None) -> str | None: ...
    async def read_asides(self): ...


# What a host may call on an attached terminal: its view, and the mirrors of
# the live panel (`state`), the session (`session_state`), and side questions.
# The `message_*` and `session_ended` calls go only to a headless caller (see
# pcode.remote_print): about its own message, and a command that ended the session.
VIEW_CALLS = frozenset(
    {name for name in vars(SessionView) if not name.startswith("_")}
    | {"state", "session_state", "aside_changed", "aside_answered", "host_closed"}
    | {"message_started", "message_dropped", "session_ended"}
)

transportable(Bridge)


def mcp_enable(text: str) -> bool:
    """An /mcp command that may sign in, which prompts queued behind it wait for."""
    words = text.split()
    return words[:1] == ["/mcp"] and words[1:2] in (["enable"], ["enable-all"])


def wake_row(text: str) -> tuple[str, str]:
    """The live row for a turn a finished job started: a badge, not an echo."""
    return "Job finished", text.partition("\n")[0].partition(" Read ")[0]


def delivered_job(event) -> str | None:
    """The job a top-level `wait_for_job` or `job_output` call just collected, if any."""
    if (
        isinstance(event, ToolSummary)
        and event.name in {"wait_for_job", "job_output"}
        and not event.parent_call_id
    ):
        return event.detail.partition(" ")[0]
    return None


class SessionController:
    """Runs a conversation: what it does next, the turns themselves, and stopping them.

    Owns the runtime, the prompt and command queues, the tasks doing work (a
    turn, MCP work, a history rewrite), and whether the session reads as busy.
    Everything it shows goes through `view`; `activity` holds the live panel's
    session state (busy, queue, prompt row), which the view paints. The view is
    the terminal in-process (`PreviewApp`), or `pcode.host.HostView` in a host.

    Areas of responsibility, in roughly the order their methods appear:

    - Slash commands: registering the commands the session handles.
    - Turn lifecycle: busy state, prompt and command queues, dispatch, turns,
      steering, and cancellation.
    - Background work: MCP enabling and sign-out, compaction, history rewrites.
    - Shell jobs: the footer's active count, a watched tail, and job wake-ups.
    - MCP servers: `/mcp` and saving default servers.
    - History: `/compact`, `/autocompact`, `/resend`.
    - The session: skills, extensions and sub-agents, building the runtime,
      switching models, effort, worktrees, startup, and resuming.
    - Side questions (`/btw`) and conversation branches (`/tree`).

    Provider sign-in and sign-out live in `pcode.logins.Logins`, held as `logins`.
    """

    def __init__(self, view: SessionView, activity, runtime=None) -> None:
        self.view = view
        self.activity = activity
        self.runtime = runtime
        # False once the owner (the terminal, or the host) is done with it.
        self.running = True
        # The terminal moved on to another controller (/detach, /switch) and
        # shares its activity: this one must no longer reset it.
        self.superseded = False
        # Building the runtime has not finished yet, or failed with this.
        self.startup_pending = False
        self.startup_error: Exception | None = None
        self.model: str | None = None
        self.workspace = Path.cwd()
        # Where saved sessions live, and whether this one is saved.
        self.session_dir: Path | None = None
        self.save_sessions = False
        # Consumed by the first saved session so it shares its ID with the
        # worktree created for it; later `/new` sessions get their own.
        self._session_id: str | None = None
        self._saved_session = None
        self.resuming = False
        # Whether the resumed conversation is on screen already (a terminal
        # draws it before the runtime loads; a host only once it has).
        self.conversation_shown = False
        # The runtime is built once the event loop is up (see initialize_runtime).
        self._needs_runtime = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # A model chosen mid-run, adopted before the next request.
        self.pending_model: str | None = None
        # User extensions load with the runtime; their commands register once it exists.
        self.extensions = None
        self.extension_command_names: list[str] = []
        self.skill_command_names: list[str] = []
        self._startup_context_shown: set[str] = set()
        # Whether commands run through the command loop (a terminal or a host),
        # so slow ones can show a row; without one, a job runs inline.
        self.interactive = False
        # Work a command asked for, done once its handler returns: a slow job
        # under a system row, a skill's prompt, an extension reload, a sign-in.
        self.job_requested: tuple[str, str, Callable[[], list[str]]] | None = None
        self.skill_requested: str | None = None
        # An extension command's prompt waits for its own turn: steered into a
        # running one, it would miss what the command set up (plan mode's
        # reminder only joins a turn's opening request).
        self.skill_queued = False
        self.skill_mcp_requested: tuple[str, tuple[str, ...]] | None = None
        self.reload_requested = False
        self.reload_pending = False  # Asked for mid-turn; `consume` runs it between turns.
        self.login_requested: str | None = None
        self.logout_requested: str | None = None
        # Runs the sign-ins and sign-outs those two ask for.
        self.logins = Logins(self)
        # Side questions run beside the conversation instead of in it, so they
        # keep their own records and never enter the queue.
        self.asides = Asides()
        # The live panel spins a row per running question; share the list so
        # it needs no refresh hook of its own.
        self.activity.asides = self.asides.items
        self.asides.on_failure = self.record_aside_failure
        self.asides.on_update = lambda aside: self.view.aside_changed(aside)
        self.asides.on_settle = self.aside_settled
        # Journaling settled side questions, one at a time and in the order
        # they settled, so a follow-up's node always lands after its parent's.
        self._side_lock = asyncio.Lock()
        self._side_records: set[asyncio.Task] = set()
        # Titling a session beside its first turn (`session_naming`): the running
        # request, and the sessions asked already while open, so a failure is
        # not retried on every turn. Opening a session (/new, /resume) clears it.
        self.naming_task: asyncio.Task | None = None
        self._naming_tried: set[str] = set()
        # A bare /rename asking the conversation's model for a name.
        self.rename_task: asyncio.Task | None = None
        self._model_suggestions: tuple[str | None, float, list[str]] | None = None
        # Whether the owner is shutting down, so a cancelled turn is not a Ctrl+C.
        self.closing: Callable[[], bool] = lambda: False
        self.prompts = PromptQueue(activity)
        self.commands: asyncio.Queue[QueuedCommand] = asyncio.Queue()
        # Backend commands sent before the runtime was ready, in order.
        self.startup_commands: list[QueuedCommand] = []
        self.ready = asyncio.Event()
        # Set while nothing holds queued prompts back: no command running, no
        # MCP work, no history rewrite. The turn loop waits on all three.
        self.command_idle = asyncio.Event()
        self.mcp_idle = asyncio.Event()
        self.compact_idle = asyncio.Event()
        for idle in (self.command_idle, self.mcp_idle, self.compact_idle):
            idle.set()
        self.live_task: asyncio.Task | None = None
        self.mcp_task: asyncio.Task | None = None
        self.compact_task: asyncio.Task | None = None
        # A browser sign-in: tracked like a turn so Ctrl+C cancels it.
        self.login_task: asyncio.Task | None = None
        # Commands queued but not started that hold the session busy.
        self.pending_mcp = 0
        self.pending_model_command = 0
        # The running turn is being cancelled to make way for an "interrupt"
        # message, so its failure must not clear the queue that message is in.
        self.interrupt_pending = False
        # The MCP server (or "defaults") being enabled or signed out of.
        self.mcp_enabling: str | None = None
        # The status row for that work, restored if a turn beside it ends first.
        self.mcp_status = ""
        # While MCP work is pending or running: how many steering messages sent
        # before it may still reach the running turn. Later ones wait for it.
        self.steering_before_mcp: int | None = None
        # Servers marked enabled in mcp.json, enabled without a browser after a
        # conversation starts (startup, /new, resume).
        self.mcp_defaults_requested = False
        # The watched job's output tail, as last pinned in the live panel.
        self.pinned: CommandOutput | None = None
        # The slash commands the session handles; the terminal runs the rest.
        self.registry = CommandRegistry()
        self.register_commands()

    # --- Slash commands ---

    def register_commands(self) -> None:
        # Registration order is /help's and completion's order. The helpers are
        # consecutive runs of that order, not `group=` buckets: the groups
        # interleave, and sorting by them would reorder /help.
        for command in (
            *self._aside_commands(),
            *self._model_commands(),
            *self._session_commands(),
            *self._resource_commands(),
        ):
            self.registry.register(command)

    def _aside_commands(self) -> tuple[Command, ...]:
        """The side question command."""
        return (
            Command(
                "/btw",
                "Ask a side question beside the running turn ($MODEL ... picks models, "
                "+EFFORT the effort); "
                "bare opens the answers",
                self.aside,
                aliases=("/side",),
                free_arguments=True,
                argument_completer=self.aside_completions,
                group="Inspect",
            ),
        )

    def _model_commands(self) -> tuple[Command, ...]:
        """Commands for the model, its sign-ins, and the extensions it runs with."""
        from pcode.models import login_sources, logout_sources

        login_targets = "Claude Code or OpenAI Codex"
        return (
            Command(
                "/model",
                "Choose a model; keeps the conversation",
                self.select_model,
                group="Model",
            ),
            Command(
                "/effort",
                "Set reasoning effort: low / medium / high / xhigh / default",
                self.effort,
                ("low", "medium", "high", "xhigh", "default"),
                group="Model",
            ),
            Command(
                "/login",
                f"Sign in to {login_targets} in a browser",
                self.logins.login,
                login_sources(),
                group="Model",
            ),
            Command(
                "/logout",
                f"Remove a stored login ({' or '.join(logout_sources())})",
                self.logins.logout,
                logout_sources(),
                # The handler validates, so its own usage message wins.
                free_arguments=True,
                group="Model",
            ),
            Command(
                "/extensions",
                "Extensions: list / on NAME / off NAME",
                self.manage_extensions,
                ("list", "on", "off"),
                free_arguments=True,
                argument_provider=self.extension_arguments,
                group="Model",
            ),
            Command(
                "/subagents",
                "Models delegate_task lists for sub-agents: MODEL ... sets them, off clears, "
                "bare lists",
                self.subagents,
                free_arguments=True,
                argument_completer=self.model_list_completions,
                group="Model",
            ),
            Command(
                "/reload",
                "Reload extensions; keeps the conversation",
                self.reload,
                group="Model",
            ),
        )

    def _session_commands(self) -> tuple[Command, ...]:
        """Commands for the conversation itself and its worktree."""
        return (
            Command(
                "/new", "Start a new conversation; clears the screen", self.new, group="Session"
            ),
            Command(
                "/rename",
                "Name this conversation for /resume: NAME | ask model",
                self.rename,
                free_arguments=True,
                group="Session",
            ),
            Command(
                "/worktree",
                "This session's git worktree: status / merge / resolve / finish / remove"
                " / list / clean",
                self.worktree,
                tuple(WORKTREE_ACTIONS),
                group="Session",
                argument_descriptions=WORKTREE_ACTIONS,
            ),
            Command(
                "/compact",
                "Summarize older context now; optional FOCUS steers the summary",
                self.compact,
                free_arguments=True,
                group="Session",
            ),
            Command(
                "/autocompact",
                "Compact automatically near the context limit: on / off / TOKENS (e.g. 200k) "
                "/ auto",
                self.autocompact,
                ("on", "off", "auto"),
                free_arguments=True,
                group="Session",
            ),
            Command(
                "/resend",
                "Ask the model again from the last checkpoint, without a new message",
                self.resend,
                group="Session",
            ),
        )

    def _resource_commands(self) -> tuple[Command, ...]:
        """Commands for what runs beside the model: MCP servers, shell jobs, plan usage."""
        return (
            Command(
                "/mcp",
                "Manage MCP servers: list / enable NAME [--save] / enable-all / "
                "disable NAME [--save] / logout NAME",
                self.mcp,
                ("list", "enable", "enable-all", "disable"),
                free_arguments=True,
                argument_provider=self.mcp_arguments,
                group="Model",
            ),
            Command(
                "/jobs",
                "Browse shell jobs and their output; stop ID / stop all / watch ID / unwatch",
                self.jobs,
                free_arguments=True,
                argument_provider=self.jobs_arguments,
                group="Session",
            ),
            Command(
                "/usage",
                "Plan limits and spend for the Claude Code and Codex logins",
                self.usage,
                group="Inspect",
            ),
        )

    # --- What the footer, an attached terminal, and the host read ---

    def context_label(self) -> str:
        """The footer's context usage: tokens used of the window or autocompact cap."""
        if not self.model or self.startup_pending or self.startup_error is not None:
            return ""
        from pcode.context_usage import context_label

        resolved = getattr(getattr(self.runtime, "agent", None), "model", None)
        history = getattr(self.runtime, "context_history", None)
        if history is None:
            history = getattr(self.runtime, "history", ())
        try:
            return context_label(resolved or self.model, history, compact_at=self.compact_cap())
        except Exception:  # noqa: BLE001 - a footer label must not break anything.
            return ""

    def session_state(self) -> dict:
        """What a terminal attached to a host shows of the session, beyond the live panel."""
        saved = getattr(self.runtime, "session", None)
        commands = []
        arguments = {}
        for command in self.registry.commands:
            commands.append(
                {
                    "name": command.name,
                    "description": command.description,
                    "arguments": list(command.arguments),
                    "aliases": list(command.aliases),
                    "free_arguments": command.free_arguments,
                    "group": command.group,
                    "argument_descriptions": dict(command.argument_descriptions or {}),
                    # Which of the controller's completers the terminal runs locally.
                    "completer": getattr(command.argument_completer, "__name__", None),
                    # What a terminal from before `completer` reads to complete /btw.
                    "models": command.argument_completer == self.aside_completions,
                }
            )
            if command.argument_provider is not None:
                try:
                    arguments[command.name] = list(command.argument_provider())
                except Exception:  # noqa: BLE001 - completion must not break anything.
                    arguments[command.name] = []
        return {
            "model": self.model,
            "pending_model": self.pending_model,
            "effort": self.current_effort(),
            "context": self.context_label(),
            "workspace": str(self.workspace),
            "session_id": saved.info.id if saved is not None else "",
            "session_title": self.session_title(),
            "session_directory": str(saved.directory) if saved is not None else "",
            "saving": saved is not None
            or getattr(self.runtime, "session_factory", None) is not None,
            "startup_pending": self.startup_pending,
            "startup_error": error_message(self.startup_error) if self.startup_error else "",
            "commands": commands,
            "arguments": arguments,
            "skills": list(self.skill_command_names),
            "jobs_directory": str(self._jobs_home() or ""),
            # For `/btw $MODEL` completion, which cannot wait on a query per key.
            # Not while starting: the first scan imports provider SDKs.
            "models": [] if self.startup_pending else list(self.model_suggestions()),
        }

    def _jobs_home(self):
        registry = getattr(self.runtime, "jobs", None)
        home = getattr(registry, "home", None)
        return home() if callable(home) else None

    def set_thinking(self, mode: str) -> None:
        """Ask the provider for the readable thinking `mode` needs from the next request."""
        if mode not in THINKING_MODES:
            return  # From a mismatched terminal over the host socket; keep the current mode.
        self.activity.thinking_mode = mode
        agent = getattr(self.runtime, "agent", None)
        if agent is not None and self.model:
            apply_thinking(agent, self.model, mode)

    async def query(self, name: str, *args):
        """One of QUERIES, for a terminal that cannot call the controller directly."""
        if name not in QUERIES:
            raise ValueError(f"Unknown query {name!r}.")
        result = getattr(self, name)(*args)
        if inspect.isawaitable(result):
            result = await result
        return result

    # --- Busy state ---

    def command_taken(self, name: str) -> bool:
        return name in TERMINAL_COMMANDS or self.registry.find(name) is not None

    def tasks(self) -> list[asyncio.Task]:
        work = (self.live_task, self.mcp_task, self.compact_task, self.login_task)
        return [task for task in work if task]

    def working(self) -> bool:
        """A turn, MCP work, or a history rewrite is running."""
        return any(not task.done() for task in self.tasks())

    def turn_running(self) -> bool:
        return self.live_task is not None and not self.live_task.done()

    async def idle(self) -> None:
        """Wait until nothing holds queued prompts back."""
        await self.command_idle.wait()
        await self.mcp_idle.wait()
        await self.compact_idle.wait()

    @property
    def commands_pending(self) -> bool:
        return bool(self.pending_mcp or self.pending_model_command)

    def refresh_busy(self) -> None:
        """Busy while anything is queued, pending, or running."""
        self.activity.busy = bool(
            self.activity.queued_prompts or self.commands_pending or self.working()
        )

    # --- Shell waits ---

    def set_cancel_policy(self, policy: str) -> None:
        """Say what an abandoned shell wait should do to its command.

        Set before cancelling, because by the time the tool call sees
        `CancelledError` there is nothing left to tell it apart from any other
        cancellation. Reset to the safe default once the turn is over.
        """
        registry = getattr(self.runtime, "jobs", None)
        if registry is not None:
            registry.cancel_policy = policy

    def release_shell_waits(self) -> None:
        """Hand any foreground shell wait back to the model as a job handle.

        Steering is delivered at the next model request, and a wait on a slow
        command is what stands between now and that request. Ending the wait
        gets the message there promptly; the command itself is untouched.
        """
        registry = getattr(self.runtime, "jobs", None)
        if registry is not None:
            registry.release_waits()

    # --- Jobs ---

    def report_finished_jobs(self, job_id: str | None = None) -> list:
        """Announce job exits in scrollback, each one once. Returns those announced."""
        registry = getattr(self.runtime, "jobs", None)
        if registry is None:
            return []
        from pcode.shell import REDUCED_SHELL_OUTPUT

        finished = registry.take_announcements("ui", job_id)
        for job in finished:
            output, truncated = registry.read_output(job)
            if truncated:
                output = REDUCED_SHELL_OUTPUT + "\n" + output
            result = output + f"\n[{job.id} · {job.outcome()} · {format_duration(job.elapsed)}]"
            event = JobFinished(
                "shell",
                f"{command_text(job.label())} → {job.id} · {job.outcome()}",
                failed=job.stopped or job.exit_code != 0,
                elapsed_seconds=job.elapsed,
                command=command_text(job.command),
                result=command_text(result),
                purpose=command_text(job.purpose),
            )
            saved = getattr(self.runtime, "session", None)
            if saved is not None:
                saved.event(event, run_id=saved.tree.active or "")
            self.view.tool_result(event)
        return finished

    def report_delivered_job(self, job_id: str) -> None:
        """Write a job's exit where the call that collected it settled.

        The call's own row is left out of scrollback because the exit notice
        says the same thing. Holding that notice until idle would print it
        after the final answer, long after the model acted on the result.
        A job that is still running, or was already reported, prints nothing.
        """
        registry = getattr(self.runtime, "jobs", None)
        job = registry.get(job_id) if registry is not None else None
        if job is None or job.running or "ui" in job.announced:
            return
        # A suppressed row does not commit streamed prose; the notice must not
        # land ahead of text the model wrote before making the call.
        self.view.finish_text()
        self.report_finished_jobs(job_id)

    def wake_prompt(self) -> str | None:
        """The turn a finished job starts on its own, or None when nothing should.

        Only a job the model launched and expects to hear about wakes it: one
        it backgrounded, or was handed a handle for when a wait ended early.
        An adopted job belongs to a model that is gone. The text is exactly
        the notice the model would have received at its next request, so
        waking costs a request, never a different conversation. Called while
        idle: a job that ended mid-turn after the last request is included,
        because nothing else is going to deliver it.
        """
        registry = getattr(self.runtime, "jobs", None)
        if registry is None or not self.model:
            return None
        wakeable = [
            job
            for job in registry.jobs.values()
            if not job.running
            # A stop is the user's or the model's own doing, not news to act on.
            and not job.stopped
            and not job.adopted
            and "model" not in job.announced
            and registry.announceable(job)
        ]
        if not wakeable:
            return None
        if load_preferences().get("job_wake", SETTINGS["job_wake"].default) != "on":
            return None
        from pcode.job_notices import notice_for

        for job in wakeable:
            job.announced.add("model")
        return "\n\n".join(notice_for(registry, job) for job in wakeable)

    # --- Sending ---

    def submit(self, text: str, mode: str, *, owner: MessageOwner | None = None) -> None:
        """Queue a message for the model, sent the way `mode` says.

        `steering` joins the running turn at its next request, `queue` waits
        for it to end, `interrupt` cancels it, and `shell` runs a `!command`
        in turn, never as steering: its result rides the next request rather
        than being spliced into a running one. `owner` hears when it starts,
        or that it was dropped; a steering message never starts a turn.
        """
        if mode == "steering" and prompt_target(text)[0] is not None:
            # A turn cannot change models mid-request: a prompt naming one gets
            # its own turn on it, never spliced into one that starts first.
            mode = "queue"
        if mode == "interrupt" and self.turn_running():
            self.clear_queue()
            self.interrupt_pending = True
            # The user is redirecting the model, not cancelling its work: an
            # in-flight shell wait is abandoned, and its command keeps running
            # under its job id.
            self.set_cancel_policy("detach")
            if not self.live_task.cancelling():
                self.live_task.cancel()
        self.prompts.put(text, mode, owner=owner)
        # Set immediately so Enter + Ctrl+C in one input batch cancels the
        # pending request rather than clearing the user's draft.
        self.activity.busy = True
        if mode == "steering" and self.turn_running():
            # Queued above, released here: the wait returns its handle and the
            # next model request carries this message.
            self.release_shell_waits()

    def command(self, text: str, tag: object = None) -> None:
        """Queue a slash command; `tag` travels with it back to the terminal."""
        idle = not self.activity.busy and not self.activity.queued_prompts
        self.commands.put_nowait((self.prompts.generation, text, idle, tag))
        self.command_idle.clear()
        if text.split()[0] in MODEL_COMMANDS:
            self.pending_model_command += 1
            self.activity.busy = True
        if mcp_enable(text):
            self.hold_later_steering()
            self.pending_mcp += 1
            # Enter + Ctrl+C in one input batch must cancel activation before
            # its command worker has had a chance to start OAuth.
            self.activity.busy = True

    def command_started(self, text: str) -> None:
        """A command from `command` is being handled: it no longer holds the session busy."""
        if text.split()[0] in MODEL_COMMANDS:
            self.pending_model_command -= 1
        elif mcp_enable(text):
            self.pending_mcp -= 1
        else:
            return
        self.refresh_busy()

    def command_finished(self) -> None:
        if self.commands_pending:
            self.activity.busy = True
        if self.commands.empty() and not self.startup_commands:
            self.command_idle.set()

    # --- Stopping ---

    def take_typed(self) -> tuple[list[str], list[tuple[str, str]]]:
        """Commands and messages (with their modes) the user left waiting, for the
        host this session moves into. Job wake-ups and resends stay behind: the
        host makes its own."""
        commands = []
        while not self.commands.empty():
            generation, text, _idle, _tag = self.commands.get_nowait()
            if generation == self.prompts.generation:
                commands.append(text)
        self.command_idle.set()
        messages = [
            (text, mode) for text, mode in self.prompts.take_all() if mode not in ("wake", "resend")
        ]
        return commands, messages

    def clear_queue(self) -> None:
        """Drop every queued message and pending command, saying what went."""
        self.startup_commands.clear()
        if self.commands.empty():
            self.command_idle.set()
        if self.pending_mcp:
            self.view.warning("Pending MCP enable command cancelled.")
            self.pending_mcp = 0
        if self.pending_model_command:
            self.view.warning("Pending model command cancelled.")
            self.pending_model_command = 0
        if count := self.prompts.clear():
            self.view.note(f"Cleared {count} queued message(s).")
        if self.steering_before_mcp is not None:
            self.steering_before_mcp = 0

    def cancel(self) -> None:
        """Ctrl+C: clear the queue and stop what is running."""
        self.interrupt_pending = False
        self.clear_queue()
        active = [task for task in self.tasks() if not task.done()]
        # Ctrl+C means "stop working", so a command the turn is waiting on is
        # stopped with it. A typed follow-up takes the other branch and only
        # abandons the wait. Either way a job the model explicitly backgrounded
        # keeps running: nothing is waiting on it to abandon.
        self.set_cancel_policy("stop")
        if active:
            # Repeated interrupts must not interrupt persistence/auth cleanup.
            # Side questions are deliberately parallel: an interrupt aimed at
            # the turn must not also throw away work the turn is not doing.
            for task in active:
                if not task.cancelling():
                    task.cancel()
        # Nothing in the foreground: stop what runs beside it, each one saying so.
        elif not (self.cancel_asides() + self.cancel_background_rename()):
            self.activity.busy = False
            # A command that ended the session cancels nothing the user ran.
            if self.running:
                self.view.cancelled()

    def cancel_asides(self) -> int:
        """Stop the running side questions alone; a running turn carries on."""
        if stopped := self.asides.cancel():
            self.view.note(f"Stopped {stopped} side question(s).")
        return stopped

    def cancel_background_rename(self) -> bool:
        """Ctrl+C with no turn running: a bare /rename's request is what is left to stop."""
        if stopped := self.cancel_rename():
            self.view.note("Stopped asking the model for a name.")
        return stopped

    def _steering_limit(self) -> int | None:
        # Like queued prompts, steering sent after MCP work waits for it, so
        # "/mcp enable x" then "use x" reaches the model with x's tools.
        if self.mcp_task is None and not self.pending_mcp:
            return None
        return self.steering_before_mcp

    def has_steering(self) -> bool:
        """Peek at deliverable input without consuming, displaying, or journaling it."""
        limit = self._steering_limit()
        return self.prompts.steering() > 0 and (limit is None or limit > 0)

    def take_steering(self) -> list[str]:
        """The runtime's hook: steering messages for the next model request."""
        limit = self._steering_limit()
        messages = self.prompts.take_steering(limit)
        self.steering_before_mcp = None if limit is None else limit - len(messages)
        for text in messages:
            self.activity.start_prompt(text)
            self.view.user(text)
        if messages:
            self.view.redraw()
        return messages

    def turn_ended(self, success: bool) -> None:
        """Settle the queue after a turn: a failure drops what was waiting behind it."""
        if not success and not self.interrupt_pending:
            self.clear_queue()
        self.interrupt_pending = False
        self.refresh_busy()

    async def consume_commands(self) -> None:
        """Run slash commands one at a time, in the order they were sent."""
        while self.running:
            generation, text, submitted_idle, tag = await self.commands.get()
            name = text.split(maxsplit=1)[0]
            try:
                if name not in FRONTEND_COMMANDS and not self.ready.is_set():
                    # Keep consuming frontend-only commands while backend
                    # commands wait, preserving their order for readiness.
                    self.startup_commands.append((generation, text, submitted_idle, tag))
                    continue
                if name in FRONTEND_COMMANDS or self.dispatchable(generation, name):
                    self.command_started(text)
                    await self.dispatch(text, idle=submitted_idle, tag=tag)
            except Exception as error:
                self.command_failed(name, error)
            finally:
                self.command_finished()
            # Every command taken off the queue reports here, dropped or not:
            # a host's `run` caller waits for exactly this.
            await self.view.after_command(tag)

    def dispatchable(self, generation: int, name: str) -> bool:
        """Whether a session command dequeued now should run; says why when it should not."""
        if generation != self.prompts.generation:
            return False  # Ctrl+C cleared the queue it was sent in.
        if self.startup_error is not None and name not in RECOVERY_COMMANDS:
            self.view.warning(STARTUP_FAILED)
            return False
        return True

    async def dispatch(self, text: str, *, idle: bool, tag: object = None) -> None:
        """Run one slash command: the session's own here, anything else in the terminal.

        `idle` says whether the session was idle when it was sent: /compact or
        /resend sent then runs ahead of prompts queued behind it since.
        """
        name = text.split(maxsplit=1)[0]
        before_queue = name in MODEL_COMMANDS and idle and not self.working()
        if (
            name in IDLE_COMMANDS
            and (self.activity.busy or self.activity.queued)
            and not before_queue
        ):
            self.view.warning(
                f"{name} is unavailable while working. "
                "Cancel with Ctrl+C or wait for the run to finish, then retry."
            )
        elif self.registry.find(name) is None:
            await self.view.run_command(text, idle=idle, tag=tag)
        else:
            # The terminal needs the tag for any popup the command opens.
            self.view.command_started(tag)
            try:
                await self.run_command(text, before_queue=before_queue)
            finally:
                self.view.command_finished()
        await self.follow_up()
        self.view.session_changed()

    async def follow_up(self) -> None:
        """Do what the command just handled asked for once its handler returned."""
        if self.job_requested is not None:
            await self.perform_job()
        if self.skill_requested is not None:
            if self.skill_mcp_requested is not None:
                skill, names = self.skill_mcp_requested
                self.skill_mcp_requested = None
                # Started before the prompt is queued: the consumer then waits
                # for it on mcp_idle.
                self.start_skill_mcp(skill, names)
            prompt, self.skill_requested = self.skill_requested, None
            queued, self.skill_queued = self.skill_queued, False
            # Queued like a typed message so send mode, steering, and
            # cancellation keep their usual meaning.
            mode = "queue" if queued else load_preferences().get("send_mode", "steering")
            self.prompts.put(prompt, mode)
            self.activity.busy = True
        if self.mcp_defaults_requested:
            self.start_mcp_defaults()
        # Not once the terminal shows another controller (/detach, /switch):
        # they share its activity, which this would reset under the new one.
        if not self.running and not self.superseded:
            self.cancel()
            if active := self.tasks():
                await asyncio.gather(*active, return_exceptions=True)
        if self.reload_requested:
            await self.reload_extensions()
        elif self.reload_pending and not (self.activity.busy or self.activity.queued_prompts):
            # Deferred by busy work that was not a turn, which `consume` would wait out.
            await self.apply_pending_reload()
        if self.login_requested:
            await self.perform_login()
        if self.logout_requested:
            await self.logins.perform_logout()

    async def perform_login(self) -> None:
        """Run the requested sign-in as cancellable work, so Ctrl+C abandons it.

        Waiting on a browser holds the session busy, which is what routes
        Ctrl+C to `cancel` rather than to the draft. A cancelled sign-in ends
        here; only the command loop's own cancellation propagates.
        """
        # Taken here, not in the task: a Ctrl+C landing before the task's first
        # step must still consume the request, or the next command would rerun it.
        source, self.login_requested = self.login_requested, None
        task = asyncio.create_task(self.logins.perform_login(source))
        self.login_task = task
        self.activity.busy = True
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            self.login_task = None
            self.refresh_busy()
        if not task.cancelled():
            task.result()

    async def run_command(self, text: str, *, before_queue: bool = False) -> None:
        """Run one of the session's own commands; a handler may be sync or async."""
        parts = text.strip().split(maxsplit=1)
        command = self.registry.find(parts[0])
        argument = parts[1].strip() if len(parts) > 1 else ""
        try:
            if argument and not command.free_arguments and argument not in command.arguments:
                usage = "|".join(command.arguments)
                raise ValueError(f"Usage: {command.name}" + (f" [{usage}]" if usage else ""))
            if before_queue:
                result = command.handler(argument, before_queue=True)
            else:
                result = command.handler(argument)
            if inspect.isawaitable(result):
                await result
        except ValueError as error:
            self.view.error(str(error))

    # --- Turns ---

    async def consume(self) -> None:
        """Run queued messages one at a time, each once nothing holds the queue."""
        await self.ready.wait()
        while self.running:
            await self.idle()
            item = await self.prompts.get()
            _generation, text, mode, owner = item
            if self.startup_error is not None:
                self.clear_queue()
                if owner is not None:
                    owner.dropped()
                self.activity.busy = False
                self.view.warning(STARTUP_FAILED)
                continue
            await self.idle()
            if not self.running:
                return
            if not self.prompts.current(item):
                # Cancelled while waiting for a command/modal. Fetched before
                # the clear, so the clear could not say so to its owner.
                if owner is not None:
                    owner.dropped()
                continue
            self.prompts.taken()
            # A model chosen mid-run takes effect here, before the request
            # that follows it is sent.
            if self.pending_model is not None:
                await self.apply_pending_model()
            await self.apply_pending_reload()
            if owner is not None:
                # Everything shown from here to `after_turn` is this message's.
                owner.started()
            success = True
            try:
                resend = mode == "resend"
                if mode == "shell":
                    self.live_task = asyncio.create_task(self.run_shell(text))
                    try:
                        success = await self.live_task
                    except asyncio.CancelledError:
                        if self.closing():
                            return
                        success = False
                elif not (resend or self.model):
                    self.view.preview_reply(text)
                else:
                    wake = mode == "wake"
                    if wake:
                        label, detail = wake_row(text)
                        self.activity.start_prompt(label, kind="system", detail=detail)
                    else:
                        self.activity.start_prompt(text)
                    self.runtime.take_steering = self.take_steering
                    self.runtime.has_steering = self.has_steering
                    self.live_task = asyncio.create_task(
                        self.run_turn(
                            text,
                            resend=resend,
                            wake=wake,
                        )
                    )
                    try:
                        success = await self.live_task
                    except asyncio.CancelledError:
                        # Cancellation before run_turn's first instruction.
                        if self.closing():
                            return
                        success = False
                        self.activity.finish_prompt("cancelled")
                        self.activity.turn_outcome = "cancelled"
                        self.view.cancelled()
            except Exception as error:
                self.view.error(error_message(error), title="Agent failed")
                self.activity.turn_outcome = "failed"
                success = False
            finally:
                self.live_task = None
            if self.closing():
                return
            self.turn_ended(success)
            # Adopt it as soon as the turn ends so the footer and /status
            # agree with what the next request will use.
            if self.pending_model is not None:
                await self.apply_pending_model()
            await self.apply_pending_reload()
            self.view.session_changed()
            await self.view.after_turn()

    async def stream_on(self, target: SideTarget, prompt: str):
        """Stream a turn of `prompt` on `target` alone, from `$MODEL` or `+EFFORT`.

        The conversation keeps its own model: only this turn's requests go to
        `target`, and the next prompt is back where /model left it. Resolving
        happens here, inside the turn, so a bad name fails it like any other
        turn failure.
        """
        if not prompt:
            raise ValueError("Nothing to send after the model; add the prompt after it.")
        self.check_effort(target)
        options = await self.target_options(target)
        if "model" in options:
            self.view.note(
                f"This prompt runs on {target.model}"
                + (f" at {target.effort} effort" if target.effort else "")
                + f"; the conversation stays on {self.model}. Another model starts "
                "without the conversation's prompt cache, so it pays for the whole prompt."
            )
        elif target.effort:
            self.view.note(f"This prompt runs at {target.effort} effort.")
        async with aclosing(self.runtime.stream(prompt, **options)) as stream:
            async for event in stream:
                yield event

    async def run_turn(
        self,
        text: str,
        *,
        resend: bool = False,
        wake: bool = False,
    ) -> bool:
        """Run a turn: `resend` asks again from the last checkpoint, and `wake` is one
        a finished job started, shown as a badge rather than as typed text."""
        runtime = self.runtime
        self.view.turn_started(text, echo=not wake)
        if wake:
            # Scrollback already carries the job's summary line; the prompt is
            # pcode's, so it is labelled as system work rather than quoted.
            label, detail = wake_row(text)
            self.activity.start_prompt(label, kind="system", detail=detail)
        else:
            self.activity.start_prompt(text)
        self.activity.status = "Waiting for model…"

        def compaction_notice(text):
            # The sentence goes to scrollback; the status row keeps to a phase
            # (see `status_parts`), and the model has the turn once it is done.
            starting = text.startswith("Compacting")
            self.activity.status = "Compacting context…" if starting else "Waiting for model…"
            self.view.note(text)

        runtime.compaction_notice = compaction_notice

        def retry_notice(text):
            # Separate abandoned partial text/thinking from the next attempt.
            self.activity.status = f"Retrying · {text}"
            self.view.turn_retry(text)

        if hasattr(runtime, "retry_notice"):
            runtime.retry_notice = retry_notice
        if hasattr(runtime, "warning_notice"):
            runtime.warning_notice = self.view.warning
        failure = None
        cancelled = False
        chosen, prompt = (None, text) if resend or wake else prompt_target(text)
        source = (
            self.stream_on(chosen, prompt)
            if chosen is not None
            else runtime.stream(None if resend else text)
        )
        # A turn creates the session's journal on first use. Attached terminals
        # read it from disk (/tools, /diffs), so they need its path mid-turn,
        # not only once the turn ends.
        session = getattr(runtime, "session", None)
        journaled = session is not None
        # The title is asked for beside the first turn rather than after it: the
        # prompt alone names it as well as prompt and reply do, and a long turn
        # left the session untitled for minutes. A session that existed already
        # (resumed, or after a first turn that failed) is named from its first
        # prompt on disk.
        first_prompt = prompt if session is None and not (resend or wake) else None
        if journaled:
            self.start_naming(first_prompt)
        try:
            async with aclosing(source) as stream:
                async for event in stream:
                    if not journaled and getattr(runtime, "session", None) is not None:
                        journaled = True
                        self.view.session_changed()
                        self.start_naming(first_prompt)
                    self.view.turn_event(event)
                    if (job_id := delivered_job(event)) is not None:
                        self.report_delivered_job(job_id)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as error:
            failure = error
        finally:
            # A turn that ended before its first event opened the session all the same.
            if getattr(runtime, "session", None) is not None:
                self.start_naming(first_prompt)
            self.view.turn_ended()
            # MCP work started beside the turn may outlast it.
            self.activity.status = self.mcp_status if self.mcp_task is not None else ""
        # Abandoning a wait is the exception, not the rule: restore the safe
        # default so the next Ctrl+C-free cancellation cannot kill a command.
        self.set_cancel_policy("detach")
        outcome = "cancelled" if cancelled else "failed" if failure else "done"
        self.activity.finish_prompt(outcome)
        self.activity.turn_outcome = outcome
        self.report_finished_jobs()
        self.view.redraw()
        if cancelled:
            self.view.cancelled()
            # A cancelled turn used to take its commands with it. Say plainly
            # what survived, so "still running" is never a surprise.
            registry = getattr(runtime, "jobs", None)
            running = registry.running() if registry is not None else []
            if running:
                self.view.note(
                    f"{len(running)} command(s) still running: "
                    + ", ".join(f"[{job.id}] {job.label()}" for job in running[:3])
                    + ". Use /jobs to list or stop them."
                )
        elif failure:
            from pcode.diagnostics import stale_install

            self.view.error(error_message(failure), title="Agent failed")
            if hint := stale_install():
                self.view.warning(hint)
        if (cancelled or failure) and runtime.session:
            # A cancel is something the user asked for, so it has nothing worth
            # pointing at. Name the traceback file rather than the directory it
            # sits in: the frames are the point of looking.
            if failure:
                directory = runtime.session.directory
                errors = directory / "errors.log"
                target = errors if errors.exists() else directory
                self.view.note(f"Session and diagnostics: {target}")
            if runtime.recovery_blocked:
                self.view.warning(runtime.recovery_blocked)
        return not (cancelled or failure)

    async def run_shell(self, text: str) -> bool:
        """Run a `!command` the user typed and hand its result to the runtime.

        Output streams into the live command panel while it runs and is
        mirrored to scrollback when it ends. The model sees it on the next
        prompt, as a `shell` tool call, so ask a follow-up to discuss it.
        """
        # Lazy: pcode.shell pulls in the agent stack, which startup avoids.
        from pcode.shell import preview_text

        command = shell_command(text)
        assert command is not None
        call_id = f"shell_mode_{id(self):x}"
        cwd, env = (
            self.runtime.shell_environment()
            if hasattr(self.runtime, "shell_environment")
            else (self.workspace, None)
        )
        self.view.user(text)
        self.activity.start_prompt(text)
        self.activity.user_command = True
        self.activity.status = "Running command…"
        buffered = ""

        def show(chunk: str) -> None:
            nonlocal buffered
            buffered += chunk
            self.view.turn_event(CommandOutput(call_id, command, preview_text(buffered)))

        run = None
        try:
            run = await execute(command, cwd=cwd, env=env, on_output=show)
        except asyncio.CancelledError:
            pass
        except OSError as error:
            self.view.error(str(error), title="Command failed to start")
        finally:
            self.view.drop_output(call_id)
            self.activity.user_command = False
            self.activity.status = ""
        if run is None:
            self.activity.finish_prompt("cancelled")
            self.view.warning("Command cancelled; the model was not told about it.")
            self.view.redraw()
            return False
        self.view.shell_result(
            command, run.output, failed=run.failed, elapsed_seconds=run.elapsed_seconds
        )
        if hasattr(self.runtime, "record_shell"):
            visible = await self.runtime.record_shell(run)
            reduced = not isinstance(visible, str) or visible != run.tool_result()
            self.view.note(
                "The model sees this command and its "
                + ("reduced output" if reduced else "output")
                + " with your next message."
            )
        self.activity.finish_prompt("done")
        self.view.redraw()
        return True

    # --- Work beside the turn loop that holds queued prompts back ---

    def start_mcp_task(self, name, coroutine, *, status: str, cancelled: str) -> None:
        """Run MCP work outside the model loop; queued prompts wait for it.

        A running turn carries on beside it and sees the change from its next
        model request. It keeps its status line and, should this fail, the
        messages queued behind it, which were not waiting on this change.
        """
        self.hold_later_steering()
        self.mcp_idle.clear()
        self.mcp_enabling = name
        self.mcp_status = status
        self.activity.busy = True
        beside_turn = self.turn_running()
        if not beside_turn:
            self.activity.status = status

        def finished(task):
            success = False
            try:
                task.result()
                success = True
            except asyncio.CancelledError:
                self.view.warning(cancelled)
            except Exception as error:
                self.report_mcp_error(name, error)
            finally:
                if not success and not beside_turn:
                    self.clear_queue()
                self.mcp_task = None
                self.refresh_busy()
                if not self.turn_running():
                    self.activity.status = ""
                self.mcp_enabling = None
                self.mcp_status = ""
                self.steering_before_mcp = None
                self.mcp_idle.set()
                self.view.redraw()
                self.view.session_changed()

        self.mcp_task = asyncio.create_task(coroutine)
        # A done callback also handles cancellation before the coroutine starts.
        self.mcp_task.add_done_callback(finished)

    def hold_later_steering(self) -> None:
        if self.steering_before_mcp is None:
            self.steering_before_mcp = self.prompts.steering()

    def cancels(self) -> str:
        """How to abandon MCP work, which Ctrl+C stops along with any turn."""
        return (
            "Ctrl+C cancels it and the running turn." if self.turn_running() else "Ctrl+C cancels."
        )

    def start_mcp_enable(self, name: str, *, save: bool = False) -> None:
        self.view.note(
            f"Enabling MCP '{name}'. OAuth sign-in happens now if needed; "
            f"{self.cancels()} No model request is made."
        )
        self.start_mcp_task(
            name,
            self.enable_mcp(name, save=save),
            status=f"Enabling MCP '{name}' — complete browser sign-in if prompted…",
            cancelled=f"MCP '{name}' sign-in cancelled; server remains off.",
        )

    def start_mcp_enable_all(self) -> None:
        """Enable every configured server not already on."""
        from pcode.mcp import configured_servers

        names = [
            name for name in sorted(configured_servers()) if name not in self.runtime.mcp.enabled
        ]
        if not names:
            self.view.note("Every configured MCP server is already enabled.")
            return
        self.view.note(
            f"Enabling MCP {', '.join(names)}. OAuth sign-in happens now if needed; "
            f"{self.cancels()} No model request is made."
        )
        self.start_mcp_task(
            ", ".join(names),
            self.enable_mcp_each(names),
            status="Enabling all MCP servers — complete browser sign-in if prompted…",
            cancelled="MCP enable cancelled; servers not yet enabled remain off.",
        )

    def start_skill_mcp(self, skill: str, names) -> None:
        """Enable what `skill` declares before its prompt, which waits on MCP work."""
        from pcode.mcp import config_path, configured_servers

        state = getattr(self.runtime, "mcp", None)
        if state is None:
            return
        try:
            configured = configured_servers()
        except ValueError as error:
            self.view.error(str(error))
            return
        if unknown := [name for name in names if name not in configured]:
            self.view.warning(
                f"The {skill} skill asks for MCP {', '.join(unknown)}, "
                f"not configured in {config_path()}."
            )
        wanted = [name for name in names if name in configured and name not in state.enabled]
        if not wanted:
            return
        # Same rule as /mcp enable: one MCP change at a time.
        if self.mcp_task is not None:
            listed = " ".join(f"`/mcp enable {name}`" for name in wanted)
            self.view.warning(
                f"The {skill} skill asks for MCP {', '.join(wanted)}, which cannot be "
                f"enabled while another MCP change is in progress. Run {listed} after it."
            )
            return
        self.view.note(
            f"Enabling MCP {', '.join(wanted)} for the {skill} skill. "
            f"OAuth sign-in happens now if needed; {self.cancels()}"
        )
        self.start_mcp_task(
            ", ".join(wanted),
            self.enable_mcp_each(wanted, reason=f" for the {skill} skill"),
            status=f"Enabling MCP for the {skill} skill — complete sign-in if prompted…",
            cancelled=f"MCP sign-in for the {skill} skill cancelled; its prompt was not sent.",
        )

    def start_mcp_defaults(self) -> None:
        from pcode.mcp import default_servers

        self.mcp_defaults_requested = False
        if getattr(self.runtime, "mcp", None) is None:
            return
        try:
            names = default_servers()
        except ValueError as error:
            self.view.error(str(error))
            return
        if not names:
            return
        self.start_mcp_task(
            "defaults",
            self.enable_mcp_defaults(names),
            status="Enabling default MCP servers…",
            cancelled="Default MCP enable cancelled; remaining servers stay off.",
        )

    def start_mcp_logout(self, name: str) -> None:
        self.start_mcp_task(
            name,
            self.logout_mcp(name),
            status=f"Signing out of MCP '{name}'…",
            cancelled=f"MCP '{name}' sign-out cancelled.",
        )

    def start_compact(self, focus: str) -> None:
        from pcode.ui import SYSTEM_COMMAND_LABELS

        self.start_history_task(
            self.runtime.compact(focus),
            # Label the work instead of echoing "/compact <focus>", which
            # reads like the command was typed as part of a prompt.
            label=SYSTEM_COMMAND_LABELS["/compact"],
            detail=focus,
            status="Compacting context…",
            note="Compacting context with the current model. Ctrl+C cancels.",
            done=lambda result: result.description(),
            cancelled="Compaction cancelled; history unchanged.",
            failed="Compaction failed",
        )

    def start_summary(self, request) -> None:
        # Checked before the task marks the session busy, which would refuse it.
        follows = self.check_bridge(request.thread)
        self.start_history_task(
            self.summarize_thread(follows, request.instructions),
            label="Summarizing side thread",
            detail=request.instructions,
            status="Summarizing side thread…",
            note="Summarizing the side thread into the conversation. Ctrl+C cancels.",
            done=lambda result: "Side thread summary added to the conversation.",
            cancelled="Summary cancelled; the conversation is unchanged.",
            failed="Side thread summary failed",
        )

    def start_history_task(
        self, work, *, label, detail, status, note, done, cancelled, failed
    ) -> None:
        """Run work that rewrites the conversation's history, holding prompts back.

        Prompts typed meanwhile queue behind it on `compact_idle`, the way a
        turn would, so nothing reads the history while it changes. `done`
        turns the result into the closing note.
        """
        self.compact_idle.clear()
        self.activity.busy = True
        self.activity.status = status
        self.activity.start_prompt(label, kind="system", detail=detail)
        self.view.note(note)

        def finished(task):
            success = False
            try:
                result = task.result()
                self.view.note(done(result))
                self.activity.finish_prompt("done")
                success = True
            except asyncio.CancelledError:
                self.activity.finish_prompt("cancelled")
                self.view.warning(cancelled)
            except Exception as error:
                self.activity.finish_prompt("failed")
                self.view.error(error_message(error), title=failed)
            finally:
                if not success:
                    self.clear_queue()
                self.compact_task = None
                self.refresh_busy()
                self.activity.status = ""
                self.compact_idle.set()
                self.view.redraw()
                self.view.session_changed()

        self.compact_task = asyncio.create_task(work)
        self.compact_task.add_done_callback(finished)

    # --- Jobs: a footer count, a watched tail, exits, and wake-ups ---

    async def watch_jobs(self) -> None:
        """Keep the job count current, and report exits once the turn is over.

        The count updates busy or idle and excludes completed jobs.
        Scrollback waits for idle, because a completion written mid-turn
        would land inside the model's streaming text.
        The model is told separately, at its next request, unless nothing
        is going to make one: then the job's notice starts the turn itself.
        Polling here costs one small file read per running job and
        replaces the model doing the same thing with `sleep`.
        """
        await self.ready.wait()
        self.adopt_jobs()
        while True:
            changed = False
            if not self.activity.busy and not self.activity.queued_prompts:
                changed = bool(self.report_finished_jobs())
                prompt = self.wake_prompt() if self.live_task is None else None
                if prompt is not None:
                    self.submit(prompt, "wake")
            # Refresh even while busy so the footer count stays current.
            if self.refresh_jobs() or changed:
                self.view.redraw()
            await asyncio.sleep(1)

    def refresh_jobs(self) -> bool:
        """Recompute the job count and watched tail. Returns whether they changed.

        Count every running job, even while a tool waits on it. Completion
        notices stay pending until the turn ends or the idle watcher reports them.
        """
        registry = getattr(self.runtime, "jobs", None)
        if registry is None:
            return False
        registry.refresh()
        count = sum(job.running for job in registry.jobs.values())
        changed = count != self.activity.job_count
        self.activity.job_count = count
        return self._refresh_watched(registry) or changed

    def _refresh_watched(self, registry) -> bool:
        from pcode.shell import preview_text

        watched = self.activity.watched_job
        job = registry.get(watched) if watched else None
        if job is None or not job.running:
            self.activity.watched_job = ""
            return self._pin(None)
        tail, _ = registry.read_output(job, max_bytes=OUTPUT_TAIL_BYTES // 2)
        return self._pin(
            CommandOutput(WATCHED_PREFIX + job.id, job.command, preview_text(tail, final=True))
        )

    def _pin(self, event: CommandOutput | None) -> bool:
        """Show `event` as the watched job's preview, replacing the last one."""
        if event == self.pinned:
            return False
        if self.pinned is not None and (event is None or event.call_id != self.pinned.call_id):
            self.view.drop_output(self.pinned.call_id)
        self.pinned = event
        if event is not None:
            self.view.show_output(event)
        return True

    def adopt_jobs(self) -> None:
        """Take over what an earlier pcode left running, and say so."""
        registry = getattr(self.runtime, "jobs", None)
        adopted = registry.adopt_orphans() if registry is not None else []
        if adopted:
            self.view.note(
                f"Adopted {len(adopted)} job{'s' if len(adopted) != 1 else ''} "
                "still running from an earlier pcode; /jobs lists them."
            )
            for job in adopted:
                self.view.note(job.summary())

    def jobs_arguments(self) -> tuple[str, ...]:
        """Complete `stop`/`watch` against jobs still running this session knows about."""
        registry = getattr(self.runtime, "jobs", None)
        if registry is None:
            return ()
        registry.refresh()
        running = sorted(
            (job for job in registry.jobs.values() if job.running), key=lambda job: job.started_at
        )
        return (
            "unwatch",
            "stop all",
            *(f"stop {job.id}" for job in running),
            *(f"watch {job.id}" for job in running),
        )

    async def jobs(self, argument: str) -> None:
        """Browse, watch, or stop the shell jobs this session started.

        Jobs outlive the turn that started them and, deliberately, the session
        itself, so the only way to know what is still running is to ask. Bare
        `/jobs` opens the browser; the subcommands act without it.
        """
        registry = getattr(self.runtime, "jobs", None)
        if registry is None:
            raise ValueError("/jobs requires a live model session.")
        registry.refresh()
        action, _, target = argument.partition(" ")
        # `list` predates the browser; it still opens it rather than failing.
        if not action or action == "list":
            if not registry.jobs:
                self.view.note("No jobs have been started.")
                return
            await self.view.browse_jobs()
            return
        if action == "unwatch":
            self.watch_job(None)
            return
        if action == "watch":
            job = registry.get(target.strip())
            if job is None:
                raise ValueError(f"No job {target.strip()!r}. Run /jobs to browse them.")
            if not job.running:
                raise ValueError(f"[{job.id}] has finished; nothing to watch.")
            self.watch_job(job.id)
            self.view.note(f"Watching [{job.id}] {job.label()}; /jobs unwatch hides it.")
            return
        if action != "stop":
            raise ValueError("/jobs takes stop ID, stop all, watch ID, or unwatch.")
        target = target.strip()
        if target == "all":
            self.stop_jobs(None)
        elif registry.get(target) is not None:
            self.stop_jobs([target])
        else:
            raise ValueError(f"No job {target!r}. Run /jobs to browse them.")

    def watch_job(self, job_id: str | None) -> None:
        """Pin a running job's output tail into the preview, or unpin with None."""
        self.activity.watched_job = job_id or ""
        self.refresh_jobs()

    def stop_jobs(self, job_ids: list[str] | None) -> None:
        """Stop these jobs, or every running one for None, and say so in scrollback."""
        registry = self.runtime.jobs
        jobs = None if job_ids is None else [registry.get(job_id) for job_id in job_ids]
        stopped = registry.stop_all([job for job in jobs if job] if jobs is not None else None)
        for job in stopped:
            # Printed here, so the idle watcher does not repeat it.
            job.announced.add("ui")
            self.view.note(f"Stopped [{job.id}] {job.label()}")
        if not stopped:
            self.view.note("Nothing was running.")
        # Now, not at the watcher's next tick: the rows answer this command.
        self.refresh_jobs()

    # --- MCP servers ---

    def mcp_arguments(self) -> tuple[str, ...]:
        from pcode.mcp import configured_servers

        state = getattr(self.runtime, "mcp", None)
        enabled = state.enabled if state else {}
        try:
            names = configured_servers()
        except ValueError:
            names = {}
        oauth = [
            name
            for name, raw in sorted(names.items())
            if isinstance(raw, dict) and raw.get("auth") == "oauth"
        ]
        default_on = {
            name for name, raw in names.items() if isinstance(raw, dict) and raw.get("enabled")
        }
        disable = []
        for name in sorted(enabled.keys() | default_on):
            if name in enabled:
                disable.append(f"disable {name}")
            if name in names:
                disable.append(f"disable {name} --save")
        return (
            "list",
            *(("enable-all",) if names else ()),
            *(
                item
                for name in sorted(names)
                for item in (f"enable {name}", f"enable {name} --save")
                if not (item.endswith("--save") and name in default_on and name in enabled)
            ),
            *disable,
            *(f"logout {name}" for name in oauth),
        )

    def mcp(self, argument: str) -> None:
        from pcode.mcp import config_path, configured_servers

        parts = argument.split()
        save = parts[-1:] == ["--save"]
        if save:
            parts = parts[:-1]
        state = getattr(self.runtime, "mcp", None)
        enabled = state.enabled if state else {}
        if not save and (not parts or parts == ["list"]):
            self.view.note(f"MCP config: {config_path()}")
            try:
                names = configured_servers()
            except ValueError as error:
                self.view.error(str(error))
                names = {}
            for name in sorted(names.keys() | enabled.keys()):
                status = "enabled" if name in enabled else "off"
                raw = names.get(name)
                if isinstance(raw, dict) and raw.get("enabled") is True:
                    status += " (default on)"
                self.view.note(f"{name}: {status}")
            if not names and not enabled:
                self.view.note("No MCP servers configured. Add an mcpServers object here.")
            self.view.note(
                'MCP defaults to off unless a server sets "enabled": true. '
                "Use /mcp enable NAME, /mcp enable-all, /mcp disable NAME, or /mcp logout NAME. "
                "Add --save to enable or disable to change the default too."
            )
            return
        if (parts != ["enable-all"] or save) and (
            len(parts) != 2
            or parts[0] not in ({"enable", "disable"} if save else {"enable", "disable", "logout"})
        ):
            raise ValueError(
                "Usage: /mcp list | /mcp enable NAME [--save] | /mcp enable-all "
                "| /mcp disable NAME [--save] | /mcp logout NAME"
            )
        # Slash commands precede queued prompts, and queued and steering
        # messages wait for MCP work, so an enable + prompt submitted in one
        # input batch authenticates first. A running turn sees the change from
        # its next model request.
        if self.mcp_enabling:
            raise ValueError("Another MCP change is still in progress. Wait for it, then retry.")
        if state is None:
            raise ValueError("MCP requires a live model. Start pcode with -m PROVIDER:MODEL.")
        if parts == ["enable-all"]:
            self.start_mcp_enable_all()
            return
        action, name = parts
        if action == "enable":
            if name not in enabled:
                self.start_mcp_enable(name, save=save)
                return
            self.view.note(f"MCP '{name}' is already enabled.")
        elif action == "logout":
            self.start_mcp_logout(name)
            return
        elif name in enabled or not save:
            state.disable(name)
            self.view.note(
                f"MCP '{name}' disabled{self.mid_turn()}. Earlier results remain in history."
            )
        if save:
            self.save_mcp_default(name, action == "enable")

    def save_mcp_default(self, name: str, enabled: bool) -> None:
        from pcode.mcp import config_path, save_default

        try:
            save_default(name, enabled)
        except Exception as error:
            # Best effort after the change itself succeeded: never report it as a failure.
            self.view.warning(f"Could not save the MCP default ({error}); this applies only here.")
            return
        default = "on in every new conversation" if enabled else "off by default"
        self.view.note(f"MCP '{name}' is now {default} ({config_path()}).")

    async def enable_mcp(self, name: str, *, save: bool = False) -> None:
        """Authorize outside the model loop; publish only a successfully enabled server."""
        await self.runtime.mcp.enable(name)
        self.view.note(
            f"MCP '{name}' enabled for this conversation{self.mid_turn()}. "
            "Its tools can perform actions with the server's permissions. "
            "OAuth sign-ins are saved for future sessions; /mcp logout NAME forgets one."
        )
        if save:
            self.save_mcp_default(name, True)

    def mid_turn(self) -> str:
        """Says when an MCP change reaches a turn already running."""
        return ", from the running turn's next model request" if self.turn_running() else ""

    def report_mcp_error(self, name: str, error: Exception) -> None:
        """Keep MCP setup tracebacks even though no model turn was started."""
        from pcode.diagnostics import error_report
        from pcode.mcp import error_message

        self.view.error(f"MCP '{name}' remains off: {error_message(error)}")
        saved = getattr(self.runtime, "session", None)
        path = saved.record_error(error, run_id=f"mcp:{name}") if saved else None
        if path is not None:
            self.view.note(f"MCP diagnostics: {path}")
        else:
            # --no-save and failed writes still need actionable frames, but must
            # not create a session or silently persist a separate diagnostics file.
            self.view.note(error_report(error))

    async def enable_mcp_each(self, names: list[str], *, reason: str = "") -> None:
        """Enable servers in order; one that fails stays off, the rest proceed."""
        for name in names:
            try:
                await self.runtime.mcp.enable(name)
            except Exception as error:
                self.report_mcp_error(name, error)
            else:
                self.view.note(f"MCP '{name}' enabled{reason}{self.mid_turn()}.")

    async def enable_mcp_defaults(self, names: list[str]) -> None:
        """Enable `"enabled": true` servers, using saved sign-ins but never a browser."""
        from pcode.mcp_oauth import SignInRequired

        for name in names:
            try:
                await self.runtime.mcp.enable(name, interactive=False)
            except SignInRequired:
                self.view.warning(f"MCP '{name}' needs a browser sign-in; run /mcp enable {name}.")
            except Exception as error:
                self.report_mcp_error(name, error)
            else:
                self.view.note(f"MCP '{name}' enabled (default on).")

    async def logout_mcp(self, name: str) -> None:
        await self.runtime.mcp.forget(name)
        self.view.note(
            f"MCP '{name}' signed out and disabled. The next /mcp enable {name} opens a "
            "browser. This does not revoke the server-side grant."
        )

    # --- Preferences the session saves (model, effort, autocompact) ---

    def persist_defaults(self, **updates: str) -> None:
        try:
            save_preferences(**updates)
        except OSError, ValueError:
            self.view.warning("Could not save defaults; this selection applies only here.")

    def forget_defaults(self, *keys: str) -> None:
        from pcode.preferences import update_preferences

        try:
            update_preferences({}, remove=keys)
        except OSError, ValueError:
            self.view.warning("Could not update defaults; this change applies only here.")

    # --- History: compaction and resending ---

    def compact(self, argument: str, *, before_queue: bool = False) -> None:
        if not self.model or not hasattr(self.runtime, "compact"):
            raise ValueError("/compact requires a live model session.")
        if not before_queue and (self.activity.busy or self.activity.queued_prompts):
            raise ValueError("/compact is unavailable while working. Cancel or wait, then retry.")
        self.start_compact(argument)

    def autocompact(self, argument: str) -> None:
        """`on`/`off` toggle; `200k` caps when it fires; `auto` drops the cap."""
        if not self.model or not hasattr(self.runtime, "auto_compact"):
            raise ValueError("/autocompact requires a live model session.")
        argument = argument.lower()
        # Allowed mid-turn: AutoCompaction reads both settings before each request.
        if argument:
            from pcode.compaction import effective_window
            from pcode.preferences import parse_token_count

            limit = None if argument in {"on", "off", "auto"} else parse_token_count(argument)
            enabling = argument == "on" or limit is not None
            if enabling and effective_window(self.runtime.agent.model) is None:
                raise ValueError(
                    "Unknown context window. Set PCODE_CONTEXT_WINDOW to the deployment's "
                    "token limit before enabling automatic compaction."
                )
            if argument == "auto":
                self.runtime.auto_compact_limit = None
                self.forget_defaults("autocompact_tokens")
            elif limit is not None:
                self.runtime.auto_compact_limit = limit
                self.persist_defaults(autocompact_tokens=str(limit))
            if enabling:
                # Setting a cap means you want compaction, so it also turns it on.
                self.runtime.auto_compact = True
                self.persist_defaults(autocompact="on")
            elif argument == "off":
                self.runtime.auto_compact = False
                self.persist_defaults(autocompact="off")
        self.view.flash(
            f"Automatic compaction: {self.autocompact_state()}. "
            "Usage: /autocompact on|off|200k|auto"
        )

    def compact_cap(self) -> int | None:
        """The token cap automatic compaction honors right now, if any."""
        if not getattr(self.runtime, "auto_compact", False):
            return None
        return getattr(self.runtime, "auto_compact_limit", None)

    def autocompact_state(self) -> str:
        from pcode.context_usage import compact_tokens

        if not getattr(self.runtime, "auto_compact", False):
            return "off"
        cap = self.compact_cap()
        return f"on at {compact_tokens(cap)}" if cap else "on (auto threshold)"

    def resend(self, argument: str, *, before_queue: bool = False) -> None:
        """Ask again from the settled checkpoint instead of typing "continue"."""
        if argument:
            raise ValueError("/resend takes no arguments.")
        if not self.model or not hasattr(self.runtime, "resend_prompt"):
            raise ValueError("/resend requires a live model session.")
        if not before_queue and (self.activity.busy or self.activity.queued_prompts):
            raise ValueError("/resend is unavailable while working. Cancel or wait, then retry.")
        previous = self.runtime.resend_prompt()
        # Sent idle, before any prompts now queued behind it: keep that order.
        self.prompts.put(previous, "resend", first=True)
        self.activity.start_prompt(previous)
        self.activity.busy = True

    # --- The session: its runtime, model, effort, extensions, and skills ---

    def register_skills(self) -> None:
        """Expose discovered SKILL.md assets as commands, skipping any collision."""
        from pcode.skills import discover_skills, skill_commands

        self.skill_command_names: list[str] = []
        for command in skill_commands(discover_skills(self.workspace), self.run_skill):
            names = (command.name, *command.aliases)
            # Bare names can collide with a built-in command; built-ins win, and
            # the prefixed form still reaches the skill.
            taken = [name for name in names if self.command_taken(name)]
            if command.name in taken:
                continue
            if taken:
                command = replace(
                    command, aliases=tuple(name for name in command.aliases if name not in taken)
                )
            self.registry.register(command)
            self.skill_command_names.append(command.name)

    def run_skill(self, skill, argument: str) -> None:
        from pcode.skills import skill_prompt

        if not self.model:
            raise ValueError(f"/skill:{skill.name} requires a live model session.")
        self.skill_requested = skill_prompt(skill, argument)
        if skill.mcp_servers:
            self.skill_mcp_requested = (skill.name, skill.mcp_servers)

    # --- Extensions and sub-agents ---

    def register_extension_commands(self) -> None:
        """Expose extension commands, replacing the previous load's; built-ins win."""
        for name in self.extension_command_names:
            self.registry.unregister(name)
        self.extension_command_names = []
        if self.extensions is None:
            return
        for extension in self.extensions.extensions:
            for command in extension.commands:
                names = (command.name, *command.aliases)
                if taken := [name for name in names if self.command_taken(name)]:
                    self.view.warning(
                        f"Extension {extension.name}: {', '.join(taken)} already exists; skipped."
                    )
                    continue
                self.registry.register(replace(command, handler=self.prompting(command)))
                self.extension_command_names.append(command.name)
        self.view.commands_changed()

    def prompting(self, command):
        """Send the text an extension command returns as a prompt, like a skill.

        A sync handler stays sync, so callers that never await still run it whole.
        """

        def request(result) -> None:
            if isinstance(result, str) and result.strip():
                if not self.model:
                    raise ValueError(f"{command.name} requires a live model session.")
                self.skill_requested = result
                self.skill_queued = True

        def run(argument: str):
            result = command.handler(argument)
            if not inspect.isawaitable(result):
                return request(result)

            async def finish() -> None:
                request(await result)

            return finish()

        return run

    def extension_arguments(self) -> tuple[str, ...]:
        """Complete `on`/`off` against the extensions this workspace discovered."""
        found = self.extensions.extensions if self.extensions else ()
        return (
            "list",
            *(f"on {e.name}" for e in found if not e.enabled),
            *(f"off {e.name}" for e in found if e.enabled),
        )

    def manage_extensions(self, argument: str) -> None:
        """`/extensions` lists what loaded; `on NAME` / `off NAME` change it and reload."""
        from pcode.ext import PROJECT_DIR, set_enabled, user_extension_dir
        from pcode.project_trust import is_trusted

        if not self.model:
            raise ValueError("/extensions requires a live model session.")
        parts = argument.split()
        if parts and parts != ["list"]:
            if len(parts) != 2 or parts[0] not in {"on", "off"}:
                raise ValueError("Usage: /extensions [list] | /extensions on|off NAME")
            action, name = parts
            known = {e.name for e in self.extensions.extensions} if self.extensions else set()
            if name not in known:
                listing = f" Known: {', '.join(sorted(known))}" if known else ""
                raise ValueError(f"Unknown extension '{name}'.{listing}")
            # Refuse before writing, so the preference cannot drift from the session.
            self.reload("")
            set_enabled(name, action == "on")
            self.view.note(f"Extension '{name}' turned {action}; reloading.")
            return
        lines = self.extensions.report(self.workspace) if self.extensions else []
        if not lines:
            lines = ["No extensions found."]
        lines.append(f"User extensions: {user_extension_dir()}")
        lines.append(
            f"Project extensions ({PROJECT_DIR}): "
            + (
                "on (repository trusted)"
                if is_trusted(self.workspace)
                else "off; answer the launch prompt or /config set project_extensions on"
            )
        )
        lines.append("Turn one on or off with /extensions on|off NAME. Ask pcode to write one.")
        self.view.note("\n".join(lines))

    async def subagents(self, argument: str) -> None:
        """`/subagents` lists the models delegate_task may pick; `MODEL ...` or `off` sets them."""
        from pcode.agent import subagent_menu

        names = list(dict.fromkeys(argument.split()))
        project = from_project("subagent_models")
        if not names:
            configured = subagent_models()
            if not configured:
                self.view.note(
                    "No sub-agent models: sub-agents run on the session's model. "
                    "/subagents MODEL [MODEL ...] lists others for delegate_task; it can also take "
                    "any provider:model name per delegation."
                )
                return
            menu, problems = await asyncio.to_thread(subagent_menu, configured)
            lines = [
                "Sub-agent models listed for delegate_task (without one, the session's model)"
                + (", set by this workspace's .pcode/preferences.json" if project else "")
                + ":",
                *(f"  {name}" for name in menu),
                *(f"Unavailable, left out: {problem}" for problem in problems),
                "/subagents MODEL [MODEL ...] replaces the list; /subagents off clears it.",
            ]
            self.view.note("\n".join(lines))
            return
        if not self.model:
            raise ValueError("/subagents requires a live model session.")
        if project:
            # A saved user choice would change nothing while the overlay decides.
            raise ValueError(
                "This workspace's .pcode/preferences.json sets subagent_models; change it "
                "with pcode config project set|unset subagent_models."
            )
        if names == ["off"]:
            names = []
        else:
            # Resolve before saving, so an unknown provider or a missing login is
            # refused here rather than silently left out of the menu.
            _, problems = await asyncio.to_thread(subagent_menu, names)
            if problems:
                raise ValueError("\n".join(problems))
        # Refuse before writing, so the preference cannot drift from the session.
        self.reload("")
        try:
            save_preferences(subagent_models=",".join(names))
        except (OSError, ValueError) as error:
            # The reload reads the saved list, so without it nothing would change.
            self.reload_requested = False
            raise ValueError(f"Could not save subagent_models: {error}") from error
        # Resolving checks only the provider and login, not the model id, and the
        # catalog is not exhaustive: flag what it does not know without refusing.
        known = set(await asyncio.to_thread(self.model_suggestions))
        if unknown := [name for name in names if name not in known]:
            self.view.warning(
                f"Not in the /model catalog, so check the spelling: {', '.join(unknown)}"
            )
        chosen = ", ".join(names) if names else "none; sub-agents run on the session's model"
        self.view.note(f"Sub-agent models: {chosen}. Reloading.")

    def model_list_completions(self, argument: str):
        """Complete the word being typed in `/subagents` from the /model catalog.

        Names already typed are not offered again, and `off` only as the first word.
        """
        from prompt_toolkit.completion import Completion

        words = argument.split()
        fragment = words.pop() if words and not argument[-1].isspace() else ""
        if words[:1] == ["off"]:
            return
        if not words and "off".startswith(fragment):
            yield Completion(
                "off", start_position=-len(fragment), display_meta="Only the session's model"
            )
        needle = fragment.casefold()
        for model in self.model_suggestions():
            if model not in words and needle in model.casefold():
                yield Completion(model, start_position=-len(fragment))

    def reload(self, argument: str) -> None:
        if argument:
            raise ValueError("/reload takes no arguments.")
        self._check_reloadable()
        if self.activity.busy or self.activity.queued_prompts:
            raise ValueError("/reload is unavailable while working. Cancel or wait, then retry.")
        self.reload_requested = True

    def _check_reloadable(self) -> None:
        if not self.model or not hasattr(self.runtime, "replace_agent"):
            raise ValueError("/reload requires a live model session.")

    def request_extension_reload(self) -> None:
        """An extension's `request_reload`: now when idle, else once the running turn ends.

        Unlike a typed /reload, the extension has usually just changed what its
        `setup` contributes (`/browser attach`), so refusing mid-turn would
        only make the user retype it. The agent is never swapped under a
        request in flight: `consume` applies it between turns.
        """
        self._check_reloadable()
        if self.activity.busy or self.activity.queued_prompts:
            if not self.reload_pending:
                self.reload_pending = True
                self._extension_notice("Extensions reload once the running work finishes.", "info")
        else:
            self.reload_requested = True

    async def apply_pending_reload(self) -> None:
        """Run a reload an extension asked for mid-turn, now that no request is in flight."""
        if not self.reload_pending:
            return
        try:
            await self.reload_extensions()
        except Exception as error:
            self.view.error(error_message(error), title="Reload failed")

    async def reload_extensions(self) -> None:
        """Re-import every extension and rebuild the agent around the same conversation."""
        from pcode.agent import create_agent

        self.reload_requested = False
        self.reload_pending = False
        loaded = await asyncio.to_thread(self._load_extensions)
        # Construct first, so a failure leaves the previous agent in place.
        agent = await asyncio.to_thread(
            create_agent, self.model, self.workspace, loaded.capabilities, loaded.subagents
        )
        apply_effort(agent, self.model, effort_for(self.model))
        apply_thinking(agent, self.model, self.activity.thinking_mode)
        self.extensions = loaded
        self.runtime.replace_agent(agent)
        await self.runtime.refresh_context()
        self.register_extension_commands()
        count = len(loaded.extensions) - len(loaded.failed) - len(loaded.disabled)
        summary = f"Reloaded {count} extension{'s' if count != 1 else ''}"
        if loaded.disabled:
            summary += f", {len(loaded.disabled)} off"
        if loaded.failed:
            summary += f", {len(loaded.failed)} failed"
        # A changed tool list or instruction invalidates the cached prompt prefix.
        self.view.note(f"{summary}. The next request rebuilds the prompt cache.")
        for line in loaded.report(self.workspace):
            self.view.note("Extension " + line)

    def _extension_notice(self, text: str, level: str) -> None:
        """Route an extension's notice to the transcript from any thread."""
        show = {"warning": self.view.warning, "error": self.view.error}.get(level, self.view.note)
        loop = self._loop
        if (
            loop is not None
            and loop.is_running()
            and threading.current_thread() is not threading.main_thread()
        ):
            loop.call_soon_threadsafe(show, text)
        else:
            show(text)

    def _load_extensions(self, workspace: Path | None = None):
        from pcode.ext import ExtensionUI, load_extensions

        return load_extensions(
            workspace or self.workspace,
            ExtensionUI(self._extension_notice, self.request_extension_reload),
            session_dir=self.session_dir,
        )

    # --- Runtime and model switching ---

    def _create_runtime(self):
        """Import and construct the backend off the terminal's event loop."""
        from pcode.agent import create_agent
        from pcode.live import AgentRuntime

        self.extensions = self._load_extensions()
        return AgentRuntime(
            create_agent(
                self.model,
                self.workspace,
                self.extensions.capabilities,
                self.extensions.subagents,
            ),
            self._saved_session,
            session_factory=self._create_session if self.save_sessions else None,
        )

    def _create_session(self, model: str | None = None):
        from pcode.sessions import SavedSession

        identity, self._session_id = self._session_id, None
        return SavedSession.create(
            model or self.model, self.workspace, self.session_dir, identity=identity
        )

    async def warn_outdated(self) -> None:
        """Warn when PyPI has a newer release. Run as a background task: it may hit the network."""
        from pcode.update_check import check_in_background

        if notice := await check_in_background():
            self.view.warning(notice)

    async def switch_model(self, model: str) -> None:
        """Adopt a model now, or record it for the next request while working."""
        if self.activity.busy or self.activity.queued_prompts:
            # Replacing the agent mid-run would change the model of a request
            # that is already in flight. Defer like /effort instead of refusing.
            if model == self.model:
                self.pending_model = None
                self.persist_defaults(model=model)
                self.view.note(f"Already using {model}.")
                return
            self.pending_model = model
            self.view.note(
                f"Model: {model} (next request). This turn finishes on {self.model or 'preview'}."
            )
            return
        await self.activate_model(model)

    async def apply_pending_model(self) -> None:
        """Adopt a model chosen mid-run, now that no request is in flight."""
        model, self.pending_model = self.pending_model, None
        if model is None:
            return
        try:
            await self.activate_model(model)
        except Exception as error:
            self.view.error(error_message(error), title="Model unchanged")

    async def activate_model(self, model: str) -> None:
        from pcode.agent import create_agent
        from pcode.live import AgentRuntime

        self.pending_model = None
        recovering = self.startup_error is not None
        if model == self.model and not recovering:
            self.persist_defaults(model=model)
            self.view.note(f"Already using {model}.")
            return
        if recovering and self.extensions is None:
            self.extensions = await asyncio.to_thread(self._load_extensions)
        # Construct first: a missing provider/login must leave the old session intact.
        capabilities = self.extensions.capabilities if self.extensions else ()
        subagents = self.extensions.subagents if self.extensions else ()
        agent = await asyncio.to_thread(
            create_agent, model, self.workspace, capabilities, subagents
        )
        apply_effort(agent, model, effort_for(model))
        apply_thinking(agent, model, self.activity.thinking_mode)
        save = self.save_sessions or getattr(self.runtime, "session_factory", None) is not None
        factory = (lambda: self._create_session(model)) if save else None
        if recovering:
            await self._finish_startup(AgentRuntime(agent, self._saved_session))
        if isinstance(self.runtime, AgentRuntime):
            saved = self.runtime.session
            if saved is not None:
                previous_model = saved.info.model
                saved.info.model = model
                try:
                    saved.save_info()
                except OSError:
                    saved.info.model = previous_model
                    raise
            self.runtime.replace_agent(agent)
            self.runtime.session_factory = factory
        else:
            self.runtime = AgentRuntime(agent, session_factory=factory)
        self.model = model
        await self.runtime.refresh_context()
        self.persist_defaults(model=model)
        self.save_sessions = save
        if recovering:
            self.startup_error = None
            self.mcp_defaults_requested = True
            if self.resuming and not self.conversation_shown:
                # The state first, so a host's terminals know which journal to read.
                self.view.session_changed()
                self.view.replay_conversation()
                self.conversation_shown = True
        self.view.note(f"Model: {model}. Continuing the current conversation.")
        self.show_startup_context()
        self.warn_without_credentials()

    # --- Effort ---

    def current_effort(self) -> str:
        if not self.model:
            return "n/a"
        from pcode.preferences import current_effort

        return current_effort(getattr(self.runtime, "agent", None), self.model)

    def effort(self, argument: str) -> None:
        value = argument.strip().lower()
        agent = getattr(self.runtime, "agent", None)
        gated = agent is not None and effort_setting(self.model, agent.model) is None
        if not value:
            self.view.flash(
                effort_unavailable(self.model)
                if gated
                else f"Effort: {self.current_effort()}. "
                "Usage: /effort low|medium|high|xhigh|default"
            )
            return
        if value not in ("low", "medium", "high", "xhigh", "default"):
            self.view.flash("Usage: /effort low|medium|high|xhigh|default")
            return
        if agent is None:
            self.view.flash("Effort is unavailable until the agent has started.")
            return
        # `default` clears a level saved before the model was known to be gated,
        # so it stays allowed where setting one does not.
        if gated and value != "default":
            self.view.flash(effort_unavailable(self.model))
            return
        # Replace rather than mutate: an active run keeps its captured settings.
        apply_effort(agent, self.model, value)
        self.persist_defaults(model=self.model)
        # Per model: raising effort on one model must not raise it on the next.
        try:
            save_model_effort(self.model, value)
        except OSError, ValueError:
            self.view.warning("Could not save defaults; this selection applies only here.")
        if gated:
            self.view.flash(f"Cleared the saved effort; {self.model} has no effort control.")
            return
        self.view.flash(f"Effort: {self.current_effort()} (next turn).")

    def adjust_effort(self, direction: int) -> None:
        levels = ("low", "medium", "high", "xhigh")
        current = self.current_effort()
        # The provider default is unspecified; use medium as the starting point.
        index = levels.index(current) if current in levels else 1
        self.effort(levels[max(0, min(len(levels) - 1, index + direction))])

    # --- Session overviews ---

    def session_overview(self) -> list[tuple[str, str]]:
        """Label/value rows describing the live conversation.

        One source for the `/status` notes and popup, so the
        two can never drift into describing the same session differently.
        """
        if not self.model:
            return [
                ("Model", "none · tools: none · network: none"),
                ("Preview turns", str(self.runtime.turns)),
                ("Mode", "Canned replies only. Start with -m PROVIDER:MODEL for a real agent."),
            ]
        totals = self.runtime.totals
        saved = self.runtime.session
        # What the session is called leads: it is what tells sessions apart,
        # and below the overhead rows it falls off a short terminal.
        rows = [
            *([("Name", saved.info.name)] if saved and saved.info.name else []),
            *([("Title", saved.info.title)] if saved and saved.info.title else []),
            ("Model", self.model),
            ("Effort", self.current_effort()),
            ("Workspace", str(self.workspace)),
            ("Turns", str(self.runtime.turns)),
            ("Tokens in/out", f"{self.runtime.input_tokens}/{self.runtime.output_tokens}"),
            # A cache read costs a fraction of an uncached token and a write costs
            # more than one, so the split is the part worth watching.
            (
                "Input cached read/write",
                f"{totals.cache_read}/{totals.cache_write} (uncached {totals.uncached_input})",
            ),
            ("Tools", "Coder tools enabled; no sandbox."),
            *self.overhead_overview(),
            (
                "Automatic compaction",
                self.autocompact_state() + " · /compact [focus] · /autocompact on|off|200k|auto",
            ),
        ]
        if saved:
            rows += [
                ("Session", saved.info.id),
                ("Saved in", str(saved.directory)),
                ("Started", saved.info.created[:16]),
                ("Updated", saved.info.updated[:16]),
            ]
        elif self.runtime.session_factory is not None:
            rows.append(("Session", "Will be saved after your first prompt."))
        else:
            rows.append(("Session", "Saving disabled; in memory only."))
        tree = getattr(self.runtime, "tree", None)
        if tree and tree.nodes:
            rows.append(("Branches", f"{len(tree.nodes)} turns in /tree"))
        mcp = getattr(self.runtime, "mcp", None)
        if enabled := sorted(getattr(mcp, "enabled", ()) or ()):
            rows.append(("MCP", ", ".join(enabled)))
        return rows

    def overhead_overview(self) -> list[tuple[str, str]]:
        """Attribute the fixed part of the prompt: instructions, assets, tool schemas.

        Read from the last request rather than re-derived, so the rows describe
        what the provider was actually sent. Nothing is available before the
        first request, where the alternative would be a parallel guess at a
        system prompt only the agent flow can resolve.
        """
        from pcode.context_breakdown import overhead_rows
        from pcode.context_usage import context_window
        from pcode.model_metadata import ContextWindowError

        parameters = getattr(self.runtime, "request_parameters", None)
        if parameters is None:
            return [("Prompt overhead", "Measured on the first model request.")]
        try:
            resolved = getattr(getattr(self.runtime, "agent", None), "model", None)
            window = context_window(resolved or self.model)
        except ContextWindowError:
            window = None
        if window and (cap := self.compact_cap()):
            # Measured against the budget compaction actually leaves, as the footer is.
            window = min(window, cap)
        return overhead_rows(parameters, window=window)

    # --- Slow command jobs ---

    def defer(self, label: str, detail: str, job: Callable[[], list[str]]) -> None:
        """Run a slow command's work under a system badge, or inline without a terminal.

        Handlers run on the terminal's event loop, so a job that takes seconds
        would freeze the screen with nothing to show for it. With a live
        terminal the work is picked up by the command loop, which paints a
        `◈ label ▸ detail` row (distinct from a model turn) and runs the job
        in a thread. The job returns lines for the transcript; a ValueError
        becomes the usual command error. A job started mid-turn leaves the
        turn's live row alone and says what it is doing in a notice instead.
        """
        if not self.interactive:
            self._report(job())
            return
        self.job_requested = (label, detail, job)

    def _report(self, lines: list[str]) -> None:
        """Show a job's lines as one note, so its own indentation sets the hierarchy."""
        if lines:
            self.view.note("\n".join(lines))

    async def perform_job(self) -> None:
        assert self.job_requested is not None
        label, detail, job = self.job_requested
        self.job_requested = None
        if self.activity.prompt_state == "running":
            await self._perform_job_alongside(label, detail, job)
            return
        self.activity.busy = True
        self.activity.start_prompt(label, kind="system", detail=detail)
        self.view.redraw()
        state = "failed"
        try:
            lines = await asyncio.to_thread(job)
            state = "done"
        except ValueError as error:
            self.view.error(str(error))
        else:
            self._report(lines)
        finally:
            self.activity.finish_prompt(state)
            self.activity.busy = bool(self.activity.queued_prompts)
            self.view.redraw()

    async def _perform_job_alongside(
        self, label: str, detail: str, job: Callable[[], list[str]]
    ) -> None:
        """Run a job beside a live turn without taking over or ending its row."""
        self.activity.flash(f"{label} \u25b8 {detail}\u2026" if detail else f"{label}\u2026")
        try:
            lines = await asyncio.to_thread(job)
        except ValueError as error:
            self.view.error(str(error))
        else:
            self._report(lines)
        finally:
            self.activity.notice = ""
            self.view.redraw()

    def usage(self, argument: str) -> None:
        from pcode.usage import usage_report

        self.defer("Checking usage", "", usage_report)

    # --- Worktrees ---

    def worktree(self, argument: str) -> None:
        from pcode import worktree

        action = argument or "status"
        if action == "list":
            self.view.note(worktree.listing(self.workspace) or "Not a git repository.")
            return
        if action == "clean":
            # Works from the mainline too, where the leftovers are most visible.
            self.defer("Cleaning worktrees", "", lambda: worktree.clean(self.workspace))
            return
        linked = worktree.describe(self.workspace)
        if linked is None:
            self.view.note(
                f"{self.workspace} is not a linked worktree. Start one with "
                "`pcode --worktree` or `/config set worktree on`."
            )
            return
        if action == "status":
            dirty = worktree.is_dirty(linked.path)
            count = worktree.unmerged_commits(linked)
            self.view.note(f"Worktree: {linked.path} (branch {linked.branch})")
            mainline = worktree.mainline_branch(linked.main)
            self.view.note(f"Mainline: {linked.main} ({mainline})")
            self.view.note(
                f"{count} unmerged commit{'s' if count != 1 else ''}"
                + (", uncommitted changes" if dirty else "")
            )
            return
        if action == "merge":
            # Allowed mid-turn: merge refuses a dirty tree, so it never runs
            # over uncommitted edits the model has in flight.
            self.defer("Merging worktree", linked.branch, lambda: [worktree.merge(linked)])
            return
        if self.activity.busy:
            raise ValueError("Wait for the current turn to finish before changing the worktree.")
        if action == "resolve":
            if not self.model:
                raise ValueError("/worktree resolve needs a live model session.")
            files = worktree.conflicted_files(linked.path)
            if not files:
                raise ValueError("No merge conflicts to resolve; run /worktree merge first.")
            # A prompt in command clothing, dispatched like a skill.
            self.skill_requested = worktree.resolve_prompt(linked, files)
        elif action == "remove":
            if worktree.unmerged_commits(linked):
                raise ValueError("Branch has unmerged commits; /worktree merge first.")

            def remove() -> list[str]:
                result = worktree.remove(linked)
                self._leave_worktree(linked)
                return [result, "This session's workspace no longer exists; /quit."]

            self.defer("Removing worktree", linked.branch, remove)
        elif action == "finish":
            # Refusals raise before anything is deleted, so the session stays put.
            def finish() -> list[str]:
                result = worktree.finish(linked)
                self._leave_worktree(linked)
                self.running = False
                return [f"\u2713 {result}"]

            self.defer("Finishing worktree", linked.branch, finish)

    def _leave_worktree(self, linked) -> None:
        """Point the saved session at the mainline so `pcode -c` still finds a directory."""
        session = getattr(self.runtime, "session", None)
        if session is None:
            return
        session.info.workspace = str(linked.main)
        try:
            session.save_info()
        except OSError:
            pass
        from pcode.sessions import forget_refs

        # Its review checkpoint was of the checkout just removed.
        forget_refs(session.info)

    # --- Startup and new conversations ---

    def show_startup_context(self) -> None:
        """Report repository instructions and skills, each line only once.

        A model switch re-runs this because starting without a model leaves
        nothing to report until a runtime exists. Repeating lines the
        transcript already carries is just noise, so only new ones print.
        """
        lines = []
        summary = getattr(self.runtime, "startup_context", None)
        if summary is not None:
            lines.extend(summary())
        if self.skill_command_names:
            lines.append("Skill commands: " + ", ".join(self.skill_command_names))
        warnings = []
        if self.extensions is not None:
            for extension, line in zip(
                self.extensions.extensions, self.extensions.report(self.workspace), strict=True
            ):
                # Shipped defaults, and extensions the user turned off, are not news
                # at every launch; /extensions lists them.
                if not extension.enabled or (extension.loaded and extension.scope == "bundled"):
                    continue
                (lines if extension.loaded else warnings).append("Extension " + line)
        for line in lines + warnings:
            if line in self._startup_context_shown:
                continue
            self._startup_context_shown.add(line)
            if line in warnings:
                self.view.warning(line)
            else:
                self.view.retained_note(line)

    def warn_without_credentials(self) -> None:
        """Say so at startup, not on the first prompt.

        An `anthropic:` model without `ANTHROPIC_API_KEY` is built with
        `defer_model_check`, so its agent keeps the unresolved model string and
        the terminal opens looking healthy. Report it before the first prompt
        fails.
        """
        if not (self.model or "").startswith("anthropic:"):
            return
        agent = getattr(self.runtime, "agent", None)
        if agent is None or not isinstance(getattr(agent, "model", None), str):
            return
        from pcode.models import anthropic_credential_hint

        self.view.warning(
            "ANTHROPIC_API_KEY is not set, so prompts will fail. " + anthropic_credential_hint()
        )

    def rename(self, argument: str) -> None:
        """Name the saved session, so /resume can list and find it by that name.

        Bare, the conversation's own model is asked for a name in the background.
        """
        session = getattr(self.runtime, "session", None)
        if session is None:
            raise ValueError("Nothing is saved yet; /rename works after the first prompt.")
        from pcode.tool_display import plain

        # One line, no control characters, and short enough for a list row.
        name = plain(" ".join(plain(argument, None).split()), 80)
        if not name:
            self.start_rename(session)
            return
        # A name typed now wins over one still being asked for.
        self.cancel_rename()
        self._save_name(session, None if name == "-" else name)
        self.view.flash(f"Session named: {name}" if name != "-" else "Session name cleared.")

    def _save_name(self, session, name: str | None) -> None:
        session.info.name = name
        try:
            # A name is not activity: the session keeps its place and date in /resume.
            session.save_info(touch=False)
        except OSError as error:
            raise ValueError(f"Could not save the name: {error}") from error
        self.view.session_changed()

    def start_rename(self, session) -> None:
        """Ask the conversation's model to name `session`, beside whatever is running.

        In a task, not awaited: a command holds queued prompts back until it
        returns, and the answer can take as long as a model request does.
        """
        if not self.model:
            raise ValueError("No model to ask for a name. Usage: /rename NAME")
        if self.startup_pending or self.startup_error is not None:
            raise ValueError("The model can name this once the agent has started; /rename NAME")
        runtime = self.runtime
        # Mid-turn, the request in flight counts: it is what a side question sees.
        if not (getattr(runtime, "history", None) or getattr(runtime, "context_history", None)):
            raise ValueError("Nothing to name yet. Usage: /rename NAME")
        if self.rename_task is not None and not self.rename_task.done():
            self.view.flash("Already asking the model for a name.")
            return
        self.rename_task = asyncio.create_task(self._suggest_name(session))
        self.view.flash("Asking the model for a name…")

    async def _suggest_name(self, session) -> None:
        from pcode.aside import ASIDE_TIMEOUT_SECONDS
        from pcode.diagnostics import provider_context
        from pcode.error_report import error_message
        from pcode.session_naming import RENAME_REQUEST, clean_title

        try:
            # A side question's deadline: a stalled provider must not hold off
            # every later /rename, and a host's idle stop, for good.
            async with asyncio.timeout(ASIDE_TIMEOUT_SECONDS):
                # Sent as it is: the side question framing would ask for an answer.
                reply = await self.runtime.aside(RENAME_REQUEST, framing=str)
        except Exception as error:  # noqa: BLE001 - nobody else is waiting to report it.
            # The warning is redacted as a turn's is, so the frames go where a turn's do.
            agent = getattr(self.runtime, "agent", None)
            session.record_error(
                error,
                run_id="rename",
                provider_context=provider_context(agent.model) if agent is not None else None,
                detail="Session name request (/rename)",
            )
            reason = (
                f"no answer within {ASIDE_TIMEOUT_SECONDS}s"
                if isinstance(error, TimeoutError)
                else error_message(error)
            )
            self.view.warning(f"Could not name the session: {reason}")
            return
        name = clean_title(reply.answer)
        if not name:
            self.view.warning("Could not name the session: the model gave no name.")
            return
        # /new and /resume cancel this; anything else that swapped it is not ours.
        if getattr(self.runtime, "session", None) is not session:
            return
        try:
            self._save_name(session, name)
        except ValueError as error:
            self.view.warning(str(error))
            return
        self.view.flash(f"Session named: {name}")
        self.view.redraw()

    def cancel_rename(self) -> bool:
        """Stop a bare /rename's request; whether one was running."""
        task = self.rename_task
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def session_title(self) -> str:
        """What the session is called: its /rename name, else its asked-for title."""
        session = getattr(self.runtime, "session", None)
        if session is None:
            return ""
        return session.info.name or session.info.title or ""

    def start_naming(self, prompt: str | None = None) -> None:
        """Ask for a title in the background, once, for a saved session that has none.

        `prompt` is the session's first prompt when the caller has it; without
        one it is read from the journal.
        """
        session = getattr(self.runtime, "session", None)
        if session is None or not self.model:
            return
        info = session.info
        if info.name or info.title or info.id in self._naming_tried:
            return
        if self.naming_task is not None and not self.naming_task.done():
            return
        setting = SETTINGS["session_naming"]
        if load_preferences().get("session_naming", setting.default) != "on":
            return
        self._naming_tried.add(info.id)
        self.naming_task = asyncio.create_task(self._name_session(session, self.model, prompt))

    async def _name_session(self, session, model: str, prompt: str | None) -> None:
        import logging

        from pcode.session_naming import suggest_title
        from pcode.sessions import session_turns

        try:
            if not (prompt and prompt.strip()):
                turns = await asyncio.to_thread(
                    session_turns, session.info, session.directory.parent
                )
                prompt = next(
                    (turn.prompt for turn in turns or () if turn.prompt.strip() and not turn.side),
                    "",
                )
            if not prompt:
                return
            title = await suggest_title(model, prompt, workspace=self.workspace)
        except Exception as error:  # noqa: BLE001 - a title is never worth an error.
            logging.getLogger(__name__).debug("session title failed: %s", error)
            return
        # Renamed meanwhile. /new and /resume cancel this, so the session is still open.
        if not title or session.info.name or getattr(self.runtime, "session", None) is not session:
            return
        session.info.title = title
        try:
            session.save_info(touch=False)
        except OSError:
            return
        self.view.session_changed()
        self.view.redraw()

    def cancel_naming(self) -> None:
        """Another conversation is opening: its title and name requests go.

        Its title may be asked again; a name only when /rename asks.
        """
        if self.naming_task is not None and not self.naming_task.done():
            self.naming_task.cancel()
        self.cancel_rename()
        self._naming_tried.clear()

    def naming(self) -> bool:
        """Whether a title or name is being asked for."""
        return any(
            task is not None and not task.done() for task in (self.naming_task, self.rename_task)
        )

    async def stop_naming(self) -> None:
        """Cancel title and name requests and wait them out, before provider processes stop."""
        tasks = [task for task in (self.naming_task, self.rename_task) if task is not None]
        self.cancel_naming()
        await asyncio.gather(*tasks, return_exceptions=True)

    def new(self, argument: str) -> None:
        self.cancel_naming()
        self.runtime.reset()
        # On the host too, not just this terminal's copy, so one attaching later agrees.
        self.activity.turn_outcome = ""
        self.view.conversation_reset("New conversation")
        self.view.note(
            "Context reset; MCP servers are off unless marked enabled. Screen cleared; "
            "input history is unchanged."
        )
        if self.model and self.runtime.session:
            self.view.note(f"Saving session: {self.runtime.session.info.id}")
        self.mcp_defaults_requested = True
        # The old conversation's name leaves the tab with it.
        self.view.session_changed()

    async def select_model(self, argument: str) -> None:
        """Pick a model in the terminal from the providers this session can reach."""
        from pcode.models import active_providers, model_catalog

        providers = await asyncio.to_thread(active_providers, self.model)
        if not providers:
            self.view.note(
                "No active model providers. Use /login to sign in, "
                "set ANTHROPIC_API_KEY, run codex login, or export another "
                "provider's API key (see docs/providers.md). "
                "If model_providers is set, check that it allows an active provider."
            )
            return
        values = model_catalog(providers, self.model)
        model = await self.view.choose_model(values, providers, self.model)
        if model is not None:
            await self.switch_model(model)

    async def initialize_runtime(self) -> None:
        """Build the agent (off the event loop) and restore a resumed conversation."""
        self._loop = asyncio.get_running_loop()
        if self._needs_runtime:
            # A cancelled to_thread await does not stop its thread. Keep ownership
            # until it finishes so a late-created runtime cannot leak on exit.
            task = asyncio.create_task(asyncio.to_thread(self._create_runtime))
            try:
                runtime = await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    runtime = await task
                except Exception:
                    pass
                else:
                    runtime.close()
                raise
            agent = getattr(runtime, "agent", None)
            if agent is not None:
                apply_effort(agent, self.model, effort_for(self.model))
                apply_thinking(agent, self.model, self.activity.thinking_mode)
            await self._finish_startup(runtime)
        elif self.resuming:
            await self.runtime.restore()
            self.restore_side_threads()

    async def _finish_startup(self, runtime) -> None:
        """Adopt the first runtime: at launch, or from /model after launch failed."""
        self.runtime = runtime
        self._needs_runtime = False
        self.register_extension_commands()
        if self.resuming:
            await runtime.restore()
            self.restore_side_threads()
            self._seen_stopped_host()

    def _seen_stopped_host(self) -> None:
        """A resumed session is open here, so /switch stops listing its stopped host."""
        from pcode.host_protocol import forget_stopped

        session = getattr(self.runtime, "session", None)
        if session is not None:
            forget_stopped(session.info.id)

    # --- Resuming another saved conversation in this process ---

    async def resume_session(self, identity: str) -> None:
        from pcode.agent import create_agent
        from pcode.live import AgentRuntime
        from pcode.sessions import SavedSession
        from pcode.worktree import leave_worktree

        current = getattr(self.runtime, "session", None)
        if current is not None and current.info.id == identity:
            self.view.note("This session is already active.")
            return
        saved = SavedSession.open(identity, self.session_dir, fork_if_open=True)
        try:
            target = self._resume_workspace(saved.info)
            # A session from another worktree gets that worktree's extensions
            # and skills; the same workspace keeps what is already loaded.
            extensions = self.extensions
            if target != self.workspace:
                extensions = await asyncio.to_thread(self._load_extensions, target)
            capabilities = extensions.capabilities if extensions else ()
            subagents = extensions.subagents if extensions else ()
            agent = create_agent(saved.info.model, target, capabilities, subagents)
            apply_effort(agent, saved.info.model, effort_for(saved.info.model))
            apply_thinking(agent, saved.info.model, self.activity.thinking_mode)
            runtime = AgentRuntime(agent, saved)
            await runtime.restore()
            await runtime.refresh_context()
        except BaseException:
            saved.abandon()
            raise
        # Keep the current conversation intact until recovery has succeeded.
        # Its title request ends first: the session it would save to is closing.
        await self.stop_naming()
        if target != self.workspace:
            # The worktree being left is tidied like at exit, but nobody is
            # asked: unmerged work stays put with a note on how to get back.
            leave_worktree(self.workspace, current, ask=None, notify=self.view.note)
        # Side questions settled so far belong in the session being left.
        await self.side_records_settled()
        close = getattr(self.runtime, "close", None)
        if close is not None:
            close()
        if target != self.workspace:
            self._switch_workspace(target, extensions)
        self.runtime = runtime
        self._seen_stopped_host()
        self.restore_side_threads()
        self.model = saved.info.model
        self.session_dir = saved.directory.parent
        self.activity.prompt = ""
        self.activity.prompt_kind = "user"
        self.activity.prompt_detail = ""
        self.activity.turn_outcome = ""
        self.view.replay_conversation()
        self.mcp_defaults_requested = True

    def _resume_workspace(self, info) -> Path:
        """Where a resumed session works: its own directory, if this repository's.

        Another worktree of the same repository is fine (the session browser
        lists them), another repository is not: the conversation's paths,
        instructions, and extensions would all be wrong there.

        A worktree removed from outside the session that owned it leaves no
        directory to go back to. The conversation is still worth resuming, so
        continue it here when this is the same repository.
        """
        from pcode.sessions import SessionError
        from pcode.worktree import repo_scope, session_scope

        target = Path(info.workspace).resolve()
        if target == self.workspace:
            return target
        if not target.is_dir():
            if session_scope(info) != repo_scope(self.workspace):
                raise SessionError(
                    f"Session workspace no longer exists: {target}. It belonged to another "
                    "repository, so this one cannot continue it."
                )
            self.view.note(
                f"Session workspace {target} no longer exists; continuing in {self.workspace}."
            )
            return self.workspace
        if repo_scope(target) != repo_scope(self.workspace):
            raise SessionError("Workspace differs; refusing cross-repo resume.")
        return target

    def _switch_workspace(self, workspace: Path, extensions) -> None:
        """Rebind everything keyed on the workspace to another worktree.

        The agent is the caller's to replace; this covers what the app itself
        derives from the path: extension commands, skill commands, and the
        status line (whose branch watcher picks the new path up on its own).
        Project preferences and trust are per repository, so they stay.
        """
        self.workspace = workspace
        self.extensions = extensions
        self.register_extension_commands()
        for name in self.skill_command_names:
            self.registry.unregister(name)
        self.register_skills()
        self.view.commands_changed()
        self.view.note(f"Workspace: {workspace}")

    # --- Side questions (/btw) and conversation branches (/tree) ---

    def aside_settled(self, aside) -> None:
        self.record_aside(aside)
        if aside.status == "answered":
            self.view.aside_answered(aside)
            return
        if aside.status == "cancelled":
            self.view.note("Side question stopped.")
        else:
            on = f" on {aside.label}" if aside.label else ""
            self.view.warning(f"Side question{on} {aside.status}. {aside.error}".strip())
            saved = getattr(self.runtime, "session", None)
            if saved is not None and (saved.directory / "errors.log").exists():
                self.view.note(f"Diagnostics: {saved.directory / 'errors.log'}")
        # Not just a redraw: an attached terminal mirrors the status, and would
        # otherwise count a stopped or failed question as running forever.
        self.view.aside_changed(aside)

    def record_aside(self, aside) -> None:
        """Journal a settled side question as a branch of the conversation tree.

        In the background, since a question settles from a callback; see
        `AgentRuntime.record_aside`. A thread whose questions were not
        recorded still works here, but is gone once the session closes.
        """
        record = getattr(self.runtime, "record_aside", None)
        if record is None:
            return
        root = (self.asides.thread(aside.thread) or [aside])[0]

        async def run():
            async with self._side_lock:
                try:
                    aside.recorded = await record(aside, root.conversation)
                except Exception as error:
                    # The answer is still on screen; only its journal entry is lost.
                    with suppress(Exception):
                        self.record_aside_failure(aside, error)
                    return
            if aside.recorded:
                # A terminal attached to a host learns it can be followed up.
                self.view.aside_changed(aside)

        task = asyncio.create_task(run())
        self._side_records.add(task)
        task.add_done_callback(self._side_records.discard)

    async def side_records_settled(self) -> None:
        """Wait until every settled side question is in the journal."""
        if self._side_records:
            await asyncio.gather(*self._side_records, return_exceptions=True)

    def restore_side_threads(self) -> None:
        """Read the conversation's side threads back from its tree into `/btw`.

        After a resume: each recorded question returns as it settled, read,
        and a follow-up continues from its saved history (`side_reply`).
        """
        from time import monotonic

        tree = getattr(self.runtime, "tree", None)
        if tree is None:
            return
        conversation = getattr(self.runtime, "conversation_id", "")
        restored = []
        for node in tree.nodes.values():
            side = node.side
            if not side:
                continue
            status = side.get("status") or {"completed": "answered"}.get(node.status, node.status)
            aside = Aside(
                question=node.prompt,
                id=node.id,
                model=side.get("model", ""),
                label=side.get("label", ""),
                effort=side.get("effort", ""),
                status=status,
                answer=node.response,
                activity="",
                error=side.get("error", ""),
                read=True,
                thread=side.get("thread") or node.id,
                conversation=conversation,
                parent=node.parent,
                bridged=side.get("kept", ""),
                recorded=True,
                session_id=side.get("session_id", ""),
            )
            if aside.thread == aside.id:
                aside.base = node.parent
            aside.finished = monotonic()
            aside.started = aside.finished - float(side.get("elapsed") or 0)
            restored.append(aside)
        for aside in self.asides.restore(restored):
            self.view.aside_changed(aside)

    async def side_reply(self, follows: Aside) -> SideReply:
        """What a follow-up to `follows` continues from: its reply, else its saved node."""
        if follows.reply is not None:
            return follows.reply
        messages = await self.runtime.side_history(follows.id)
        chosen = (
            await self.target_options(SideTarget(follows.model, follows.effort))
            if follows.model or follows.effort
            else {}
        )
        return SideReply(
            answer=follows.answer,
            messages=messages,
            conversation_id=follows.session_id or getattr(self.runtime, "conversation_id", ""),
            model=chosen.get("model"),
            settings=chosen.get("settings"),
        )

    async def aside(self, argument: str) -> None:
        """`/btw [$MODEL[+EFFORT] | +EFFORT ...] QUESTION` asks beside the turn.

        Bare `/btw` reads the answers.
        """
        models, question = parse_models(argument)
        if not question:
            if not self.asides.items:
                raise ValueError(
                    "No side questions yet. Ask one with /btw QUESTION; "
                    "it runs beside the conversation without interrupting it."
                )
            request = await self.view.read_asides()
            if request is None:
                return
            if request.action == "merge":
                await self.merge_thread(request.thread)
            else:
                self.start_summary(request)
            return
        if not self.model:
            raise ValueError("/btw needs a model; this is a local UI preview.")
        if self.startup_pending or self.startup_error is not None:
            raise ValueError("/btw is unavailable until the agent has started.")
        for target in models:
            self.check_effort(target)
        await self.start_aside(question, models)

    def check_effort(self, target: SideTarget) -> None:
        """Refuse an effort the way /effort refuses it, before anything starts.

        Rather than asking at an effort the provider would reject. The
        conversation's own model is judged on the object /effort judges, so
        one command cannot accept what the other refuses; another model is
        judged on its name, which is all that is known before `side_model`
        resolves it.
        """
        if not target.effort:
            return
        agent = getattr(self.runtime, "agent", None)
        name = target.model or self.model
        resolved = getattr(agent, "model", None) if name == self.model else None
        if effort_setting(name, resolved) is None:
            raise ValueError(effort_unavailable(name))

    async def target_options(self, target: SideTarget) -> dict:
        """How a run asks `target`: `AgentRuntime.aside`/`stream` keyword arguments.

        Another model is resolved now, so a bad name fails before anything is
        sent. The conversation's own model needs nothing, or, at another
        effort, its settings with that effort applied, taken now so a later
        /effort cannot reach a run already asked.
        """
        from pcode.agent import side_model, with_effort

        if target.model not in ("", self.model):
            return {"model": await asyncio.to_thread(side_model, target.model, target.effort)}
        if not target.effort:
            return {}
        agent = self.runtime.agent
        return {
            "settings": with_effort(self.model, agent.model, agent.model_settings, target.effort)
        }

    async def start_aside(self, question: str, models: list[SideTarget] | None = None) -> None:
        """Run a side question in the background, on the context available now.

        With `models`, one side question starts per target. The conversation's
        own model takes the default path, which shares its prompt cache; every
        other one is resolved first, so a bad name fails the command before
        anything starts. An effort on the conversation's own model stays on
        that path, with the effort applied to its settings for that question.
        """
        models = models or [SideTarget()]
        others = [target for target in models if target.model not in ("", self.model)]
        options = [await self.target_options(target) for target in models]
        labels = model_labels(models)
        tree = getattr(self.runtime, "tree", None)
        for target, chosen in zip(models, options):
            self.asides.start(
                question,
                self._aside_work(question, chosen),
                model=target.model,
                label=labels[target],
                effort=target.effort,
                conversation=getattr(self.runtime, "conversation_id", ""),
                base=tree.active if tree is not None else None,
            )
        if others:
            names = ", ".join(dict.fromkeys(target.model for target in others))
            self.view.note(
                f"Asking beside the conversation on {names}: the turn keeps running and "
                "this question does not join it. Another model starts without the "
                "conversation's prompt cache, so it pays for the whole prompt. "
                "/btw opens the answers."
            )
        else:
            self.view.note(
                "Asking beside the conversation: the turn keeps running and "
                "this question does not join it. /btw opens the answer."
            )

    def _aside_work(
        self,
        question: str,
        options: dict,
        target: SideTarget | None = None,
        follows: Aside | None = None,
    ):
        """The background run for one side question, streaming into its record.

        `target` is resolved in the run, so a follow-up naming a model it
        cannot use fails in its thread, where it was asked. So is what a
        follow-up to `follows` continues from, read back from its node when
        the thread came from the journal.
        """

        async def work(aside):
            def report(answer: str, activity: str) -> None:
                self.asides.update(aside, answer=answer, activity=activity)

            chosen = options
            if follows is not None:
                chosen = {**chosen, "after": await self.side_reply(follows)}
            if target is not None:
                chosen = {**chosen, **await self.target_options(target)}
            return await self.runtime.aside(question, report=report, **chosen)

        return work

    def follow_up_aside(self, thread: str, question: str) -> None:
        """Ask `question` as a follow-up in a side question's thread.

        It continues from the thread's newest answer, on the model and effort
        that answered it unless the question starts with a `$MODEL[+EFFORT]`
        or `+EFFORT` of its own, as `/btw` does; see `AgentRuntime.aside`.
        Raises `ValueError` when there is nothing to continue yet, or the
        target is unusable, which the viewer shows as is.
        """
        models, question = parse_models(question)
        if len(models) > 1:
            raise ValueError("A follow-up asks one model; start a new /btw to ask several.")
        follows = self.asides.follows(thread)
        target = self.follow_up_target(follows, models[0]) if models else None
        options = {}
        if target is not None:
            options["fresh"] = True
        self.asides.start(
            question,
            self._aside_work(question, options, target, follows),
            model=target.model if target else follows.model,
            label=model_labels([target])[target] if target else follows.label,
            effort=target.effort if target else follows.effort,
            thread=thread,
            parent=follows.id,
        )

    def follow_up_target(self, follows: Aside, target: SideTarget) -> SideTarget | None:
        """Where a follow-up naming `target` runs, read against its thread; `None` stays put.

        A bare `+EFFORT` keeps the thread's model, and naming the
        conversation's model means it, not whatever the thread is on. Naming
        exactly where the thread already runs changes nothing, so the follow-up
        keeps the thread's cache rather than starting a new session.
        """
        model = target.model or follows.model
        target = SideTarget("" if model == self.model else model, target.effort)
        if (target.model, target.effort) == (follows.model, follows.effort):
            return None
        self.check_effort(target)
        return target

    def check_bridge(self, thread: str) -> Aside:
        """The answer a thread would be brought into the conversation from.

        Raises `ValueError` saying why it cannot be yet: like forking in
        /tree, changing the conversation waits for the running turn, and a
        thread from another conversation has nowhere here to go.
        """
        if self.activity.busy or self.activity.queued:
            raise ValueError("Adding to the conversation waits for the running turn")
        follows = self.asides.follows(thread)
        root = self.asides.thread(thread)[0]
        tree = getattr(self.runtime, "tree", None)
        if (
            tree is None
            or root.conversation != getattr(self.runtime, "conversation_id", None)
            or (root.base is not None and root.base not in tree.nodes)
        ):
            raise ValueError("This thread was asked in another conversation")
        return follows

    async def keep_thread(self, thread: str, action: str, instructions: str = "") -> None:
        """Bring a side thread into the conversation from the /btw thread view.

        `action` is "merge" or "summary", as the viewer's `Bridge` says; a
        `ValueError` says why it cannot be yet.
        """
        if action == "merge":
            self.check_bridge(thread)
            # Outside the command queue, so hold prompts back the way a
            # history task does: none may start on the tree while it moves.
            self.compact_idle.clear()
            try:
                await self.merge_thread(thread)
            finally:
                self.compact_idle.set()
        elif action == "summary":
            self.start_summary(Bridge(thread, "summary", instructions))
        else:
            raise ValueError(f"Unknown way to keep a side thread: {action}")

    async def merge_thread(self, thread: str) -> None:
        """Continue the conversation from a side thread, which is already a branch of it."""
        from pcode.diagnostics import strip_controls

        follows = self.check_bridge(thread)
        await self.side_records_settled()
        if not follows.recorded:
            raise ValueError("This thread is not in the conversation tree, so it cannot be merged")
        root = self.asides.thread(thread)[0]
        moved = await self.runtime.merge_aside(follows.id, root.base)
        if not moved:
            self.view.note(
                "This side thread is a branch in /tree where it was asked, and the "
                "conversation has moved on since, so it stays where it is. /tree switches to it."
            )
            return
        follows.bridged = "merged"
        self.view.aside_changed(follows)
        tree = self.runtime.tree
        steps = [
            tree.nodes[identity]
            for identity in tree.path(follows.id)
            if tree.nodes[identity].side.get("thread") == thread
        ]
        for node in steps:
            self.view.user(strip_controls(node.prompt))
            self.view.show_events((Message(strip_controls(node.response)),))
        count = f"{len(steps)} side question{'s' if len(steps) > 1 else ''}"
        self.view.note(
            f"Merged {count} into the conversation, which continues from the last answer."
        )

    async def summarize_thread(self, follows: Aside, instructions: str) -> None:
        """Add a summary of `follows`'s thread to the conversation, and show it."""
        from pcode.diagnostics import strip_controls

        asked = self.asides.thread(follows.thread)
        reply = await self.side_reply(follows)
        questions = [aside.question for aside, _ in exchanges(asked, reply.messages)]
        request = summary_request(questions, instructions)
        summary = await self.runtime.summarize_aside(reply, request, instructions)
        follows.bridged = "summarized"
        if follows.recorded:
            self.runtime.mark_side(follows.id, "summarized")
        self.view.aside_changed(follows)
        self.view.user(strip_controls(request))
        self.view.show_events((Message(strip_controls(summary)),))

    def aside_completions(self, argument: str):
        """Complete a `$MODEL` word in `/btw` arguments from the /model catalog.

        After a `+`, bare or ending a `$MODEL` word, the /effort levels complete.
        """
        from prompt_toolkit.completion import Completion

        effort = effort_fragment(argument)
        if effort is not None:
            for level in EFFORTS:
                if level.startswith(effort.casefold()):
                    yield Completion(
                        level, start_position=-len(effort), display=EFFORT_MARK + level
                    )
            return
        fragment = model_fragment(argument)
        if fragment is None:
            return
        needle = fragment.casefold()
        for model in self.model_suggestions():
            if needle in model.casefold():
                yield Completion(
                    MODEL_MARK + model,
                    start_position=-(len(fragment) + len(MODEL_MARK)),
                    display=model,
                )

    def model_suggestions(self) -> list[str]:
        """The /model picker's catalog, kept briefly so typing does not re-scan it."""
        from time import monotonic

        from pcode.models import active_providers, model_catalog

        cached = self._model_suggestions
        if cached is None or cached[0] != self.model or monotonic() - cached[1] > 30:
            models = model_catalog(active_providers(self.model), self.model)
            cached = self._model_suggestions = (self.model, monotonic(), models)
        return cached[2]

    def record_aside_failure(self, aside, error: BaseException) -> None:
        """Keep a failed side question's frames beside the session's turn failures.

        The viewer shows only the error summary, and nothing about a side
        question is journaled, so without this its traceback is simply lost.
        """
        from pcode.diagnostics import provider_context

        saved = getattr(self.runtime, "session", None)
        if saved is None:
            return
        agent = getattr(self.runtime, "agent", None)
        saved.record_error(
            error,
            run_id=f"aside {aside.id}",
            provider_context=(
                provider_context(aside.model or agent.model) if agent is not None else None
            ),
            detail=f"Side question ({aside.status}"
            + (f" on {aside.model}" if aside.model else "")
            + (f" at {aside.effort} effort" if aside.effort else "")
            + f"): {aside.question}",
        )

    async def navigate_tree(self, identity: str | None, edit: bool = False) -> str:
        if self.activity.busy or self.activity.queued:
            raise ValueError("/tree is unavailable while working or messages are queued.")
        draft = await self.runtime.navigate(identity, edit=edit)
        self.activity.turn_outcome = ""
        self.view.show_branch()
        self.view.note(
            "Context switched; previous branches are kept. File changes and tool effects "
            "are not undone."
        )
        return draft

    def command_failed(self, name: str, error: Exception) -> None:
        """Report a slash command that raised, with frames saved for diagnosis."""
        from pcode.diagnostics import stale_install

        self.view.error(error_message(error, unexpected=f"{name} failed"))
        if hint := stale_install():
            self.view.warning(hint)
        saved = getattr(self.runtime, "session", None)
        if saved is not None and (path := saved.record_error(error, run_id=name)):
            self.view.note(f"Session and diagnostics: {path}")
