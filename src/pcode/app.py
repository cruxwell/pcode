"""Compose the terminal with either the offline preview or a real Coder agent."""

import argparse
import asyncio
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import (
    AsyncExitStack,
    ExitStack,
    aclosing,
    asynccontextmanager,
    nullcontext,
    suppress,
)
from pathlib import Path

from prompt_toolkit.application import get_app
from prompt_toolkit.styles import DynamicStyle
from rich.cells import cell_len
from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from pcode import glyphs, terminal_color
from pcode.appearance import AppearanceWatch
from pcode.cli import ask, restore_stdin
from pcode.commands import Command, CommandRegistry, DeferredDescriptions
from pcode.completion import SHELLS as COMPLETION_SHELLS
from pcode.completion import complete_with
from pcode.config import (
    IMMEDIATE_SETTINGS,
    config_argument_descriptions,
    config_arguments,
    configure,
)
from pcode.config import USAGE as CONFIG_USAGE
from pcode.controller import (
    COMMAND_MODE,
    HANDOFF_TAG,
    MODEL_COMMANDS,
    SessionController,
    background_prompt,
    delivered_job,
    mcp_enable,
    runs_in_terminal,
)
from pcode.drafts import Drafts, default_dir
from pcode.error_report import error_message, workspace_gone
from pcode.kitty_spinner import KittySpinners
from pcode.library_log import install as install_library_log
from pcode.preferences import (
    ANTHROPIC_THINKS_BY_DEFAULT,
    COMMAND_MODES,
    GOOGLE_PROVIDERS,
    OPENAI_PROVIDERS,
    SETTINGS,
    SYNTAX_THEMES,
    THINKING_KEYS,
    THINKING_MODES,
    apply_effort,
    apply_thinking,
    canonical,
    effort_for,
    hints_preference,
    load_preferences,
    overlay_popups,
    parse_height,
    preference_count,
    save_preferences,
    thinking_mode_preference,
    thinking_settings,
    tool_rows_preference,
)
from pcode.prefix_keys import PrefixKeys, shortcut_label
from pcode.runtime import (
    EditCompleted,
    Message,
    PlanUpdated,
    PreviewRuntime,
    ToolSummary,
)
from pcode.shell_mode import shell_command
from pcode.startup_input import StartupInput
from pcode.stream_display import PrintedReply, present_events, present_stream_event
from pcode.terminal_notify import Reconcilable, Reconciler, TabProgress, TabTitle
from pcode.theme import (
    THEMES,
    ambiguous_wide,
    kitty_graphics_supported,
    replay_pending_input,
    take_initial_cursor_row,
    terminal_background,
)
from pcode.tool_display import plain, recorded_exit_code
from pcode.ui import (
    WATCHED_PREFIX,
    Activity,
    TerminalOutput,
    Transcript,
    create_prompt,
    suspended_editor,
)
from pcode.ui_surface import native_transcript_active
from pcode.worktree import (
    SESSION_WORKTREE_PREFIX,
    WORKTREES_DIR,
    leave_worktree,
    session_scope,
)


def location_label(workspace: Path, branch: str) -> str:
    """Compact `path@branch` for the footer.

    A session worktree repeats itself three times (`…/.worktrees/pcode-abc123`
    plus branch `pcode-abc123`), so collapse it to the repository it belongs to
    and let the branch name the checkout: `~/p/pcode@abc123`.
    """
    path, label = workspace, branch
    if branch and path.name == branch and path.parent.name == WORKTREES_DIR:
        path = path.parent.parent
        label = branch.removeprefix(SESSION_WORKTREE_PREFIX) or branch
    try:
        relative = path.relative_to(Path.home())
        directory = "~" if relative == Path(".") else f"~/{relative}"
    except ValueError:
        directory = str(path)
    if not label or label == path.name:
        return directory
    return f"{directory}@{label}"


BRANCH_POLL_SECONDS = 30
"""Safety net for a checkout made outside this session; turns refresh it directly."""

# Queue modes for a turn a session host started without this terminal asking:
# another terminal's, steering that arrived too late, or the one running at
# attach. "follow-quiet" is one whose prompt is already in scrollback.

HOST_POLL_SECONDS = 2
"""How often an attached terminal looks at the other hosts, for the footer and notices."""

HOST_CANCEL_GRACE = 5.0
"""Seconds a host may stay busy after a cancel before Ctrl+D detaches rather than cancels again."""


# How long the app-restart signal waits for a first prompt's turn to save the
# session (`_first_prompt_saved`); something else holding the queue, it gives up.
FIRST_SAVE_TIMEOUT = 10.0

NOTHING_TO_MOVE = (
    "Nothing to move to a background host yet: a session is saved with its "
    "first prompt. `pcode --host` starts one in a host."
)


def restore_signal_handler(loop, signum: int, previous) -> None:
    """Undo `loop.add_signal_handler(signum, ...)`, putting `previous` back.

    `remove_signal_handler` resets the default action on the way, which for
    SIGUSR2 kills the process should one land just then, and blocking it here
    covers this thread alone. So `previous` goes straight over the loop's
    handler and only the loop's record of it is dropped: asyncio's Unix loop
    keeps that in `_signal_handlers`. Any other loop takes the public route,
    with the signal held off on this thread at least.
    """
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signum})
    try:
        handlers = getattr(loop, "_signal_handlers", None)
        if not isinstance(handlers, dict) or signum not in handlers:
            loop.remove_signal_handler(signum)
            signal.signal(signum, previous)
            return
        signal.signal(signum, previous)
        del handlers[signum]
        if not handlers:  # As `remove_signal_handler`: the loop's wakeup fd goes too.
            with suppress(ValueError, OSError):
                signal.set_wakeup_fd(-1)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


class _PopupSuperseded(Exception):
    """Another popup opened after this command was queued."""


def _controller_attribute(name: str) -> property:
    """A session attribute the terminal reads and sets on its controller."""
    return property(
        lambda self: getattr(self.controller, name),
        lambda self, value: setattr(self.controller, name, value),
    )


