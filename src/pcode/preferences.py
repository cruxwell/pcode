"""Non-secret, user-level defaults, separate from conversation storage."""

import json
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock
from pygments.styles import get_all_styles
from rich.cells import cell_len
from rich.spinner import SPINNERS as RICH_SPINNERS

from pcode.delta import LAYOUTS as DIFF_LAYOUTS
from pcode.profiling import PROFILE_MODES
from pcode.transcript_log import CHAR_BUDGET

# Every Pygments style installed here, including any added by a plugin package.
# The scan costs a few milliseconds once; Rich imports Pygments regardless.
# `terminal` is not a Pygments style: it hands every color, code included, to
# the terminal's own ANSI palette, so pcode matches whatever scheme it runs in.
TERMINAL_SYNTAX = "terminal"
SYNTAX_THEMES = (TERMINAL_SYNTAX, *sorted(get_all_styles()))


def _steady_width(frames: list[str]) -> bool:
    """Whether every frame takes the same few cells, so the row never shifts.

    Emoji spinners are out: terminals disagree on their width, and the
    variation-selector ones (`arrow2`) measure one cell but draw two.
    """
    widths = {cell_len(frame) for frame in frames}
    return (
        len(widths) == 1
        and widths <= {1, 2, 3}
        and all(ord(char) < 0x1F000 and char != "\ufe0f" for frame in frames for char in frame)
    )


# Rich's named spinners that fit the status row: `pcode config set spinner NAME`.
SPINNERS = tuple(
    sorted(name for name, spec in RICH_SPINNERS.items() if _steady_width(spec["frames"]))
)

EFFORTS = ("low", "medium", "high", "xhigh", "default")
# `/show-thinking` and Ctrl+T, in cycling order.
THINKING_MODES = ("off", "status-line", "scrollback")
OPENAI_PROVIDERS = ("openai", "openai-chat", "openai-responses", "openai-codex")
# Routes that reach a Claude model and so take Anthropic's own effort setting.
ANTHROPIC_PROVIDERS = ("anthropic", "claude")


@dataclass(frozen=True)
class Setting:
    default: str | None
    choices: tuple[str, ...] = ()
    positive_integer: bool = False
    # A count that may be zero, so it cannot reuse positive_integer's floor.
    whole_number: bool = False
    # An os.pathsep-separated list of directories; empty means "none".
    path_list: bool = False
    # A comma-separated list of names; empty meaning depends on the setting.
    name_list: bool = False
    # Terminal rows as a whole number, or a share of the screen below 1 (0.5).
    height: bool = False
    # `ctrl`, or the leader key(s) pressed before a shortcut's letter.
    key_prefix: bool = False
    # Escape, or literal printable characters to leave vi insert mode.
    vi_escape_sequence: bool = False
    # An additional, printable leader used only in vi normal mode.
    vi_key_prefix: bool = False
    # Tokens as `200000`, `200k`, or `1.5m`; see parse_token_count.
    token_count: bool = False
    # Command-line arguments, split as a shell would; empty means none.
    arguments: bool = False
    # One line shown beside the key in /config completions.
    description: str = ""

    def validate(self, key: str, value: str) -> None:
        if self.arguments:
            try:
                shlex.split(value)
            except ValueError as error:
                raise ValueError(f"{key} must be shell-style arguments: {error}.") from None
        elif self.path_list:
            if value and any(not entry.strip() for entry in value.split(os.pathsep)):
                raise ValueError(f"{key} must be directories separated by '{os.pathsep}'.")
        elif self.name_list:
            if value and any(
                not entry.strip() or any(char.isspace() for char in entry.strip())
                for entry in value.split(",")
            ):
                raise ValueError(f"{key} must be comma-separated names without whitespace.")
            if key == "model_providers":
                from pcode.models import PROVIDERS

                unknown = {
                    entry.strip() for entry in value.split(",") if entry.strip()
                } - PROVIDERS.keys()
                if unknown:
                    raise ValueError(f"Unknown model providers: {', '.join(sorted(unknown))}")
        elif self.vi_escape_sequence:
            if not value or not value.isprintable() or any(char.isspace() for char in value):
                raise ValueError(
                    f"{key} must be escape or printable characters without whitespace."
                )
        elif self.vi_key_prefix:
            parse_vi_key_prefix(value)
        elif self.key_prefix:
            parse_key_prefix(value)
        elif self.token_count:
            parse_token_count(value)
        elif self.height:
            if parse_height(value) is None:
                raise ValueError(
                    f"{key} must be a whole number of rows or a fraction of the screen "
                    "between 0 and 1 (0.5 is half)."
                )
        elif (self.positive_integer or self.whole_number) and value not in self.choices:
            # `choices` beside a number are its named values, such as "off".
            floor = 0 if self.whole_number else 1
            if not value.isascii() or not value.isdecimal() or int(value) < floor:
                raise ValueError(
                    f"{key} must be a whole number (0 or more)"
                    + "".join(f" or {choice}" for choice in self.choices)
                    + "."
                    if self.whole_number
                    else f"{key} must be a positive integer."
                )
        elif self.choices:
            if value not in self.choices:
                raise ValueError(f"{key} must be one of: {', '.join(self.choices)}")
        elif not value or any(char.isspace() for char in value):
            raise ValueError(f"{key} must be a non-empty model name without whitespace.")


# Compaction keeps up to an eighth of its budget verbatim plus a summary, on top
# of the fixed prompt (instructions, tool schemas, MCP), which alone can pass
# 10k. A cap much below this compacts every few requests or cannot make room.
MIN_AUTO_COMPACT_TOKENS = 50_000


def parse_token_count(text: str) -> int:
    """Read `200000`, `200k`, or `1.5m` as an automatic compaction cap."""
    value = text.strip().lower().replace("_", "").replace(",", "")
    scale = {"k": 1_000, "m": 1_000_000}.get(value[-1:], 1)
    if scale != 1:
        value = value[:-1]
    try:
        tokens = int(float(value) * scale)
    except ValueError, OverflowError:
        raise ValueError(f"Not a token count: {text!r}. Use e.g. 200000 or 200k.") from None
    if tokens < MIN_AUTO_COMPACT_TOKENS:
        raise ValueError(
            f"Use at least {MIN_AUTO_COMPACT_TOKENS // 1000}k tokens: below that, compaction "
            "cannot leave enough room."
        )
    return tokens


def parse_height(value: str | None) -> float | None:
    """Whole rows (12), or a share of the screen below 1 (0.5); None if invalid."""
    if not value:
        return None
    try:
        height = float(value)
    except ValueError:
        return None
    if height >= 1:
        return height if value.isascii() and value.isdecimal() else None
    return height if height > 0 else None


def screen_rows(height: float, rows: int) -> int:
    """A `parse_height` value as rows of a screen `rows` tall, never more than it has."""
    return min(rows, int(rows * height) if height < 1 else int(height))


# Ctrl chords every surface keeps for itself, or that the terminal sends as
# another key; a leader on one of them would take away something essential.
RESERVED_CHORDS = {
    "c-c": "cancels and closes popups",
    "c-d": "exits and half-pages",
    "c-h": "is Backspace",
    "c-i": "is Tab",
    "c-j": "inserts a newline",
    "c-m": "is Enter",
    "c-[": "is Escape",
    # Terminals send Ctrl+/ as Ctrl+_ (0x1f).
    "c-_": "opens keybinding help (Ctrl+/)",
}
_CTRL_HEADS = ("ctrl+", "ctrl-", "control+", "control-", "c-")
_CTRL_NAMES = {"space": "@", "spc": "@"}
_CTRL_SYMBOLS = "@]\\^_"


def _prefix_key(token: str) -> str:
    """One leader key as prompt_toolkit names it: ctrl+p is c-p, ctrl+space is c-@."""
    text = token.casefold()
    head = next((head for head in _CTRL_HEADS if text.startswith(head)), None)
    if head is not None:
        rest = _CTRL_NAMES.get(text[len(head) :], text[len(head) :])
        if len(rest) == 1 and ("a" <= rest <= "z" or rest in _CTRL_SYMBOLS):
            key = f"c-{rest}"
            if key in RESERVED_CHORDS:
                raise ValueError(
                    f"key_prefix cannot use {token}: it {RESERVED_CHORDS[key]} everywhere."
                )
            return key
    elif text.startswith("f") and text[1:].isdecimal() and 1 <= int(text[1:]) <= 24:
        return text
    raise ValueError(
        f"key_prefix: {token!r} is not a key pcode can use as a leader. "
        "Use ctrl, or keys such as ctrl+p, ctrl+space, ctrl+] or f2."
    )


def parse_vi_key_prefix(value: str) -> tuple[str, ...]:
    """An optional single printable leader, separate from the global prefix."""
    if value == "off":
        return ()
    if value == "<space>":
        return (" ",)
    if len(value) == 1 and value.isprintable() and not value.isspace():
        return (value,)
    raise ValueError(
        "vi_key_prefix must be off, <space>, or one printable non-whitespace character."
    )


def parse_key_prefix(value: str) -> tuple[str, ...]:
    """`ctrl` is () and makes each shortcut a Ctrl chord; otherwise the leader's keys.

    Several keys, separated by spaces, form one leader pressed in sequence
    (`ctrl+x ctrl+p`), the way Emacs spells a prefix. Raises ValueError.
    """
    tokens = value.split()
    if [token.casefold() for token in tokens] == ["ctrl"]:
        return ()
    if not tokens:
        raise ValueError("key_prefix must be ctrl or a leader key such as ctrl+p.")
    return tuple(_prefix_key(token) for token in tokens)


SEND_MODES = ("steering", "queue", "interrupt")

# A user-level directory shared across workspaces, plus the workspace's own
# `.agents/skills`, which the asset-root scan already covers but which belongs in
# the listed default so replacing the list is an informed choice.
DEFAULT_SKILL_DIRS = os.pathsep.join(("~/.agents/skills", ".agents/skills"))