class PreviewApp:
    def __init__(
        self,
        theme: str | None = None,
        console: Console | None = None,
        *,
        model: str | None = None,
        workspace: Path | None = None,
        runtime=None,
        saved_session=None,
        save: bool = False,
        session_dir: Path | None = None,
        resume: bool = False,
        initial_prompt: str | None = None,
        session_id: str | None = None,
        host=None,
    ) -> None:
        terminal_color.configure(load_preferences().get("color_depth"))
        self.activity = Activity(
            show_activity=load_preferences().get("show_activity", "on") == "on",
            show_tasks=load_preferences().get("show_tasks", "on") == "on",
            autohide_tasks=load_preferences().get(
                "autohide_tasks", SETTINGS["autohide_tasks"].default
            )
            == "on",
            attach_tasks=load_preferences().get("attach_tasks", SETTINGS["attach_tasks"].default)
            == "on",
            attach_activity=load_preferences().get(
                "attach_activity", SETTINGS["attach_activity"].default
            )
            == "on",
            task_style=load_preferences().get("task_style", SETTINGS["task_style"].default),
            frame_style=load_preferences().get("frame_style", SETTINGS["frame_style"].default),
            tool_glyphs=load_preferences().get("tool_glyphs", SETTINGS["tool_glyphs"].default),
            tool_max_rows=tool_rows_preference(),
            tool_linger_seconds=preference_count("tool_linger_seconds"),
            tasks_max_height=parse_height(load_preferences().get("tasks_max_height")),
            tasks_min_rows=preference_count("tasks_min_rows"),
            tasks_min_columns=preference_count("tasks_min_columns"),
            show_hints=hints_preference(),
            thinking_mode=thinking_mode_preference(),
        )
        self.preview = PreviewRuntime()
        # The conversation itself, with this terminal as its view.
        self.controller = SessionController(self, self.activity, runtime or self.preview)
        self.send_mode = load_preferences().get("send_mode", "steering")
        # Ctrl+S picks a mode for the next prompt only; the saved default stands.
        self.send_mode_once: str | None = None
        self.model = model
        self.initial_prompt = initial_prompt
        # Consumed by the first saved session so it shares its ID with the
        # worktree created for it; later `/new` sessions get their own.
        self._session_id = session_id
        self.worktree_owner = session_id
        self.resuming = resume
        self.save_sessions = save or saved_session is not None
        self.workspace = (workspace or Path.cwd()).resolve()
        self.branch = ""
        self.session_dir = saved_session.directory.parent if saved_session else session_dir
        # Where turns stream, and the editor; set once the prompt exists.
        self.output: TerminalOutput | None = None
        self.prompt_session = None
        self._saved_session = saved_session
        # A `pcode.remote.HostLaunch`: the conversation runs in a session host
        # and this terminal attaches to it once its event loop is up.
        self._host_launch = host
        self._needs_runtime = bool(model and runtime is None and host is None)
        self._startup_pending = self._needs_runtime or resume or host is not None
        # Other running hosts, for the footer; refreshed while attached to one.
        self.hosts: list = []
        self._host_watch: Callable[[], None] | None = None
        self._host_watch_task = None
        self.switch_requested: str | None = None
        # `/fork [PROMPT]`, with its prompt ("" bare).
        self.fork_requested: str | None = None
        # `/new PROMPT`'s prompt (bare `/new` is the session's own).
        self.new_requested: str | None = None
        self.restart_requested = False
        self.restart_all_requested = False
        # `/restart` is moving this terminal to the host's successor.
        self.restarting = False
        self.host_stopped = False
        # A command this terminal sent ended the host's session (`/worktree finish`).
        self.session_over = False
        # `/detach`: leave the host running on exit, which otherwise stops it.
        self.detach_requested = False
        # `/detach` in a session running here: move it into a host first.
        self.handoff_requested = False
        # A session running here started another in the background: it moves
        # into a host once idle, so showing that one leaves this one running.
        self.host_when_idle = False
        # That move is queued as a `/detach` (HANDOFF_TAG), which then stays
        # quiet if the session got busy meanwhile. The queue generation it was
        # sent in: a Ctrl+C that drops it lets the next try queue another.
        self.handoff_queued: int | None = None
        # Set once a host owns this terminal's in-process session.
        self.moved_to_host = False
        # One move into a host at a time: a `/detach` and the app-restart signal.
        self._move_lock = asyncio.Lock()
        # SIGUSR2 (`request_app_restart`): carry the session on in a host, then exit.
        self.app_restart_requested = False
        self._app_restart_task: asyncio.Task | None = None
        # Set once the session this terminal starts with is up (or failed to start).
        self._startup_done = asyncio.Event()
        # The editor's Enter, for commands that send as if typed.
        self._submit_typed: Callable[[str], None] | None = None
        # Queues a command (and its tag) the terminal runs itself, while the editor runs.
        self._run_in_terminal: Callable[[str, object], None] | None = None
        # The host this terminal showed before the current one, for `/attach -`;
        # None after leaving a session that ran in this terminal.
        self.previous_host: str | None = None
        # Its conversation, to resume should that host have stopped meanwhile
        # (or, after leaving one that ran here, always).
        self.previous_session: str | None = None
        # How this terminal came to show its host ("Switched to session …").
        self._attach_note: str | None = None
        # Writes an escape to the terminal emulator (desktop notifications).
        self._emulator: Callable[[str], None] | None = None
        # The tab's progress bar, while this terminal runs its prompt.
        self._progress: TabProgress | None = None
        self._reconciler: Reconciler | None = None
        self._appearance: AppearanceWatch | None = None
        self._kitty_spinners: KittySpinners | None = None
        # The in-process controller's loops, while this terminal runs them.
        self._loops: list = []
        agent = getattr(self.runtime, "agent", None)
        if agent is not None and model:
            apply_effort(agent, model, effort_for(model))
        if agent is not None and model:
            apply_thinking(agent, model, self.activity.thinking_mode)
        glyph_setting = load_preferences().get("glyphs", SETTINGS["glyphs"].default)
        self.transcript = Transcript(
            console or Console(color_system=terminal_color.rich_color_system()),
            theme or load_preferences().get("theme", SETTINGS["theme"].default),
            activity=self.activity,
            probe_width=console is None and glyph_setting == "auto",
            probe_cursor=console is None and not os.environ.get("PROMPT_TOOLKIT_NO_CPR"),
            probe_glide=console is None,
        )
        if console is None:
            # The editor's own terminal, which the startup probe has measured.
            ascii_wanted = glyphs.wanted(glyph_setting, wide=ambiguous_wide())
            glyphs.apply(ascii_wanted)
            glyphs.apply_nerd(
                glyphs.wanted_nerd(glyph_setting, ascii_on=ascii_wanted or glyphs.active())
            )
        from pcode import dock

        # Editor-only (`PCODE_EDITOR_ONLY=1`, `dock.EditorOnly`): this terminal
        # shows just the editor and the transcript goes nowhere, since the app
        # draws it from the host. Only for a host client: a session running in
        # this process (no model to start a host with, say) speaks through this
        # transcript alone (the trust prompt, "needs a model", config errors),
        # so the flag is ignored and pcode runs as in any terminal.
        self.editor_only = host is not None and dock.editor_only_requested()
        if self.editor_only:
            self.transcript.console.file = dock.NullSink(sys.stdout.fileno())
        self.running = True
        self._popup_generation = 0
        self._command_popup_generation: int | None = None
        self.inspector_requested: str | None = None
        self.diffs_requested = False
        # The checkpoint last marked reviewed, for a session with no saved id to key it by.
        self.reviewed_checkpoint = None
        self.links_requested = False
        self.copy_requested = False
        self.paste_requested = False
        self.config_requested = False
        # Unsaved conversations have no journal to re-read, so keep their changes.
        self.edits: list[EditCompleted] = []
        self.session_requested = False
        self.session_info_requested = False
        self.tree_requested = False
        self.worker_view_requested = False
        # The viewer follows answers that settle while it is open, so auto-open
        # has nothing to do then.
        self.aside_view_open = False
        # `popup_view` as `drawer_host` last read it, and when.
        self._drawer_pref = False
        self._drawer_pref_at = float("-inf")
        # Popups open one at a time, overlay or full screen; overlays
        # (`pcode.popup_surface`) stop being tried once two failed to open.
        self._popup_lock = asyncio.Lock()
        self._overlay_failures = 0
        # A native pick list failed once: the terminal and pcode disagree on it.
        self._native_pick_off = False
        # `/merge` and `/summarize` sent from a /btw thread in the drawer, while they run.
        self._btw_tasks: set[asyncio.Task] = set()
        # The popup generation an auto-open `/btw` was queued in. That viewer
        # opens on the answer, not the editor, since it can land mid-keystroke.
        # Matched by generation rather than order: a queued command can be
        # dropped (Ctrl+C) or superseded without ever reaching `read_asides`.
        # Counted too: a thread opened in the drawer shows no popup, so the
        # generation stays put, and once its auto-opens are spent a `/btw`
        # typed in that same generation is the reader's own.
        self._auto_open_generation: int | None = None
        self._auto_opens_pending = 0
        self.registry = CommandRegistry()
        self._session_commands: set[str] = set()
        for command in (
            Command(
                "/help",
                "List commands and keyboard shortcuts",
                self.help,
                aliases=("/commands",),
                group="App",
            ),
            Command(
                "/config",
                "Browse saved settings, or use list / diff / get KEY / set KEY VALUE / unset KEY",
                self.config,
                free_arguments=True,
                argument_provider=config_arguments,
                argument_descriptions=DeferredDescriptions(config_argument_descriptions),
                group="App",
            ),
            Command(
                "/bind",
                "List, set, or reset prompt keybindings; bind KEY /command [args]",
                self.bind,
                free_arguments=True,
                argument_completer=self.complete_bind,
                group="App",
            ),
            Command(
                "/unbind",
                "Disable a prompt binding; /bind reset KEY restores its default",
                self.unbind,
                free_arguments=True,
                group="App",
            ),
            Command("/quit", "Exit pcode", self.quit, aliases=("/exit",), group="App"),
            Command(
                "/status",
                "Show model, workspace, session, and context usage",
                self.status,
                group="Inspect",
            ),
            Command(
                "/tools",
                "Browse tool calls and results; 'failed' or 'edits' narrows them",
                self.tools,
                ("failed", "edits"),
                group="Inspect",
            ),
            Command(
                "/diffs",
                "Git diff of this worktree's branch, or of files edited this session",
                self.diffs,
                group="Inspect",
            ),
            Command(
                "/links",
                "Pick a URL from this conversation and open it in the browser",
                self.links,
                group="Inspect",
            ),
            Command(
                "/copy",
                "Choose a response to copy, or a quote or code block from it",
                self.copy,
                group="Inspect",
            ),
            Command(
                "/paste-image",
                "Save the clipboard's image and add its path to the prompt",
                self.paste_image,
                group="Inspect",
            ),
            Command(
                "/tree",
                "Browse the conversation tree and fork from any point",
                self.select_tree,
                group="Inspect",
            ),
            self.controller.registry.find("/btw"),
            Command(
                "/agents",
                "Follow delegated agents' own output, live and read-only",
                self.agents,
                group="Inspect",
            ),
            self.controller.registry.find("/model"),
            self.controller.registry.find("/effort"),
            self.controller.registry.find("/advisor"),
            self.controller.registry.find("/mcp"),
            self.controller.registry.find("/login"),
            self.controller.registry.find("/logout"),
            self.controller.registry.find("/extensions"),
            self.controller.registry.find("/subagents"),
            self.controller.registry.find("/reload"),
            self.controller.registry.find("/new"),
            self.controller.registry.find("/delete"),
            Command(
                "/resume",
                "Browse and search saved sessions to resume",
                self.select_session,
                group="Session",
            ),
            self.controller.registry.find("/rename"),
            Command(
                "/attach",
                "Show a running session here, leaving this one running if it is in a "
                "host: HOST | - (the previous one); bare lists them",
                self.switch,
                aliases=("/switch",),
                free_arguments=True,
                group="Session",
            ),
            Command(
                "/fork",
                "Copy this conversation into a background session in its own worktree; "
                "with PROMPT it works on that and this terminal stays here",
                self.fork,
                free_arguments=True,
                group="Session",
            ),
            Command(
                "/restart",
                "Restart this background session's host on the pcode installed now, "
                "continuing a running turn; all: every host on older code",
                self.restart,
                arguments=("all",),
                group="Session",
            ),
            Command(
                "/stop",
                "End this background session's host and quit, as any exit does",
                self.stop_host,
                group="Session",
            ),
            Command(
                "/detach",
                "Quit, leaving this session's background host running; "
                "a session running here moves into a host instead",
                self.detach,
                group="Session",
            ),
            self.controller.registry.find("/compact"),
            self.controller.registry.find("/autocompact"),
            self.controller.registry.find("/jobs"),
            self.controller.registry.find("/continue"),
            self.controller.registry.find("/worktree"),
            Command(
                "/show-activity",
                "Activity panel (thinking, tool calls, tasks, commands): on / off; bare toggles",
                self.show_activity,
                ("on", "off"),
                group="Display",
            ),
            Command(
                "/show-tasks",
                "Task list in the activity panel: on / off; bare toggles",
                self.show_tasks,
                ("on", "off"),
                group="Display",
            ),
            Command(
                "/autohide-tasks",
                "Hide the task list when a turn ends: on / off; bare toggles",
                self.autohide_tasks,
                ("on", "off"),
                group="Display",
            ),
            Command(
                "/show-thinking",
                "Where thinking shows: off / activity-panel / scrollback; bare cycles",
                self.show_thinking,
                THINKING_MODES,
                # The handler also takes the values' old names.
                free_arguments=True,
                group="Display",
            ),
            Command(
                "/show-edits",
                "Edit diffs and previews in scrollback: on / off; bare toggles",
                self.show_edits,
                ("on", "off"),
                group="Display",
            ),
            Command(
                "/show-commands",
                "Where shell command output shows: off / activity-panel / scrollback; bare cycles",
                self.show_commands,
                COMMAND_MODES,
                # The handler also takes the values' old names.
                free_arguments=True,
                group="Display",
            ),
            Command(
                "/group-tools",
                "Fold runs of tool calls into one scrollback line: on / off; bare toggles",
                self.group_tools,
                ("on", "off"),
                group="Display",
            ),
            Command(
                "/theme",
                "Set the palette: dark / light / auto; bare toggles dark/light",
                self.theme,
                THEMES,
                group="Display",
            ),
            Command(
                "/syntax",
                "Set the code, menu and prompt style for the active palette",
                self.syntax,
                SYNTAX_THEMES,
                group="Display",
            ),
            Command(
                "/theme-preview",
                "Sample output plus every syntax style and how to select one",
                self.theme_preview,
                group="Display",
            ),
            Command(
                "/redraw",
                "Rebuild scrollback at the current width and display settings",
                lambda _: self.transcript.regenerate(),
                group="Display",
            ),
        ):
            self.registry.register(command)
        # Skills (and, once the runtime exists, extension commands) come after.
        self.controller.register_skills()
        self.commands_changed()

    # The session's state, read and set through the controller that owns it.
    runtime = _controller_attribute("runtime")
    model = _controller_attribute("model")
    workspace = _controller_attribute("workspace")
    session_dir = _controller_attribute("session_dir")
    save_sessions = _controller_attribute("save_sessions")
    resuming = _controller_attribute("resuming")
    extensions = _controller_attribute("extensions")
    asides = _controller_attribute("asides")
    running = _controller_attribute("running")
    _startup_pending = _controller_attribute("startup_pending")
    _startup_error = _controller_attribute("startup_error")
    skill_command_names = _controller_attribute("skill_command_names")
    _session_id = _controller_attribute("_session_id")
    worktree_owner = _controller_attribute("worktree_owner")
    _saved_session = _controller_attribute("_saved_session")
    _needs_runtime = _controller_attribute("_needs_runtime")

    def current_effort(self) -> str:
        return self.controller.current_effort()

    def adjust_effort(self, direction: int) -> None:
        self.controller.adjust_effort(direction)

    def switch_model(self, model: str):
        return self.controller.switch_model(model)

    # SessionView: what the controller shows. See pcode.controller.SessionView.

    def user(self, text: str) -> None:
        self.transcript.user(text)

    def note(self, text: str) -> None:
        self.transcript.note(text)

    def retained_note(self, text: str) -> None:
        self.transcript.retained_note(text)

    def flash(self, text: str) -> None:
        self.transcript.flash(text)

    def warning(self, text: str) -> None:
        self.transcript.warning(text)

    def error(self, text: str, *, title: str = "Error") -> None:
        self.transcript.error(text, title=title)

    def cancelled(self) -> None:
        self.transcript.cancelled()

    def tool_result(self, event) -> None:
        self.transcript.tool_result(event)

    def shell_result(
        self, command: str, output: str, *, failed: bool, elapsed_seconds: float
    ) -> None:
        self.transcript.shell_result(
            command, output, failed=failed, elapsed_seconds=elapsed_seconds
        )

    def session_changed(self) -> None:
        pass  # In this process the footer reads the controller directly.

    def plan_changed(self, items: list[dict]) -> None:
        pass  # The task list follows the turn's `PlanUpdated` events instead.

    def commands_changed(self) -> None:
        """Mirror the session's commands (skills and extensions come and go) in the editor's."""
        owned = self.controller.registry
        for name in self._session_commands - {command.name for command in owned.commands}:
            self.registry.unregister(name)
        for command in owned.commands:
            if self.registry.find(command.name) is not command:
                self.registry.replace(command)
        self._session_commands = {command.name for command in owned.commands}

    def conversation_reset(self, title: str) -> None:
        """A new conversation: clear the screen and the live panel, and say so."""
        self.activity.reset()
        self.edits.clear()
        self.reviewed_checkpoint = None
        self.transcript.clear()
        self.transcript.print(Rule(title, style="pcode.muted"))
        self.reconcile_terminal()

    def replay_conversation(self) -> None:
        # A host that loaded its conversation after this terminal attached
        # draws it now, under the note that said how it got here.
        self.replay(note=self._attach_note if self.hosted else None)

    def preview_reply(self, text: str) -> None:
        """No model: answer from the canned preview runtime."""
        self.handle(text)

    def show_events(self, events) -> None:
        self.transcript.events(tuple(events))

    def show_branch(self) -> None:
        """Redraw the conversation after /tree moved it to another branch."""
        self.activity.reset()
        if self.runtime.session:
            self.replay()
            return
        from pcode.diagnostics import strip_controls

        tree = self.runtime.tree
        with self.transcript.restore():
            for node_id in tree.path(tree.active):
                node = tree.nodes[node_id]
                self.transcript.user(strip_controls(node.prompt))
                if node.response:
                    self.transcript.events((Message(strip_controls(node.response)),))
        self.activity.plan = tree.nodes[tree.active].plan if tree.active else []

    def aside_changed(self, aside) -> None:
        # The footer counts side questions, and an open viewer follows the
        # answer as it streams, so both only need to know that something moved.
        self.redraw()
        # A question that stopped may have been all that kept this session here.
        self.hand_off_when_idle()

    def aside_answered(self, aside) -> None:
        """Say a side answer is ready, opening it when that is the preference."""
        # Queued as a command rather than opened here: the command consumer owns
        # the terminal, so the viewer waits for whatever popup or command is
        # already using it instead of racing it.
        opening = self.auto_open_asides()
        if opening:
            if self._auto_open_generation != self._popup_generation:
                self._auto_open_generation = self._popup_generation
                self._auto_opens_pending = 0
            self._auto_opens_pending += 1
            self.controller.command("/btw", self._popup_generation)
        on = f"{aside.label}: " if aside.label else ""
        self.transcript.note(
            f"Side answer ready ({on}{plain(aside.question, 60)}). "
            + ("Opening it." if opening else "/btw opens it.")
        )
        self.redraw()
        # Queued behind the viewer: it moves once that closes.
        self.hand_off_when_idle()

    def redraw(self) -> None:
        if self.output is not None:
            self.output.app.invalidate()

    def turn_started(self, text: str, *, echo: bool) -> None:
        self.activity.tools.clear()
        self.activity.forget_calls()
        self.activity.cache_note = ""
        if echo:
            self.output.begin_turn(text)
        if self._progress is not None:
            self._progress.turn_started()

    def turn_event(self, event) -> None:
        if self._progress is not None:
            self._progress.turn_event()
        present_stream_event(
            event,
            output=self.output,
            transcript=self.transcript,
            activity=self.activity,
            present=self.present_events,
        )

    def turn_retry(self, text: str) -> None:
        # Separate abandoned partial text/thinking from the next attempt.
        self.output.finish_thinking(complete=True)
        self.output.finish()
        if self._progress is not None:
            self._progress.turn_retry()
        self.activity.plan_preview = None
        self.activity.edit_previews.clear()
        self.transcript.note(text)
        self.output.app.invalidate()

    def reconcile_terminal(self) -> None:
        """Bring the title and pane session in line now, not at the next slow tick.

        Cheap: an item whose state has not changed writes nothing.
        """
        if self._reconciler is not None:
            self._reconciler.tick()

    def turn_ended(self) -> None:
        if self._progress is not None:
            self._progress.turn_ended()
        # A first turn names the session; /rename and /resume are turns too.
        self.reconcile_terminal()
        self.activity.edit_previews.clear()
        # A watched job is not the turn's; its preview stays pinned.
        for key in [k for k in self.activity.command_outputs if not k.startswith(WATCHED_PREFIX)]:
            del self.activity.command_outputs[key]
        self.activity.plan_preview = None
        self.transcript.settle_tools()
        self.output.end_turn()
        # Drop whatever the turn left running.
        self.activity.tools.clear()
        self.activity.workers.end_turn()

    def finish_text(self) -> None:
        if self.output is not None:
            self.output.finish()

    def show_output(self, event) -> None:
        self.activity.command_outputs.pop(event.call_id, None)
        self.activity.command_outputs[event.call_id] = event

    def drop_output(self, call_id: str) -> None:
        self.activity.command_outputs.pop(call_id, None)
        self.redraw()

    async def after_turn(self) -> None:
        await self.output.flush()
        if not self.running:
            self.output.app.exit()
            return
        self.hand_off_when_idle()
        # A turn (or a shell command) is the usual reason the branch moved, so
        # read it here rather than polling fast enough to catch one.
        if await asyncio.to_thread(self.refresh_branch):
            self.output.app.invalidate()

    async def _initialize_runtime(self) -> None:
        await self.controller.initialize_runtime()

    def command_started(self, tag) -> None:
        self._command_popup_generation = tag

    def command_finished(self) -> None:
        self._command_popup_generation = None

    async def choose_model(self, values, providers, current):
        """The model picker; returns the choice, or None when dismissed."""
        from pcode import model_ui

        output, session = self.output, self.prompt_session
        try:
            async with self.popup(
                output, session, layout="sheet", title=model_ui.TITLE, pick=True
            ) as surface:
                picked = await surface.pick(
                    model_ui.model_items(values, current),
                    title=model_ui.TITLE,
                    header=model_ui.HEADER,
                    empty=model_ui.EMPTY,
                    custom=model_ui.custom_pattern(),
                    # The chord that opens it closes it too.
                    cancel_keys=("c-l",),
                    style=session.app.style,
                )
        except _PopupSuperseded:
            return None
        return picked.id if picked is not None else None

    async def browse_jobs(self) -> None:
        from pcode.jobs_ui import JobBrowser

        output, session = self.output, self.prompt_session
        try:
            async with self.popup(output, session, title="Jobs") as surface:
                browser = JobBrowser(
                    self.runtime.jobs,
                    stop=lambda job: self.controller.stop_jobs([job.id]),
                    watch=lambda job: self.controller.watch_job(job.id if job else None),
                    watched=lambda: self.activity.watched_job,
                    rich_theme=self.transcript.rich_theme,
                    code_theme=self.transcript.code_theme,
                    color_system=self.transcript.console.color_system,
                    input=surface.input,
                    output=surface.output,
                    style=session.app.style,
                )
                await browser.run()
        except _PopupSuperseded:
            pass

    async def read_secret(self, title: str, prompt: str, detail: str = "") -> str | None:
        """Masked single-line secret popup; returns the value, or None when dismissed."""
        from pcode.secret_ui import SecretPrompt

        output, session = self.output, self.prompt_session
        if output is None or session is None:
            return None
        try:
            async with self.popup(output, session, overlay=False) as surface:
                dialog = SecretPrompt(
                    title,
                    prompt,
                    detail=detail,
                    input=surface.input,
                    output=surface.output,
                    style=session.app.style,
                )
                return await dialog.run()
        except _PopupSuperseded:
            return None

    def prompt_bindings(self):
        bindings = getattr(self.prompt_session, "command_bindings", None)
        if bindings is None:
            raise ValueError("Keybindings require an interactive prompt.")
        return bindings

    def bind(self, argument: str) -> None:
        try:
            result = self.prompt_bindings().manage(argument)
        except OSError as error:
            raise ValueError(f"Could not access keybindings: {error}") from None
        self.transcript.note(result)
        self.prompt_session.app.invalidate()

    def unbind(self, argument: str) -> None:
        try:
            result = self.prompt_bindings().manage(argument, unbind=True)
        except OSError as error:
            raise ValueError(f"Could not access keybindings: {error}") from None
        self.transcript.note(result)
        self.prompt_session.app.invalidate()

    def complete_bind(self, argument: str):
        bindings = getattr(self.prompt_session, "command_bindings", None)
        return bindings.complete(argument) if bindings is not None else ()

    def config(self, argument: str) -> None:
        if not argument.strip():
            self.config_requested = True
            return
        self.transcript.note(self.apply_config(shlex.split(argument)))

    async def browse_config(self, output: TerminalOutput, session) -> None:
        from pcode.config_ui import ConfigBrowser

        self.config_requested = False
        try:
            async with self.popup(output, session, title="Settings") as surface:
                browser = ConfigBrowser(
                    save=self.apply_config,
                    input=surface.input,
                    output=surface.output,
                    style=session.app.style,
                )
                await browser.run()
        except (ValueError, OSError) as error:
            self.transcript.warning(f"Could not open configuration: {error}")

    def apply_config(self, args: list[str]) -> str:
        """One write and immediate-layout path for commands and the browser."""
        try:
            result = configure(args)
        except OSError as error:
            raise ValueError(f"Could not access global defaults: {error}") from None
        # Layout-only settings apply at once rather than on the next launch.
        preferences = load_preferences()
        self.activity.attach_tasks = (
            preferences.get("attach_tasks", SETTINGS["attach_tasks"].default) == "on"
        )
        self.activity.attach_activity = (
            preferences.get("attach_activity", SETTINGS["attach_activity"].default) == "on"
        )
        self.activity.tasks_max_height = parse_height(preferences.get("tasks_max_height"))
        self.activity.tasks_min_rows = preference_count("tasks_min_rows")
        self.activity.tasks_min_columns = preference_count("tasks_min_columns")
        self.activity.task_style = preferences.get("task_style", SETTINGS["task_style"].default)
        self.activity.frame_style = preferences.get("frame_style", SETTINGS["frame_style"].default)
        self.activity.tool_glyphs = preferences.get("tool_glyphs", SETTINGS["tool_glyphs"].default)
        self.activity.tool_max_rows = tool_rows_preference()
        self.activity.tool_linger_seconds = preference_count("tool_linger_seconds")
        self.activity.show_hints = hints_preference()
        self.transcript.live_edits = (
            preferences.get("live_edits", SETTINGS["live_edits"].default) == "on"
        )
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()
        edits = args[1:] if args[:1] == ["project"] else args
        if len(edits) >= 2 and edits[0] in ("set", "unset") and edits[1] in IMMEDIATE_SETTINGS:
            result = result.replace("Applies on next launch.", "Layout settings apply immediately.")
        return result

    def set_show_activity(self, shown: bool) -> None:
        self.activity.show_activity = shown
        self.persist_defaults(show_activity="on" if shown else "off")
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()

    @staticmethod
    def toggle_argument(command: str, argument: str, current: bool) -> bool:
        """Resolve `on`/`off`, or flip `current` when the argument is empty."""
        if not argument:
            return not current
        if argument not in ("on", "off"):
            raise ValueError(f"Usage: {command} [on|off]")
        return argument == "on"

    def show_activity(self, argument: str) -> None:
        activity = self.activity
        if argument:
            shown = self.toggle_argument("/show-activity", argument, activity.show_activity)
            # `on` means on screen now, even in a pane below the size thresholds.
            activity.tasks_unhidden = shown and activity.screen_small
            if shown:
                activity.tasks_autohidden = False
        else:
            # Bare acts on what is on screen, like Ctrl+O: in a small pane it
            # overrides the size rule rather than saving the panel as off.
            shown = activity.toggle_panel()
        self.set_show_activity(shown)
        state = "on" if activity.show_activity else "off"
        if activity.show_activity and activity.tasks_too_big:
            state += " (hidden in this pane: see tasks_min_rows and tasks_min_columns)"
        self.transcript.flash(
            f"Activity panel: {state}. Usage: /show-activity [on|off] ({self.shortcut('o')})"
        )

    def show_tasks(self, argument: str) -> None:
        """`/show-tasks [on|off]`: the task list alone, inside the activity panel."""
        activity = self.activity
        # The list's own switch, so a hidden panel or a small pane cannot pin it on.
        listed = activity.show_tasks and not activity.tasks_autohidden
        shown = self.toggle_argument("/show-tasks", argument, listed)
        activity.show_tasks = shown
        activity.tasks_autohidden = False
        self.persist_defaults(show_tasks="on" if shown else "off")
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()
        state = "on" if shown else "off"
        if shown and not activity.panel_shown:
            state += " (the activity panel is hidden: /show-activity)"
        self.transcript.flash(f"Show tasks: {state}. Usage: /show-tasks [on|off]")

    def autohide_tasks(self, argument: str) -> None:
        enabled = self.toggle_argument("/autohide-tasks", argument, self.activity.autohide_tasks)
        self.activity.autohide_tasks = enabled
        if not enabled:
            self.activity.tasks_autohidden = False
        self.persist_defaults(autohide_tasks="on" if enabled else "off")
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()
        state = "on" if enabled else "off"
        self.transcript.flash(
            f"Auto-hide tasks after each turn: {state}. Usage: /autohide-tasks [on|off]"
        )

    def show_edits(self, argument: str) -> None:
        shown = self.toggle_argument("/show-edits", argument, self.transcript.show_edits)
        self.transcript.show_edits = shown
        self.persist_defaults(show_edits="on" if shown else "off")
        self.transcript.regenerate()
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()
        self.transcript.flash(
            f"Show edits: {'on' if shown else 'off'}. Usage: /show-edits [on|off] "
            f"({self.shortcut('z')})"
        )

    def group_tools(self, argument: str) -> None:
        grouped = self.toggle_argument("/group-tools", argument, self.transcript.group_tools)
        self.transcript.group_tools = grouped
        self.persist_defaults(group_tools="on" if grouped else "off")
        self.transcript.regenerate()
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()
        self.transcript.flash(
            f"Group tools: {'on' if grouped else 'off'}. Usage: /group-tools [on|off] "
            f"({self.shortcut(']')})"
        )

    def set_thinking_mode(self, mode: str) -> None:
        scrollback = self.activity.show_thinking
        self.activity.thinking_mode = mode
        self.controller.set_thinking(mode)
        self.persist_defaults(show_thinking=mode)
        # Only scrollback's own thinking needs the transcript rebuilt.
        if self.activity.show_thinking != scrollback:
            self.transcript.regenerate()
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()

    def show_thinking(self, argument: str) -> None:
        """`/show-thinking [off|activity-panel|scrollback]`; the bare command cycles."""
        argument = canonical("show_thinking", argument)
        if argument and argument not in THINKING_MODES:
            raise ValueError(f"Usage: /show-thinking [{'|'.join(THINKING_MODES)}]")
        if not argument:
            current = self.activity.thinking_mode
            modes = THINKING_MODES
            if self.editor_only:
                # Pcode.app's transcript shows thinking or not, as the host's mode says;
                # this editor's own copy is only what preferences said when it started.
                modes = ("off", "scrollback")
                current = getattr(self.controller, "state", {}).get("thinking_mode", current)
            index = modes.index(current) if current in modes else -1
            argument = modes[(index + 1) % len(modes)]
        self.set_thinking_mode(argument)
        lines = [
            f"Thinking: {argument}. Usage: /show-thinking [{'|'.join(THINKING_MODES)}]; "
            f"bare cycles ({self.shortcut('t')})"
        ]
        model = self.model or ""
        provider = model.split(":", 1)[0]
        if provider in THINKING_KEYS:
            wanted = thinking_settings(model, None, argument)
            display = wanted.get("anthropic_thinking", {}).get("display")
            if display == "updates":
                lines.append("Asks for progress updates between tool calls from the next request.")
            elif wanted:
                lines.append("Asks for thinking summaries from the next request.")
            else:
                lines.append("Asks for no readable thinking from the next request.")
            if provider == "anthropic":
                # Only scrollback turns thinking on, and only where it was off.
                if wanted and not ANTHROPIC_THINKS_BY_DEFAULT.search(model):
                    lines.append("This model only thinks when asked: more latency and tokens.")
            elif wanted and provider in OPENAI_PROVIDERS:
                lines.append("An unverified OpenAI organisation is refused summaries; use off.")
            elif (
                provider in GOOGLE_PROVIDERS
                and not wanted
                and (effort := self.current_effort()) not in ("default", "n/a")
            ):
                # A Gemini level owns the setting and always returns summaries.
                lines[-1] = (
                    f"Effort {effort} still returns thinking summaries; /effort default stops them."
                )
        self.transcript.flash("\n".join(lines))

    @property
    def next_send_mode(self) -> str:
        """The mode the next prompt sends with: a send-mode shortcut pick, else the default."""
        return self.send_mode_once or self.send_mode

    def cycle_send_mode(self) -> None:
        """Cycle the mode for the next send only; the saved default is untouched."""
        from pcode.preferences import SEND_MODES

        mode = SEND_MODES[(SEND_MODES.index(self.next_send_mode) + 1) % len(SEND_MODES)]
        self.send_mode_once = None if mode == self.send_mode else mode

    def set_show_commands(self, mode: str) -> None:
        mirrored = self.transcript.command_scrollback
        self.transcript.command_mode = mode
        self.persist_defaults(show_commands=mode)
        # Reproject retained results as well as future completions.
        if self.transcript.command_scrollback != mirrored:
            self.transcript.regenerate()
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()

    def show_commands(self, argument: str) -> None:
        """`/show-commands [off|activity-panel|scrollback]`; the bare command cycles."""
        usage = f"Usage: /show-commands [{'|'.join(COMMAND_MODES)}]"
        # `on` is what `scrollback` was called before there were three modes.
        argument = canonical("show_commands", argument)
        if argument and argument not in COMMAND_MODES:
            raise ValueError(usage)
        if not argument:
            current = self.transcript.command_mode
            argument = COMMAND_MODES[(COMMAND_MODES.index(current) + 1) % len(COMMAND_MODES)]
        self.set_show_commands(argument)
        self.transcript.flash(f"Commands: {argument}. {usage}; bare cycles ({self.shortcut('g')})")

    def shortcut(self, key: str) -> str:
        """How this session's prompt spells shortcut ``key``, e.g. Ctrl+O."""
        bindings = getattr(self.prompt_session, "command_bindings", None)
        if bindings is not None:
            return bindings.action_label(key)
        shortcuts = getattr(self.prompt_session, "shortcuts", None)
        return shortcuts.label(key) if shortcuts is not None else shortcut_label(key)

    def persist_defaults(self, **updates: str) -> None:
        try:
            save_preferences(**updates)
        except OSError, ValueError:
            self.transcript.warning("Could not save defaults; this selection applies only here.")

    def forget_defaults(self, *keys: str) -> None:
        from pcode.preferences import update_preferences

        try:
            update_preferences({}, remove=keys)
        except OSError, ValueError:
            self.transcript.warning("Could not update defaults; this change applies only here.")

    def _claim_popup(self) -> None:
        """Count a popup as shown, so requests queued before it are superseded."""
        # Include requests queued during preparation and terminal
        # handoff, not just those waiting when this command began.
        self._popup_generation += 1
        if self._command_popup_generation is not None:
            self._command_popup_generation = self._popup_generation

    def popups_overlay(self) -> bool:
        """Whether popups open in an overlay beside the conversation (unless `popup_view popup`).

        Only in a terminal that answered the overlay probe, on this machine
        (it finds pcode's socket by path), and until two in a row failed to open.
        """
        from pcode import theme

        # Twice running: once may be a cold start (a fresh install compiling).
        if self._overlay_failures >= 2 or not theme.popup_supported():
            return False
        if not self.transcript.console.is_terminal:
            return False
        if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
            return False
        return overlay_popups()

    def _superseded(self) -> bool:
        return (
            self._command_popup_generation is not None
            and self._command_popup_generation != self._popup_generation
        )

    @asynccontextmanager
    async def popup(
        self,
        output: TerminalOutput,
        session,
        *,
        layout: str = "wide",
        overlay: bool = True,
        title: str = "",
        pick: bool = False,
    ):
        """Give a modal a terminal of its own, superseding pending popups.

        Yields a `PopupSurface`: pass its `input` and `output` to the popup.
        That is an overlay when `popups_overlay()` (`layout` is its shape, `sheet`
        for short lists and prompts, `wide` for browsers), else pcode's own
        terminal, given to the popup whole. `overlay=False` always takes the
        full screen: a secret is typed where nothing else can take keys.
        `title` heads an overlay's panel until a dialog sets its own.
        `pick` asks for a surface only used to `pick`: the terminal's native
        picker where it has one (it has no terminal), else as above.
        One popup at a time, wherever it opens.
        """
        from pcode import popup_surface, theme

        if self._superseded():
            raise _PopupSuperseded
        async with self._popup_lock:
            # Shown meanwhile, another popup may have superseded this one.
            if self._superseded():
                raise _PopupSuperseded
            native = (
                pick and overlay and theme.native_pick_supported() and not self._native_pick_off
            )
            if native and self.popups_overlay():
                async with AsyncExitStack() as stack:
                    try:
                        surface = await stack.enter_async_context(
                            popup_surface.pick_overlay(
                                session.app.output, session.app.input, layout=layout
                            )
                        )
                    except popup_surface.OverlayUnavailable:
                        self._overlay_failed()
                    else:
                        self._overlay_failures = 0
                        self._claim_popup()
                        surface.full_screen = lambda: self._full_screen_popup(output, session)
                        surface.native_failed = self._native_pick_failed
                        yield surface
                        return
            elif overlay and self.popups_overlay():
                async with AsyncExitStack() as stack:
                    try:
                        surface = await stack.enter_async_context(
                            popup_surface.overlay(
                                session.app.output,
                                session.app.input,
                                layout=layout,
                                title=title,
                            )
                        )
                    except popup_surface.OverlayUnavailable:
                        self._overlay_failed()
                    else:
                        self._overlay_failures = 0
                        self._claim_popup()
                        yield surface
                        return
            async with self._full_screen_popup(output, session) as surface:
                yield surface

    def _native_pick_failed(self, reason: str) -> None:
        """The terminal's native picker took the socket but showed no list (a version skew).

        That pick takes the full screen; later ones open in a terminal overlay.
        """
        logging.getLogger(__name__).warning("Native pick list failed: %s", reason)
        self._native_pick_off = True
        self._overlay_failed()
        self.transcript.warning(
            f"The terminal could not show a native list ({reason}); "
            "lists open in a terminal popup for the rest of this session."
        )

    def _overlay_failed(self) -> None:
        """An overlay did not open; this one takes the full screen, and after two all do."""
        self._overlay_failures += 1
        if not self.popups_overlay():
            self.transcript.warning(
                "The terminal did not open the popup overlay; "
                "popups take the full screen for the rest of this session."
            )

    @asynccontextmanager
    async def _full_screen_popup(self, output: TerminalOutput, session):
        """The popup takes pcode's terminal: the editor waits, the conversation is redrawn after."""
        from pcode import drawer
        from pcode.popup_surface import PopupSurface

        await output.flush(drain=True)
        # The /btw drawer would cover the popup: out of the way until it is done.
        thread_view = getattr(session, "btw_view", None)
        hide = bool(getattr(thread_view, "drawer_open", False))
        try:
            async with output.lock:
                async with suspended_editor(session.app):
                    output.release_screen()
                    # A separate parser prevents the editor's escape-flush timer
                    # from stealing the modal's first Escape key.
                    stdin = getattr(session.app.input, "stdin", None)
                    # It also filters appearance replies, which arrive at any time.
                    modal_input = (
                        StartupInput(stdin, b"", self.follow_appearance)
                        if stdin is not None
                        else session.app.input
                    )
                    if hide:
                        session.app.output.write_raw(drawer.HIDE)
                    try:
                        self._claim_popup()
                        yield PopupSurface(modal_input, session.app.output)
                    finally:
                        if modal_input is not session.app.input:
                            modal_input.close()
        finally:
            if hide:
                session.app.output.write_raw(drawer.SHOW)
                session.app.output.flush()
            # Reuse resize's transcript replay, even when resize replay is off.
            # Flush only after releasing the writer lock and restoring the
            # normal screen, including dismissal, cancellation, and failures.
            self.transcript.regenerate()
            await output.flush()

    def tools(self, argument: str) -> None:
        self.inspector_requested = argument

    async def inspect_tools(self, output: TerminalOutput, session) -> None:
        from pcode.inspection import ToolArchive
        from pcode.inspector_ui import ToolInspector

        failed = self.inspector_requested == "failed"
        edits = self.inspector_requested == "edits"
        self.inspector_requested = None
        if self.hosted:
            # A host's journal is re-read when a turn ends (or on an edit), so
            # mid-turn the tree could still end at the previous turn, and the branch filter
            # below would drop every call the running turn has made.
            self.runtime.refresh()
        saved = getattr(self.runtime, "session", None)
        if saved is not None or hasattr(self.runtime, "inspections"):
            # The browser owns a snapshot, never the archive streaming mutates.
            # File indexing can then run off-thread, including saved history.
            from copy import deepcopy

            archive = deepcopy(getattr(self.runtime, "inspections", None) or ToolArchive())
            if saved is not None:
                await asyncio.to_thread(archive.update, saved.directory / "transcript.jsonl")
            self.add_running_tools(archive)
        else:
            archive = ToolArchive()
            for call in self.activity.tools.calls:
                archive.event(call.event)
            for change in self.edits:
                archive.event(change)
            archive.settle("unknown")
        tree = getattr(self.runtime, "tree", None)
        if tree is not None:
            selected = set(tree.path(tree.active))
            archive.calls = [call for call in archive.calls if call.run_id in selected]
        async with self.popup(output, session, title="Tools") as surface:
            inspector = ToolInspector(
                archive,
                failed=failed,
                edits=edits,
                rich_theme=self.transcript.rich_theme,
                code_theme=self.transcript.code_theme,
                delta=self.transcript.delta,
                dedent=self.transcript.diff_dedent,
                color_system=self.transcript.console.color_system,
                input=surface.input,
                output=surface.output,
                style=session.app.style,
            )
            await inspector.run()

    def add_running_tools(self, archive) -> None:
        """The journal says what finished; the tools panel, what still runs.

        A journal read mid-turn cannot tell a running call from an abandoned one,
        so it marks both `unknown`.
        """
        running = [call.event for call in self.activity.tools.calls if call.settled is None]
        listed = {call.call_id: call for call in archive.calls}
        for event in running:
            if (call := listed.get(event.call_id)) is None:
                archive.event(event)
            elif call.state == "unknown":
                call.state = "running"

    def diffs(self, argument: str) -> None:
        if argument:
            raise ValueError("Usage: /diffs")
        self.diffs_requested = True

    def links(self, argument: str) -> None:
        if argument:
            raise ValueError("Usage: /links")
        self.links_requested = True

    def copy(self, argument: str) -> None:
        if argument:
            raise ValueError("Usage: /copy")
        self.copy_requested = True

    def paste_image(self, argument: str) -> None:
        """Save the clipboard's image and name it in the draft, which sends it along.

        Terminals paste text only, so the picture is read from the system
        clipboard and kept as a file; the prompt carries its path like any other.
        """
        if argument:
            raise ValueError("Usage: /paste-image")
        # Done by `run_command`, which awaits it and then flushes what it wrote.
        self.paste_requested = True

    async def _paste_image(self) -> None:
        """Read the clipboard off the loop: helpers can take seconds on a large image."""
        self.paste_requested = False
        from pcode.clipboard import image
        from pcode.prompt_images import paste, reference

        data, why = await asyncio.to_thread(image)
        if data is None:
            self.transcript.warning(
                f"{why}. Save the image to a file and put its path in the prompt."
            )
            return
        path, unsendable = await asyncio.to_thread(paste, data)
        if unsendable:
            # Said now, not when the turn starts and finds it cannot be sent.
            self.transcript.warning(f"The pasted image is too large to send: {unsendable}.")
            return
        if self.prompt_session is not None:
            buffer = self.prompt_session.default_buffer
            before = buffer.text[: buffer.cursor_position]
            space = "" if not before or before[-1].isspace() else " "
            buffer.insert_text(f"{space}{reference(path)} ")
            self.prompt_session.app.invalidate()
        self.transcript.flash(f"Pasted an image: {reference(path)}")

    async def choose_copy(self, output: TerminalOutput, session) -> None:
        from pcode.clipboard import copy as copy_to_clipboard
        from pcode.copy_ui import answers
        from pcode.tree_ui import copy_dialog

        self.copy_requested = False
        tree = getattr(self.runtime, "tree", None)
        if thread := self.btw_thread():
            from pcode.aside_ui import thread_tree

            # With a /btw thread open, /copy reads that thread.
            tree = thread_tree(thread)
        history = answers(tree) if tree is not None else []
        if not history:
            self.transcript.note("No response to copy yet.")
            return
        choices = history[0].snippets()
        choice = choices[0]
        if len(history) > 1 or len(choices) > 1:
            async with self.popup(output, session, title="Copy") as surface:
                dialog = copy_dialog(
                    tree,
                    rich_theme=self.transcript.rich_theme,
                    code_theme=self.transcript.code_theme,
                    color_system=self.transcript.console.color_system,
                    input=surface.input,
                    output=surface.output,
                    style=session.app.style,
                )
                choice = await dialog.run_async()
            if choice is None:
                return
        copied, truncated = copy_to_clipboard(choice.text, session.app.output if session else None)
        name = choice.kind
        limit = " (truncated)" if truncated else ""
        if copied:
            self.transcript.flash(f"Copied {name}{limit}.")
        else:
            self.transcript.error(f"Could not copy {name}.")

    async def choose_link(self, output: TerminalOutput, session) -> None:
        from pcode import links_ui
        from pcode.links import conversation_links, link_editor_command, open_link

        self.links_requested = False
        tree = getattr(self.runtime, "tree", None)
        if thread := self.btw_thread():
            from pcode.aside_ui import thread_links

            # With a /btw thread open, /links reads that thread.
            links = message_links = thread_links(thread)
        else:
            has_tree = tree is not None and tree.nodes
            links = conversation_links(tree) if has_tree else []
            message_links = conversation_links(tree, include_tools=False) if has_tree else []
        if not links:
            self.transcript.note(
                "No links in this side thread yet."
                if thread
                else "No links in this conversation yet."
            )
            return
        url: str | None = None
        command: list[str] | None = None
        editor_error: str | None = None
        # ExitStack keeps appearance queries paused until popup() restores raw mode.
        with ExitStack() as stack:
            async with self.popup(
                output, session, layout="sheet", title=links_ui.TITLE, pick=True
            ) as surface:
                url = await links_ui.pick_link(surface, links, message_links, session.app.style)
                if url is not None:
                    try:
                        command = link_editor_command(url)
                    except RuntimeError as error:
                        editor_error = str(error)
                if command is not None and surface.full_screen is not None:
                    # A native list has no terminal: $EDITOR takes pcode's own, as
                    # on the full screen, while this popup still holds the lock.
                    surface.leave_native()
                    async with surface.full_screen():
                        editor_error = await self._run_link_editor(command, stack)
                elif command is not None:
                    editor_error = await self._run_link_editor(command, stack)
        if url is None:
            return
        if editor_error is not None:
            self.transcript.error(f"Could not open {url}: {editor_error}")
            return
        if command is not None:
            self.transcript.flash(f"Opened {url}")
            return
        try:
            open_link(url)
        except (OSError, RuntimeError) as error:
            self.transcript.error(f"Could not open {url}: {error}")
            return
        self.transcript.flash(f"Opened {url}")

    async def _run_link_editor(self, command: list[str], stack: ExitStack) -> str | None:
        """Open a link in $EDITOR in the popup's terminal; an error, or None."""
        from pcode.popup_surface import terminal_fds

        if self._appearance:
            stack.enter_context(self._appearance.paused())
        try:
            await asyncio.to_thread(subprocess.run, command, check=False, **terminal_fds())
        except OSError as error:
            return f"Could not start {command[0]}: {error}"
        return None

    def recorded_edits(self) -> list[EditCompleted]:
        """Prefer the saved journal on the active branch; fall back to this process."""
        from pcode.edits import change_from_record

        saved = getattr(self.runtime, "session", None)
        if saved is None:
            return list(self.edits)
        return [
            change_from_record(record)
            for record in saved.active_records()
            if record.get("kind") == "EditCompleted"
        ]

    def review_key(self) -> str | None:
        """The session id its review checkpoint is kept under; None for an unsaved one."""
        info = getattr(getattr(self.runtime, "session", None), "info", None)
        return getattr(info, "id", None)

    def load_review(self):
        """The session's git review for /diffs, or None outside a git repository."""
        from pcode import git_diff

        info = getattr(getattr(self.runtime, "session", None), "info", None)
        key = self.review_key()
        reviewed = git_diff.reviewed(self.workspace, key) if key else self.reviewed_checkpoint
        return git_diff.load_review(
            self.workspace,
            [edit.path for edit in self.recorded_edits()],
            getattr(info, "start_commit", None),
            getattr(info, "created", None),
            reviewed,
        )

    def mark_reviewed(self, checkpoint) -> None:
        """Record a checkpoint: in git for a saved session, else for this process."""
        from pcode import git_diff

        if (key := self.review_key()) is not None:
            git_diff.mark_reviewed(self.workspace, key, checkpoint)
        self.reviewed_checkpoint = checkpoint

    async def browse_diffs(self, output: TerminalOutput, session) -> None:
        from pcode.edit_ui import DIFF_CHROME, DiffBrowser, notes_prompt, render_review
        from pcode.git_diff import GitDiffError

        self.diffs_requested = False
        delta, dedent = self.transcript.delta, self.transcript.diff_dedent
        # The diff pane's width: the screen less its frame and scrollbar. A
        # popup on an overlay of another width lays it out again (`rewidth`).
        width = max(1, session.app.output.get_size().columns - DIFF_CHROME)

        def load():
            review = self.load_review()
            return review, render_review(review, delta, width, dedent=dedent) if review else {}

        try:
            review, rendered = await asyncio.to_thread(load)
        except GitDiffError as error:
            self.transcript.error(f"git diff unavailable: {plain(str(error), limit=160)}")
            return
        if review is None:
            self.transcript.note(
                "/diffs reviews git changes, and this workspace is not in a git repository."
                " Each file tool's edits are in /tools edits."
            )
            return
        async with self.popup(output, session, title="Diffs") as surface:
            browser = DiffBrowser(
                review,
                reload=self.load_review,
                mark=self.mark_reviewed,
                code_theme=self.transcript.code_theme,
                delta=delta,
                rendered=rendered,
                width=width,
                dedent=dedent,
                input=surface.input,
                output=surface.output,
                style=session.app.style,
                handoff=self._appearance.paused if self._appearance else nullcontext,
            )
            await browser.run()
        if prompt := notes_prompt(browser.notes):
            buffer = session.default_buffer
            draft = buffer.text.rstrip()
            buffer.text = f"{draft}\n\n{prompt}" if draft else prompt
            buffer.cursor_position = len(buffer.text)
            count = len(browser.notes)
            self.transcript.flash(f"Added {count} review note{'s' * (count != 1)} to the prompt.")

    def help(self, argument: str) -> None:
        self.transcript.help(self.registry, self.shortcut)

    def present_events(self, events) -> None:
        present_events(events, activity=self.activity, transcript=self.transcript, edits=self.edits)

    def theme_preview(self, argument: str) -> None:
        self.present_events(self.preview.demo())
        self.transcript.syntax_gallery()

    def theme(self, argument: str) -> None:
        """Pick and save a palette; bare `/theme` flips it for this session only.

        Saving the flip would pin every later session to it, and with it turn
        off following the terminal's light/dark switches.
        """
        theme = argument or ("light" if self.transcript.resolved_theme == "dark" else "dark")
        self.transcript.theme = theme
        if not argument:
            message = f"{theme} for this session; /theme {theme} saves it"
        else:
            self.persist_defaults(theme=theme)
            message = theme
        if theme == "auto":
            message += f" ({self.transcript.resolved_theme})"
            if self._appearance is not None:
                # The detected value may be stale; the answer repaints if it differs.
                self._appearance.refresh()
        self.transcript.flash(f"Theme: {message}.")
        self.transcript.regenerate()

    def follow_appearance(self, detected: str) -> None:
        """The terminal reported its appearance; repaint what follows it.

        Always invalidates the editor: shaded frames mix from the reported
        background even when the theme stays (another dark profile), or under
        a fixed `/theme`. A mode 2031 flip carries no color, so it asks for
        one; the OSC 11 answer records a matching theme, so this asks once.
        """
        reported = terminal_background()
        if self._appearance is not None and (reported is None or reported[1] != detected):
            self._appearance.refresh()
        if detected != self.transcript.detected_theme:
            self.transcript.detected_theme = detected
            if self.transcript.theme == "auto":
                self.transcript.regenerate()
        if self.prompt_session is not None:
            self.prompt_session.app.invalidate()

    def syntax(self, argument: str) -> None:
        """Choose the Pygments style for code, the completion menu and the prompt.

        Each palette keeps its own style, so switching to the other palette and
        back restores the style picked for it rather than the last one set.
        """
        palette = self.transcript.resolved_theme
        if argument:
            SETTINGS[f"syntax_{palette}"].validate(f"syntax_{palette}", argument)
            self.transcript.syntax_themes[palette] = argument
            self.persist_defaults(**{f"syntax_{palette}": argument})
        self.transcript.flash(f"Syntax ({palette}): {self.transcript.syntax_themes[palette]}.")
        self.transcript.regenerate()

    def status(self, argument: str) -> None:
        """Popup in the interactive editor; plain notes wherever there is no editor."""
        if argument:
            raise ValueError("Usage: /status")
        if self.transcript.output is not None:
            self.session_info_requested = True
            return
        for label, value in self.controller.session_overview():
            self.transcript.note(f"{label}: {value}")

    def select_session(self, argument: str) -> None:
        self.session_requested = True

    # Session hosts: the conversation runs in another process (`pcode.host`)
    # and this terminal only renders it, so it can leave and come back.

    @property
    def hosted(self) -> bool:
        # `is True`: a Mock runtime in tests answers every attribute.
        return getattr(self.runtime, "remote", False) is True

    def switch(self, argument: str) -> None:
        """`/attach` (once `/attach`): show another running session in this terminal."""
        if not self.model and not self.hosted:
            raise ValueError("/attach needs a model; this is a local UI preview.")
        argument = argument.strip()
        if argument.split(maxsplit=1)[:1] == ["new"]:
            # Host IDs are hex, so this was only ever the old `/switch new`.
            raise ValueError(
                f"Starting a session is /new PROMPT now (or {self.shortcut('n')} in the "
                "/attach list)."
            )
        if argument == "-" and self.previous_host is None and self.previous_session is None:
            raise ValueError("No previous session in this terminal yet; /attach lists them all.")
        self.check_leaving("/attach")
        self.switch_requested = argument

    def new_in_background(self, prompt: str) -> None:
        """`/new PROMPT`: a new session works on it in a host; this terminal stays here."""
        if not self.model:
            raise ValueError("/new PROMPT needs a model; this is a local UI preview.")
        self.new_requested = prompt

    def fork(self, argument: str) -> None:
        if not self.model:
            raise ValueError("/fork needs a model; this is a local UI preview.")
        if self.active_session_id() is None:
            raise ValueError(
                "Nothing to fork yet: a session is saved with its first prompt. "
                "/new PROMPT starts another beside it."
            )
        prompt = argument.strip()
        # With a prompt the copy works in the background; this terminal stays.
        if not prompt:
            self.check_leaving("/fork")
        self.fork_requested = prompt

    def check_leaving(self, command: str) -> None:
        """Refuse to show another session while leaving would stop this one's turn."""
        if self.hosted:
            return
        if self.host_when_idle and self.handoff_blocker():
            # Not just a turn: a side question would end here too.
            raise ValueError(
                "This session runs in this terminal until it is idle, then moves into a "
                f"background host so {command} leaves it running. Wait or cancel, then retry."
            )
        if not (self.activity.busy or self.activity.queued_prompts):
            return
        raise ValueError(
            "This session runs in this terminal, so leaving it stops its turn. "
            f"Wait or cancel, then {command}."
        )

    def stop_host(self, argument: str) -> None:
        if argument:
            raise ValueError("Usage: /stop")
        if not self.hosted:
            raise ValueError("/stop ends a session host; this session runs in this terminal.")
        # The worktree is left for this terminal to tidy once it exits, asking
        # the way a local session does; a host has nobody to ask.
        self.runtime.stop(keep_worktree=True)
        self.host_stopped = True
        self.running = False

    def detach(self, argument: str) -> None:
        if argument:
            raise ValueError("Usage: /detach")
        if self.hosted:
            self.detach_requested = True
            self.running = False
            return
        # Running here: move it into a host first, and stay on it.
        if not self.model:
            raise ValueError("/detach needs a model; this is a local UI preview.")
        if getattr(self.runtime, "session", None) is None:
            raise ValueError(NOTHING_TO_MOVE)
        # Mid-turn too: the turn stops at its checkpoint and the host continues it.
        self.handoff_requested = True

    def handoff_blocker(self) -> str | None:
        """Why this session, running here, cannot move into a host without stopping anything."""
        if (
            self.activity.busy
            or self.activity.queued_prompts
            or self.controller.working()
            or self.asides.running
            or self.controller.summarizing
        ):
            return (
                "This session runs in this terminal, so what it is doing can't move to a "
                "background host. Wait or cancel, then /detach."
            )
        if getattr(self.runtime, "session", None) is None:
            return NOTHING_TO_MOVE
        return None

    def hand_off_when_idle(self) -> None:
        """Queue the move into a host `host_when_idle` asked for, if nothing holds it back."""
        if (
            self.host_when_idle
            and self.running
            and not self.hosted
            # One at a time; one Ctrl+C dropped with the queue no longer counts.
            and self.handoff_queued != self.controller.prompts.generation
            and self.handoff_blocker() is None
        ):
            self.handoff_queued = self.controller.prompts.generation
            # Through the command queue, so it waits for any command running now.
            # The tag tells it from a `/detach` typed meanwhile (see run_command).
            self.controller.command("/detach", HANDOFF_TAG)

    def hand_off_now(self) -> None:
        """The queued move into a host: quietly not yet, if busy again since."""
        self.handoff_queued = None
        if self.host_when_idle and not self.hosted and self.handoff_blocker() is None:
            self.handoff_requested = True
        # Otherwise the next turn's end (or side answer) tries again.

    def stop_host_on_exit(self) -> None:
        """Quitting (Ctrl+D, `/quit`) ends the host as `/stop` does, unless `/detach` asked."""
        if self.hosted and not (
            self.host_stopped or self.detach_requested or self.session_over or self.runtime.lost
        ):
            self.stop_host("")

    def background_finished(self, entry) -> None:
        """A turn ended in another session: say so on the desktop.

        Nothing is written to this transcript: another session's turn is not
        this conversation. Every finished turn is announced, watched or not,
        and once per turn however many terminals notice it.
        """
        from pcode.host_protocol import claim
        from pcode.terminal_notify import notification

        what = {"failed": "failed", "cancelled": "was cancelled"}.get(entry.outcome, "finished")
        title = plain(entry.label(), 60)
        if self._emulator is None:
            return
        if claim(f"{entry.id}-{entry.turns}"):
            self._emulator(notification(f"pcode: {title} — {what}"))

    def restart(self, argument: str) -> None:
        if argument == "all":
            self.restart_all_requested = True
            return
        if argument:
            raise ValueError("Usage: /restart [all]")
        if not self.hosted:
            raise ValueError("/restart restarts a session host; quit and relaunch for new code.")
        # Mid-turn too: the host stops the turn at its checkpoint, and its
        # successor continues it ahead of anything queued.
        self.restart_requested = True

    async def restart_host(self) -> None:
        async with self._move_lock:
            await self._restart_host()

    async def _restart_host(self) -> None:
        """Restart this host on the code now installed and show the new one here.

        The host starts its successor itself, which keeps its environment and
        remote profile: this terminal may have attached from another project.
        So is one older than that, or with nothing saved to resume (a fresh
        host on the new code replaces it).
        """
        from pcode.host_protocol import NOTHING_TO_RESUME, list_hosts
        from pcode.remote import (
            TOO_OLD_TO_RESTART,
            HostError,
            restart_entry,
            wait_for_exit,
            wait_for_host,
        )
        from pcode.rpc import RemoteError

        self.restart_requested = False
        runtime = self.runtime
        if not self.hosted:
            return  # Restarted meanwhile into a session this terminal does not show.
        busy = bool(self.activity.busy or self.activity.queued_prompts)
        entry = next((e for e in await asyncio.to_thread(list_hosts) if e.id == runtime.id), None)
        if entry is None and busy:
            # Stopping it from here would lose the turn it is running.
            self.transcript.warning(
                "The session host did not restart: its entry is gone, so it cannot be "
                "asked to continue its turn in a successor. Wait or cancel, then /restart."
            )
            return
        if entry is not None:
            successor = skipped = ""
            # Its going is expected: no "has exited" warning for it.
            self.restarting = True
            try:
                with self.activity.waiting("Restarting the session host"):
                    # Idle, an interrupting restart is a plain one.
                    successor, skipped = await restart_entry(entry, interrupt=True)
                    if successor:
                        controller, welcome = await wait_for_host(successor, self, self.activity)
            except (OSError, HostError, RemoteError) as error:
                message = f"The session host did not restart: {error}"
                if successor:
                    message += f" /attach {successor} shows its successor once it is up."
                elif getattr(self.controller, "host_stopped", False) and runtime.session_id:
                    message += f" pcode --continue {runtime.session_id} resumes it."
                if getattr(self.controller, "host_stopped", False):
                    self.activity.busy = False
                self.transcript.error(message)
                return
            finally:
                self.restarting = False
            if successor:
                await self.adopt_controller(
                    controller, welcome, f"Restarted on the current pcode ({successor})"
                )
                return
            if busy or skipped not in (TOO_OLD_TO_RESTART, NOTHING_TO_RESUME):
                # Busy (a host too old to interrupt): stopping it would lose the turn.
                self.transcript.warning(f"The session host did not restart: {skipped}.")
                return
        # Kept: the new host resumes in the same worktree. Its going is
        # expected: an app pane must not take it for a stop and exit.
        self.restarting = True
        try:
            runtime.stop(keep_worktree=True)
            with self.activity.waiting("Stopping the session host"):
                await wait_for_exit(runtime.pid)
        finally:
            self.restarting = False
        if runtime.session_id:
            await self.start_host_session(
                resume=runtime.session_id, note="Restarted on the current pcode"
            )
        else:
            await self.start_host_session("")

    async def restart_stale_hosts(self) -> None:
        """`/restart all`: every host on older pcode restarts, this terminal's own last.

        Each other host starts its own successor, so it keeps its environment
        rather than taking this terminal's. A busy one's turn stops at a
        checkpoint and its successor continues it, as `/restart` does.
        """
        from pcode.host_protocol import code_fingerprint, list_hosts
        from pcode.remote import restart_and_report

        self.restart_all_requested = False
        own = self.runtime.id if self.hosted else None
        code = await asyncio.to_thread(code_fingerprint)
        stale = [entry for entry in await asyncio.to_thread(list_hosts) if entry.stale(code)]
        others = [entry for entry in stale if entry.id != own]
        for entry in others:
            with self.activity.waiting(f"Restarting session {entry.id}"):
                line, failed = await restart_and_report(entry, interrupt=True)
            (self.transcript.warning if failed else self.transcript.note)(line)
        if own is not None and any(entry.id == own for entry in stale):
            await self.restart_host()
        elif not others:
            self.transcript.note("Every session host already runs the pcode installed now.")

    def request_app_restart(self) -> None:
        """SIGUSR2: leave the session running in a host on the pcode installed now, then exit.

        For restarting the terminal app around pcode (dev/app-restart.md):
        once this process has gone, `pcode -c <id>` attaches to that host.
        """
        if self.app_restart_requested:
            return
        self.app_restart_requested = True
        session = self.prompt_session
        future = session.app.future if session is not None else None
        if future is not None and not future.done():
            self._app_restart_task = session.app.create_background_task(self._app_restart())
        # Else `start` schedules it once the editor runs, or the editor has
        # exited already and its cleanup is under way: nothing to start then.

    async def _app_restart(self) -> None:
        """`request_app_restart`'s work: always ends by exiting, whatever the move did."""
        from pcode.host_protocol import code_fingerprint, list_hosts

        try:
            # A host still starting has nothing to move or restart yet.
            await self._startup_done.wait()
            async with self._move_lock:
                if not self.hosted:
                    await self._first_prompt_saved()
                    if self.model and getattr(self.runtime, "session", None) is not None:
                        # A session not saved yet has nothing to carry on.
                        await self._move_to_host(resume_on_failure=False)
                    elif self.activity.queued_prompts:
                        self.transcript.warning(
                            "Not sent, as the session had not started saving yet: "
                            + " · ".join(self.activity.queued_prompts)
                        )
                elif not self.runtime.lost:
                    code = await asyncio.to_thread(code_fingerprint)
                    entries = await asyncio.to_thread(list_hosts)
                    entry = next((e for e in entries if e.id == self.runtime.id), None)
                    if entry is not None and entry.stale(code):
                        await self._restart_host()
        except Exception as error:
            self.transcript.error(error_message(error, unexpected="Moving for the restart failed"))
        finally:
            self._exit_for_app_restart()

    async def _first_prompt_saved(self, timeout: float = FIRST_SAVE_TIMEOUT) -> None:
        """Give a first prompt sent just before the app-restart signal the moment
        it takes to start its turn, which saves the session, so it moves too."""
        runtime = self.runtime
        if not self.model or getattr(runtime, "session_factory", None) is None:
            return  # Never saved: nothing would move however long this waited.
        deadline = time.monotonic() + timeout
        while (
            getattr(runtime, "session", None) is None
            and (self.activity.queued_prompts or self.controller.turn_running())
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)

    def _exit_for_app_restart(self) -> None:
        """Exit, leaving any host running, past any popup or nested prompt that is open."""
        if self.hosted:
            self.detach_requested = True  # Not `stop_host_on_exit`'s stop.
        elif isinstance(self.controller, SessionController):
            # A move that failed holds the queue: it must not start a turn on the way out.
            self.controller.handoff_idle.clear()
        self.running = False
        session = self.prompt_session
        future = session.app.future if session is not None else None
        if future is None or future.done():
            return
        # The exit cancels the editor's background tasks (popups and nested
        # prompts run in them) only after waiting for any terminal handoff to
        # end, and an open full-screen popup holds one until it closes: stop
        # them first. Exiting would cancel every one of them anyway.
        current = asyncio.current_task()
        for task in list(getattr(session.app, "_background_tasks", ())):
            if task is not current and not task.done():
                task.cancel()
        session.app.exit()

    async def move_to_host(self) -> None:
        """`/detach` in a session running here: carry it on in a new host, still shown here.

        The conversation, its worktree and its running jobs go to the host;
        only this terminal's own process stays behind. Mid-turn, the turn
        stops at its checkpoint and the host continues it ahead of anything
        queued (`settle_for_handoff`). Should the host fail to start, the
        session carries on here as if nothing happened.
        """
        async with self._move_lock:
            await self._move_to_host()

    async def _move_to_host(self, *, resume_on_failure: bool = True) -> None:
        """`move_to_host` under its lock. Without `resume_on_failure` a failed move
        leaves the session stopped (the app-restart signal is about to exit):
        nothing queued starts here, nor does the interrupted turn continue."""
        self.handoff_requested = False
        # One try: should it fail, the session carries on here as before.
        self.host_when_idle = False
        if self.hosted or self.moved_to_host or not isinstance(self.controller, SessionController):
            return  # Moved already (a `/detach` and the app-restart signal both asked).
        if getattr(self.runtime, "session", None) is None:
            raise ValueError(NOTHING_TO_MOVE)
        interrupted = await self.controller.settle_for_handoff()
        try:
            await self._hand_to_host(interrupted)
        except BaseException:
            if resume_on_failure and not self.moved_to_host:
                self.controller.resume_here(interrupted)
            raise

    async def _hand_to_host(self, interrupted: bool) -> None:
        from filelock import Timeout

        from pcode.remote import HostError, spawn_host, wait_for_host
        from pcode.sessions import SessionError, is_open

        runtime = self.runtime
        saved = runtime.session
        jobs = getattr(runtime, "jobs", None)
        # Both before the host starts: it takes the session's lock, and adopts
        # jobs while it boots.
        # A title still being asked for would be written beside the host's own.
        await self.controller.stop_naming()
        released = jobs.release(saved.info.id) if jobs is not None else None
        saved.close()
        wait = self.activity.begin_wait("Moving this session to a background host")
        process = None
        let_go = False
        try:
            if is_open(saved.directory):
                # A lock is per thread: one taken on another thread is still held.
                raise SessionError("This session's lock could not be handed to a host.")
            let_go = True
            identity, process, log = await asyncio.to_thread(
                spawn_host,
                model=self.model,
                workspace=self.workspace,
                resume=saved.info.id,
                session_dir=self.session_dir,
                # No `carry`: the host continues the turn only once this terminal
                # has it (`adopt_controller`), so a host that fails to come up and
                # is killed never runs it alongside `resume_here`.
            )
            controller, welcome = await wait_for_host(identity, self, self.activity, process, log)
            # From here the host owns the session: an exit before this terminal
            # shows it must leave its worktree to it.
            self.moved_to_host = True
        except BaseException as error:
            if process is not None and process.poll() is None:
                # Killed, never stopped: a stopped host tidies the worktree
                # this session is still working in.
                process.kill()
                await asyncio.to_thread(process.wait)
            try:
                saved.lock.acquire(timeout=0)
            except Timeout:
                if let_go:
                    self.transcript.warning(
                        "Another process opened this session meanwhile; turns here no "
                        "longer save safely. Quit, then `pcode --continue` it."
                    )
            if released is not None and not jobs.reclaim(released):
                # The killed host took them over first: its record is an orphan now.
                self.controller.adopt_jobs()
            if not isinstance(error, Exception):
                raise
            # Host errors carry the log tail and path; nothing secret.
            detail = str(error) if isinstance(error, (HostError, OSError)) else error_message(error)
            raise SessionError(
                f"Could not move this session to a background host, so it carries on here: {detail}"
            ) from error
        finally:
            self.activity.end_wait(wait)
        # At once, before anything below can fail or be cancelled (a quit
        # mid-move): the host owns the session now, and nothing else would
        # continue its turn or send what was typed for it. What the host shows
        # meanwhile waits for `controller.start`.
        if interrupted:
            controller.continue_interrupted()
        unrun: list[str] = []
        self._carry_typed(self.controller, controller, unrun)
        where = (
            f"Moved mid-turn; continuing in the background host {identity}"
            if interrupted
            else f"Moved to background session {identity}"
        )
        try:
            await self.adopt_controller(
                controller,
                welcome,
                f"{where}: /detach or closing the terminal now leaves it running",
                handed_off=True,
                unrun=unrun,
            )
        finally:
            # After the replay, which would redraw it away.
            if unrun:
                self.transcript.warning(
                    "Not run, as this terminal is exiting; run again in the new one if "
                    "still wanted: " + ", ".join(unrun)
                )

    def _carry_typed(self, previous: SessionController, controller, unrun: list[str]) -> None:
        """Send what was typed for `previous` on to the host it moved into, in order.

        A command the terminal runs itself queues here, as a hosted terminal's
        always does, unless this process is exiting for an app restart, which
        would cancel it unrun: it goes in `unrun`, for the caller to name.
        """
        for text, mode in previous.take_typed():
            if mode != COMMAND_MODE:
                controller.submit(text, mode)
            elif not runs_in_terminal(text):
                controller.command(text, self._popup_generation)
            elif self.app_restart_requested or self._run_in_terminal is None:
                unrun.append(text)
            else:
                self._run_in_terminal(text, self._popup_generation)

    @staticmethod
    def _retire(previous: SessionController) -> None:
        """The session left `previous` for a host: it runs nothing more here,
        and nothing waits on it to."""
        previous.running = False
        previous.superseded = True
        previous.handoff_idle.set()

    async def switch_session(self, output: TerminalOutput, session) -> None:
        """`/attach`: pick a running host (or start one) and show it in this terminal."""
        from pcode.host_protocol import find_host, forget_stopped, list_hosts, list_stopped

        argument, self.switch_requested = self.switch_requested or "", None
        previous = argument == "-"
        if previous:
            if self.previous_host is None:
                # The session left ran in this terminal: carry it on in a host.
                if self.previous_session:
                    await self.open_saved_session(self.previous_session, self.active_session_id())
                return
            argument = self.previous_host
        current = getattr(self.runtime, "id", None) if self.hosted else None
        if argument:
            try:
                entry = await asyncio.to_thread(find_host, argument)
            except LookupError as error:
                # Its host stopped once idle: carry the conversation on instead.
                stopped = (
                    self.previous_session
                    if previous
                    else await asyncio.to_thread(_stopped_session, argument, self.session_dir)
                )
                if stopped:
                    forget_stopped(stopped)
                    await self.open_saved_session(stopped, self.active_session_id())
                    return
                self.transcript.warning(
                    f"The previous session ({argument}) is no longer running; /attach lists "
                    "the ones that are."
                    if previous
                    else str(error)
                )
                return
            if entry.id == current:
                self.transcript.note("This terminal is already showing that session.")
                return
            await self.attach_host(entry)
            return
        # Opened even with none listed: its new-session key starts an empty one.
        entries = await asyncio.to_thread(lambda: list_hosts() + list_stopped())
        from pcode import host_ui
        from pcode.host_protocol import code_fingerprint

        code = code_fingerprint()
        async with self.popup(
            output, session, layout="sheet", title=host_ui.TITLE, pick=True
        ) as surface:
            picked = await surface.pick(
                host_ui.host_items(entries, current, code=code),
                title=host_ui.TITLE,
                header=host_ui.header(entries),
                empty=host_ui.empty(entries),
                match="substring",
                actions=(host_ui.NEW_SESSION, host_ui.STOP),
                style=session.app.style,
            )
        choice = host_ui.choice(picked)
        if choice is None:
            return
        action, identity = choice
        if action == "new":
            await self.start_host_session("")
            return
        entry = next(entry for entry in entries if entry.id == identity)
        if entry.state == "stopped":
            # Seen once chosen either way; a resumed host forgets it too.
            forget_stopped(entry.session_id)
            if action == "stop":
                self.transcript.flash(f"Removed {entry.label()[:60]} from /attach.")
                return
            await self.open_saved_session(entry.session_id, self.active_session_id())
        elif action == "stop":
            await self.stop_other_host(entry)
        elif entry.id == current:
            self.transcript.note("This terminal is already showing that session.")
        else:
            await self.attach_host(entry)

    def session_ended(self) -> None:
        """The host: a command from this terminal ended the session, so the host is stopping."""
        self.session_over = True
        self.running = False
        app = self.prompt_session.app if self.prompt_session is not None else None
        # Not twice: `/stop` may already have asked it to exit.
        if app is not None and app.is_running and not (app.future is None or app.future.done()):
            app.exit()

    def host_closed(self) -> None:
        """The host this terminal was showing went away (stopped elsewhere, or crashed)."""
        if self.session_over or self.restarting:
            return  # Expected: this terminal is on its way out, or to the successor.
        runtime = self.runtime
        successor = getattr(self.controller, "successor", None)
        if self.editor_only and not successor:
            self._app_host_closed(runtime)
            return
        if self.editor_only:
            # The warning below goes nowhere in an app pane: say it in the editor.
            self.activity.flash(
                f"The session host is restarting as {successor}: /attach {successor}.",
                seconds=24 * 3600,
            )
        if successor:
            self.transcript.warning(
                f"The session host {runtime.id} is restarting on the current pcode as "
                f"{successor}: /attach {successor} shows it once it is up."
            )
        else:
            session = runtime.session_id and f" pcode --continue {runtime.session_id} resumes it."
            self.transcript.warning(
                f"The session host {runtime.id} has exited. /attach picks another;{session}"
            )
        self.activity.busy = False
        if self.activity.startup_label:
            self.activity.finish_startup(ok=False)
        self.redraw()

    def _app_host_closed(self, runtime) -> None:
        """`host_closed` in a Pcode.app pane, whose transcript goes nowhere.

        Stopped on purpose (`pcode --kill-hosts`, the sidebar, another
        terminal's /stop): exit, so the app closes the pane and the session
        drops back to its recent list. Gone without a word (crashed, killed):
        keep the pane and say so in the editor, the one place this shows text;
        otherwise the pane sits there with no host and an empty transcript.
        """
        if getattr(self.controller, "host_stopped", False):
            self.session_ended()
            return
        self.activity.busy = False
        if self.activity.startup_label:
            self.activity.finish_startup(ok=False)
        self.activity.flash(
            f"The session host {runtime.id} has exited. /resume reopens a session here.",
            seconds=24 * 3600,
        )
        self.redraw()

    async def stop_other_host(self, entry) -> None:
        from pcode.remote import stop_entry

        if self.hosted and entry.id == self.runtime.id:
            self.stop_host("")
            return
        with self.activity.waiting(f"Stopping session {entry.id}"):
            await stop_entry(entry)
        self.transcript.flash(f"Stopped session {entry.id} ({entry.label()[:60]}).")

    async def _connect_or_respawn_host(self, launch):
        """Connect to `launch`, spawning a fresh host if a resumed host just vanished."""
        from pcode.host_protocol import list_hosts
        from pcode.remote import HostError, HostLaunch, app_host_id, spawn_host, wait_for_exit

        try:
            return await launch.connect(self, self.activity)
        except HostError:
            # `--continue` routed to an existing host that stopped before we could
            # attach (idle-stopped, or killed when an older app quit): start a new one.
            if launch.process is not None or not launch.resume:
                raise
            # Wait for the dying host process to exit and release `.lock` so we
            # don't fork the session while it is still finishing shutdown.
            if launch.pid is not None:
                await wait_for_exit(launch.pid, timeout=15.0)
            hosts = list_hosts(launch.directory)
            # Another live host (e.g. a `/restart` successor) may already own it.
            existing = next((other for other in hosts if other.session_id == launch.resume), None)
            if existing is not None:
                if existing.id == launch.id:
                    raise
                return await HostLaunch.running(existing).connect(self, self.activity)
            workspace = self.workspace
            if not workspace.is_dir():
                from pcode.sessions import read_info, resolve_session

                path = resolve_session(launch.resume, self.session_dir, Path.cwd())
                workspace = _resumed_workspace(read_info(path), None).resolve()
            identity, process, log = await asyncio.to_thread(
                spawn_host,
                identity=app_host_id(launch.directory),
                model=self.model,
                workspace=workspace,
                resume=launch.resume,
                session_dir=self.session_dir,
                directory=launch.directory,
            )
            fresh = HostLaunch(identity, process, log, directory=launch.directory)
            return await fresh.connect(self, self.activity)

    async def attach_host(self, entry) -> None:
        from pcode.host_protocol import list_hosts
        from pcode.remote import HostError, HostLaunch

        try:
            with self.activity.waiting(f"Connecting to session {entry.id}"):
                controller, welcome = await HostLaunch.running(entry).connect(self, self.activity)
        except HostError:
            # Picked from a list read a moment ago, and stopped since for being idle.
            gone = all(other.id != entry.id for other in list_hosts())
            if not (gone and entry.session_id):
                raise
            await self.open_saved_session(entry.session_id, self.active_session_id())
            return
        await self.adopt_controller(controller, welcome, f"Switched to session {entry.id}")

    async def fork_session(self, prompt: str) -> None:
        """`/fork`: continue a copy of this conversation in a host of its own.

        The copy has this conversation's active branch, up to its last settled
        step when a turn is running, and in git a worktree of its own started
        from the commit this workspace is on.
        """
        from pcode import worktree

        identity = self.active_session_id()
        if identity is None:  # `switch` checked; the session cannot go unsaved since.
            return
        workspace = self.workspace
        if await asyncio.to_thread(worktree.head_commit, workspace) is None:
            outside = await asyncio.to_thread(worktree.main_checkout, workspace) is None
            why = "is not a git checkout" if outside else "has no commit to branch from"
            self.transcript.note(f"{workspace} {why}, so the fork shares it with this session.")
        elif await asyncio.to_thread(worktree.is_dirty, workspace):
            self.transcript.note(
                "Uncommitted changes here stay behind: the fork's worktree starts from HEAD."
            )
        await self.start_host_session(prompt, resume=identity, fork=True)

    async def start_host_session(
        self,
        prompt: str = "",
        *,
        resume: str | None = None,
        note: str | None = None,
        fork: bool = False,
    ) -> None:
        """Start a host for a new (or resumed) conversation beside this one.

        A new one starts from the main checkout, so with the worktree setting on
        it gets its own worktree rather than sharing this one. A `fork` of
        `resume` continues a copy of it in a worktree cut from this one's HEAD.
        With a prompt it is left working in the background; without, this
        terminal switches to it.
        """
        from pcode import worktree
        from pcode.remote import spawn_host, wait_for_host

        base = (
            self.workspace if resume else worktree.main_checkout(self.workspace) or self.workspace
        )
        if fork:
            waiting = f"Forking {resume[:8]} into a new session host"
        elif resume:
            waiting = f"Resuming {resume[:8]} in a new session host"
        else:
            waiting = "Starting a session host"
        wait = self.activity.begin_wait(waiting)
        try:
            identity, process, log = await asyncio.to_thread(
                spawn_host,
                model=self.model,
                workspace=base,
                resume=resume,
                session_dir=self.session_dir,
                fork=fork,
                worktree=fork,
            )
            self.transcript.note(f"Starting session {identity}… (log: {log})")
            if prompt:
                # Its state goes nowhere: this terminal stays on its own session.
                controller, _ = await wait_for_host(identity, None, Activity(), process, log)
            else:
                controller, welcome = await wait_for_host(
                    identity, self, self.activity, process, log
                )
        finally:
            self.activity.end_wait(wait)
        if prompt:
            controller.submit(prompt, "queue")
            # Just started, so the one to look in on: `/attach -` goes there.
            self.previous_host = identity
            self.previous_session = controller.runtime.session_id or None
            controller.close()
            what = f"Forked into session {identity}, which is" if fork else f"Session {identity} is"
            self.transcript.note(
                f"{what} working on it in the background. "
                "Ctrl+^ (or /attach -) shows it, /attach lists every session; "
                "this terminal stays here."
            )
            if self._host_watch is not None:
                self._host_watch()
            # An unsaved session here has nothing to keep running.
            saved = getattr(self.runtime, "session", None)
            if not self.hosted and saved is not None and not self.host_when_idle:
                self.host_when_idle = True
                self.transcript.note(
                    "This session moves into a background host once it is idle, so "
                    "switching to the other one leaves it running."
                )
            self.hand_off_when_idle()
            return
        if fork:
            what = f"Forked into session {identity}"
        elif resume:
            what = f"Resumed {resume} in session host {identity}"
        else:
            what = f"New session {identity}"
        await self.adopt_controller(controller, welcome, f"{note} ({identity})" if note else what)

    async def adopt_controller(
        self,
        controller,
        welcome: dict,
        note: str,
        *,
        handed_off: bool = False,
        preserve_welcome: bool = False,
        unrun: list[str] | None = None,
    ) -> None:
        """Show the host behind `controller` in this terminal, leaving the current session.

        A session running in this process ends here (its journal keeps it for
        /resume or `pcode -c`); another host keeps running. `handed_off`: the
        session moved into that host, which has its turn and what was typed
        for it already (`_hand_to_host`); typed since goes there too, with
        terminal commands that will not run collected in `unrun`.
        """
        unrun = [] if unrun is None else unrun
        previous = self.controller
        if isinstance(previous, SessionController):
            # Handed off: the host now runs that same conversation.
            if not handed_off and (saved := getattr(previous.runtime, "session", None)):
                note += f" · left {saved.info.id} (/attach - resumes it)"
                # No host to come back to: `/attach -` resumes it in one.
                self.previous_host = None
                self.previous_session = saved.info.id
            elif not handed_off:
                # Nothing saved to come back to, and the session last started
                # in the background may be the one shown next.
                self.previous_host = self.previous_session = None
            try:
                await self.leave_controller(previous)
            except BaseException:
                if handed_off:
                    # Cancelled (a quit) or failed: the host has the session all the same.
                    self._carry_typed(previous, controller, unrun)
                    self._retire(previous)
                    self.transcript.warning(
                        f"This session carries on in the background host {controller.id}; "
                        f"reattach with: pcode --attach {controller.id}"
                    )
                    # Leaving it running, once what was sent to it has gone out.
                    with suppress(Exception, asyncio.CancelledError):
                        await asyncio.wait_for(controller.detach(), 2.0)
                raise
            close = getattr(previous.runtime, "close", None)
            if close is not None:
                close()
            # No await from here to the swap, so nothing typed falls between.
            if handed_off:
                # Typed while it was let go: meant for the session, which runs there.
                self._carry_typed(previous, controller, unrun)
            else:
                previous.clear_queue()  # Says what was dropped, as leaving it always has.
        else:
            if not previous.runtime.lost:
                self.previous_host = previous.id
                self.previous_session = previous.runtime.session_id or None
            previous.close()
        self.controller = controller
        if isinstance(previous, SessionController):
            # Only now, while this terminal reads `running` from the new one:
            # the loop running this command ends once it has finished.
            self._retire(previous)
        controller.on_closed = self.host_closed
        if forked := welcome.get("forked_from"):
            note += f" · continuing a copy of {forked}"
            if not welcome.get("fork_requested"):
                note += ", which was open elsewhere"
        self._attach_note = note
        self.activity.reset()
        if self._progress is not None:
            self._progress.switched()
        self.reconcile_terminal()
        self.edits.clear()
        self.reviewed_checkpoint = None
        for name, value in welcome["activity"].items():
            controller.apply_field(name, value)
        self.commands_changed()
        journal = controller.runtime.session
        end = welcome["settled_end"] if welcome["settled_end"] is not None else 0
        if preserve_welcome and (journal is None or not journal.transcript_records(end)):
            # Brand-new hosted session: keep the welcome banner in scrollback
            # rather than clearing the screen to draw a single attach note.
            if journal is not None:
                self.activity.plan = journal.latest_plan()
            self.activity.tools.clear()
            self.transcript.retained_note(note)
        elif journal is not None:
            self.replay(journal, note=note, end=end)
        else:
            with self.transcript.restore():
                self.transcript.retained_note(note)
        await controller.start(welcome)
        if controller.startup_pending:
            self.activity.begin_startup("Starting agent")
        elif self.activity.startup_label:
            self.activity.finish_startup(ok=controller.startup_error is None)
        if self._host_watch is not None:
            self._host_watch()
        self.redraw()

    def select_tree(self, argument: str) -> None:
        self.tree_requested = True

    def auto_open_asides(self) -> bool:
        """Whether a settled side answer should open the viewer by itself."""
        from pcode.btw_view import current

        if self.aside_view_open or current(self.activity) or self._thread_takes_draft():
            return False
        default = SETTINGS["btw_auto_open"].default
        return load_preferences().get("btw_auto_open", default) == "on"

    async def read_asides(self):
        """The side-answer viewer; returns a thread to bring into the conversation, if any.

        With a drawer (`btw_drawer`) it opens the thread there, for the main
        editor to reply to, and returns at once: the view keeps a thread itself
        (`keep_thread`), so no command waits on it. Asked for with several
        threads, it lets the reader pick which; Ctrl+Q already goes straight
        back to the last one.
        """
        from pcode.btw_view import current, open_thread

        latest = self.asides.latest()
        auto = (
            self._auto_opens_pending > 0
            and self._command_popup_generation is not None
            and self._command_popup_generation == self._auto_open_generation
        )
        if auto:
            self._auto_opens_pending -= 1
        if self.btw_drawer():
            if latest is None:  # The command said so already.
                return None
            # Queued when the answer arrived: since then the reader may have
            # opened a thread and be mid-reply, which this must not take over.
            if auto and (current(self.activity) or self._thread_takes_draft()):
                return None
            thread = latest.thread
            if not auto and len(self.asides.threads()) > 1:
                browser, _ = await self._browse_asides(selected=latest.id, pick=True)
                thread = browser.picked if browser else None
                if thread is None:
                    return None
            open_thread(self.activity, thread)
            self.redraw()
            return None
        _, request = await self._browse_asides(
            ask=self.controller.follow_up_aside,
            check_bridge=self.controller.check_bridge,
            selected=latest.id if latest else None,
            edit=not auto,
        )
        return request

    async def _browse_asides(self, **options):
        """Run the side-answer viewer: it and what it returned, or Nones if superseded."""
        from pcode.aside_ui import AsideBrowser

        output, session = self.output, self.prompt_session
        self.aside_view_open = True
        try:
            async with self.popup(output, session, title="Side questions") as surface:
                browser = AsideBrowser(
                    self.asides,
                    stop=self.controller.cancel_asides,
                    rich_theme=self.transcript.rich_theme,
                    code_theme=self.transcript.code_theme,
                    color_system=self.transcript.console.color_system,
                    input=surface.input,
                    output=surface.output,
                    style=session.app.style,
                    handoff=self._appearance.paused if self._appearance else nullcontext,
                    **options,
                )
                return browser, await browser.run()
        except _PopupSuperseded:
            return None, None
        finally:
            self.aside_view_open = False

    # --- The /btw thread in the drawer, replied to from the editor (`pcode.btw_view`) ---

    def btw_drawer(self) -> bool:
        """Whether /btw threads open in a drawer, where the main editor replies to them.

        Without one (`drawer_host`), the popup viewer shows them.
        """
        return self.transcript.console.is_terminal and self.drawer_host() is not None

    def drawer_host(self) -> str | None:
        """The session host a drawer beside the terminal reads threads from (`pcode.drawer`).

        Only for a terminal that answered the drawer probe, while `popup_view`
        is not `popup`, and
        a session host whose socket that terminal can find; else None. Asked
        every frame, so the setting is read again at most once a second.
        """
        from pcode import drawer, theme
        from pcode.host_protocol import host_dir

        if not theme.drawer_supported():
            return None
        now = time.monotonic()
        if now - self._drawer_pref_at > 1.0:
            self._drawer_pref_at = now
            self._drawer_pref = overlay_popups()
        runtime = getattr(self.controller, "runtime", None)
        if not (self._drawer_pref and getattr(runtime, "remote", False)):
            return None
        if not drawer.reachable(host_dir()) or not drawer.valid_host(runtime.id):
            return None
        return runtime.id

    def _thread_takes_draft(self) -> bool:
        """Whether opening a thread now would take a draft meant for the conversation.

        An answer that opens by itself must not: Enter would then send the
        draft to the thread instead.
        """
        session = self.prompt_session
        draft = session.default_buffer.text.strip() if session is not None else ""
        return bool(draft) and self.btw_drawer()

    def btw_key(self) -> None:
        """Ctrl+Q: toggle between the conversation and the thread last shown.

        Without a drawer it is a bare /btw: the viewer.
        """
        from pcode.btw_view import current, open_thread, reopen_thread

        activity = self.activity
        if current(activity):
            self.close_btw()
        elif not self.btw_drawer():
            self._submit_typed("/btw")
        elif (thread := reopen_thread(activity)) is None:
            self.transcript.flash("No side questions yet. Ask one with /btw QUESTION.")
        else:
            open_thread(activity, thread)

    def btw_next(self) -> None:
        """Ctrl+\\: show the next older thread in place of this one, wrapping to the newest."""
        from pcode.btw_view import next_thread, open_thread

        thread = next_thread(self.activity)
        if thread is None:
            self.transcript.flash("This is the only side thread.")
        else:
            open_thread(self.activity, thread)

    def asides_seen(self, identities: list[str]) -> None:
        """The thread view showed these answers; a session host hears so (`asides_read`)."""
        seen = getattr(self.controller, "asides_seen", None)
        if seen is not None:
            seen(identities)

    def btw_thread(self) -> list:
        """The open /btw thread, which the editor, /copy and /links then go to."""
        from pcode.btw_view import current

        return current(self.activity)

    def close_btw(self) -> None:
        from pcode.btw_view import close

        close(self.activity)
        self.redraw()

    def btw_submit(self, text: str) -> bool | None:
        """Send `text` to the open /btw thread.

        None leaves it to the conversation: commands other than the thread's
        own, and anything typed while no thread is open. Those close the
        thread first, as their output goes to the conversation; /copy and
        /links read the thread, and /btw asks beside it, so they stay.
        True keeps the draft, refused with the reason shown.
        """
        from pcode.btw_view import current

        if not current(self.activity):
            return None
        parts = text.split(maxsplit=1)
        if not parts:
            return None
        name = parts[0]
        argument = parts[1] if len(parts) > 1 else ""
        if shell_command(text) is not None:
            # A `!command` runs in the shell, as ever.
            self.close_btw()
            return None
        thread = current(self.activity)[0].thread
        # `/etc/hosts …` is a path, so a question, as in the popup's editor.
        if not text.startswith("/") or "/" in name[1:]:
            try:
                self.controller.follow_up_aside(thread, text)
            except ValueError as error:
                self.transcript.flash(str(error))
                return True
            return False
        if name == "/close":
            self.close_btw()
            return False
        if name == "/stop":
            if not self.controller.cancel_asides() and not self.hosted:
                self.transcript.flash("No side question is running.")
            return False
        if name in ("/merge", "/summarize"):
            action = "merge" if name == "/merge" else "summary"
            try:
                self.controller.check_bridge(thread, action)
            except ValueError as error:
                self.transcript.flash(str(error))
                return True

            async def keep() -> None:
                try:
                    await self.controller.keep_thread(thread, action, argument.strip())
                except ValueError as error:
                    if self.activity.btw_thread is not None:
                        self.transcript.flash(str(error))
                    self.transcript.warning(str(error))
                    return
                except Exception as error:
                    message = error_message(error, unexpected=f"{name} failed")
                    if self.activity.btw_thread is not None:
                        self.transcript.flash(message)
                    self.transcript.error(message)
                    return
                if self.activity.btw_thread == thread:
                    self.close_btw()

            task = asyncio.create_task(keep())
            self._btw_tasks.add(task)
            task.add_done_callback(self._btw_tasks.discard)
            return False
        if name not in ("/copy", "/links", "/btw", "/side"):
            # Unknown slash commands (like a mistyped /summarize) stay in the
            # thread with the draft intact instead of closing the drawer.
            if self.registry.find(name) is None:
                self.transcript.flash(f"Unknown command: {name}")
                return True
            self.close_btw()
        return None

    def agents(self, argument: str) -> None:
        if not self.activity.workers.items:
            self.transcript.note("No agents yet. They appear once the model delegates a task.")
            return
        self.worker_view_requested = True

    async def read_workers(self, output: TerminalOutput, session) -> None:
        from pcode.worker_ui import WorkerBrowser

        self.worker_view_requested = False
        async with self.popup(output, session, title="Agents") as surface:
            browser = WorkerBrowser(
                self.activity.workers,
                rich_theme=self.transcript.rich_theme,
                code_theme=self.transcript.code_theme,
                color_system=self.transcript.console.color_system,
                show_thinking=self.activity.show_thinking,
                input=surface.input,
                output=surface.output,
                style=session.app.style,
            )
            prefill = await browser.run()
        if prefill:
            # "Answer for w2: " and friends: the user finishes it, the parent
            # relays it. A draft already typed is kept after it.
            buffer = session.default_buffer
            draft = buffer.text
            buffer.text = f"{prefill}{draft}"
            buffer.cursor_position = len(prefill)

    async def choose_tree(self, output: TerminalOutput, session) -> None:
        from pcode.tree_ui import tree_dialog

        self.tree_requested = False
        tree = getattr(self.runtime, "tree", None)
        if tree is None or not tree.nodes:
            self.transcript.note("No conversation turns yet. Send a message to start a tree.")
            return
        # Reading the tree is safe at any time; switching context is not, because
        # a running turn owns the history it would be replaced with. Browse now,
        # fork when the turn ends — or ask the branch a side question with /btw.
        navigable = not (self.activity.busy or self.activity.queued)
        async with self.popup(output, session, title="Conversation tree") as surface:
            dialog = tree_dialog(
                tree,
                navigable=navigable,
                rich_theme=self.transcript.rich_theme,
                code_theme=self.transcript.code_theme,
                color_system=self.transcript.console.color_system,
                input=surface.input,
                output=surface.output,
                style=session.app.style,
            )
            selection = await dialog.run_async()
        if selection is not None:
            identity, edit = selection
            draft = await self.controller.navigate_tree(identity, edit=edit)
            # A cancelled picker leaves the editor alone. Only user selection prefills it.
            if edit:
                session.default_buffer.text = draft
                session.default_buffer.cursor_position = len(draft)

    async def choose_session(self, output: TerminalOutput, session) -> None:
        self.session_requested = False

        def load():
            # Off the event loop: the first import of pcode.sessions pulls in the
            # agent stack, and grouping by repository can still run git for an
            # unusual checkout, so the browser gets every workspace's scope ready.
            from pcode.sessions import list_sessions
            from pcode.worktree import repo_scope

            records = list_sessions(self.session_dir)
            spaces = {Path(info.workspace).resolve() for info in records}
            scopes = {space: repo_scope(space) for space in spaces}
            scope = repo_scope(self.workspace)
            return records, scopes, any(found == scope for found in scopes.values())

        with self.activity.waiting("Loading saved sessions"):
            records, scopes, any_here = await asyncio.to_thread(load)
        from pcode.session_ui import SessionBrowser
        from pcode.sessions import session_root

        if not any_here:
            self.transcript.note("No saved sessions for this workspace.")
            return
        active_id = self.active_session_id()
        async with self.popup(output, session, title="Sessions") as surface:
            browser = SessionBrowser(
                records,
                root=self.session_dir or session_root(),
                workspace=self.workspace,
                active_id=active_id,
                scopes=scopes,
                rich_theme=self.transcript.rich_theme,
                code_theme=self.transcript.code_theme,
                color_system=self.transcript.console.color_system,
                input=surface.input,
                output=surface.output,
                style=session.app.style,
            )
            identity = await browser.run()
        if identity is None:
            return
        await self.open_saved_session(identity, active_id)

    def active_session_id(self) -> str | None:
        if self.hosted:
            return self.runtime.session_id or None
        current = getattr(self.runtime, "session", None)
        return current.info.id if current else None

    def active_host_id(self) -> str | None:
        return (self.runtime.id or None) if self.hosted else None

    async def open_saved_session(self, identity: str, active_id: str | None) -> None:
        """Show a saved session here: where it runs, else resumed in a host or this process."""
        from pcode.host_protocol import list_hosts

        running = [entry for entry in list_hosts() if entry.session_id == identity]
        if identity == active_id:
            self.transcript.note("This session is already active.")
        elif running:
            # Shown where it runs: resuming the saved session would continue a copy.
            await self.attach_host(running[0])
        elif self.hosted:
            await self.start_host_session(resume=identity)
        else:
            await self.controller.resume_session(identity)

    async def show_session_info(self, output: TerminalOutput, session) -> None:
        from pcode.session_ui import session_info_dialog

        self.session_info_requested = False
        rows = await self.controller.query("session_overview")
        if self.hosted:
            from pcode.host_protocol import find_host
            from pcode.host_ui import status_rows

            try:
                entry = await asyncio.to_thread(find_host, self.runtime.id)
            except LookupError:
                host = [("Host", f"{self.runtime.id} (pid {self.runtime.pid}) · not running")]
            else:
                host = status_rows(entry)
            rows = [*host, *rows]
        async with self.popup(output, session, layout="sheet", title="Session") as surface:
            dialog = session_info_dialog(
                rows,
                input=surface.input,
                output=surface.output,
                style=session.app.style,
            )
            await dialog.run_async()

    def replay(self, saved=None, *, note: str | None = None, end: int | None = None) -> None:
        """Redraw a saved conversation. `saved` names one the runtime does not hold yet.

        `end` stops at that journal offset: a host sends the rest as it happened.
        """
        from pcode.diagnostics import strip_controls

        saved = saved or self.runtime.session
        self.activity.plan = saved.latest_plan()
        # Reopening never leaves tools running. Settled results belong to the
        # transcript, including hidden command payloads needed by later toggles.
        self.activity.tools.clear()
        with self.transcript.restore():
            self.transcript.retained_note(note or f"Resumed {saved.info.id}")
            if saved.forked_from:
                self.transcript.retained_note(forked_note(saved))
            for record in saved.transcript_records(end):
                kind = record["kind"]
                if kind in ("turn_started", "steering"):
                    if record["prompt"]:
                        self.transcript.user(strip_controls(record["prompt"]))
                elif kind in ("Thinking", "thinking_partial"):
                    self.transcript.thinking(strip_controls(record["text"]).rstrip("\n") + "\n\n")
                elif kind == "CacheBust":
                    continue  # Kept in the journal; live runs show it in the footer.
                elif kind == "EditCompleted":
                    from pcode.edits import change_from_record

                    self.transcript.edit(change_from_record(record))
                elif kind in {"ToolSummary", "JobFinished"}:
                    result = record.get("result")
                    self.transcript.tool_result(
                        ToolSummary(
                            record["name"],
                            strip_controls(record["detail"]),
                            record.get("failed", False),
                            record.get("call_id", ""),
                            record.get("elapsed_seconds"),
                            strip_controls(record.get("error", "")),
                            strip_controls(record.get("command", "")),
                            result=strip_controls(result) if isinstance(result, str) else None,
                            outcome=record.get("outcome", ""),
                            parent_call_id=record.get("parent_call_id", ""),
                            purpose=strip_controls(record.get("purpose", "")),
                            execution=record.get("execution", ""),
                            exit_code=recorded_exit_code(record.get("exit_code")),
                        )
                    )
                elif kind in ("Message", "partial"):
                    self.transcript.events((Message(strip_controls(record["markdown"])),))
                    if kind == "partial":
                        self.transcript.warning("[Partial output from an interrupted run]")

    def quit(self, argument: str) -> None:
        self.running = False

    def refresh_branch(self) -> bool:
        """Read only Git metadata; called off the UI thread, never during rendering.

        Reports whether the branch moved, so an unchanged one costs no repaint.
        """
        previous = self.branch
        try:
            result = subprocess.run(
                ["git", "-C", str(self.workspace), "symbolic-ref", "--quiet", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=1,
            )
            if result.returncode == 1:  # Detached HEAD: show its short commit instead.
                result = subprocess.run(
                    ["git", "-C", str(self.workspace), "rev-parse", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
            self.branch = plain(result.stdout.strip(), limit=None) if result.returncode == 0 else ""
        except OSError, subprocess.TimeoutExpired:
            self.branch = ""
        return self.branch != previous

    def composer_state(self) -> dict:
        """Composer/turn-local state for Pcode.app's native docked status strip."""
        steering_pending = self.activity.queued_modes.count("steering") if self.activity.busy else 0
        queued = (self.activity.queued - steering_pending) if self.activity.busy else 0
        return {
            "send_mode": self.next_send_mode,
            "send_mode_once": bool(self.send_mode_once),
            "starting": bool(self._startup_pending),
            "steering_pending": steering_pending,
            "queued": max(0, queued),
            "jobs": self.activity.job_count,
            "btw_running": self.asides.running,
            "btw_ready": self.asides.unread,
            "cache_note": self.activity.cache_note or "",
        }

    def toolbar(self):
        width = get_app().output.get_size().columns
        location = plain(location_label(self.workspace, self.branch), limit=None)
        model = self.model if self.model else "preview"
        if self.model and (effort := self.current_effort()) != "n/a":
            model += f" ({effort})"
        # Read left to right and cut from the right: the path leads, then send
        # mode and live activity, then metadata, with the help hint last.
        native_header = self.editor_only or native_transcript_active()
        btw = self.btw_thread()
        btw_model_override = False
        if btw:
            newest = btw[-1]
            if newest.model:
                model = newest.label or newest.model
                btw_model_override = True
            elif newest.effort:
                model = f"{self.model} ({newest.effort})" if self.model else newest.effort
                btw_model_override = True
            segments = [("btw", "btw reply")]
        else:
            once = " (once)" if self.send_mode_once else ""
            segments = [("mode", f"{self.next_send_mode}{once}")]
        if self._startup_pending:
            segments.extend([("sep", " · "), ("activity", "starting")])
        # No "working" label: the status on the editor's top border already says so.
        if self.activity.busy:
            if self.activity.queued:
                steering = self.activity.queued_modes.count("steering")
                queued = self.activity.queued - steering
                if steering:
                    segments.extend([("sep", " · "), ("activity", f"{steering} steering pending")])
                if queued:
                    segments.extend([("sep", " · "), ("activity", f"{queued} queued")])
        if count := self.activity.job_count:
            label = "job" if count == 1 else "jobs"
            segments.extend([("sep", " · "), ("activity", f"{count} {label}")])
        # Side questions are not "working": they neither block input nor end the
        # turn, so they get their own counter rather than the activity label.
        if running := self.asides.running:
            segments.extend([("sep", " · "), ("activity", f"{running} btw running")])
        if unread := self.asides.unread:
            segments.extend([("sep", " · "), ("activity", f"{unread} btw ready")])
        if not native_header or btw_model_override or not self.model:
            segments.extend([("sep", " · "), ("model", plain(model, limit=None))])
        if not native_header:
            context = self.controller.context_label()
            # Colorize the token counts distinctly from the " · " and "/" around them.
            parts = re.split(r"(~?\d[\d.]*[a-z]?)", context)
            segments.extend(
                ("context-value", part)
                if re.fullmatch(r"~?\d[\d.]*[a-z]?", part)
                else ("context", part)
                for part in parts
                if part
            )
        if self.activity.cache_note:
            segments.extend([("sep", " · "), ("cache", self.activity.cache_note)])
        if self.activity.show_hints:
            shortcuts = getattr(self.prompt_session, "shortcuts", None)
            if shortcuts is None:
                shortcuts = PrefixKeys()
            segments.extend([("sep", " · "), ("hint", shortcuts.summary())])
        details = "".join(value for _, value in segments)
        # The path takes any spare width; under pressure it keeps a third of
        # the row, and never so much that the send mode after it is cut.
        spare = width - cell_len(details) - 4  # The leading space and " · ".
        # One more cell than the separators for the line's own trailing "…".
        floor = min(width // 3, width - cell_len(segments[0][1]) - 5)
        path = Text(location)
        path.truncate(max(spare, floor, 1), overflow="ellipsis")
        text = Text(f" {path.plain} · {details}" if path.plain else f" {details}")
        text.truncate(width, overflow="ellipsis")
        prefix = [("text", " ")]
        if path.plain:
            prefix.extend([("location", path.plain), ("sep", " · ")])
        segments = prefix + segments
        # Slice the already cell-truncated text, preserving its ellipsis and the
        # same narrow-terminal priorities without splitting wide characters.
        fragments = []
        remaining = text.plain
        for role, value in segments:
            if not remaining:
                break
            fragments.append((f"class:bottom-toolbar.{role}", remaining[: len(value)]))
            remaining = remaining[len(value) :]
        return fragments

    def handle(self, text: str) -> bool:
        """Handle commands/preview synchronously; return whether a live run is needed."""
        text = text.strip()
        if not text:
            return False
        if self.model and not text.startswith("/"):
            return True
        if text.startswith("/"):
            try:
                if not self.registry.dispatch(text):
                    self.transcript.warning(
                        "Unknown command. Type /help to see available commands."
                    )
            except ValueError as error:
                self.transcript.error(str(error))
            return False
        self.transcript.user(text)
        self.present_events(self.preview.reply(text))
        return False

    async def run_live(
        self,
        output: TerminalOutput,
        text: str,
        *,
        continuing: bool = False,
        wake: bool = False,
    ) -> bool:
        """Run one turn shown through `output`: the controller's turn, this terminal its view."""
        self.output = output
        return await self.controller.run_turn(text, continuing=continuing, wake=wake)

    async def run_command(self, text: str, *, idle: bool, tag) -> None:
        """Run a slash command the controller handed back to this terminal.

        `idle` says whether the session was idle when it was sent, and `tag`
        is the popup generation it was sent in (see `popup`).
        """
        output, session = self.output, self.prompt_session
        self._command_popup_generation = tag
        try:
            if tag is HANDOFF_TAG:
                self.hand_off_now()
            elif (prompt := background_prompt(text)) is not None:
                # The session's own registry has /new too: that is the bare one.
                try:
                    self.new_in_background(prompt)
                except ValueError as error:
                    self.transcript.error(str(error))
            else:
                self.handle(text)
            if self.config_requested:
                await self.browse_config(output, session)
            if self.worker_view_requested:
                await self.read_workers(output, session)
            if self.tree_requested:
                await self.choose_tree(output, session)
            if self.session_requested:
                await self.choose_session(output, session)
            if self.switch_requested is not None:
                await self.switch_session(output, session)
            if self.fork_requested is not None:
                prompt, self.fork_requested = self.fork_requested, None
                await self.fork_session(prompt)
            if self.new_requested is not None:
                prompt, self.new_requested = self.new_requested, None
                await self.start_host_session(prompt)
            if self.restart_requested:
                await self.restart_host()
            if self.restart_all_requested:
                await self.restart_stale_hosts()
            if self.handoff_requested:
                await self.move_to_host()
            if self.session_info_requested:
                await self.show_session_info(output, session)
            if self.inspector_requested is not None:
                await self.inspect_tools(output, session)
            if self.diffs_requested:
                await self.browse_diffs(output, session)
            if self.links_requested:
                await self.choose_link(output, session)
            if self.copy_requested:
                await self.choose_copy(output, session)
            if self.paste_requested:
                await self._paste_image()
        except _PopupSuperseded:
            pass
        finally:
            self._command_popup_generation = None

    async def after_command(self, tag=None) -> None:
        await self.output.flush()
        if not self.running and self.prompt_session.app.is_running:
            self.prompt_session.app.exit()

    async def run_async(self) -> None:
        loop = asyncio.get_running_loop()
        previous = signal.getsignal(signal.SIGUSR2)
        try:
            # `request_app_restart`, through everything below, cleanup included.
            loop.add_signal_handler(signal.SIGUSR2, self.request_app_restart)
        except NotImplementedError, RuntimeError, ValueError:
            await self._run_editor()  # No signals here (not the main thread).
            return
        from pcode import cli

        if cli.app_restart_signalled:  # Before this process got this far.
            cli.app_restart_signalled = False
            self.request_app_restart()
        try:
            await self._run_editor()
        finally:
            restore_signal_handler(loop, signal.SIGUSR2, previous)

    async def _run_editor(self) -> None:
        # This frontend owns the terminal; suppress the framework's unsolicited banner.
        os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
        # A brand-new session shows the welcome banner; attaching to an existing
        # host or resuming a saved conversation replays directly instead of
        # flashing a welcome block only to wipe it a moment later.
        fresh_launch = not self.resuming and (
            self._host_launch is None or self._host_launch.process is not None
        )
        welcome_rows = (
            self.transcript.welcome(self.model, str(self.workspace)) if fresh_launch else 0
        )
        initial_row = take_initial_cursor_row()
        # Typed before a session host is attached; sent to it once it is.
        early: list[tuple[str, str, object]] | None = [] if self._host_launch else None

        async def refresh_metadata():
            refresh = getattr(self.runtime, "refresh_context", None)
            if refresh is not None:
                try:
                    await refresh()
                except Exception:
                    # Optional metadata must not take down a usable editor.
                    pass
                finally:
                    session.app.invalidate()

        async def initialize_host():
            try:
                await connect_host()
            finally:
                self._startup_done.set()

        async def connect_host():
            nonlocal early
            launch, self._host_launch = self._host_launch, None
            try:
                controller, welcome = await self._connect_or_respawn_host(launch)
            except Exception as error:
                self._startup_error = error
                self._startup_pending = False
                self.activity.finish_startup(ok=False)
                self.transcript.error(error_message(error), title="Session host failed")
                if self.editor_only:
                    # The transcript goes nowhere here, and the app's has no host
                    # to show: keep the reason in the editor until it is read.
                    self.activity.flash(
                        f"Session host failed: {error_message(error)}", seconds=24 * 3600
                    )
                early = None
                session.app.invalidate()
                return
            note = f"Attached to session host {controller.id}"
            await self.adopt_controller(controller, welcome, note, preserve_welcome=fresh_launch)
            waiting, early = early or [], None
            for kind, text, detail in waiting:
                if kind == "command":
                    command(text, detail)
                else:
                    self.controller.submit(text, detail)

        async def initialize():
            # Replaying is a journal read of a few milliseconds, while the
            # provider stack below takes a second or two to import. Draw the
            # conversation first: waiting for a backend you have not used yet
            # to see what was already said is a wait for nothing.
            controller = self.controller
            replayed = self.resuming and self._saved_session is not None
            if replayed:
                self.replay(self._saved_session)
                controller.conversation_shown = True
            try:
                await self._initialize_runtime()
                controller.show_startup_context()
                controller.warn_without_credentials()
                saved = getattr(self.runtime, "session", None)
                if self.model and saved:
                    self.transcript.retained_note(f"Saving session: {saved.info.id}")
                if self.resuming and not replayed:
                    self.replay()
            except Exception as error:
                self._startup_error = error
                self.transcript.error(error_message(error), title="Agent startup failed")
                controller.clear_queue()
                self.activity.busy = False
            else:
                session.app.create_background_task(refresh_metadata())
                session.app.create_background_task(controller.warn_outdated())
                controller.start_mcp_defaults()
            finally:
                self._startup_pending = False
                if self.activity.startup_label:
                    self.activity.finish_startup(ok=self._startup_error is None)
                controller.ready.set()
                for command in controller.startup_commands:
                    controller.commands.put_nowait(command)
                controller.startup_commands.clear()
                self._startup_done.set()
                session.app.invalidate()

        async def watch_hosts() -> None:
            """Track other sessions for notifications, and say when one finishes."""
            from pcode.host_protocol import list_hosts

            # Finished-turn counts, not states: a turn shorter than the poll
            # interval goes working-idle-working unseen but still counts.
            turns: dict[str, int] = {}
            while self.running:
                entries = await asyncio.to_thread(list_hosts)
                current = self.runtime.id if self.hosted else None
                for entry in entries:
                    seen = turns.get(entry.id)
                    if entry.id != current and seen is not None and entry.turns > seen:
                        self.background_finished(entry)
                turns = {entry.id: entry.turns for entry in entries}
                others = [entry for entry in entries if entry.id != current]
                key = [(e.id, e.state, e.unseen) for e in others]
                if key != [(e.id, e.state, e.unseen) for e in self.hosts]:
                    self.hosts = others
                    session.app.invalidate()
                await asyncio.sleep(HOST_POLL_SECONDS)

        def start_host_watch() -> None:
            # Only once a host is involved: a plain session never polls for them.
            if self._host_watch_task is None:
                self._host_watch_task = session.app.create_background_task(watch_hosts())

        self._host_watch = start_host_watch

        def emulator(sequence: str) -> None:
            from pcode.terminal_notify import send

            if load_preferences().get("desktop_notifications", "on") == "on":
                send(session.app.output, sequence)

        self._emulator = emulator

        local: asyncio.Queue = asyncio.Queue()

        async def run_local_commands():
            """In a hosted session, the terminal's own commands run here, not in the host."""
            while True:
                text, tag = await local.get()
                try:
                    await self.run_command(text, idle=True, tag=tag)
                except Exception as error:
                    self.transcript.error(error_message(error, unexpected=f"{text} failed"))
                await self.after_command()

        def run_in_terminal(text, tag):
            local.put_nowait((text, tag))

        self._run_in_terminal = run_in_terminal

        def command(text, tag):
            # A host's terminal runs its own commands itself, even before the
            # host answers; in-process they keep their place in the session's queue.
            hosted = self.hosted or early is not None
            if hosted and runs_in_terminal(text):
                run_in_terminal(text, tag)
            else:
                self.controller.command(text, tag)

        # The unanswered cancel the "has not stopped" warning was shown for.
        warned_cancel: float | None = None

        def cancel():
            """Ctrl+C. Before the host answers, it drops what was typed ahead for it."""
            nonlocal warned_cancel
            if not early:
                sent = self.controller.cancel_sent if self.host_unresponsive() else None
                if sent is not None and sent != warned_cancel:
                    warned_cancel = sent
                    self.transcript.warning(
                        f"The session host {self.runtime.id} has not stopped. "
                        "Ctrl+D detaches this terminal and leaves it running."
                    )
                self.cancel()
                return
            commands = [text for kind, text, _ in early if kind == "command"]
            prompts = len(early) - len(commands)
            early.clear()
            if any(mcp_enable(text) for text in commands):
                self.transcript.warning("Pending MCP enable command cancelled.")
            if any(text.split()[0] in MODEL_COMMANDS for text in commands):
                self.transcript.warning("Pending model command cancelled.")
            if prompts:
                self.transcript.flash(f"Cleared {prompts} queued message(s).")
            self.activity.busy = False

        def busy_exit():
            """Ctrl+D while busy: cancels, or leaves a host that ignored the last cancel."""
            if early or not self.host_unresponsive():
                cancel()
                return
            self.detach("")
            session.app.exit()

        def submit(text, *, mode_override: str | None = None):
            text = text.strip()
            # Ctrl+Enter interrupts the conversation's turn: never a thread's reply.
            if text and mode_override is None and (kept := self.btw_submit(text)) is not None:
                return kept
            if text and mode_override is not None:
                self.close_btw()
            ready = early is None and getattr(self.controller, "ready", None)
            if (early is not None or (ready and not ready.is_set())) and text in {"/quit", "/exit"}:
                # Do not strand exit behind a command waiting for initialization.
                self.running = False
                session.app.exit()
                return
            if text.startswith("/"):
                name = text.split(maxsplit=1)[0]
                if early is not None and not runs_in_terminal(text):
                    early.append(("command", text, self._popup_generation))
                    if name in MODEL_COMMANDS or mcp_enable(text):
                        # As in-process: Ctrl+C now cancels it, not the draft.
                        self.activity.busy = True
                else:
                    command(text, self._popup_generation)
                return
            if shell_command(text) is not None:
                mode = "shell"
            elif text:
                # One send consumes a Ctrl+S pick; the saved default returns.
                mode = mode_override or self.next_send_mode
                self.send_mode_once = None
            else:
                return
            if early is not None:
                early.append(("submit", text, mode))
                self.activity.busy = True
            else:
                self.controller.submit(text, mode)

        self._submit_typed = submit
        drafts = Drafts(default_dir())
        self._kitty_spinners = KittySpinners.from_preferences(
            load_preferences(),
            supported=self.transcript.console.is_terminal and kitty_graphics_supported(),
        )
        from pcode import dock

        if self.editor_only:
            docked = dock.open_editor_only()
        else:
            # SPIKE: the editor on a terminal docked under this one (`PCODE_DOCK=1`).
            docked = await dock.open_dock() if dock.requested() else None
        dock_io = {}
        if docked is not None:
            dock_io["output"] = docked.output
            # Editor-only reads stdin, the application's default.
            if docked.input is not None:
                dock_io["input"] = docked.input
        session = create_prompt(
            self.registry,
            **dock_io,
            activity=self.activity,
            transcript=self.transcript,
            workspace=lambda: self.workspace,
            on_submit=submit,
            on_interrupt_submit=lambda text: submit(text, mode_override="interrupt"),
            on_command=submit,
            on_cancel=cancel,
            on_busy_exit=busy_exit,
            on_panel=self.set_show_activity,
            on_thinking=self.show_thinking,
            on_commands=lambda: self.show_commands(""),
            on_effort=self.adjust_effort,
            on_send_mode=self.cycle_send_mode,
            on_model=lambda: submit("/model"),
            on_previous_session=lambda: submit("/attach -"),
            on_copy_response=lambda: submit("/copy"),
            on_paste_image=lambda: submit("/paste-image"),
            on_btw=self.btw_key,
            on_btw_next=self.btw_next,
            on_btw_close=self.close_btw,
            bottom_toolbar=self.toolbar,
            # Whichever controller is on screen: /attach replaces it.
            session_title=lambda: self.controller.session_title(),
            composer_state=self.composer_state,
            drafts=drafts,
            kitty_spinners=self._kitty_spinners,
            vi_mode=load_preferences().get("editing_mode", "emacs") == "vi",
        )
        # A thread shown in the drawer is read: a host's other terminals stop calling it ready.
        thread_view = getattr(session, "btw_view", None)
        if thread_view is not None:
            thread_view.on_read = self.asides_seen
            thread_view.drawer_host = self.drawer_host
        output = TerminalOutput(
            self.transcript.console,
            session.app,
            code_theme=lambda: self.transcript.code_theme,
            rich_theme=lambda: self.transcript.rich_theme,
        )
        self.transcript.output = output
        self.output = output
        self.prompt_session = session
        session.app.style = DynamicStyle(
            lambda: self.transcript.prompt_style(
                self.activity.task_style, self.activity.frame_style
            )
        )
        try:
            terminal = session.app.output.fileno()
        except NotImplementedError, OSError, ValueError:
            terminal = None
        if docked is not None:
            # SPIKE: title, progress and session id belong to the pane, not the dock.
            terminal = sys.stdout.fileno()
        if terminal is not None:
            mode = load_preferences().get("terminal_progress", "auto")
            self._progress = TabProgress(self.activity, terminal, mode)
            # Any key here means the failed turn's red bar has been seen.
            session.app.key_processor.before_key_press += self._progress.key_pressed
            title_mode = load_preferences().get(
                "terminal_title", SETTINGS["terminal_title"].default
            )
            # Whichever controller is on screen: /attach replaces it.
            from pcode import drawer, theme

            items: list[Reconcilable] = []
            tab_title = TabTitle(lambda: self.controller.session_title(), terminal, title_mode)
            if tab_title.enabled:
                items.append(tab_title)
            # Over ssh the session is the remote machine's: a restored local pane can't reopen it.
            if theme.drawer_supported() and not os.environ.get("SSH_CONNECTION"):
                items.append(drawer.PaneSession(self.active_session_id, terminal))
                items.append(drawer.PaneHost(self.active_host_id, terminal))
            self._reconciler = Reconciler(items)
            # Docked, `terminal` is pcode's own pane, not the tinted dock, and its
            # replies reach `Dock.attach`'s reply filter, so this follows a
            # light/dark switch there too (re-tinting the dock).
            self._appearance = AppearanceWatch(
                terminal, active=lambda: self.transcript.theme == "auto"
            )

        async def watch_branch():
            """Keep the footer's branch current without a Git process every 2 s.

            A branch moves because the session moved it, so the turn boundary
            below is the moment that matters and this loop only has to notice a
            checkout made in another terminal. Polling faster ran `git` ~1800
            times an hour and repainted the whole layout each time, for a value
            that changes once a session.
            """
            while True:
                if await asyncio.to_thread(self.refresh_branch):
                    session.app.invalidate()
                await asyncio.sleep(BRANCH_POLL_SECONDS)

        stalls = None
        if load_preferences().get("stall_log", SETTINGS["stall_log"].default) == "on":
            from pcode.stall_log import StallWatch

            # Read from the watcher thread: plain attribute reads only.
            stalls = StallWatch(
                context=lambda: {"busy": self.activity.busy, "status": self.activity.status}
            )

        def start():
            restore_stdin()
            replay_pending_input(session.app, self.follow_appearance)
            if initial_row is not None and session.app.output.responds_to_cpr:
                total_rows = session.app.output.get_size().rows
                if total_rows > 0:
                    row = min(total_rows, initial_row + welcome_rows)
                    session.app.renderer._min_available_height = max(0, total_rows - row + 1)
            if stalls is not None:
                session.app.create_background_task(stalls.heartbeat())
            session.app.create_background_task(watch_branch())
            session.app.create_background_task(output.run())
            session.app.create_background_task(run_local_commands())
            session.app.create_background_task(drafts.run())
            if self._progress is not None:
                session.app.create_background_task(self._progress.run())
            if self._reconciler is not None:
                session.app.create_background_task(self._reconciler.run())
            if self._appearance is not None:
                session.app.create_background_task(self._appearance.run())
            if early is not None:
                label = (
                    "Resuming session"
                    if self.resuming
                    else "Starting session"
                    if self._host_launch and self._host_launch.process
                    else "Connecting to session"
                )
                self.activity.begin_startup(label)
                session.app.create_background_task(initialize_host())
            else:
                if self._startup_pending:
                    self.activity.begin_startup(
                        "Resuming session" if self.resuming else "Starting pcode"
                    )
                elif fresh_launch and self.transcript.console.is_terminal:
                    # Preview mode has no backend wait: run just the brief settle sweep.
                    self.activity.finish_startup(ok=True)
                self.run_controller(self.controller)
                session.app.create_background_task(initialize())
            if self.initial_prompt:
                # Queued like a typed message: it waits for the backend the same
                # way, and Ctrl+C clears it the same way.
                submit(self.initial_prompt)
            if docked is not None:
                docked.attach(session.app, on_theme=self.follow_appearance)
            if self.app_restart_requested and self._app_restart_task is None:
                # Signalled before the editor ran: act on it now (once: the
                # editor's future exists a moment before this runs).
                self._app_restart_task = session.app.create_background_task(self._app_restart())

        try:
            await session.app.run_async(pre_run=start)
            # Before `leave_controller` closes the connection the stop goes out on.
            self.stop_host_on_exit()
        finally:
            # Whatever is still unsent, without waiting for the next save.
            drafts.save()
            if docked is not None:
                docked.close()
            if stalls is not None:
                stalls.stop()
            if self._progress is not None:
                self._progress.close()
                self._progress = None
            if self._reconciler is not None:
                self._reconciler.close()
                self._reconciler = None
            if self._appearance is not None:
                self._appearance.close()
                self._appearance = None
            if self._kitty_spinners is not None:
                self._kitty_spinners.close(session.app.output.write_raw, session.app.output.flush)
                self._kitty_spinners = None
            await self.leave_controller(self.controller, flush=True)
            if thread_view is not None:
                # Close any open thread, and with it the drawer.
                self.close_btw()
                thread_view.sync()
                # The editor no longer renders, so a drawer's close goes out now.
                output.app.output.flush()
            await output.flush(drain=True)
            self.transcript.output = None
        self.print_resume_hint()

    def host_unresponsive(self) -> bool:
        """Whether the host is still busy a while after this terminal's cancel.

        A hung host keeps its socket open, so it never counts as gone, and
        Ctrl+C or Ctrl+D would only send more cancels it ignores.
        """
        sent = getattr(self.controller, "cancel_sent", None) if self.hosted else None
        return sent is not None and time.monotonic() - sent >= HOST_CANCEL_GRACE

    def cancel(self) -> None:
        """Ctrl+C, for whichever controller this terminal is showing now."""
        # It drops queued commands, auto-open `/btw`s among them: left counted,
        # one would swallow the next `/btw` typed in the same generation.
        self._auto_opens_pending = 0
        self.controller.cancel()

    def run_controller(self, controller: SessionController) -> None:
        """Start an in-process controller's loops, which this terminal owns."""
        app = self.prompt_session.app
        controller.closing = lambda: not app.is_running
        controller.interactive = True
        self._loops = [
            app.create_background_task(work)
            for work in (
                controller.watch_jobs(),
                controller.consume(),
                controller.consume_commands(),
            )
        ]

    async def leave_controller(self, controller, *, flush: bool = False) -> None:
        """Stop showing `controller`: detach from a host, or end an in-process session.

        `flush` (exiting) waits a moment for what was sent to a host to go out.
        """
        if self.hosted and controller is self.controller:
            # Leave before the loop shuts down, so the connection closing
            # reads as this terminal detaching, not the host dying.
            detach = getattr(controller, "detach", None)
            if not flush or detach is None:
                controller.close()
                return
            # And let what was sent go out (a move's continue and typed
            # messages, a stop) before an exit can cut it off.
            with suppress(TimeoutError):
                await asyncio.wait_for(detach(), 2.0)
            return
        if not isinstance(controller, SessionController):
            controller.close()
            return
        loops, self._loops = self._loops, []
        # In-process, the command leaving (/attach, /detach) runs in one of
        # these loops: awaiting itself would never end. adopt_controller stops it.
        loops = [task for task in loops if task is not asyncio.current_task()]
        for task in loops:
            task.cancel()
        # Side questions outlive turns, not the terminal; nor does a title request.
        await controller.asides.close()
        # Stopping them settled them: their journal entries go in before the session closes.
        await controller.side_records_settled()
        await controller.stop_naming()
        for task in controller.tasks():
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*loops, *controller.tasks(), return_exceptions=True)
        if controller.extensions is not None:
            await controller.extensions.close()
            controller.extensions = None

    def print_resume_hint(self) -> None:
        if self.hosted:
            runtime = self.runtime
            if self.host_stopped:
                line = "Stopped the session host."
                if runtime.session_id:
                    line += f" Continue with: pcode --continue {runtime.session_id}"
            elif self.session_over:
                if not runtime.session_id:
                    return
                line = f"Continue with: pcode --continue {runtime.session_id}"
            elif runtime.lost:
                line = f"The session host {runtime.id} had exited."
            else:
                line = (
                    f"Session {runtime.id} keeps running in the background. "
                    f"Reattach: pcode --attach {runtime.id}"
                )
            self.transcript.console.print(line, markup=False)
            return
        saved = getattr(self.runtime, "session", None)
        if saved is None:
            self.transcript.console.print("Session not saved; no continue command available.")
        else:
            from pcode.sessions import session_root

            command = ["pcode", "--continue", saved.info.id]
            if saved.directory.parent.resolve() != session_root().resolve():
                command.extend(["--session-dir", str(saved.directory.parent.resolve())])
            self.transcript.console.print(f"Continue with: {shlex.join(command)}", markup=False)

    def run(self) -> None:
        asyncio.run(_stopping_provider_processes(self.run_async()))

    async def run_print_async(self, prompt: str, *, stdout=None) -> bool:
        """Answer one prompt without an editor: the reply to `stdout`, the rest to the transcript.

        The transcript console is expected to be stderr, so a pipe reading
        stdout sees the reply alone. A terminal gets rendered Markdown; a pipe
        gets its source, which is what a reader downstream can work with.
        Returns whether the turn succeeded.
        """
        os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
        reply = PrintedReply(
            sys.stdout if stdout is None else stdout,
            transcript=self.transcript,
            present=self.present_events,
        )
        if not self.model:
            for event in self.preview.reply(prompt):
                if isinstance(event, Message):
                    reply.write(event.markdown)
            return True
        # The tab looks busy from launch to exit: startup is part of the wait.
        self.activity.start_prompt(prompt)
        async with reply.tab_progress(self.activity).shown() as tab:
            try:
                await self._initialize_runtime()
            except Exception as error:
                self.transcript.error(error_message(error), title="Agent startup failed")
                return False
            if self._saved_session is not None and self._saved_session.forked_from:
                self.transcript.note(forked_note(self._saved_session))

            def retry_notice(text: str) -> None:
                tab.turn_retry()
                self.transcript.note(text)

            self.runtime.compaction_notice = self.transcript.note
            if hasattr(self.runtime, "retry_notice"):
                self.runtime.retry_notice = retry_notice
            if hasattr(self.runtime, "warning_notice"):
                self.runtime.warning_notice = self.transcript.warning
            if hasattr(self.runtime, "notice"):
                self.runtime.notice = self.transcript.note
            try:
                async with aclosing(self.runtime.stream(prompt)) as stream:
                    async for event in stream:
                        tab.turn_event()
                        if isinstance(event, PlanUpdated):
                            self.activity.plan = event.items
                        reply.event(event)
                        if (job_id := delivered_job(event)) is not None:
                            self.controller.report_delivered_job(job_id)
            except Exception as error:
                reply.settle()
                self.controller.report_finished_jobs()
                self.transcript.error(
                    workspace_gone(self.workspace) or error_message(error), title="Agent failed"
                )
                saved = getattr(self.runtime, "session", None)
                if saved is not None:
                    self.transcript.note(f"Session and diagnostics: {saved.directory}")
                return False
        reply.settle()
        self.controller.report_finished_jobs()
        self.print_resume_hint()
        return True

    def run_print(self, prompt: str) -> bool:
        return asyncio.run(_stopping_provider_processes(self.run_print_async(prompt)))


def _profile_capture(args) -> tuple[Path, bool, bool] | None:
    """The capture this run should write: (directory, cpu tracing, memory tracing).

    Flags win over the saved `profile` default, which exists so the everyday
    session that feels slow is captured without remembering to ask for it.
    """
    from pcode.profiling import PROFILE_MODES, new_capture

    if args.no_profile or args.launch:
        # The picker execs away, so a capture of it would never be finished;
        # the pcode it starts makes its own.
        return None
    if args.profile is None:
        mode = load_preferences().get("profile", SETTINGS["profile"].default)
        if mode == "off" or mode not in PROFILE_MODES:
            return None
        return new_capture(), mode == "cpu", mode == "memory"
    # `--profile` without DIR names its own directory under the state directory.
    directory = new_capture() if args.profile is True else args.profile
    return directory, args.profile_cpu, args.profile_memory


def main() -> None:
    install_library_log()
    parser = argparse.ArgumentParser(
        description="Streaming terminal with a Coder agent",
        epilog=f"Global defaults, without starting a session: pcode {CONFIG_USAGE}",
    )
    # The workspace's `.pcode/preferences.json` overlays user defaults, so it has
    # to be known before the first load_preferences() (the --theme default).
    _select_project_root(sys.argv[1:])
    parser.add_argument("--version", action=_VersionAction)
    parser.add_argument(
        "--theme",
        choices=THEMES,
        default=load_preferences().get("theme", SETTINGS["theme"].default),
        help="Color theme (default: saved preference)",
    )
    parser.add_argument(
        "-m",
        "--model",
        help="Pydantic Agent model string; omitted = saved default or offline preview",
    )
    parser.add_argument(
        "-C", "--workspace", type=Path, help="Coder workspace (default: current directory)"
    )
    parser.add_argument(
        "--worktree",
        nargs="?",
        const=True,
        metavar="NAME",
        help="Work in a fresh .worktrees/NAME git worktree (default NAME: the session ID)",
    )
    parser.add_argument(
        "--no-worktree",
        action="store_true",
        help="Stay in the current checkout even when the worktree default is on",
    )
    # One positional list serves both the message and the `config` subcommand,
    # so an unquoted `pcode fix the bug` works and `config` needs no subparser.
    parser.add_argument(
        "prompt",
        nargs="*",
        metavar="PROMPT",
        help="Send this message first; with --print, read stdin when omitted",
    )
    parser.add_argument(
        "-p",
        "--print",
        action="store_true",
        help="Answer PROMPT without the editor: reply on stdout, tool activity on stderr",
    )
    parser.add_argument(
        "--theme-preview",
        dest="theme_preview",
        action="store_true",
        help="Print an offline sample and the syntax-style gallery, then exit",
    )
    # The flag was named --demo before it grew the style gallery; keep the old
    # spelling working for scripts and the Homebrew smoke test, out of --help.
    parser.add_argument("--demo", dest="theme_preview", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--sessions", action="store_true", help="List saved sessions and exit")
    parser.add_argument(
        "--delete-session",
        metavar="ID",
        help="Delete a saved session (refused while it is open) and exit",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="With --sessions: drop superseded step checkpoints and report the space freed",
    )
    parser.add_argument(
        "--completions",
        choices=COMPLETION_SHELLS,
        metavar="SHELL",
        help=f"Print a shell completion script ({', '.join(COMPLETION_SHELLS)}) and exit",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="resume",
        nargs="?",
        const="latest",
        metavar="SESSION",
        help=(
            "Continue a session ID/prefix, or a copy of it if it is open elsewhere; "
            "omit SESSION for this directory's latest"
        ),
    )
    parser.add_argument(
        "--fork",
        action="store_true",
        help="With --continue: always continue a copy, leaving the original session unchanged",
    )
    parser.add_argument(
        "--session-dir", type=Path, help="Override the private session storage directory"
    )
    parser.add_argument(
        "--no-save", action="store_true", help="Keep this live session in memory only"
    )
    parser.add_argument(
        "--host",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run the conversation in a background session host that outlives this terminal; "
        "--no-host runs it inside this terminal (the default, unless session_host is on)",
    )
    complete_with(
        parser.add_argument(
            "--attach",
            nargs="?",
            const="",
            metavar="HOST",
            help="Attach to a running session host by host or session ID prefix; "
            "omit HOST for the most recent",
        ),
        "hosts",
    )
    parser.add_argument("--hosts", action="store_true", help="List running session hosts and exit")
    parser.add_argument(
        "--launch",
        nargs="?",
        const="exit",
        choices=("exit", "shell"),
        metavar="THEN",
        help="Pick a workspace from those with saved sessions and start pcode there; "
        "THEN is what follows when it quits: exit (default) or a login shell in that "
        "workspace, for a terminal app's launch command",
    )
    parser.add_argument(
        "--json", action="store_true", help="With --hosts: one JSON array, for scripts and plugins"
    )
    host_kinds = ("all", "stale", "idle")
    parser.add_argument(
        "--kill-hosts",
        choices=host_kinds,
        help="Stop every session host, those running older pcode code, or those with "
        "nothing running (even with a terminal attached), and exit",
    )
    # The flag's old name, kept working for scripts and habit.
    parser.add_argument(
        "--stop-hosts", dest="kill_hosts", choices=host_kinds, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--restart-hosts",
        choices=("stale", "all"),
        help="Restart session hosts running older pcode code (or all of them) on the pcode "
        "installed now, keeping each conversation, and exit; busy ones are skipped",
    )
    parser.add_argument(
        "--email-listen",
        action="store_true",
        help="Take tasks by email from the account set up with --email-setup; "
        "each starts a sandboxed background session in its own worktree",
    )
    parser.add_argument(
        "--email-ttl",
        default="8h",
        metavar="DURATION",
        help="With --email-listen: stop listening after this long (default 8h)",
    )
    parser.add_argument(
        "--email-setup",
        nargs="?",
        const="",
        metavar="ADDRESS",
        help="Store a Gmail app password in the keychain and enable --email-listen for it",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        nargs="?",
        const=True,
        metavar="DIR",
        help="Sample process-tree CPU/RSS to a new private DIR (default: the state directory)",
    )
    parser.add_argument(
        "--no-profile",
        action="store_true",
        help="Skip the capture this run even when the profile default is on",
    )
    parser.add_argument(
        "--profile-cpu",
        action="store_true",
        help="Also trace function CPU time across threads (much slower; requires --profile)",
    )
    parser.add_argument(
        "--profile-memory",
        action="store_true",
        help="Also trace Python allocations (slower; requires --profile)",
    )
    args = parser.parse_args()
    if args.delete_session:
        from pcode.sessions import SessionError, delete_session

        try:
            delete_session(args.delete_session)
        except SessionError as error:
            sys.exit(f"pcode: {error}")
        return
    if args.completions:
        from pcode.completion import install_hint, render

        print(render(parser, args.completions), end="")
        print(f"# Install: {install_hint(args.completions)}")
        return
    args.command = None
    if args.resume is not None:
        from pcode.session_ids import is_session_selector

        if not is_session_selector(args.resume):
            # SESSION is optional, so argparse would otherwise swallow the first
            # word of `pcode -c fix the bug`. Anything unlike an ID is prompt text.
            args.prompt.insert(0, args.resume)
            args.resume = "latest"
    if args.prompt and args.prompt[0] == "config":
        args.command = "config"
        args.arguments = args.prompt[1:]
    args.prompt = " ".join(args.prompt).strip() or None
    if (args.profile_cpu or args.profile_memory) and args.profile is None:
        parser.error("--profile-cpu and --profile-memory require --profile")
    if args.profile is not None and args.no_profile:
        parser.error("--profile and --no-profile are mutually exclusive")
    if args.worktree and args.no_worktree:
        parser.error("--worktree and --no-worktree are mutually exclusive")
    with ExitStack() as stack:
        capture = _profile_capture(args)
        if capture is not None:
            from pcode.profiling import profile_session, prune_captures

            directory, cpu, memory = capture
            try:
                stack.enter_context(profile_session(directory, cpu=cpu, memory=memory))
            except (OSError, ValueError) as error:
                parser.error(
                    f"Cannot start profile ({type(error).__name__}); use a new writable DIR"
                )
            # Only automatic captures are pruned, and only once this one exists,
            # so retention counts the directory the session is writing to.
            if not isinstance(args.profile, Path):
                prune_captures()
        _run_cli(args, parser)


class _VersionAction(argparse.Action):
    """`--version`, looked up only when asked: reading package metadata costs
    every start ~10 ms (`importlib.metadata` and the scan for `pcode`)."""

    def __init__(self, option_strings, dest, **kwargs):
        kwargs.setdefault("help", "show program's version number and exit")
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        from importlib.metadata import version

        print(f"pcode {version('pcode')}")
        parser.exit()


def _select_project_root(argv: list[str]) -> None:
    """Point preferences at `-C DIR` (else the cwd) ahead of full argument parsing."""
    from pcode.preferences import set_project_root

    try:
        root = Path.cwd()
    except OSError:  # started in a directory that has since been deleted
        root = Path.home()
    for index, arg in enumerate(argv):
        if arg in ("-C", "--workspace") and index + 1 < len(argv):
            root = Path(argv[index + 1])
        elif arg.startswith("--workspace="):
            root = Path(arg.partition("=")[2])
        elif arg.startswith("-C") and len(arg) > 2 and not arg.startswith("--"):
            root = Path(arg[2:])
    set_project_root(root if root.is_dir() else Path.cwd())


def _enter_worktree(
    workspace: Path,
    requested,
    *,
    always: bool = False,
    base: str | None = None,
    owner: str | None = None,
) -> tuple[Path, str | None]:
    """Create the session's worktree when asked to, returning (workspace, session id).

    `requested` is None (use the `worktree` setting), True (unnamed), or a
    name. Session worktrees are named `pcode-<name>` so `git worktree list`
    and `git branch` show which ones pcode made; unnamed ones use the session
    ID's prefix, so `pcode -c <prefix>` finds the session. Already inside a
    linked worktree, or outside git, the workspace is left alone rather than
    nested. `always` (a remote host) makes one regardless, and fails outside
    git; `base` is the commit or branch it starts from, else the mainline.
    The worktree is claimed for `owner`, an existing session moving in, or
    else the new session ID returned, so only that session tidies it away.
    """
    from uuid import uuid4

    from pcode import worktree

    if requested is None and load_preferences().get("worktree", "off") != "on":
        return workspace, None
    if worktree.main_checkout(workspace) is None:
        if requested is None and not always:
            return workspace, None
        raise worktree.WorktreeError(
            f"--worktree needs a git repository; {workspace} is not in one."
        )
    if worktree.is_linked(workspace) and not always:
        return workspace, None
    identity = str(uuid4())
    name = requested if isinstance(requested, str) else identity[:8]
    created = worktree.create(workspace, SESSION_WORKTREE_PREFIX + name, base)
    try:
        # Unclaimed it would fall back to the older rules, where any process
        # leaving it clean may remove it; better to fail as setup does.
        worktree.claim(created, owner or identity)
        worktree.run_setup(created, stream=sys.stderr)
    except worktree.WorktreeError, OSError:
        worktree.remove(created, force=True)
        raise
    print(f"worktree: {created.path} (branch {created.branch})", file=sys.stderr)
    return created.path, identity


def _leave_worktree_on_exit(app, ask=input, stream=None, owner: str | None = None) -> None:
    """`leave_worktree` for the process exit: notes go to stderr, and a deleted
    session is dropped from the runtime so nothing writes to it afterwards.
    `owner` is the session ID a worktree was made for, should none be saved."""
    stream = stream or sys.stderr
    session = getattr(app.runtime, "session", None)
    deleted = leave_worktree(
        app.workspace,
        session,
        ask=ask,
        notify=lambda text: print(text, file=stream),
        owner=owner,
        session_dir=app.session_dir,
    )
    if deleted:
        app.runtime.session = None


async def _stopping_provider_processes(main):
    """Run `main`, then stop the `claude:` and `antigravity:` processes this loop started.

    Both modules import the provider stack (a second or more), so they are
    looked up only afterwards, and only if something loaded them: importing
    them first held back the editor's first frame, and a module never
    imported has started no processes to stop.
    """
    try:
        return await main
    finally:
        pool = sys.modules.get("pcode.claude_sdk.session_pool")
        antigravity = sys.modules.get("pcode.antigravity")
        try:
            if pool is not None:
                await pool.shutdown()
        finally:
            if antigravity is not None:
                await antigravity.shutdown()


def forked_note(saved) -> str:
    """Say where a copied session came from, and that the two share a workspace."""
    note = f"Continuing a copy of session {saved.forked_from}"
    note += "." if saved.fork_requested else ", which is open in another process."
    active = saved.tree.nodes.get(saved.tree.active) if saved.tree.active else None
    if active is not None and active.status == "interrupted":
        note += " Its running turn was copied up to its last safe step; /continue carries it on."
    return note + f" Both sessions work in {saved.info.workspace}."


def _resumed_workspace(info, requested: Path | None) -> Path:
    """Where a resumed session runs: back in its own directory, not the caller's.

    Another worktree of the same repository is fine: the session goes back to
    its own directory. Another repository is refused. In-process and hosted
    resumes both come here; a host started in the caller's directory instead
    can be left in another session's worktree, which that session removes.
    """
    from pcode.sessions import SessionError

    if requested and str(requested.resolve()) != info.workspace:
        from pcode.worktree import repo_scope

        if repo_scope(requested) != session_scope(info):
            raise SessionError(
                "Workspace differs from the saved session; refusing cross-repo resume."
            )
    return _resume_workspace(info, requested)


def _resume_workspace(info, requested: Path | None) -> Path:
    """Where to continue a session whose own workspace may have been deleted.

    A session worktree is removed from outside the session that owns it -- a
    merge from a sibling session, `/worktree clean`, `git worktree prune` --
    and the conversation is still worth resuming afterwards. Prefer an explicit
    `-C DIR`, already checked to be the same repository, then the checkout the
    worktree was made from. Refusing outright would strand the session on a
    path nothing can recreate. A deleted session worktree is recreated first.
    """
    from pcode.sessions import SessionError
    from pcode.worktree import restore

    workspace = Path(info.workspace)
    # An explicit `-C DIR` elsewhere is where the user asked to continue.
    if not workspace.is_dir() and (requested is None or requested.resolve() == workspace):
        owners = (info.id, info.forked_from or "")
        if note := restore(workspace, owners, sys.stderr):
            print(f"workspace: {note}", file=sys.stderr)
    if workspace.is_dir():
        return workspace
    for candidate in (requested, Path(info.project) if info.project else None):
        if candidate is not None and candidate.is_dir():
            print(
                f"workspace: {workspace} no longer exists; continuing in {candidate}",
                file=sys.stderr,
            )
            return candidate
    raise SessionError(
        f"The session's workspace {workspace} no longer exists, and neither does its "
        f"project checkout. Continue it elsewhere with `pcode -C DIR --continue {info.id}`."
    )


def _kill_hosts(which: str, entries: list, code: str) -> None:
    """`--kill-hosts all|stale|idle`, reporting each host as it stops.

    `idle` is for the hosts forgotten terminal tabs keep alive: an attached
    terminal stops a host from stopping itself. Each host checks for itself
    that nothing but its terminals would be lost, then stops exactly as it
    would once idle, so its worktree and an unread reply are kept the same way.
    """
    from pcode.remote import HostError, stop_entry, stop_if_idle
    from pcode.rpc import RemoteError

    chosen = [
        entry
        for entry in entries
        if which in ("all", "idle") or (which == "stale" and entry.stale(code))
    ]

    async def stop(entry) -> None:
        try:
            if which == "idle" and entry.state != "idle":
                # Running a turn, or starting, says so in its entry; the host checks the rest.
                busy = "it is starting up" if entry.state == "starting" else "a turn is running"
            elif which == "idle":
                busy = await stop_if_idle(entry)
            elif not await stop_entry(entry):
                raise HostError("it was told to stop but is still running after 30 s")
            else:
                busy = ""
        except (OSError, ValueError, HostError, RemoteError) as error:
            # Usually gone since it was listed (stopped itself, or crashed).
            print(f"Could not stop {entry.id}: {error}", file=sys.stderr, flush=True)
            return
        if busy:
            print(f"Skipped {entry.id}  {entry.label()}: {busy}", flush=True)
        else:
            print(f"Stopped {entry.id}  {entry.label()}", flush=True)

    async def stop_all() -> None:
        # All at once: each may take up to 30 s to tear down, which one after
        # another looked like a hang with a dozen hosts.
        await asyncio.gather(*(stop(entry) for entry in chosen))

    if chosen:
        print(f"Stopping {len(chosen)} session host(s)…", file=sys.stderr, flush=True)
        asyncio.run(stop_all())
    else:
        print("No stale session hosts." if which == "stale" else "No session hosts are running.")


def _restart_hosts(which: str, entries: list, code: str) -> None:
    """`--restart-hosts stale|all`, reporting each host as it restarts or why it did not."""
    from pcode.remote import restart_and_report

    chosen = [entry for entry in entries if which == "all" or entry.stale(code)]
    for entry in chosen:
        # A busy host restarts too, continuing its turn, as `/restart all` does.
        line, failed = asyncio.run(restart_and_report(entry, interrupt=True))
        print(line, file=sys.stderr if failed else sys.stdout)
    if not chosen:
        print("No stale session hosts." if which == "stale" else "No session hosts are running.")


def _stopped_session(selector: str, root: Path | None = None) -> str | None:
    """The saved session `--attach SELECTOR` means when no running host matches it.

    A stopped host's ID still finds its session while /attach lists it; any
    session ID prefix finds a saved session.
    """
    from pcode.host_protocol import list_hosts, list_stopped
    from pcode.sessions import SessionError, resolve_session

    if any(
        entry.id.startswith(selector) or entry.session_id.startswith(selector)
        for entry in list_hosts()
    ):
        return None  # One host, or an ambiguous prefix find_host reports.
    stopped = {entry.session_id for entry in list_stopped() if entry.id.startswith(selector)}
    if len(stopped) == 1:
        return stopped.pop()
    if selector == "latest":
        return None  # A word to resolve_session, not an ID.
    try:
        return resolve_session(selector, root).name
    except SessionError:
        return None


def _pick_host(selector: str, workspace: Path):
    """`--attach [HOST]`: by prefix, else the latest in this repository, else the latest."""
    from pcode.host_protocol import find_host, list_hosts
    from pcode.worktree import repo_scope

    if selector:
        return find_host(selector)
    entries = list_hosts()
    if not entries:
        raise LookupError("No session hosts are running; start one with pcode.")
    scope = repo_scope(workspace)
    here = [entry for entry in entries if repo_scope(Path(entry.workspace)) == scope]
    return (here or entries)[0]


def _running_host(selector: str, session_dir: Path | None, workspace: Path):
    """The host already running this session, if any.

    Continuing a session that is open elsewhere makes a copy of it, which is
    right for one open in another terminal's own process, but a host exists to
    be attached to: going there keeps one conversation instead of two.
    """
    from pcode.host_protocol import list_hosts

    hosts = list_hosts()
    # A full ID, as an app pane passes, names its host outright (in the default
    # store; another `--session-dir` resolves it there first).
    running = next((entry for entry in hosts if entry.session_id == selector), None)
    if running is not None and session_dir is None:
        return running
    from pcode.sessions import SessionError, resolve_session

    try:
        identity = resolve_session(selector, session_dir, workspace).name
    except SessionError:
        return None  # The usual resume path reports it.
    return next((entry for entry in hosts if entry.session_id == identity), None)


def _run_hosted(args: argparse.Namespace) -> None:
    """Run this terminal against a session host: a new one, or one already running.

    The host is started from here so it inherits this terminal's environment;
    the terminal then only renders it. Trust is asked here too, since the host
    has nobody to ask. The host makes the worktree, as the process that works in it.
    """
    from pcode.remote import HostLaunch, app_host_id, spawn_host

    entry = None
    if args.attach is not None:
        try:
            entry = _pick_host(args.attach, args.workspace or Path.cwd())
        except LookupError:
            # `--continue` routed to a host that vanished before we picked it:
            # fall through to spawning a fresh host for `args.resume`.
            if not args.resume:
                raise
            args.attach = None
    if entry is not None:
        fallback_resume = entry.session_id if args.resume and entry.session_id else None
        launch = HostLaunch.running(entry, resume=fallback_resume)
        model, workspace = entry.model, Path(entry.workspace)
    else:
        from pcode.preferences import rejected_project_keys, set_project_root
        from pcode.project_trust import prompt_trust
        from pcode.sessions import SessionError, read_info, resolve_session

        workspace = (args.workspace or Path.cwd()).resolve()
        resume, saved_model = None, None
        if args.resume:
            # One already running in a host was routed to --attach by the caller,
            # unless --fork asked for a copy of it.
            path = resolve_session(args.resume, args.session_dir, workspace)
            info = read_info(path)
            workspace = _resumed_workspace(info, args.workspace).resolve()
            resume, saved_model = path.name, info.model
        if not workspace.is_dir():
            raise SessionError(f"Workspace {workspace} is not an existing directory.")
        set_project_root(workspace)
        if rejected := rejected_project_keys():
            print(
                f"pcode: ignoring user-only settings in .pcode/preferences.json: "
                f"{', '.join(rejected)} (set them with `pcode config set`)",
                file=sys.stderr,
            )
        prompt_trust(workspace, ask=ask)
        model = saved_model or args.model or load_preferences().get("model")
        identity, process, log = spawn_host(
            identity=app_host_id(),
            model=model,
            workspace=workspace,
            resume=resume,
            session_dir=args.session_dir,
            no_save=args.no_save,
            worktree=args.worktree,
            no_worktree=args.no_worktree,
            fork=args.fork,
        )
        launch = HostLaunch(identity, process, log)
    app = PreviewApp(
        theme=args.theme,
        model=model,
        workspace=workspace,
        session_dir=args.session_dir,
        resume=bool(args.resume),
        initial_prompt=args.prompt,
        host=launch,
    )
    try:
        app.run()
    finally:
        if app.hosted:
            app.runtime.close()
    if app.hosted:
        _leave_host(app)


def _print_hosted(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """`--attach --print`: one message or command for a running host, without the editor."""
    from pcode.remote import HostError
    from pcode.remote_print import print_to_host
    from pcode.rpc import RemoteError

    try:
        entry = _pick_host(args.attach, args.workspace or Path.cwd())
    except LookupError as error:
        parser.exit(2, f"{error}\n")
    # Stdout carries the reply alone, as for a local --print.
    app = PreviewApp(theme=args.theme, console=Console(stderr=True))
    try:
        ok = asyncio.run(
            print_to_host(entry, args.prompt, transcript=app.transcript, present=app.present_events)
        )
    except KeyboardInterrupt:
        parser.exit(130)  # It has said what it left running.
    # ValueError: a malformed line from the host (OSError covers timeouts and resets).
    except (HostError, RemoteError, OSError, ValueError) as error:
        parser.exit(2, error_message(error) + "\n")
    if not ok:
        parser.exit(1)


def _email(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """`--email-setup` and `--email-listen` (`pcode.email_remote`)."""
    from pcode.email_remote import cli
    from pcode.email_remote.mailbox import SetupError

    if args.email_setup is not None and args.email_listen:
        parser.error("--email-setup and --email-listen are separate steps")
    if args.prompt or args.print or args.attach is not None or args.resume:
        parser.error("--email-listen and --email-setup take no prompt or session options")
    try:
        if args.email_setup is not None:
            status = cli.setup(args.email_setup or None)
        else:
            status = cli.listen(args.workspace or Path.cwd(), ttl=args.email_ttl, model=args.model)
    except (SetupError, ValueError, OSError) as error:
        parser.exit(2, f"pcode: {error_message(error)}\n")
    except KeyboardInterrupt:
        parser.exit(130)
    sys.exit(status)


def _leave_host(app) -> None:
    """A terminal on a host exits: a host left running tidies its own worktree."""
    app.runtime.close()
    if app.host_stopped:
        _tidy_stopped_host(app)


def _tidy_stopped_host(app) -> None:
    """After `/stop`: the host kept its worktree so this terminal can ask, as a local exit does."""
    from pcode.remote import wait_for_exit_sync
    from pcode.sessions import SavedSession, SessionError

    runtime = app.runtime
    wait_for_exit_sync(runtime.pid)
    saved = None
    if runtime.session_id:
        try:
            saved = SavedSession.open(runtime.session_id, app.session_dir)
        except SessionError:
            saved = None  # Open elsewhere again, or gone: leave the session to them.
    try:
        leave_worktree(
            app.workspace,
            saved,
            ask=ask,
            notify=lambda text: print(text, file=sys.stderr),
            owner=runtime.session_id or None,
            session_dir=app.session_dir,
        )
    finally:
        if saved is not None:
            saved.close()


def _run_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.command == "config":
        try:
            print(configure(args.arguments))
        except (OSError, ValueError) as error:
            parser.exit(2, f"{error}\n")
        return
    if args.email_setup is not None or args.email_listen:
        _email(args, parser)
        return
    if args.launch:
        refused = (
            args.prompt,
            args.print,
            args.resume,
            args.workspace,
            args.attach is not None,
            args.fork,
            args.sessions,
            args.compact,
            args.theme_preview,
            args.hosts,
            args.json,
            args.kill_hosts,
            args.restart_hosts,
            args.profile is not None,
            args.profile_cpu,
            args.profile_memory,
        )
        if any(refused):
            parser.error(
                "--launch picks the workspace and session itself; it only takes "
                "--model, --theme, --host/--no-host, --worktree/--no-worktree, "
                "--no-save and --session-dir"
            )
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("--launch needs a terminal")
        from pcode.launch import launch

        # Passed on to the pcode it starts: `always` to any session, `fresh`
        # only to a new one (a continued session keeps its model and workspace).
        always, fresh = [], []
        if args.session_dir:
            always += ["--session-dir", str(args.session_dir.resolve())]
        if args.host is not None:
            always.append("--host" if args.host else "--no-host")
        # Only an explicit --theme: its default is this directory's preference,
        # and the chosen project's own preference should win over that.
        if any(arg == "--theme" or arg.startswith("--theme=") for arg in sys.argv[1:]):
            always += ["--theme", args.theme]
        if args.model:
            fresh += ["--model", args.model]
        if args.worktree:
            fresh.append("--worktree" if args.worktree is True else f"--worktree={args.worktree}")
        if args.no_worktree:
            fresh.append("--no-worktree")
        if args.no_save:
            fresh.append("--no-save")
        sys.exit(launch(args.launch, args.session_dir, always=always, fresh=fresh))
    if args.resume and (args.no_save or args.theme_preview):
        parser.error("--continue cannot be combined with --no-save or --theme-preview")
    if args.fork and not args.resume:
        parser.error("--fork needs --continue")
    if args.fork and args.attach is not None:
        parser.error("--fork cannot be combined with --attach")
    if args.compact and not args.sessions:
        parser.error("--compact applies to --sessions")
    if args.json and not args.hosts:
        parser.error("--json applies to --hosts")
    if args.print:
        if args.theme_preview or args.sessions:
            parser.error("--print cannot be combined with --theme-preview or --sessions")
        if args.prompt is None:
            if sys.stdin.isatty():
                parser.error("--print needs a PROMPT argument or text on stdin")
            args.prompt = sys.stdin.read()
        if not args.prompt.strip():
            parser.error("--print needs a non-empty prompt")
    if args.sessions:
        from pcode.sessions import compact_snapshots, list_sessions, session_root

        console = Transcript(Console(), args.theme)
        records = list_sessions(args.session_dir)
        if not records:
            console.note("No saved sessions.")
        # Rows are plain so the listing can be piped; only the summary is a note.
        root = args.session_dir or session_root()
        freed = 0
        for info in records:
            line = f"{info.id}  {info.status}  {info.model}  {info.workspace}"
            if args.compact:
                before, after = compact_snapshots(root / info.id)
                freed += before - after
                line += f"  {(before - after) / 1_000_000:.0f} MB freed"
            console.print(Text(line))
        if args.compact:
            console.note(f"Reclaimed {freed / 1_000_000_000:.2f} GB. Open sessions were skipped.")
        return
    if args.theme_preview:
        # --theme-preview never constructs a provider, even when -m is also supplied.
        app = PreviewApp(theme=args.theme)
        app.transcript.welcome()
        # There is no mutable panel in the non-interactive sample.
        app.transcript.events(app.preview.demo(), show_tools=True)
        app.transcript.syntax_gallery()
        return
    if args.hosts or args.kill_hosts or args.restart_hosts:
        from pcode.host_protocol import code_fingerprint, list_hosts
        from pcode.host_ui import host_row, ordered

        code = code_fingerprint()
        entries = ordered(list_hosts())
        if args.kill_hosts:
            _kill_hosts(args.kill_hosts, entries, code)
            return
        if args.restart_hosts:
            _restart_hosts(args.restart_hosts, entries, code)
            return
        if args.json:
            import json

            # A contract for callers such as the Obsidian bridge: add keys, never rename.
            keys = ("id", "session_id", "state", "workspace", "title", "last_prompt", "attached")
            print(json.dumps([{key: getattr(entry, key) for key in keys} for entry in entries]))
            return
        for entry in entries:
            print(f"{entry.id}  {host_row(entry, None, code=code).strip()}")
        if not entries:
            print("No session hosts are running.")
        return
    if (
        args.attach
        and not args.resume
        and (stopped := _stopped_session(args.attach, args.session_dir))
    ):
        # Its host stopped once idle; the conversation carries on as --continue would.
        args.attach, args.resume = None, stopped
    if args.print and args.attach is not None:
        _print_hosted(args, parser)
        return
    # A session runs in a background host when asked (--host, session_host on);
    # the canned preview (no model) has nothing to host and stays in-process.
    # Pcode.app's editor-only panes are the exception: always hosted, they pick
    # a model with /model in the host's preview.
    from pcode.dock import editor_only_requested

    has_model = bool(args.model or args.resume or load_preferences().get("model"))
    hosted = (
        args.attach is not None
        or (args.host is True and (has_model or editor_only_requested()))
        or (
            args.host is None
            and has_model
            and load_preferences().get("session_host", SETTINGS["session_host"].default) == "on"
        )
    )
    if args.resume and not args.fork and args.attach is None and not args.print:
        running = _running_host(args.resume, args.session_dir, args.workspace or Path.cwd())
        if running is not None:
            print(
                f"pcode: session {running.session_id[:8]} is running in background host "
                f"{running.id}; attaching to it.",
                file=sys.stderr,
            )
            args.attach, hosted = running.id, True
    if hosted and not args.print and not args.theme_preview:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("attaching to a session host needs a terminal")
        try:
            _run_hosted(args)
        except (LookupError, OSError, ValueError) as error:
            parser.exit(2, error_message(error) + "\n")
        return
    if not args.print and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        parser.error("interactive mode needs a terminal; use --print PROMPT to answer without one")
    saved = None
    app = None
    try:
        if args.resume:
            from pcode.sessions import SavedSession, SessionError

            saved = SavedSession.open(
                args.resume,
                args.session_dir,
                args.workspace or Path.cwd(),
                fork_if_open=True,
                fork=args.fork,
            )
            try:
                if args.model and args.model != saved.info.model:
                    raise SessionError(
                        "Cannot change models when resuming; start a new session instead."
                    )
                args.workspace = _resumed_workspace(saved.info, args.workspace)
            except BaseException:
                saved.abandon()
                saved = None  # Already closed; the `finally` below must not close it again.
                raise
            args.model = saved.info.model
        if not args.resume and not args.model:
            args.model = load_preferences().get("model")
        workspace = args.workspace or Path.cwd()
        if not workspace.is_dir():
            from pcode.sessions import SessionError

            raise SessionError(f"Workspace {workspace} is not an existing directory.")
        from pcode.preferences import rejected_project_keys, set_project_root

        set_project_root(workspace)
        if rejected := rejected_project_keys():
            print(
                f"pcode: ignoring user-only settings in .pcode/preferences.json: "
                f"{', '.join(rejected)} (set them with `pcode config set`)",
                file=sys.stderr,
            )
        from pcode.project_trust import prompt_trust

        # Before the worktree, whose setup script is one of the things being
        # trusted. --print has no one to ask, so untrusted code is skipped.
        prompt_trust(workspace, ask=None if args.print else ask)
        session_id = None
        if not args.resume and not args.no_worktree and not args.theme_preview:
            workspace, session_id = _enter_worktree(workspace, args.worktree)
        app = PreviewApp(
            theme=args.theme,
            model=args.model,
            workspace=workspace,
            saved_session=saved,
            save=not args.no_save,
            session_dir=args.session_dir,
            resume=bool(args.resume),
            initial_prompt=args.prompt,
            session_id=session_id,
            # Keep stdout for the reply alone when printing.
            console=Console(stderr=True) if args.print else None,
        )
        if args.print:
            ok = app.run_print(args.prompt)
            _leave_worktree_on_exit(app, ask=None, owner=session_id)
            if not ok:
                parser.exit(1)
        else:
            app.run()
            # `is True`: tests stand a Mock in for the app, which answers every attribute.
            if app.hosted is True or app.moved_to_host is True:
                # Moved to a host meanwhile (/detach, /attach): leave as a hosted terminal does.
                _leave_host(app)
            else:
                _leave_worktree_on_exit(app, owner=session_id)
    except Exception as error:
        parser.exit(2, error_message(error) + "\n")
    finally:
        if app is not None and app.model and hasattr(app.runtime, "close"):
            app.runtime.close()
        if saved is not None:
            # A copy made for a resume that never started is removed, not listed.
            saved.close() if app is not None else saved.abandon()


if __name__ == "__main__":
    main()