SETTINGS = {
    "model_providers": Setting(
        "",
        name_list=True,
        description="Limit /model to comma-separated providers; empty shows all active providers",
    ),
    "send_mode": Setting(
        "steering",
        SEND_MODES,
        description="Enter mid-turn: steer the running turn, queue for after, or interrupt",
    ),
    "repo_context_walk_up": Setting(
        "on",
        ("on", "off"),
        description="Also load AGENTS.md files from directories above the workspace",
    ),
    "repo_context_nested": Setting(
        "off",
        ("off", "pointer", "contents"),
        description="Nested AGENTS.md found while reading: ignore, mention its path, or inject it",
    ),
    "skill_commands": Setting(
        "prefix",
        ("prefix", "bare", "both", "off"),
        description="SKILL.md assets as slash commands: /skill:NAME, /NAME, both, or none",
    ),
    # Relative entries resolve against the workspace; `~` expands to the user's home.
    "skill_dirs": Setting(
        DEFAULT_SKILL_DIRS,
        path_list=True,
        description=f"Extra skill directories, '{os.pathsep}'-separated, after workspace roots",
    ),
    # Extensions run arbitrary Python at launch, so a workspace's `.pcode/extensions`
    # is opt-in; the user-level directory beside preferences.json always loads.
    # `on` trusts every repository; `trusted_projects` is the per-repository grant
    # the launch prompt appends to (primary checkout paths).
    "project_extensions": Setting(
        "off",
        ("on", "off"),
        description="Load every workspace's .pcode/extensions without asking (runs code at launch)",
    ),
    "trusted_projects": Setting(
        "",
        path_list=True,
        description="Checkouts whose .pcode/extensions load without asking (launch prompt appends)",
    ),
    "extension_dirs": Setting(
        "",
        path_list=True,
        description=f"Extra extension directories, '{os.pathsep}'-separated, after the user one",
    ),
    # Which discovered extensions run, by name. An extension loads unless it is
    # named in `extensions_off`, or declares `DEFAULT_ENABLED = False` and is not
    # named in `extensions_on`. `/extensions on|off NAME` writes both.
    "extensions_off": Setting(
        "",
        name_list=True,
        description="Extensions never loaded, comma-separated (/extensions off NAME)",
    ),
    "extensions_on": Setting(
        "",
        name_list=True,
        description="Opt-in extensions to load, comma-separated (/extensions on NAME)",
    ),
    # `--worktree [NAME]` and `--no-worktree` override per run.
    "worktree": Setting(
        "off",
        ("on", "off"),
        description="Start each session in its own .worktrees/<session> git worktree",
    ),
    "worker_isolation": Setting(
        "off",
        ("on", "off"),
        description="Isolate built-in worker tasks in git worktrees (requires worktree=on)",
    ),
    "worker_concurrency": Setting(
        "0",
        whole_number=True,
        description="Concurrent worker cap; 0 is unlimited (reload to apply)",
    ),
    "subagent_models": Setting(
        "",
        name_list=True,
        description="Models delegate_task lists for sub-agents, comma-separated (/subagents)",
    ),
    # An untouched worktree is always removed; uncommitted changes are always kept.
    # Off by default: one process starts faster and leaves nothing behind.
    # `/detach` moves a session into a host when it should outlive the terminal.
    "session_host": Setting(
        "off",
        ("on", "off"),
        description="Run each session in a background host that outlives the terminal; "
        "off runs it inside the terminal until /detach moves it into one",
    ),
    # 0 by default: a stopped host costs nothing, since /switch still lists one
    # with an unseen result and resumes it.
    "session_host_idle_minutes": Setting(
        "0",
        ("off",),
        whole_number=True,
        description="Stop a background session idle this long with no terminal; "
        "0 stops it as soon as it is idle, off never",
    ),
    # `pcode --email-listen` (pcode.email_remote). All user-only: a repository
    # must not choose who controls it by email or loosen the limits.
    "email_owner": Setting(
        None, description="The Gmail address that may control pcode by email (--email-setup)"
    ),
    "email_mcp": Setting(
        "off", ("off", "on"), description="Start default MCP servers in email-started sessions"
    ),
    "email_turn_minutes": Setting(
        "30", whole_number=True, description="Wall-clock limit per email turn; 0 is none"
    ),
    "email_turn_requests": Setting(
        "100", whole_number=True, description="Model requests per email turn; 0 is no limit"
    ),
    "email_turn_tool_calls": Setting(
        "100", whole_number=True, description="Tool calls per email turn; 0 is no limit"
    ),
    "email_concurrent_sessions": Setting(
        "2", positive_integer=True, description="Email sessions working at once"
    ),
    "email_max_sessions": Setting(
        "20", positive_integer=True, description="Sessions one --email-listen may start"
    ),
    "email_session_inputs": Setting(
        "20", positive_integer=True, description="Emails waiting per session"
    ),
    "email_max_inputs": Setting(
        "100", positive_integer=True, description="Emails waiting across every session"
    ),
    # Defaults mirror claude_sdk.session_pool.MAX_IDLE_SESSIONS and IDLE_SECONDS.
    "claude_idle_processes": Setting(
        "1",
        whole_number=True,
        description="claude: models: finished Claude Code processes kept warm (~120 MB each); "
        "0 stops each after its turn",
    ),
    "claude_idle_minutes": Setting(
        "10",
        positive_integer=True,
        description="claude: models: minutes a finished Claude Code process is kept warm",
    ),
    "desktop_notifications": Setting(
        "on",
        ("on", "off"),
        description="Desktop notification when a background session finishes (OSC 9)",
    ),
    "session_naming": Setting(
        "on",
        ("on", "off"),
        description="Title each new session from its first prompt, asking its own model "
        "at low effort; /rename overrides",
    ),
    "terminal_title": Setting(
        "on",
        ("on", "off"),
        description="Set the terminal tab title to the session's name (OSC 0)",
    ),
    "terminal_progress": Setting(
        "auto",
        ("auto", "on", "off"),
        description="Tab progress bar while a turn runs (OSC 9;4); auto sends it only to "
        "terminals known to draw it",
    ),
    "worktree_exit": Setting(
        "ask",
        ("ask", "merge", "keep"),
        description="Worktree with unmerged commits on exit: ask, merge and remove, or keep",
    ),
    "retry_attempts": Setting(
        "3",
        whole_number=True,
        description="Automatic retries after a dropped connection or streamed overload; 0 disables",
    ),
    # Pydantic AI's default of 1 ends the turn on a second malformed call, which a
    # long `replacements` array can hit by itself.
    "tool_retries": Setting(
        "3",
        whole_number=True,
        description="Corrections offered to the model per turn when a tool call fails validation",
    ),
    # Anthropic mangles the `replacements` array roughly one call in ten. Strict
    # mode was meant to make that unsamplable but measurably does not (see
    # `pcode.strict_tools`); it stays on because nothing got worse. Models that
    # cannot honor it, and schemas outside the subset it accepts, decline it on
    # their own, so "on" costs nothing where it does not apply.
    "strict_tools": Setting(
        "on",
        ("on", "off"),
        description="Constrain edit_file arguments with Anthropic strict tool use",
    ),
    "cache_notices": Setting(
        "on",
        ("on", "off"),
        description="Note in the transcript when a request reuses less of the prompt cache",
    ),
    "debug": Setting(
        "off",
        ("on", "off"),
        description="Write prompt-cache request fingerprints with each cache notice",
    ),
    # Captures land in the state directory, oldest pruned; `--no-profile` skips
    # one run and `--profile DIR` still names its own directory.
    "profile": Setting(
        "off",
        PROFILE_MODES,
        description="Record each session's resource use; cpu/memory add a slow tracer",
    ),
    # A heartbeat task and one polling thread; cheap enough to leave on so the
    # freeze nobody expected is already logged when it happens.
    "stall_log": Setting(
        "on",
        ("on", "off"),
        description="Log what blocked the editor whenever typing stalls ~150 ms or more",
    ),
    "transcript_max_chars": Setting(
        str(CHAR_BUDGET),
        positive_integer=True,
        description="Retained transcript text budget for resume and redraw (characters)",
    ),
    "error_scrollback_lines": Setting(
        "20",
        positive_integer=True,
        description="Lines of an error notice kept in scrollback before it is clipped",
    ),
    "tool_error_scrollback": Setting(
        "off",
        ("on", "off"),
        description="Keep a failed tool call's full diagnostic in scrollback, not one line",
    ),
    "regenerate_on_resize": Setting(
        "on",
        ("on", "off"),
        description="Rebuild scrollback at the new width when the terminal resizes",
    ),
    "paced_scrollback": Setting(
        "typed",
        ("typed", "rows", "off"),
        description="Type settled prose out, or roll blocks in by row, instead of landing at once",
    ),
    "show_edits": Setting(
        "on",
        ("on", "off"),
        description="Show a diff preview of each file edit in the transcript",
    ),
    # Layout-only: /config applies it at once.
    "live_edits": Setting(
        "off",
        ("on", "off"),
        description="Preview an edit or run_code snippet at the bottom while the model writes it",
    ),
    # Read at launch, like show_edits.
    "diff_renderer": Setting(
        "delta",
        ("delta", "rich"),
        description="Diffs in scrollback and /diffs: delta when installed (else rich), or rich",
    ),
    "delta_args": Setting(
        "",
        arguments=True,
        description="delta arguments, e.g. '--line-numbers'; git config is ignored, and these "
        "win over pcode's own",
    ),
    "diff_layout": Setting(
        "unified",
        DIFF_LAYOUTS,
        description="delta layout: unified, side-by-side, or auto (side-by-side at 180+ columns)",
    ),
    "diff_dedent": Setting(
        "on",
        ("on", "off"),
        description="Strip the indentation every line of a diff hunk shares, delta or rich",
    ),
    "show_commands": Setting(
        "off",
        ("on", "off"),
        description=(
            "Mirror shell command output into scrollback (the g shortcut, Ctrl+G by default, "
            "toggles it)"
        ),
    ),
    "group_tools": Setting(
        "on",
        ("on", "off"),
        description="Fold each run of consecutive tool calls into one summary line",
    ),
    "command_scrollback_lines": Setting(
        "20",
        positive_integer=True,
        description="Lines of shell output mirrored into scrollback per command",
    ),
    # The pinned live preview competes with the editor for screen space, so it
    # caps separately from the scrollback mirror.
    "command_preview_lines": Setting(
        "10",
        height=True,
        description="Lines of the pinned live preview of a running shell command, "
        "or 0.25 for a quarter of the screen",
    ),
    "show_tasks": Setting(
        "on", ("on", "off"), description="Show the model's plan as a pinned task list"
    ),
    "autohide_tasks": Setting(
        "off", ("on", "off"), description="Hide the task list when a turn ends"
    ),
    # Checked against each terminal's own pane on every resize, so a split
    # hides the list and unsplitting brings it back; 0 never hides.
    "tasks_min_rows": Setting(
        "30",
        whole_number=True,
        description="Hide the task list in a pane shorter than this (0: never)",
    ),
    "tasks_min_columns": Setting(
        "100",
        whole_number=True,
        description="Hide the task list in a pane narrower than this (0: never)",
    ),
    "show_hints": Setting(
        "on",
        ("on", "off"),
        description="Show the contextual keybindings indicator in the prompt status line",
    ),
    "attach_tasks": Setting(
        "on", ("on", "off"), description="Draw the task list inside the editor box"
    ),
    "frame_style": Setting(
        "shaded",
        ("borders", "shaded"),
        description="Editor and task boxes: line borders, or background shades "
        "mixed from the terminal's own background",
    ),
    "task_style": Setting(
        "status",
        ("status", "icons"),
        description="Task rows: shade text by status, or one weight with coloured icons",
    ),
    "tool_glyphs": Setting(
        "auto",
        ("auto", "on", "off"),
        description="Symbols on the tool row; off spells out verbs but shell's $ "
        "(auto: off on the Linux console or with ASCII glyphs)",
    ),
    "glyphs": Setting(
        "auto",
        ("auto", "unicode", "ascii"),
        description="Draw marks, borders and spinners as Unicode or plain ASCII (auto: ASCII "
        "on the Linux console, a non-UTF-8 locale, or a terminal drawing them two cells wide)",
    ),
    "color_depth": Setting(
        "auto",
        ("auto", "truecolor", "256"),
        description="Colors the editor and transcript draw with (auto: 24-bit where the "
        "terminal says or is known to support it, else 256)",
    ),
    "tool_max_lines": Setting(
        "3",
        positive_integer=True,
        description="Max tool calls listed above the status row, newest last; "
        "a short pane shows fewer",
    ),
    "tool_linger_seconds": Setting(
        "10",
        whole_number=True,
        description="Seconds a finished tool call stays listed above the status row (0: until "
        "newer calls push it out)",
    ),
    "tasks_max_height": Setting(
        None,
        height=True,
        description="Max height of the task list plus editor: rows, or 0.5 for half the screen",
    ),
    # Read when the prompt is built, so it applies on the next launch.
    "spinner": Setting(
        "dots",
        SPINNERS,
        description="Animation on the status row while the model works (a Rich spinner name)",
    ),
    # Distinct from `spinner` so a running command never reads as the model.
    "tool_spinner": Setting(
        "arc",
        SPINNERS,
        description="Animation on a running tool call's row (a Rich spinner name)",
    ),
    "show_thinking": Setting(
        "status-line",
        THINKING_MODES,
        description="Where the model's thinking shows: its own rows above the status row, "
        "streamed into scrollback, or nowhere",
    ),
    # Read when the prompt is built, so it applies on the next launch.
    "thinking_max_lines": Setting(
        "10",
        height=True,
        description="Max rows of thinking above the status row (status-line mode), "
        "or 0.2 for a fifth of the screen; a fixed count is capped at a quarter of it",
    ),
    "editing_mode": Setting(
        "emacs", ("emacs", "vi"), description="Key bindings for the prompt editor"
    ),
    "vi_key_prefix": Setting(
        "off",
        vi_key_prefix=True,
        description="Additional vi normal-mode shortcut leader: off, <space>, or one printable "
        "character (next launch)",
    ),
    "vi_escape_sequence": Setting(
        "escape",
        vi_escape_sequence=True,
        description="Leave vi insert mode: escape or a literal sequence such as jj; "
        "Escape always works (next launch)",
    ),
    "vi_wrapped_rows": Setting(
        "on",
        ("on", "off"),
        description="Vi normal-mode j/k move by wrapped screen row, not by line (next launch)",
    ),
    "theme": Setting(
        "auto",
        ("dark", "light", "auto"),
        description="Palette for the terminal background; auto detects it",
    ),
    # Chosen per palette so `theme auto` keeps highlighting legible on either
    # background. `terminal` uses the terminal's ANSI colors for everything.
    "syntax_dark": Setting(
        TERMINAL_SYNTAX,
        SYNTAX_THEMES,
        description="terminal (ANSI colors) or a Pygments style, for the dark palette",
    ),
    "syntax_light": Setting(
        TERMINAL_SYNTAX,
        SYNTAX_THEMES,
        description="terminal (ANSI colors) or a Pygments style, for the light palette",
    ),
    "autocompact": Setting(
        "on",
        ("on", "off"),
        description="Compact the conversation automatically as the context window fills",
    ),
    "autocompact_tokens": Setting(
        None,
        token_count=True,
        description="Compact automatically by this many context tokens (e.g. 200k), even if "
        "the window has more room; unset uses about 90% of the window",
    ),
    # On by default so the wheel scrolls popups; plain drag-to-select then needs
    # a modifier. Read when a popup opens, so no restart is needed.
    # Read as each popup opens; the main prompt picks it up on the next launch.
    "key_prefix": Setting(
        "ctrl",
        key_prefix=True,
        description=(
            "Shortcut prefix: ctrl for Ctrl+key chords, or a leader such as ctrl+p "
            "pressed before the key"
        ),
    ),
    "popup_mouse": Setting(
        "on",
        ("on", "off"),
        description=(
            "Mouse clicks, wheel scrolling and drag-to-copy in popups; "
            "off leaves text selection to the terminal"
        ),
    ),
    # Read when a side answer settles, so `/config set` applies without a restart.
    "btw_auto_open": Setting(
        "on",
        ("on", "off"),
        description="Open the side-answer viewer as soon as a /btw answer is ready",
    ),
    # Read each time the viewer opens, so `/config set` applies without a restart.
    "btw_view": Setting(
        "popup",
        ("panel", "popup"),
        description=(
            "Show /btw threads in a panel above the editor, which replies to them, "
            "or in a full-screen popup"
        ),
    ),
    # Read when a job finishes, so `/config set` applies without a restart.
    "job_wake": Setting(
        "on",
        ("on", "off"),
        description="Start a turn when a job the model backgrounded finishes while idle",
    ),
    # Edits, plans, shell, and delegation stay native so their transcript display survives.
    "code_mode": Setting(
        "off",
        ("on", "off"),
        description="Batch read-only tool calls through one sandboxed run_code snippet",
    ),
    # Read by the bundled web_research extension. Applies on /reload or next launch.
    "web_search": Setting(
        "auto",
        ("auto", "local", "off"),
        description="Web tools: provider-native when available, local-only, or none",
    ),
    # Read by the bundled web_research extension, under `web_search`: `off` there
    # removes fetching too. `native` admits fetches that return a summary, not the page.
    "web_fetch": Setting(
        "auto",
        ("auto", "native", "local"),
        description=(
            "Page fetching: provider-native where it returns the page text, any provider-native"
            " fetch (including Claude Code's summaries), or pcode's own get_page"
        ),
    ),
    "tool_output_mode": Setting(
        "spill",
        ("spill", "truncate", "off"),
        description="Large tool results: spill to a file with a handle, truncate, or keep whole",
    ),
    "tool_output_threshold": Setting(
        "10000",
        positive_integer=True,
        description="Characters a tool result may have before it is spilled or truncated",
    ),
    "tool_output_preview_chars": Setting(
        "1000",
        positive_integer=True,
        description="Characters of a spilled result shown inline beside its handle",
    ),
    "tool_output_max_chars": Setting(
        "4000",
        positive_integer=True,
        description="Characters kept when a tool result is truncated",
    ),
    "tool_output_strategy": Setting(
        "head_tail",
        ("head", "tail", "head_tail"),
        description="Which part of a truncated tool result survives",
    ),
    "tool_output_retention_hours": Setting(
        "0",
        whole_number=True,
        description="Hours to keep spilled results on disk; 0 keeps them forever",
    ),
    "effort": Setting(
        "default", EFFORTS, description="Default reasoning effort for models /effort has not set"
    ),
    "model": Setting(None, description="Model name; unset uses the offline preview"),
}


# Settings a repository must not be able to set for whoever clones it: each
# either runs code on launch or chooses which credential or local process to
# use. They are read from the user file only; a project file setting them is
# reported by `rejected_project_keys` and otherwise ignored.
USER_ONLY = frozenset(
    {
        "project_extensions",
        "trusted_projects",
        "extension_dirs",
        "extensions_off",
        "extensions_on",
        *(key for key in SETTINGS if key.startswith("email_")),
    }
)

PROJECT_PREFERENCES = Path(".pcode") / "preferences.json"

# The launch workspace, fixed once by the CLI so every later load_preferences()
# call sees the same overlay. Not the current directory: `-C` and worktrees
# make those differ, and the worktree carries the same committed file anyway.
_project_root: Path | None = None


def set_project_root(path: Path | None) -> None:
    global _project_root
    _project_root = path.resolve() if path is not None else None


def config_dir() -> Path:
    """pcode's user config directory: `$PCODE_CONFIG_DIR`, else `$XDG_CONFIG_HOME/pcode`.

    Preferences, `mcp.json`, extensions, the worktree setup script, and stored
    logins all live here. `PCODE_CONFIG_DIR` names the directory directly so
    independent instances can have their own login and settings without moving
    every other XDG-aware program along with them.
    """
    override = os.environ.get("PCODE_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "pcode"


def preferences_path() -> Path:
    return config_dir() / "preferences.json"


def project_preferences_path() -> Path | None:
    """The workspace's overlay file, or None before the CLI has named a workspace."""
    return _project_root / PROJECT_PREFERENCES if _project_root is not None else None


def _read_valid(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return valid_preferences(data)


def valid_preferences(data: dict) -> dict[str, str]:
    """Keep valid overrides before merging layers, so invalid values inherit."""
    result = {}
    for key, setting in SETTINGS.items():
        value = data.get(key)
        if not isinstance(value, str):
            continue
        try:
            setting.validate(key, value)
        except ValueError:
            continue
        result[key] = value
    return result


def load_preferences() -> dict[str, str]:
    """User defaults with the workspace's `.pcode/preferences.json` layered on top."""
    merged = _read_valid(preferences_path())
    for key, value in _read_valid(project_preferences_path()).items():
        if key not in USER_ONLY:
            merged[key] = value
    return merged


def subagent_models() -> list[str]:
    """The `subagent_models` setting as model names, in order, without repeats."""
    value = load_preferences().get("subagent_models", "")
    return list(dict.fromkeys(entry.strip() for entry in value.split(",") if entry.strip()))


def from_project(key: str) -> bool:
    """Whether the workspace's `.pcode/preferences.json` decides `key`."""
    return key not in USER_ONLY and key in _read_valid(project_preferences_path())


def rejected_project_keys() -> list[str]:
    """User-only keys the project file tries to set, for a launch warning."""
    path = project_preferences_path()
    if path is None:
        return []
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return []
    return sorted(USER_ONLY & set(data)) if isinstance(data, dict) else []


def read_preferences(path: Path | None = None) -> dict:
    """Read without discarding unknown keys or silently repairing a broken file."""
    path = path or preferences_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        raise ValueError(
            f"Invalid preferences JSON: {path}. Repair the file before editing."
        ) from None
    if not isinstance(data, dict):
        raise ValueError(f"Preferences must be a JSON object: {path}")
    return data


def save_preferences(**updates: str) -> None:
    update_preferences(updates)


def update_preferences(
    updates: dict[str, str], *, remove: tuple[str, ...] = (), path: Path | None = None
) -> None:
    path = path or preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize read/modify/write across terminals, including legacy shortcuts.
    with FileLock(str(path) + ".lock", timeout=5):
        data = read_preferences(path)
        for key in remove:
            data.pop(key, None)
        data.update(updates)
        write_json(path, data)


def write_json(path: Path, data: dict) -> None:
    # Atomic replacement avoids leaving a partial file after an interrupted write.
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
            name = file.name
            json.dump(data, file, indent=2, ensure_ascii=False)
            file.write("\n")
        # Keep an existing file's mode; a new one stays private (0600).
        try:
            shutil.copymode(path, name)
        except FileNotFoundError:
            pass
        os.replace(name, path)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


# Per-model effort lives outside SETTINGS: it is a mapping, not a string, and
# the `effort` setting stays as the fallback for models never chosen explicitly.
MODEL_EFFORTS_KEY = "model_efforts"


def _model_efforts(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return {}
    stored = data.get(MODEL_EFFORTS_KEY) if isinstance(data, dict) else None
    if not isinstance(stored, dict):
        return {}
    return {
        model: effort
        for model, effort in stored.items()
        if isinstance(model, str) and effort in EFFORTS
    }


def model_efforts() -> dict[str, str]:
    """Saved effort per model, with the workspace overlay layered on top."""
    merged = _model_efforts(preferences_path())
    merged.update(_model_efforts(project_preferences_path()))
    return merged


def effort_for(model: str | None) -> str | None:
    """The effort to request for `model`: its own, else the shared default."""
    saved = model_efforts().get(model or "")
    return saved if saved is not None else load_preferences().get("effort")


def save_model_effort(model: str, effort: str) -> None:
    """Record `effort` for `model` alone, leaving other models untouched."""
    with FileLock(str(preferences_path()) + ".lock", timeout=5):
        path = preferences_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_preferences(path)
        stored = data.get(MODEL_EFFORTS_KEY)
        stored = dict(stored) if isinstance(stored, dict) else {}
        stored[model] = effort
        data[MODEL_EFFORTS_KEY] = stored
        write_json(path, data)


def anthropic_profile(model: str | None, resolved=None) -> dict:
    """The profile for `model`, from `resolved` when it is a model object.

    Only meaningful once the caller knows the model is an Anthropic one: the
    object's own profile is returned whatever it is. Before /login a model is
    still its name, and a real `Model` always has a non-empty profile, so a
    missing attribute means the name lookup rather than credential loading.
    """
    profile = getattr(resolved, "profile", None)
    if profile is not None:
        return profile
    from pydantic_ai.profiles.anthropic import anthropic_model_profile

    return anthropic_model_profile((model or "").split(":", 1)[-1]) or {}


def effort_setting(model: str | None, resolved=None) -> str | None:
    """The settings key /effort writes for `model`, or None where it has none.

    Anthropic gates effort per model (Opus 4.5+, Sonnet 4.6+): the rest reject
    `output_config.effort` outright, and the adapter forwards an explicitly set
    `anthropic_effort` without consulting the profile. So asking Haiku for an
    effort is a 400 on the next turn, not a setting the provider ignores.
    """
    provider = (model or "").split(":", 1)[0]
    if provider in OPENAI_PROVIDERS:
        return "openai_reasoning_effort"
    if provider in ANTHROPIC_PROVIDERS:
        if anthropic_profile(model, resolved).get("anthropic_supports_effort"):
            return "anthropic_effort"
    return None


def effort_unavailable(model: str | None) -> str:
    """Why /effort refuses `model`, naming the model where the provider is fine."""
    if (model or "").split(":", 1)[0] in ANTHROPIC_PROVIDERS:
        return (
            f"{model} has no effort control; Anthropic gates it to "
            "Opus 4.5+ and Sonnet 4.6+ models."
        )
    base = "Effort control requires an OpenAI/Codex, Anthropic, or Claude model"
    return f"{base}; {model} is not one." if model else f"{base}."


def current_effort(agent, model: str) -> str:
    """The effort `agent` will request next, as /effort names it.

    `n/a` where the model has no effort control, which `default` would hide:
    nothing is sent either way, but only one of them can be changed.
    """
    resolved = getattr(agent, "model", None)
    key = effort_setting(model, resolved)
    if key is None:
        return "n/a"
    settings = getattr(resolved, "settings", None) or {}
    settings = {**settings, **(getattr(agent, "model_settings", None) or {})}
    effort = settings.get(key, "default")
    return "xhigh" if effort == "max" else effort


def apply_effort(agent, model: str, effort: str | None) -> None:
    resolved = getattr(agent, "model", None)
    key = effort_setting(model, resolved)
    if effort not in EFFORTS or key is None:
        return
    settings = dict(agent.model_settings or {})
    if effort == "default":
        settings.pop(key, None)
    else:
        # Older Claude models call their highest effort "max", not "xhigh".
        if key == "anthropic_effort" and effort == "xhigh":
            supports = anthropic_profile(model, resolved).get("anthropic_supports_xhigh_effort")
            effort = "xhigh" if supports else "max"
        settings[key] = effort
    agent.model_settings = settings


# Claude models whose thinking is on with no `thinking` field, per Anthropic's
# thinking docs. The status line (the default mode) only asks these for
# readable thinking: on the rest, asking would turn thinking on, which costs
# tokens and is refused alongside a non-default temperature.
ANTHROPIC_THINKS_BY_DEFAULT = re.compile(
    r"claude-(?:(?:opus|sonnet)-(?:[5-9]|\d{2,})\b|fable|mythos)"
)
# Models that write progress updates between tool calls, per Anthropic's
# thinking docs. `display: "updates"` returns those short notes, written for
# someone watching the agent, and nothing else: a status line's own format.
# `(?!\d)`: `fable-5` must not also match a future `fable-50`, which would get
# the beta (and a 400) without supporting it.
ANTHROPIC_PROGRESS_UPDATES = re.compile(r"claude-(?:opus-5-5|sonnet-5-5|fable-5|mythos-5-1)(?!\d)")
UPDATES_BETA = "thinking-display-updates-2026-08-18"


def thinking_mode_preference() -> str:
    """The saved `/show-thinking` mode, or its default."""
    return load_preferences().get("show_thinking", SETTINGS["show_thinking"].default)


def hints_preference() -> bool:
    """Whether the prompt's keybinding help indicator (`show_hints`) is on."""
    return load_preferences().get("show_hints", SETTINGS["show_hints"].default) == "on"


def preference_count(key: str) -> int:
    """A saved whole-number setting, or its default when unset or not a number."""
    value = str(load_preferences().get(key, ""))
    if not value.isascii() or not value.isdecimal():
        value = SETTINGS[key].default
    return int(value)


def tool_rows_preference() -> int:
    """The most tool rows above the status row (`tool_max_lines`)."""
    return int(load_preferences().get("tool_max_lines", SETTINGS["tool_max_lines"].default))


def openai_profile(model: str, resolved=None) -> dict:
    """The profile for an OpenAI `model`, from `resolved` when it is a model object."""
    profile = getattr(resolved, "profile", None)
    if profile is not None:
        return profile
    from pydantic_ai.profiles.openai import openai_model_profile

    return openai_model_profile(model.split(":", 1)[-1]) or {}


def thinking_settings(model: str, resolved, mode: str) -> dict:
    """The request settings that make thinking readable in `mode`.

    `off` asks for nothing. `status-line` wants short text: Anthropic's
    progress updates where the model writes them, else summaries.
    `scrollback` wants the fullest text the provider returns: Anthropic's
    summaries (it never returns raw thinking) and OpenAI's `auto`, which is
    each model's most detailed summarizer. `claude:` always gets summaries
    and `openai-codex:` detailed ones, whatever the mode (see
    `claude_sdk.SessionConfig` and `cache_settings`).

    `claude:` is left on summaries on purpose. Its CLI rejects
    `--thinking-display updates`, and sends `updates` itself only on some
    logins (not a subscription's). `CLAUDE_CODE_EXTRA_BODY` plus
    `ANTHROPIC_BETAS` can force it, and the server accepts that, but under
    the CLI's harness Opus 5.5 writes its progress notes as ordinary text:
    forcing `updates` only hid the reasoning, and left the row empty
    (checked live, CLI 2.1.283).
    """
    if mode not in ("status-line", "scrollback"):
        return {}
    provider = model.split(":", 1)[0]
    if provider == "anthropic":
        if mode == "status-line" and not ANTHROPIC_THINKS_BY_DEFAULT.search(model):
            return {}
        if not anthropic_profile(model, resolved).get("anthropic_supports_adaptive_thinking"):
            # Older models think only when asked. The budget is below the
            # adapter's default max_tokens (4096); effort and output limits
            # are never changed as a side effect of visibility.
            return {
                "anthropic_thinking": {
                    "type": "enabled",
                    "budget_tokens": 2048,
                    "display": "summarized",
                }
            }
        if mode == "status-line" and ANTHROPIC_PROGRESS_UPDATES.search(model):
            return {
                "anthropic_thinking": {"type": "adaptive", "display": "updates"},
                "anthropic_betas": [UPDATES_BETA],
            }
        return {"anthropic_thinking": {"type": "adaptive", "display": "summarized"}}
    if provider in ("openai", "openai-responses"):
        # One summarizer per model; `auto` picks it, and a model that does not
        # reason is never sent a reasoning setting. An API-key organisation
        # that is not verified gets a 400 for asking: `off` is the way out.
        if openai_profile(model, resolved).get("openai_supports_reasoning"):
            return {"openai_reasoning_summary": "auto"}
    return {}


# The settings `thinking_settings` owns, by provider; others are left alone.
# Nothing else in pcode sets `anthropic_betas`.
THINKING_KEYS = {
    "anthropic": ("anthropic_thinking", "anthropic_betas"),
    "openai": ("openai_reasoning_summary",),
    "openai-responses": ("openai_reasoning_summary",),
}


def apply_thinking(agent, model: str, mode: str) -> None:
    """Request readable thinking for `mode` on future turns, not just a UI preview."""
    keys = THINKING_KEYS.get(model.split(":", 1)[0])
    if keys is None:
        return
    current = getattr(agent, "model_settings", None)
    wanted = thinking_settings(model, getattr(agent, "model", None), mode)
    if not wanted and not any(key in (current or {}) for key in keys):
        return
    settings = {k: v for k, v in (current or {}).items() if k not in keys}
    settings.update(wanted)
    # Replace instead of mutating settings captured by an in-flight run.
    agent.model_settings = settings or None
