"""Editable prompt with replayable output in the terminal's normal scrollback."""

import asyncio
import os
import re
from asyncio import Future
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import asdict, dataclass, field, replace
from functools import cache, cached_property, lru_cache, partial, wraps
from io import StringIO
from time import monotonic

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app, get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import merge_completers
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Always, Condition, has_focus
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import (
    ConditionalContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.containers import VerticalAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.renderer import Renderer
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Label
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.spinner import SPINNERS as RICH_SPINNERS
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from pcode import glyphs
from pcode.block import INDENT, RULE, RUNNING, block_heading
from pcode.command_transcript import CommandTranscript
from pcode.commands import CommandRegistry, SlashCompleter
from pcode.delta import Delta
from pcode.delta import from_preferences as delta_from_preferences
from pcode.edit_transcript import (
    EditTranscript,
    LiveDeltaPreview,
    edit_preview_rows,
    prefetch_edits,
)
from pcode.file_refs import FileReferenceCompleter, ReferenceLexer, reference_fragment
from pcode.frame import TITLE_CHROME, Frame
from pcode.input_keys import configure_newline_keys
from pcode.jobs import WATCHED_PREFIX
from pcode.keyboard_protocol import keyboard_output
from pcode.keymap import PromptKeymap
from pcode.layout_speed import install_fast_layout_division
from pcode.popup_ui import shortcut_hint
from pcode.preferences import (
    SETTINGS,
    SYNTAX_THEMES,
    TERMINAL_SYNTAX,
    load_preferences,
    parse_height,
    screen_rows,
)
from pcode.prefix_keys import PrefixKeys, shortcut_label
from pcode.prompt_keys import PromptCallbacks, prompt_key_bindings
from pcode.runtime import CacheBust, CommandOutput, Event, Message, Thinking, ToolSummary
from pcode.shell_mode import SHELL_PREFIX
from pcode.syntax_colors import derive_colors
from pcode.task_prompt import TaskPrompt
from pcode.terminal_color import color_depth, true_color
from pcode.terminal_text import safe_text
from pcode.theme import detect_theme, terminal_background
from pcode.theme_gallery import SyntaxGallery
from pcode.thinking_markdown import ThinkingMarkdown
from pcode.tool_display import (
    COMMAND_TOOLS,
    EDIT_TOOLS,
    PLAN_TOOLS,
    command_preview,
    command_text,
    label,
    plain,
    split_outcome,
    tool_summary_lines,
)
from pcode.tool_panel import (
    ACTIVE_TASK_ICON,
    DELEGATE,
    TASK_ROWS,
    ToolCall,
    ToolHistory,
    panel_fragments,
    task_panel_rows,
)
from pcode.transcript_log import RetainedMarkdown, TranscriptLog, recorded
from pcode.transcript_notice import Note, TranscriptNotice
from pcode.word_wrap import WordWrapProcessor
from pcode.workers import Workers

# `task_style icons`: task text has one weight whatever its status, and the
# coloured icon alone says how a task stands. Cancelled keeps its strike: a
# mark, not a shade. A delegate's hue still colours its rows, since its class
# is named after these in the row's style string.
ICON_TASK_STYLES = {
    "plan.pending": "nodim nobold fg:default",
    "plan.in_progress": "nodim nobold fg:default",
    "plan.completed": "nodim nobold fg:default",
    "plan.cancelled": "nodim nobold fg:default strike",
    "plan.blocked": "nodim nobold fg:default",
}


def _mix(color: str, toward: str, amount: float) -> str:
    """`color` moved `amount` (0 to 1) of the way to `toward`; both `#rrggbb`."""
    start = [int(color[i : i + 2], 16) for i in (1, 3, 5)]
    end = [int(toward[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(
        f"{round(a + (b - a) * amount):02x}" for a, b in zip(start, end, strict=True)
    )


def shaded_styles(shade: str, top: str) -> dict[str, str]:
    """`frame_style shaded`: the editor and task boxes as surfaces, not outlines.

    `shade` fills the box. Border glyphs keep their cells in the shade's own
    color, so the geometry, and every height sum built on it, is the same in
    both styles.

    `top` is the editor's header block (see `header_shade`): its top row
    (status, task heading or session name), and while a turn runs the
    activity band under it, so the turn's live rows read as one header
    without a drawn line.
    """
    # Appended after the palette's `frame.border`: for equal classes the later
    # rule wins, so these override its muted color.
    return {
        "shaded": f"bg:{shade}",
        # nodim: the terminal palette mutes borders with dim, which would
        # leave a faint line in the surface's own color.
        "shaded frame.border": f"nodim fg:{shade}",
        "shaded.top": f"bg:{top}",
        "shaded.top frame.border": f"nodim fg:{top}",
    }


@cache
def frame_shade(background: str, theme: str) -> str:
    """A shade lifted off `background` toward the theme's contrast.

    Mixed from the terminal's actual background rather than fixed, so the box
    reads as a gentle step up on any scheme. Light backgrounds need a smaller
    step: the eye picks out darkening sooner than lightening.
    """
    if theme == "light":
        return _mix(background, "#000000", 0.045)
    return _mix(background, "#ffffff", 0.06)


@cache
def header_shade(background: str, theme: str) -> str:
    """The editor's header block: a step darker than the box, much the same
    step to the eye on either theme.

    Dark, halfway back to the background, so the step scales with the box's
    own lift: a fixed share of the shade all but vanished there (three of
    255 levels on a typical dark scheme, none on black). Light, a small
    share toward black, about as far as the dark step looks; the box's own
    share overdid it, reading as a stripe rather than a header.
    """
    shade = frame_shade(background, theme)
    if theme == "light":
        return _mix(shade, "#000000", 0.028)
    return _mix(shade, background, 0.5)


@dataclass(frozen=True)
class Palette:
    accent: str
    muted: str
    surface: str
    foreground: str
    selected: str
    task_heading: str
    # A finished task's tick.
    success: str
    # The hue ring for running sub-agents: each takes the first free slot and
    # keeps it, so its header and plan rows read as one block.
    agents: tuple[str, str, str]

    @cache
    def rich_theme(self) -> Theme:
        # Body text/background remain terminal-native; accents and bounded code
        # surfaces are explicitly paired for the selected appearance.
        return Theme(
            {
                "pcode.accent": self.accent,
                "pcode.brand": f"bold {self.accent}",
                "pcode.muted": self.muted,
                # Scrollback notes: italic like the live panel's own notices,
                # and dimmed so they recede behind the model's prose.
                "pcode.note": f"italic dim {self.muted}",
                "pcode.thinking": f"dim {self.muted}",
                "pcode.error": "bold red",
                "pcode.warning": "bold yellow",
                # A /btw thread's heading (`pcode.btw_view`), in the hue the
                # editor's box takes while it replies to the thread.
                "pcode.btw": f"bold {self.agents[1]}",
                "pcode.btw.badge": f"bold reverse {self.agents[1]}",
                "markdown.code": f"{self.foreground} on {self.surface}",
                "markdown.code_block": self.foreground,
                "markdown.block_quote": f"italic {self.muted}",
                "markdown.h1": f"bold underline {self.accent}",
                "markdown.h2": f"bold {self.accent}",
                "markdown.h3": f"bold {self.accent}",
                "markdown.h4": f"italic {self.accent}",
                "markdown.h5": f"italic {self.accent}",
                "markdown.h6": self.muted,
                "markdown.h7": f"italic {self.muted}",
                "markdown.hr": self.muted,
                "markdown.link": f"underline {self.accent}",
                "markdown.link_url": f"underline {self.accent}",
                "markdown.list": self.accent,
                "markdown.item.number": self.accent,
                "markdown.table.border": self.muted,
                "markdown.table.header": f"bold {self.accent}",
            }
        )

    @cache
    def prompt_style(
        self,
        menu: "Palette | None" = None,
        task_style: str = "status",
        shades: tuple[str, str] | None = None,
    ) -> Style:
        # Palette is immutable. Reuse the Style so DynamicStyle's identity-based
        # invalidation hash changes only with the palette, not on every redraw.
        # `menu` colors the completion popup, which follows the selected syntax
        # style rather than this palette; it is immutable and cached too.
        # `task_style` is the `task_style` setting (see ICON_TASK_STYLES).
        menu = self if menu is None else menu
        highlight = (
            f"reverse bg:default {menu.accent}"
            if menu.selected == "reverse"
            else f"noreverse bg:{menu.selected} {menu.accent}"
        )
        return Style.from_dict(
            {
                "plan": self.muted,
                "plan.heading": f"nodim {self.task_heading} bold",
                # Every task finished: the count turns the colour of the turn's tick.
                "plan.heading.done": f"nodim {self.success} bold",
                "plan.hint": f"nodim nobold {self.muted}",
                # Task rows by status, in three weights: the active one is
                # loud, what is left is muted, and settled work recedes.
                # `tool_panel.panel_fragments` gives the icon and tree guides
                # classes of their own, so the colour that says how a task
                # stands survives a delegate's hue on its text.
                "plan.pending": self.muted,
                "plan.in_progress": f"nodim {self.accent} bold",
                "plan.completed": f"{self.muted} dim",
                "plan.cancelled": f"{self.muted} dim strike",
                "plan.blocked": "nodim ansiyellow",
                # A running sub-agent's row. Its colour comes from its hue.
                "plan.agent": "nodim",
                # Colour only. A row's style string names its hue class after
                # its status class (`tool_panel.plan_row`), so the hue wins on
                # colour and leaves the status's weight (bold, dim, strike)
                # alone. It lives outside `plan.`: a `plan.*` class would
                # re-apply the base `plan` rule, which is `dim` in the
                # terminal palette, over the status.
                **{f"agent.hue.{index}": hue for index, hue in enumerate(self.agents)},
                "plan.icon.pending": self.muted,
                "plan.icon.in_progress": f"nodim {self.accent} bold",
                "plan.icon.completed": f"nodim bold {self.success}",
                "plan.icon.cancelled": f"{self.muted} dim",
                "plan.icon.blocked": "nodim ansiyellow bold",
                "plan.tree": self.muted,
                "prompt": f"{self.accent} bold",
                # The live area has three weights. Live: the spinner and the
                # phase word, the one thing that says the turn is moving, in
                # the task heading's hue and bold like the footer's running
                # jobs, so the status row never reads as one of the task rows
                # under it, whose in-progress icon takes the accent.
                # Content: what it is doing, in the same hue unbolded, so the
                # border's text keeps one colour whether it holds the name or
                # the turn's detail, and never reads as prose. A sub-agent's
                # line there takes its own hue instead (`activity.agent`).
                # Chrome: counts, clocks, notices, jobs and queues, muted.
                "activity.spinner": self.task_heading,
                "activity.badge": self.accent,
                "activity.phase": f"nodim {self.task_heading} bold",
                "activity.detail": f"nodim {self.task_heading}",
                # Weight only: the colour comes from its `agent.hue.N` class.
                "activity.agent": "nodim",
                "activity.meta": self.muted,
                # The thinking rows of the activity panel: the model's newest
                # thought, faded like scrollback thinking (`pcode.thinking`) and
                # italic so it reads as a quote rather than as the turn's phase
                # (accent) or what it is doing (the task heading's hue). Muted
                # and hue-less, so a thought never reads as either.
                "activity.thinking": f"italic {self.muted}",
                # Keep the reasoning marker upright beside the italic text.
                "activity.thinking.icon": f"noitalic {self.muted}",
                # The tool row under the thought: upright where the thought is
                # italic, and muted whether its call runs or has settled, so it
                # never flips shade; a running call's mark is the status row's
                # spinner in this shade, not its accent. A success's `✓` is the
                # row's own muted shade, never the plan's green: dimmed green
                # still read as a completed task's `✓` beside the task rows, so
                # colour on a check means the plan. A failure is dimmer still,
                # without a hue: plenty of commands are meant to fail (a grep
                # with no match, a probe), so the row notes it without alarm.
                "activity.tool": self.muted,
                "activity.tool.done": self.muted,
                "activity.tool.failed": f"dim {self.muted}",
                # System work is pcode's own: the badge and accent mark it, and
                # its queued rows keep an italic detail.
                "activity.system": self.accent,
                "activity.system.detail": f"italic {self.muted}",
                # Short-lived answers to a keystroke live above the spinner
                # rather than in scrollback; italics mark them as chrome.
                "activity.notice": f"italic {self.muted}",
                # A run's pending group line, shown only when no status row
                # carries its tally. Still live, so muted rather than dimmed
                # like the settled scrollback line it becomes.
                "activity.group": self.muted,
                # A side question runs beside the turn, not as part of it, so its
                # row spins in the muted shade rather than the accent.
                "activity.aside": self.muted,
                # The live preview block, drawn the way scrollback draws a
                # settled one: a heading on the opening line, rules around it.
                "block.rule": self.muted,
                "block.heading": self.accent,
                "frame.border": self.muted,
                # The editor while it replies to a /btw thread
                # (`pcode.btw_view`): a hue of its own, the second
                # sub-agent's, so it is plain at a glance where Enter goes.
                "btw.reply frame.border": f"nodim {self.agents[1]}",
                # The divider over the conversation's newest rows meanwhile:
                # the thread's hue points up to it, and muted text below.
                "btw.divider": f"nodim bold {self.agents[1]}",
                "btw.key": f"nodim {self.agents[1]}",
                "btw.conversation": self.muted,
                # The editor box's session name is the session's own metadata,
                # in the task heading's hue (unbolded) so it never reads as the
                # reply above it, nor as the accent's busy status row. The last
                # turn's mark leads it: a quiet tick, a muted stop for a cancel
                # the user asked for, and red only for a failure.
                "session.name": f"nodim {self.task_heading}",
                "session.done": f"nodim {self.success}",
                "session.cancelled": self.muted,
                "session.unknown": self.muted,
                "session.failed": "nodim ansired bold",
                # The keybinding overlay: keys in the accent so the eye finds
                # them first, its way out muted in the bottom border.
                "hint.key": f"{self.accent} bold",
                "frame.footer": self.muted,
                "hint.message": "yellow",
                "editor.mode": "noreverse nodim bg:#b8b8b8 fg:#ffffff",
                "editor.mode.normal": "bg:#2563eb",
                "editor.mode.visual": "bg:#7c3aed",
                "editor.mode.replace": "bg:#b91c1c",
                # Keep foreground and background paired with the terminal theme:
                # the app palette may still be dark on a light terminal.
                "bottom-toolbar": "noreverse nodim bg:default fg:default",
                "bottom-toolbar.text": "fg:default",
                # Same roles as the prompt chrome: what matters (where, what is
                # running) in the accent, labels muted so the row stays quiet.
                "bottom-toolbar.sep": self.muted,
                "bottom-toolbar.location": f"{self.accent} bold",
                "bottom-toolbar.mode": self.task_heading,
                "bottom-toolbar.hint": self.muted,
                "bottom-toolbar.model": self.accent,
                "bottom-toolbar.context": self.muted,
                "bottom-toolbar.context-value": self.accent,
                "bottom-toolbar.activity": f"{self.task_heading} bold",
                "bottom-toolbar.cache": self.muted,
                "completion-menu": f"bg:{menu.surface} {menu.foreground}",
                "completion-menu.completion": f"bg:{menu.surface} {menu.foreground}",
                # The toolkit's selected-row default uses reverse; explicitly
                # disable it so light themes keep dark text on a light surface.
                "completion-menu.completion.current": f"{highlight} bold",
                "completion-menu scrollbar.background": f"bg:{menu.surface}",
                "completion-menu scrollbar.button": (
                    "reverse bg:default" if menu.selected == "reverse" else f"bg:{menu.selected}"
                ),
                # Full-screen popups keep native body surfaces, but share the
                # completion menu's paired highlight colors, even when a syntax
                # style has a different appearance from the terminal.
                "popup selected": f"{highlight} nodim nounderline",
                "popup cursor-line": f"{highlight} nodim nounderline",
                "popup scrollbar.background": f"noreverse bg:{menu.surface} fg:default",
                "popup scrollbar.button": f"noreverse bg:{menu.accent} fg:default",
                "popup scrollbar.arrow": f"noreverse bg:default {self.accent} bold",
                "completion-menu.meta.completion": f"bg:{menu.surface} {menu.muted}",
                "completion-menu.meta.completion.current": (
                    "reverse bg:default fg:default"
                    if menu.selected == "reverse"
                    else f"bg:{menu.selected} {menu.foreground}"
                ),
                # A file reference is neither prose nor a command: underlining
                # it marks the token without competing with the prompt chevron.
                "reference": f"{self.task_heading} underline",
                "auto-suggestion": self.muted,
                **(ICON_TASK_STYLES if task_style == "icons" else {}),
                **(shaded_styles(*shades) if shades is not None else {}),
            }
        )


PALETTES = {
    "dark": Palette(
        "#88c0d0",
        "#8994a6",
        "#242933",
        "#e5e9f0",
        "#384457",
        "#c4b5fd",
        "#a3be8c",
        ("#c4b5fd", "#f5a97f", "#f5bde6"),
    ),
    "light": Palette(
        "#006b80",
        "#586575",
        "#edf0f4",
        "#202630",
        "#d0e7ef",
        "#7c3aed",
        "#2f7d32",
        ("#7c3aed", "#c2410c", "#be185d"),
    ),
}

# `/syntax terminal`: named ANSI colors, so the prompt, plan rows and popup
# follow the terminal's own scheme. Muted text is `dim` rather than bright
# black, which several schemes (Solarized) paint as the background itself, and
# the selected row is reversed for the same reason. `fg:default` keeps the
# toolkit's own RGB defaults (black popup metadata, grey suggestions) from
# showing through.
# Cyan, green and yellow already mean active, done and blocked, which leaves
# magenta, blue and red for the sub-agents.
TERMINAL_PALETTE = Palette(
    "ansicyan",
    "fg:default dim",
    "default",
    "default",
    "reverse",
    "ansimagenta",
    "ansigreen",
    ("ansimagenta", "ansiblue", "ansired"),
)


@cache
def syntax_palette(style_name: str, fallback: Palette, backdrop: str | None = None) -> Palette:
    """The palette a Pygments style implies, backed by `fallback`'s colors.

    `backdrop` is the background the colors will be painted on when it is not
    the style's own; see `derive_colors`.

    Cached because prompt_toolkit's DynamicStyle invalidates on the identity of
    the object it is handed: a fresh Palette on every redraw would rebuild the
    whole style tree. Palette is frozen, so it is a usable cache key.
    """
    return Palette(**derive_colors(style_name, asdict(fallback), backdrop))


def syntax_themes(preferences: dict[str, str] | None = None) -> dict[str, str]:
    """The saved Pygments style for each palette, by palette name.

    Fenced code keeps its own background, so the style has to be chosen per
    palette rather than derived from one: a dark style on a light terminal is
    readable but jarring, and `theme auto` can pick either at startup.
    """
    preferences = load_preferences() if preferences is None else preferences
    return {
        name: preferences.get(f"syntax_{name}", SETTINGS[f"syntax_{name}"].default)
        for name in PALETTES
    }


# Rich owns scrollback, not the prompt palette. Use terminal-defined ANSI colors
# and leave the background alone so output fits either terminal appearance.
# In particular, Rich's default inline code paints a black background.
TERMINAL_THEME = Theme(
    {
        "pcode.accent": "cyan",
        "pcode.brand": "bold cyan",
        "pcode.muted": "default",
        "pcode.note": "italic dim default",
        "pcode.thinking": "dim default",
        "pcode.error": "bold red",
        "pcode.warning": "bold yellow",
        "pcode.btw": "bold magenta",
        "pcode.btw.badge": "bold reverse magenta",
        "markdown.code": "bold cyan",
        "markdown.code_block": "default",
        "markdown.block_quote": "italic default",
        "markdown.h1": "bold underline",
        "markdown.h2": "bold",
        "markdown.h3": "bold cyan",
        "markdown.h4": "italic cyan",
        "markdown.h5": "italic",
        "markdown.h6": "italic",
        "markdown.h7": "italic",
        "markdown.hr": "default",
        "markdown.link": "underline blue",
        "markdown.link_url": "underline blue",
        "markdown.table.border": "cyan",
        "markdown.table.header": "bold",
    }
)


# Marks rows pcode drives itself. The diamond reads as a system marker rather
# than the "❯" prompt chevron, and the arrows suggest folding history inward.
SYSTEM_BADGE = "◈"
SYSTEM_SEPARATOR = "▸"
# Slash commands pcode runs itself, labelled the same whether queued or running.
SYSTEM_COMMAND_LABELS = {"/compact": "Compacting context"}


def system_command(text: str) -> tuple[str, str] | None:
    """Split a slash command into its badge label and detail, or None if it is a prompt."""
    name, _, detail = plain(text, limit=None).strip().partition(" ")
    label = SYSTEM_COMMAND_LABELS.get(name)
    return (label, detail.strip()) if label else None


# A notice answers a keystroke, so it only has to outlast reading it once.
NOTICE_SECONDS = 5.0
# Scrollback columns a sub-agent's calls sit in from their delegate's row.
CHILD_INDENT = 4
NOTICE_ROWS = 6
# Running side questions likewise; `/btw` has the full list.
ASIDE_ROWS = 3
# A wait on the session host shorter than this never gets a row: most answer
# within a frame or two, and a row that flashes for them reads as a glitch.
WAIT_GRACE_SECONDS = 0.25


# The status row is redrawn several times a second while a turn runs, so a
# longer gap means the row went away; its phase clock starts over.
PHASE_GAP_SECONDS = 1.0
# Least time the status row keeps what it says before saying something else.
# A turn can change phase or tool several times a second, and a row that
# rewrites itself that fast cannot be read. Once the hold is up the row jumps
# to whatever is current, skipping anything that came and went meanwhile.
STATUS_HOLD_SECONDS = 2.5
# Seconds the editor box's activity band keeps rows it no longer fills before
# giving them back (see `PromptLayout.band_rows`).
BAND_HOLD_SECONDS = 2.5
# Status text the row leads with verbatim; anything longer is detail.
PHASE_WORDS = 3
# Cells of detail worth more than the status row's tally and clock.
DETAIL_MIN_CELLS = 16
# Characters of streamed thinking kept for the thinking rows: its latest line.
THINKING_KEEP = 2000
# Default for `thinking_max_lines`: rows the newest thought may wrap to above
# the status row. A thought is a sentence or two, which one row rarely holds.
THOUGHT_ROWS = 10
# Default for `tool_max_lines`: the turn's latest calls listed above the
# status row, so parallel calls each show and a finished one lingers.
TOOL_ROWS = 3
# Default for `tool_linger_seconds`: a finished call's row clears after this,
# so a long wait on the model does not keep showing stale calls.
TOOL_LINGER_SECONDS = 10
# Marks the first thought row in the spinner's column, without resembling
# a stalled frame of the spinners below it.
THOUGHT_ICON = "∴"
# The tool row's stand-ins for common verbs (`tool_display.LABELS`), so it
# says `$ make test` rather than `Run shell · make test`. A tool not listed
# keeps its verb spelled out. Each is one cell wide, ambiguous-width or not.
# ASCII, in every font, so `tool_glyphs off` keeps it.
PLAIN_TOOL_GLYPHS = dict.fromkeys(("Run shell", "Run code", "Start command", "Check command"), "$")
TOOL_GLYPHS = {
    **PLAIN_TOOL_GLYPHS,
    **dict.fromkeys(("Search code", "Find files", "List directory", "Find tools"), "⌕"),
    **dict.fromkeys(("Edit file", "Write file"), "✎"),
    **dict.fromkeys(("Read file", "Read results", "Read job output", "File info"), "⎘"),
    "Wait for job": "⧖",
}


def tool_glyphs_on(setting: str) -> bool:
    """Whether the `tool_glyphs` setting (`auto`, `on`, `off`) draws the symbols.

    Almost no monospace font has `⌕`, `⎘` or `⧖`: the terminal draws
    them from a fallback font, which on macOS and most desktops looks fine
    and on the Linux console is a box, so `auto` leaves them off where
    `TERM` says it is that console (tmux on it says otherwise). A terminal
    cannot say which fonts it has, so anywhere else a missing symbol is the
    user's to turn off.
    """
    if setting == "auto":
        return os.environ.get("TERM") != "linux" and not glyphs.active()
    return setting == "on"


def status_parts(status: str) -> tuple[str, str]:
    """Split free-text status into the row's phase word and its detail.

    Several modules write `Activity.status`; the convention is `Phase · detail…`
    (`Running shell · src…`, `Retrying · Overloaded…`). A short bare status
    is all phase (`Thinking…`); a sentence with no phase becomes detail, so it
    never renders as one long highlighted word.
    """
    text = plain(status, limit=None).strip().removesuffix("…").strip()
    phase, separator, detail = text.partition(" · ")
    if separator:
        return phase, detail.removesuffix("…").strip()
    if not text:
        return "Working", ""
    if len(text.split()) <= PHASE_WORDS:
        return text, ""
    return "Working", text


ASCII_SPINNERS = {"spinner": "line", "tool_spinner": "balloon"}


def status_spinner(setting: str = "spinner") -> Spinner:
    """A spinner for the bottom block, as the `setting` preference names it.

    `spinner` is the status row's (waits on a model), `tool_spinner` running
    tool calls'.
    """
    name = load_preferences().get(setting, SETTINGS[setting].default)
    frames = RICH_SPINNERS.get(name, {}).get("frames", "")
    if glyphs.active() and not "".join(frames).isascii():
        # Stand-ins would freeze it on one character; keep the two kinds of
        # wait distinct with two different ASCII spinners.
        name = ASCII_SPINNERS[setting]
    spinner = Spinner(name)
    # Every frame is a full layout pass (~2-3ms), so the animation loop alone
    # costs a few percent of a core for the length of a turn. Rich's built-in
    # interval is tuned for a dedicated terminal spinner, not for driving
    # pcode's whole bottom block; slow it ~1.6x, which still reads as motion
    # but noticeably cuts render frequency.
    spinner.interval = round(spinner.interval * 1.6)
    return spinner


def clock(seconds: float) -> str:
    """`8s`, `2m05s`: whole seconds, since the row is not a stopwatch."""
    seconds = max(0, int(seconds))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m{seconds % 60:02d}s"


@dataclass
class StatusLine:
    """The status row's parts, in the one order every state uses.

    Left: spinner, optional badge, the phase (the live word, accented), then
    the detail. Right, in a fixed column: the run's tool tally and the phase
    clock, muted. Narrow panes drop the tally, then the clock, then cut the
    detail, and only then the phase. A tool call's command or path never
    rides this row: it gets the tool rows above (`Activity.tool_rows`).
    """

    phase: str
    detail: str = ""
    tally: str = ""
    elapsed: float | None = None
    badge: str = ""
    separator: str = "·"
    # The tool call the line reports, running or just finished; the tool row
    # shows what it acts on.
    call: ToolCall | None = None
    # Whether `call` had finished when the line was taken.
    settled: bool = False
    # The hue slot of the sub-agent the detail describes, if it describes one.
    hue: int | None = None

    def fragments(self, spinner: str, width: int, *, rule: bool = False) -> list[tuple[str, str]]:
        """The row, `width` cells wide when it has meta; `rule` draws the gap as border."""
        if width < 1:
            return []
        head = [("class:activity.spinner", f"{spinner} ")]
        if self.badge:
            head.append(("class:activity.badge", f"{self.badge} "))
        head.append(("class:activity.phase", self.phase))
        needed = sum(cell_len(text) for _, text in head)
        detail = f" {self.separator} {plain(self.detail, limit=None)}" if self.detail else ""
        # The meta column gives way before the detail is cut to a stub.
        wanted = needed + min(cell_len(detail), DETAIL_MIN_CELLS)
        meta = [part for part in (self.tally, self._clock()) if part]
        while meta and wanted + 2 + cell_len(" · ".join(meta)) > width:
            meta.pop(0)
        suffix = " · ".join(meta)
        room = width - (cell_len(suffix) + 2 if suffix else 0)
        # A detail with no room to say anything is dropped, not left as `·…`.
        if detail and room - needed >= DETAIL_MIN_CELLS // 2:
            style = "class:activity.detail"
            if self.hue is not None:
                style = f"class:activity.agent,agent.hue.{self.hue}"
            head.append((style, detail))
        fitted = fit_fragments(head, room)
        if not suffix:
            return fitted
        pad = room - sum(cell_len(text) for _, text in fitted) + 2
        gap = ("class:frame.border", f" {'─' * (pad - 2)} ") if rule else ("", " " * pad)
        return [*fitted, gap, ("class:activity.meta", suffix)]

    def _clock(self) -> str:
        return "" if self.elapsed is None else clock(self.elapsed)


@dataclass
class _HeldStatus:
    """The status line on screen, and what decides how long it may stay."""

    line: StatusLine
    since: float  # When this text first showed.
    taken: float  # When `line`, and so its clock, was computed.
    seen: float  # Last drawn.
    turn: tuple[str, str]  # The prompt it belongs to.
    call: ToolCall | None  # The running tool call it reports, if any.


def _status_text(line: StatusLine) -> tuple:
    """What a status line says, apart from its ticking clock and tally."""
    return (line.badge, line.phase, line.detail, line.call, line.settled)


def _urgent(line: StatusLine) -> bool:
    """News the status row shows at once, whatever it is holding."""
    failed = line.settled and line.call is not None and line.call.failed
    return line.phase.startswith("Retrying") or failed


def call_parts(call: ToolCall) -> tuple[str, str]:
    """A call's verb (`Run shell`) and what it acts on (`ls -la`), if anything."""
    label, _, detail = call.line(timed=False).partition(" · ")
    return label, detail


def _row_key(call: ToolCall) -> tuple:
    """What a tool row says of `call`, so back-to-back lookalikes can share it.

    Its state is part of it: a failure, or a call still running, never hides
    behind a lookalike's mark.
    """
    return (*call_parts(call), call.change, call.failed, call.settled is None)


def head_rows(text: str, width: int, rows: int) -> list[str]:
    """`text` wrapped to `width`, keeping its first `rows`; a cut gets a trailing ellipsis."""
    text = plain(text, limit=None)
    if width < 1 or rows < 1 or not text:
        return []
    console = Console(width=width)
    wrapped = [
        row.plain.rstrip()  # Rich keeps the space a row broke at.
        for row in Text(text).wrap(console, width, overflow="fold", no_wrap=False)
    ]
    if len(wrapped) <= rows:
        return wrapped
    kept = wrapped[:rows]
    # Mark the cut on the last row kept; a full row gives up a cell for it.
    last = Text(kept[-1] + "…")
    last.truncate(width, overflow="ellipsis")
    kept[-1] = last.plain
    return kept


# A summary section's title: `**Tracing the resize path**` or `## Tracing...`.
# One bold run only: `**A** and **B**` is prose with emphasis, not a title.
THOUGHT_HEADING = re.compile(r"\*\*(?P<bold>[^*]+)\*\*|#{1,6}\s+(?P<hash>.+)")


def latest_thought(text: str) -> str:
    """What the thinking row says for one block of streamed thinking.

    Detailed summaries (OpenAI's, and many of Anthropic's) come in titled
    sections; the newest title reads at a glance where the prose under it
    would scroll past. Untitled text (Anthropic's summaries, progress
    updates) shows its newest line.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title, fenced = "", False
    for line in lines:
        if line.startswith("```"):
            fenced = not fenced  # A `# comment` in a code block is not a title.
        elif not fenced and (heading := THOUGHT_HEADING.fullmatch(line)):
            title = (heading["bold"] or heading["hash"]).strip()
    if title:
        return title
    # The buffer keeps the block's tail only (THINKING_KEEP), so a long
    # section can outlive its title; its newest line stands in then.
    return lines[-1].replace("**", "") if lines else ""


# How the last turn ended, at the head of the idle editor box's title.
TURN_MARKS = {"done": "✓", "cancelled": "⊘", "failed": "✗"}
# A settled tool call's mark on the live tool rows: a lighter tick than the
# plan's `✓`, so a finished command never reads as a finished task beside it.
TOOL_DONE_MARK = "√"
# A named session whose last outcome this process never saw (see `session_label`).
UNKNOWN_TURN_MARK = "•"


def fit_fragments(fragments: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    """Cut styled fragments to `width` cells, marking a cut with an ellipsis.

    Newlines and control characters are flattened first, so a pasted command
    cannot break the row. The ellipsis goes on the last fragment kept; a
    spinner or badge that cannot fit whole is cropped without one.
    """
    fragments = [(style, plain(text, limit=None)) for style, text in fragments]
    if sum(cell_len(text) for _, text in fragments) <= width:
        return fragments
    fitted = []
    remaining = max(0, width)
    for style, text in fragments:
        # Strictly less: the fragment that overflows needs a cell for the ellipsis.
        if cell_len(text) < remaining:
            fitted.append((style, text))
            remaining -= cell_len(text)
            continue
        part = Text(text)
        part.truncate(remaining, overflow="ellipsis" if remaining > 1 else "crop")
        if part.plain:
            fitted.append((style, part.plain))
        break
    return fitted


@dataclass(eq=False)
class Wait:
    """Something this terminal is waiting on, and since when."""

    label: str
    started: float = field(default_factory=monotonic)


def _invalidate() -> None:
    """Repaint the running editor, if there is one; state can change outside it."""
    if (app := get_app_or_none()) is not None:
        app.invalidate()


def chrome_rows(text: str, width: int, style: str) -> list[tuple[str, str]]:
    """Wrap transient chrome text to the pane, bounded so it cannot take the screen."""
    if width < 1:
        return []
    console = Console(width=width)
    rows = [
        (style, row.plain)
        for line in text.splitlines()
        for row in Text(plain(line, limit=None)).wrap(
            console, width, overflow="fold", no_wrap=False
        )
    ]
    return rows[:NOTICE_ROWS]


@dataclass
class Activity:
    # The activity panel: the newest thought, the turn's latest tool calls and
    # the task list, shown and hidden together (Ctrl+O). The status row is not
    # part of it: it says the turn is running whatever the panel shows.
    show_tasks: bool = True
    # Hide the widget again as soon as a turn ends, without forgetting that the
    # user wants it shown while the model works.
    autohide_tasks: bool = False
    # Draw the widget as the top section of the editor box instead of its own box.
    attach_tasks: bool = True
    # Draw the thought and tool rows in the editor box, under its status row,
    # instead of above the box (see `PromptLayout.band_rows`).
    attach_activity: bool = True
    # The `task_style` setting: `status` shades task text by status, `icons`
    # gives it one weight, colours only the icon, and pads rows off the frame.
    task_style: str = "status"
    # The `frame_style` setting: `borders` draws box lines, `shaded` paints the
    # boxes in shades of the terminal's background instead (see `frame_shade`).
    frame_style: str = "borders"
    # The `tool_glyphs` setting: `auto`, `on` or `off` (see `tool_glyphs_on`).
    tool_glyphs: str = "auto"
    # The `tool_max_lines` setting: the most tool rows in the activity panel.
    tool_max_rows: int = TOOL_ROWS
    # The `tool_linger_seconds` setting: how long a finished call keeps its
    # tool row; 0 keeps it until newer calls push it out.
    tool_linger_seconds: int = TOOL_LINGER_SECONDS
    # Cap on the task widget plus the editor box: whole rows, or a share of the
    # screen below 1 (0.5 is half). None keeps the default layout.
    tasks_max_height: float | None = None
    tasks_autohidden: bool = False
    # Hide the widget in a pane shorter or narrower than these (0 never hides),
    # so one preference suits a full screen and a split alike. Each terminal
    # measures its own pane: see `fit_screen`.
    tasks_min_rows: int = 0
    tasks_min_columns: int = 0
    screen_small: bool = False
    # Ctrl+O showed the widget in a small pane; dropped when the pane crosses
    # the threshold, so the next split or unsplit decides afresh.
    tasks_unhidden: bool = False
    # Inline shortcut hints, such as the key that hides the task list.
    show_hints: bool = True
    # Where the model's thinking shows: `off`, `status-line` (its own rows
    # in the activity panel), or `scrollback`. See THINKING_MODES.
    thinking_mode: str = "status-line"
    busy: bool = False
    status: str = ""
    queued: int = 0
    queued_prompts: list[str] = field(default_factory=list)
    queued_modes: list[str] = field(default_factory=list)
    prompt: str = ""
    prompt_state: str = ""
    # How the conversation's last model turn ended (`done`, `cancelled`,
    # `failed`), for the editor box's title. Unlike `prompt_state`, a `!command`,
    # `/compact` or slow slash command never touches it, and a switch of
    # conversation clears it.
    turn_outcome: str = ""
    # True while a `!command` typed at the prompt is running.
    user_command: bool = False
    # "user" echoes what was typed; "system" marks work pcode runs on its own
    # behalf (compaction, for example) so it never reads as part of the prompt.
    prompt_kind: str = "user"
    prompt_detail: str = ""
    plan: list[dict] = field(default_factory=list)
    plan_preview: list[dict] | None = None
    tools: ToolHistory = field(default_factory=ToolHistory)
    workers: Workers = field(default_factory=Workers)
    command_outputs: dict[str, CommandOutput] = field(default_factory=dict)
    edit_previews: dict = field(default_factory=dict)
    notice: str = ""
    notice_expires: float = 0.0
    # The footer's note of this turn's latest prompt-cache drop, e.g.
    # `cache miss 0/166k`; the full notice is only in the session journal.
    cache_note: str = ""
    # All running shell jobs, including those a tool is waiting on. The footer
    # shows the count; `/jobs` holds the details. Updated by the job watcher.
    job_count: int = 0
    # A job whose output tail is pinned into the command preview by `/jobs watch`.
    watched_job: str = ""
    # The session's side-question records (`Asides.items`, shared, not copied):
    # the running ones get a spinner row below the prompt's.
    asides: list = field(default_factory=list)
    # The /btw thread drawn in the conversation's place (`pcode.btw_view`),
    # which the editor then sends to; None while the conversation is shown.
    # This terminal's own, never synced from the host.
    btw_thread: str | None = None
    # What this terminal is waiting on (the session host starting, a command
    # it has not finished): its own state, never synced from the host.
    waits: list[Wait] = field(default_factory=list)
    # The tail of the newest thinking block, for the thinking row. Derived
    # from the events this terminal renders, never synced from the host.
    thought: str = ""
    # No thinking block is open: the next delta (or whole block) starts one.
    thought_done: bool = True
    # The status row's phase clock: (phase, since, last drawn). Drawing state,
    # so it is never compared, copied into a repr, or synced from the host.
    _phase: tuple[str, float, float] = field(
        default=("", 0.0, 0.0), init=False, repr=False, compare=False
    )
    # The status line on screen while a hold keeps it. Drawing state like `_phase`.
    _held: _HeldStatus | None = field(default=None, init=False, repr=False, compare=False)
    # The turn's latest call on the tool row, kept there once it finishes.
    _last_call: ToolCall | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def show_thinking(self) -> bool:
        """Whether scrollback carries the model's thinking."""
        return self.thinking_mode == "scrollback"

    def think(self, text: str) -> None:
        """Add streamed thinking; a new block replaces the last one."""
        if not text:
            return
        if self.thought_done:
            self.thought, self.thought_done = "", False
        self.thought = (self.thought + text)[-THINKING_KEEP:]

    def forget_thought(self) -> None:
        """Drop the thinking rows: the answer they led to is streaming now."""
        self.thought, self.thought_done = "", True

    @property
    def thought_shown(self) -> bool:
        """Whether the thinking rows have a thought to show in the activity panel."""
        return (
            self.thinking_mode == "status-line"
            and self.status_shown
            and self.panel_shown
            and bool(latest_thought(self.thought).strip())
        )

    def thought_fragments(
        self, width: int, rows: int = THOUGHT_ROWS, *, hanging: bool = False
    ) -> list[list[tuple[str, str]]]:
        """The thinking rows of the activity panel, in `status-line` mode.

        Held through the tool calls that follow a thought, which it usually
        explains, and gone once the answer streams into scrollback: thinking
        is the lead-up, not a caption on the result.

        `hanging` indents the rows after the first under the thought's text,
        as the editor box draws them: the marker then stands alone in the
        column the status spinner, the task icons and the prompt share, and
        the tool rows indented beneath read as the thought's own calls.
        """
        if not self.thought_shown or width < 3:
            return []
        thought = latest_thought(self.thought)
        if hanging:
            indent = " " * (cell_len(THOUGHT_ICON) + 1)
            return [
                [
                    ("class:activity.thinking.icon", f"{THOUGHT_ICON} " if index == 0 else indent),
                    ("class:activity.thinking", row),
                ]
                for index, row in enumerate(head_rows(thought, width - len(indent), rows))
            ]
        # Wrap the marker with the text so continuation rows use the full
        # width, without a hanging indent. Only the first row has an upright
        # marker; the thought itself stays italic.
        wrapped = head_rows(f"{THOUGHT_ICON} {thought}", width, rows)
        return [
            [
                ("class:activity.thinking.icon", f"{THOUGHT_ICON} " if index == 0 else ""),
                (
                    "class:activity.thinking",
                    row.removeprefix(THOUGHT_ICON).lstrip() if index == 0 else row,
                ),
            ]
            for index, row in enumerate(wrapped)
        ]

    def begin_wait(self, label: str) -> Wait:
        """Start a wait that shows a spinner row once it outlasts the grace period."""
        wait = Wait(label)
        self.waits.append(wait)
        _invalidate()  # Starts the animation timer that draws the row later.
        return wait

    def end_wait(self, wait: Wait) -> None:
        if wait in self.waits:
            self.waits.remove(wait)
            _invalidate()

    @contextmanager
    def waiting(self, label: str):
        wait = self.begin_wait(label)
        try:
            yield wait
        finally:
            self.end_wait(wait)

    def wait_fragments(self, spinner: str, width: int):
        """The newest wait past its grace period, as a system row; empty otherwise.

        A running status row already says what the host is doing (a turn, or a
        command's `◈ label ▸ detail` job), so the terminal's own wait on that
        same work stays out of the way: one spinner at a time.
        """
        if self.status_shown:
            return []
        now = monotonic()
        shown = [wait for wait in self.waits if now - wait.started >= WAIT_GRACE_SECONDS]
        if not shown or width < 1:
            return []
        wait = shown[-1]
        text = Text(f"{spinner} {SYSTEM_BADGE} {plain(wait.label, limit=None)}")
        text.append(f" \u00b7 {now - shown[0].started:.0f}s")
        text.truncate(width, overflow="ellipsis")
        return [("class:activity.system", text.plain)]

    @property
    def asides_running(self) -> bool:
        return any(aside.running for aside in self.asides)

    def aside_rows(self, spinner: str, width: int, budget: int = ASIDE_ROWS):
        """One muted spinner row per running side question, folded to the budget."""
        running = [aside for aside in self.asides if aside.running]
        if budget <= 0 or not running or width < 1:
            return []
        shown = running if len(running) <= budget else running[: max(0, budget - 1)]
        rows = []
        for aside in shown:
            parts = [f"{spinner} btw", plain(aside.question, limit=None)]
            if aside.label:
                parts.insert(1, aside.label)
            if aside.activity:
                parts.append(plain(aside.activity, limit=None))
            parts.append(f"{aside.elapsed:.0f}s")
            text = Text(" \u00b7 ".join(parts))
            text.truncate(width, overflow="ellipsis")
            rows.append(("class:activity.aside", text.plain))
        if len(shown) < len(running):
            rows.append(("class:activity.aside", f"\u2026 {len(running) - len(shown)} more (/btw)"))
        return rows

    def flash(self, text: str, seconds: float = NOTICE_SECONDS) -> None:
        """Replace the transient notice shown above the spinner.

        One slot, not a queue: the newest answer is the one being waited for,
        and stacking acknowledgements would push the editor down the screen.
        """
        self.notice = text
        self.notice_expires = monotonic() + seconds

    @property
    def notice_shown(self) -> bool:
        return bool(self.notice) and monotonic() < self.notice_expires

    def notice_rows(self, width: int) -> list[tuple[str, str]]:
        """Wrap the notice to the pane, bounded so chrome cannot take the screen."""
        if not self.notice_shown:
            return []
        return chrome_rows(self.notice, width, "class:activity.notice")

    def panel_heading(self) -> str:
        return self.panel_title()

    def reset(self) -> None:
        """Clear the panel for a new conversation, keeping the draft and queue."""
        self.command_outputs.clear()
        self.edit_previews.clear()
        self.plan = []
        self.plan_preview = None
        self.tools.clear()
        self.workers = Workers()
        self.prompt = ""
        self.prompt_state = ""
        self.turn_outcome = ""
        self.prompt_kind = "user"
        self.prompt_detail = ""
        self.status = ""
        self._held = None
        self.forget_calls()
        self.thought, self.thought_done = "", True
        self.tasks_autohidden = False

    def forget_calls(self) -> None:
        """Empty the tool rows: a new turn never opens on the last one's calls.

        Its own step, since a terminal attached to a host learns of a new
        turn from `turn_started` alone; `start_prompt` runs only on the host.
        """
        self._last_call = None
        self.tools.turn_calls.clear()

    def height_cap(self, rows: int) -> int | None:
        """The task widget plus editor box's row limit on a screen this tall."""
        cap = self.tasks_max_height
        return None if cap is None else screen_rows(cap, rows)

    def fit_screen(self, columns: int, rows: int) -> None:
        """Note whether this pane is below the widget's size thresholds."""
        small = rows < self.tasks_min_rows or columns < self.tasks_min_columns
        if small != self.screen_small:
            self.screen_small = small
            self.tasks_unhidden = False

    @property
    def panel_shown(self) -> bool:
        """The activity panel is visible: enabled, not auto-hidden after the
        last turn, and the pane fits."""
        return self.show_tasks and not self.tasks_autohidden and not self.tasks_too_big

    @property
    def tasks_too_big(self) -> bool:
        """The pane is below the size thresholds and Ctrl+O has not overridden them."""
        return self.screen_small and not self.tasks_unhidden

    def toggle_tasks(self) -> bool:
        """Ctrl+O acts on what is on screen, so auto-hidden reads as hidden.

        In a small pane it only overrides the size rule for that pane, leaving
        `show_tasks` on so a larger pane still shows the widget.
        """
        shown = self.panel_shown
        self.tasks_autohidden = False
        if self.screen_small and self.show_tasks:
            self.tasks_unhidden = not shown
            return self.show_tasks
        self.show_tasks = not shown
        self.tasks_unhidden = self.show_tasks and self.screen_small
        return self.show_tasks

    def finish_prompt(self, state: str) -> None:
        """End the turn, auto-hiding the widget when that option is enabled."""
        self.prompt_state = state
        if self.autohide_tasks:
            self.tasks_autohidden = True

    def start_prompt(self, text: str, *, kind: str = "user", detail: str = "") -> None:
        """Show a running row, tagged so system work never looks like typed input."""
        self.tasks_autohidden = False
        self.thought, self.thought_done = "", True
        self._held = None  # A new turn never opens on the last one's line.
        self.forget_calls()
        self.prompt = text
        self.prompt_kind = kind
        self.prompt_detail = detail
        self.prompt_state = "running"

    @property
    def displayed_plan(self) -> list[dict]:
        return self.plan if self.plan_preview is None else self.plan_preview

    def plan_rows(self, budget: int, spinner: str = ACTIVE_TASK_ICON):
        """`spinner` marks the step in progress while a turn runs."""
        if not self.panel_shown:
            return []
        # Persisted task status describes unfinished work, not a live request.
        # Use the turn lifecycle rather than busy, which also includes queued input.
        icon = spinner if self.status_shown else "○"
        # A configured height is room the user asked the tasks to fill.
        max_tasks = TASK_ROWS if self.tasks_max_height is None else budget
        return task_panel_rows(self.displayed_plan, self.tools, budget, icon, max_tasks)

    def hidden_step(self) -> str:
        """The active plan step, when the task panel is not showing it.

        Its present-continuous form (`Adding the migration`), else its text:
        `active_form` is optional, and the status row's detail is a gerund.
        Judged by the widget's visibility alone: the row is sized from
        activity state, so a preview that squeezes the plan rows out of a
        short pane does not count as hiding it.
        """
        if self.panel_shown:
            return ""
        step = next((i for i in self.displayed_plan if i.get("status") == "in_progress"), None)
        if step is None:
            return ""
        return step.get("active_form") or step.get("content") or ""

    @property
    def plan_done(self) -> bool:
        items = self.displayed_plan
        return bool(items) and all(item.get("status") == "completed" for item in items)

    def panel_title(self) -> str:
        items = self.displayed_plan
        if not items:
            return "Tools"
        completed = sum(item.get("status") == "completed" for item in items)
        return f"Tasks {completed}/{len(items)}"

    @property
    def status_shown(self) -> bool:
        """The live row exists only while a turn runs; the prompt is in scrollback."""
        return self.prompt_state == "running"

    def _phase_seconds(self, phase: str) -> float:
        """Seconds the row has shown this phase: a stall reads as `Thinking · 40s`.

        Kept by whoever draws the row rather than synced from the host, and
        restarted after a gap in drawing, which only happens between turns.
        """
        now = monotonic()
        shown, since, seen = self._phase
        if shown != phase or now - seen > PHASE_GAP_SECONDS:
            since = now
        self._phase = (phase, since, now)
        return now - since

    def status_line(self, tally: str = "") -> StatusLine:
        """What the status row says, before it is fitted to a width."""
        if self.prompt_kind != "user":
            phase = plain(self.prompt, limit=None)
            return StatusLine(
                phase,
                plain(self.prompt_detail, limit=None),
                badge=SYSTEM_BADGE,
                separator=SYSTEM_SEPARATOR,
                elapsed=self._phase_seconds("\0system" + phase),
            )
        phase, detail = status_parts(self.status)
        call = self.tools.active
        running = self.tools.running
        if call is not None and call.settled is None:
            # A tool's label is already a verb (`Run shell`, `Read file`), so
            # one call is its own phase; parallel calls are counted instead.
            # The clock is the call's own, whatever the model said last.
            self._phase_seconds("")
            phase = call_parts(call)[0] if running < 2 else f"Running {running} tools"
            return StatusLine(
                phase, self.hidden_step(), tally=tally, elapsed=call.elapsed, call=call
            )
        clock, hue = phase, None
        if agents := self.tools.delegates:
            # The panel lists the agents themselves, so this row just says
            # the turn is waiting on them, unless the panel is hidden. One
            # clock for the whole wait, however the count changes.
            count = len(agents)
            phase = f"Waiting for {count} agent{'s' * (count > 1)}"
            if not self.panel_shown:
                detail, hue = agents[-1].line(timed=False), agents[-1].hue
            else:
                detail = ""
            clock = "\0sub-agents"
        elif phase.startswith("Running") and not running and not self.user_command:
            # Written for a call that has since finished; the model has the turn.
            phase = clock = "Waiting for model"
            detail = ""
        # With the panel hidden, the active step says what the turn is for.
        detail = detail or self.hidden_step()
        line = StatusLine(phase, detail, tally=tally, elapsed=self._phase_seconds(clock), hue=hue)
        if call is not None:
            # Just finished: held briefly and marked done, so a burst of fast
            # calls reads as progress rather than strobing.
            line.call, line.settled = call, True
        return line

    def tool_rows(
        self, line: StatusLine, width: int, limit: int | None = None, spinner: str = "·"
    ) -> list[list[tuple[str, str]]]:
        """The tool rows between the thought and the status row: `◜ $ ls -la`.

        `spinner` marks a running call: the tool spinner's current frame.

        The turn's latest calls, oldest first, at most `tool_max_rows` (and
        `limit`, the pane's share). Parallel calls each get a row, and a
        finished call keeps its row, marked `✓` or `✗`, until newer calls
        push it out or `tool_linger_seconds` passes, so the editor box does
        not jump with every call yet a long wait on the model does not show
        stale ones. The call the status row's verb acts on always keeps its
        row, so at one row this is that call alone. Back-to-back calls that
        would draw the same row (a retried search, a run of edits with the
        same counts) share one, counted `×2`.
        """
        # Work pcode runs itself (`prompt_kind` system) has no tool rows.
        if not (self.status_shown and self.panel_shown) or self.prompt_kind != "user" or width < 1:
            return []
        current = line.call or self._last_call
        # A sub-agent's call is not among them: it shows only while the status
        # row reports it (appended below).
        groups: list[list[ToolCall]] = []
        for call in self.tools.turn_calls:
            if groups and _row_key(groups[-1][-1]) == _row_key(call):
                groups[-1].append(call)
            else:
                groups.append([call])
        if linger := self.tool_linger_seconds:
            cutoff = monotonic() - linger
            groups = [
                group
                for group in groups
                if any(
                    call is line.call or call.settled is None or call.settled > cutoff
                    for call in group
                )
            ]
            # `_last_call` lingers like any other; the status row's own call stays.
            if current is not line.call and current is not None and current.settled is not None:
                current = current if current.settled > cutoff else None

        def has_current(group: list[ToolCall]) -> bool:
            return any(call is current for call in group)

        count = max(1, self.tool_max_rows if limit is None else min(self.tool_max_rows, limit))
        room = count - (current is not None)
        others = [group for group in groups if not has_current(group)]
        kept = others[-room:] if room else []
        shown = [g for g in groups if has_current(g) or any(g is k for k in kept)]
        if current is not None and not any(has_current(group) for group in shown):
            shown.append([current])  # A sub-agent's call, newest of all.
        return [
            self._tool_row(
                current if has_current(group) else group[-1], line, width, spinner, len(group)
            )
            for group in shown
        ]

    def _tool_row(
        self, call: ToolCall, line: StatusLine, width: int, spinner: str, repeats: int = 1
    ) -> list[tuple[str, str]]:
        """One call's row: its state mark, then its verb's glyph (`◜ $ ls -la`).

        Every row has the same columns, flush with the thought's `∴` or with
        none, so parallel calls line up and nothing shifts as a thought comes
        and goes: the mark (the tool spinner's frame in the muted shade while
        it runs, `✓` or `✗` once settled), then the glyph (`TOOL_GLYPHS`). A
        verb with no glyph is spelled out whenever the status row does not
        say it for this call.
        """
        label, detail = call_parts(call)
        glyph = (TOOL_GLYPHS if tool_glyphs_on(self.tool_glyphs) else PLAIN_TOOL_GLYPHS).get(label)
        if glyph:
            text = detail or label
        elif detail and call is line.call and line.phase == label:
            text = detail
        else:
            text = " · ".join(filter(None, (label, detail)))
        # What tells lookalike rows apart, kept whole when a long path is cut.
        tail = "".join(
            filter(None, (call.change and f" · {call.change}", repeats > 1 and f" ×{repeats}"))
        )
        if call.settled is None:
            mark = ("class:activity.tool", spinner)
        elif call.failed:
            mark = ("class:activity.tool.failed", "✗")
        else:
            mark = ("class:activity.tool.done", TOOL_DONE_MARK)
        # A wide spinner (`dots12`) would push running rows' text past
        # settled ones'; pad the settled mark's column to its width.
        pad = "" if call.settled is None else " " * max(0, cell_len(spinner) - 1)
        body = ("class:activity.tool", pad + " " + " ".join(filter(None, (glyph, text))))
        if not tail:
            return fit_fragments([mark, body], width)
        if cell_len(tail) >= width // 2:
            return fit_fragments([mark, body, ("class:activity.tool", tail)], width)
        return [*fit_fragments([mark, body], width - cell_len(tail)), ("class:activity.tool", tail)]

    def held_status_line(self, tally: str = "", hold: float = 0.0) -> StatusLine:
        """The status line, keeping what the row said for at least `hold` seconds.

        Only a change of text counts: the clock and tally keep moving on a
        held line. The hold gives way at once to a new turn, a gap in drawing,
        a retry or failure, and a held tool call that has finished, so it only
        ever skips churn, never news.
        """
        line = self._held_line(tally, hold)
        # The tool row keeps the last call this row showed, so the two agree.
        if line.call is not None:
            self._last_call = line.call
        return line

    def _held_line(self, tally: str, hold: float) -> StatusLine:
        line = self.status_line(tally)
        if hold <= 0:
            return line
        now = monotonic()
        turn = (self.prompt_kind, self.prompt)
        active = self.tools.active
        call = active if active is not None and active.settled is None else None
        shown = self._held
        same = shown is not None and _status_text(line) == _status_text(shown.line)
        fresh = (
            shown is None
            or shown.turn != turn
            or now - shown.seen > PHASE_GAP_SECONDS
            or (shown.call is not None and shown.call.settled is not None)
        )
        if fresh or same or now - shown.since >= hold or _urgent(line):
            since = shown.since if same and not fresh else now
            self._held = _HeldStatus(line, since, now, now, turn, call)
            return line
        shown.seen = now
        held = shown.line
        elapsed = None if held.elapsed is None else held.elapsed + now - shown.taken
        return replace(held, tally=line.tally, elapsed=elapsed)

    def status_fragments(
        self, spinner: str, width: int, tally: str = "", hold: float = 0.0, *, rule: bool = False
    ):
        """The editor's top border: `⠋ Phase · detail ── ✓ 7 tools · 12s`."""
        return self.held_status_line(tally, hold).fragments(spinner, width, rule=rule)

    def queue_rows(self, budget: int):
        """Show the next queued prompts, leaving room for the editor on short panes."""
        if budget <= 0:
            return []
        visible = budget if len(self.queued_prompts) <= budget else budget - 1
        rows = []
        for index, text in enumerate(self.queued_prompts[:visible]):
            mode = self.queued_modes[index] if index < len(self.queued_modes) else "queue"
            prefix = {
                "steering": "Steering",
                "interrupt": "Interrupting",
            }.get(mode, "Queued")
            system = system_command(text)
            if system is None:
                rows.append(("class:plan", f"{prefix}: {text}"))
                continue
            # Pending system work reads as a labelled action, matching the row
            # it becomes once it starts, rather than as an echoed command.
            label, detail = system
            body = f"{SYSTEM_BADGE} {label}"
            if detail:
                body += f" {SYSTEM_SEPARATOR} {detail}"
            rows.append(("class:activity.system.detail", f"{prefix} {body}"))
        remaining = len(self.queued_prompts) - visible
        if remaining > 0:
            state = "pending" if "steering" in self.queued_modes else "queued"
            rows.append(("class:plan", f"… {remaining} more {state}"))
        return rows


def command_heading(activity: Activity, event: CommandOutput) -> str:
    """Name a running command the way scrollback will name it once it settles.

    Same marker, title and elapsed layout as `CommandTranscript`; only the
    marker differs, because nothing has finished yet. A watched job and a
    `!command` typed at the prompt have no live tool call to name them.
    """
    call = next((c for c in activity.tools.calls if c.event.call_id == event.call_id), None)
    if call is not None:
        purpose = plain(call.event.purpose, limit=60) if call.event.purpose else ""
        name = label(call.event.name)
        return block_heading(
            RUNNING, f"{name} · {purpose}" if purpose else name, monotonic() - call.started
        )
    if event.call_id.startswith(WATCHED_PREFIX):
        return block_heading(RUNNING, f"Job {event.call_id[len(WATCHED_PREFIX) :]}")
    return block_heading(RUNNING, "Shell")


@Output.register
class CursorSafeOutput:
    """Keep renderer erases out of history and hide the cursor during handoffs."""

    def __init__(self, output: Output):
        self.output = keyboard_output(output)
        self._hidden = 0
        self._visible = True

    def __getattr__(self, name):
        return getattr(self.output, name)

    def erase_down(self) -> None:
        # Avoid ED at column zero: terminals such as tmux preserve a full-screen
        # erase in scrollback, including the supposedly transient editor frame.
        # prompt_toolkit calls this at column zero. Split the erase into ED from
        # column one plus EL from column zero, keeping the final cursor unchanged.
        if self.output.get_size().columns < 2:
            self.output.erase_down()
            return
        self.output.cursor_forward(1)
        self.output.erase_down()
        self.output.cursor_backward(1)
        self.output.erase_end_of_line()

    def show_cursor(self) -> None:
        self._visible = True
        if not self._hidden:
            self.output.show_cursor()

    def hide_cursor(self) -> None:
        self._visible = False
        self.output.hide_cursor()

    @contextmanager
    def hidden_cursor(self):
        self._hidden += 1
        self.output.hide_cursor()
        self.output.flush()
        try:
            yield
        finally:
            self._hidden -= 1
            if not self._hidden:
                if self._visible:
                    self.output.show_cursor()
                self.output.flush()


# DEC private mode 2026: the terminal holds the frame between these, so the
# editor's erase, the transcript write, and the repaint appear as one change.
# Terminals without it ignore both sequences.
SYNC_START = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"


class Handoff:
    """What the body of an atomic ``suspended_editor`` reports for the repaint.

    ``top_row`` is the 1-based terminal row the erase left the cursor on; the
    body overrides it when it homes the cursor itself. ``rows_written`` is how
    many rows the body's output advanced the cursor; leaving it ``None`` falls
    back to asking the terminal where the cursor ended up.
    """

    def __init__(self, top_row: int | None):
        self.top_row = top_row
        self.rows_written: int | None = None


def layout_top_row(app: Application) -> int | None:
    """The terminal row the editor starts on, if the renderer still knows it."""
    renderer = app.renderer
    if (
        renderer._in_alternate_screen
        or renderer._min_available_height <= 0
        or renderer._last_size != app.output.get_size()
    ):
        return None
    return renderer.rows_above_layout + 1


@asynccontextmanager
async def suspended_editor(app: Application, *, atomic: bool = False):
    """Hand the terminal to direct output, then repaint the editor exactly once.

    With ``atomic`` the handoff is wrapped in synchronized output and, when the
    body reports how many rows it wrote (``Handoff.rows_written``), the editor
    is repainted immediately from the computed cursor row instead of after a
    cursor position report. The whole erase, write, and repaint then reach the
    terminal as one frame with no await in between, so the editor and live
    panel never visibly disappear. A report is still requested and lands
    before the next handoff; the renderer's cursor bookkeeping is relative, so
    a miscount (Rich and the terminal disagreeing on a glyph's width) only
    misjudges the free rows below the editor until that reply corrects it,
    exactly as prompt_toolkit tolerates a layout taller than its report.
    Popups and external programs must not use ``atomic``: a frame held across
    them would freeze the terminal until its guard timeout.

    An atomic handoff also keeps raw mode and extended keyboard reporting,
    including while waiting for a cursor report. Only an external program
    needs cooked mode, and in cooked mode the tty echoes anything typed
    during the handoff and turns a Return into a newline, which the editor then
    reads as Ctrl+J. Paced scrollback makes handoffs frequent while the user
    may well be typing, so the window has to be harmless.

    This mirrors prompt_toolkit's ``in_terminal``, except for when the editor
    is painted again. ``in_terminal`` repaints immediately after the handoff,
    before the cursor position report it has just requested arrives, so that
    paint knows nothing about the space below the cursor and lands the editor
    at its preferred height, directly under the new output. The report then
    re-renders the layout across the remaining screen ~1/30 s later (bounded
    by ``min_redraw_interval``), which moves the editor back to the bottom.
    Whenever the transcript leaves rows free below it (startup, the first tool
    calls of a session) every write makes the editor visibly jump up and back.
    Waiting for the report first paints the editor at its final position.

    To check for a regression, do not trust ``tmux capture-pane``: it shows only
    the settled frame and cannot see two paints inside one redraw interval.
    Record the raw byte stream with timestamps instead
    (``tmux pipe-pane -o 'python3 stamp.py >> out.bin'``) and look for a second
    editor paint after a scrollback write.
    """
    # Offline harnesses pass a bare stand-in for the app; nothing to suspend.
    if not isinstance(app, Application) or not app._is_running:
        yield Handoff(None)
        return
    # Chain to any handoff already in progress, as in_terminal does.
    previous = app._running_in_terminal_f
    done: Future[None] = Future()
    app._running_in_terminal_f = done
    try:
        if previous is not None:
            await previous
        if app.output.responds_to_cpr:
            # Also collects the report an atomic exit left outstanding, before
            # cooked mode could echo it onto the screen as text.
            await app.renderer.wait_for_cpr_responses()
        preserve = getattr(app.output, "preserve_keyboard", nullcontext)
        with preserve() if atomic else nullcontext():
            handoff = Handoff(layout_top_row(app) if atomic else None)
            if atomic:
                app.output.write_raw(SYNC_START)  # erase() flushes it
            app.renderer.erase()
            app._running_in_terminal = True
            # A popup takes over SIGWINCH, but the editor's ``_poll_output_size``
            # task keeps calling ``_on_resize`` on any size change. That erases
            # from the cursor down (over the popup) and requests a CPR the popup
            # then reads as input. Ignore resizes until the handoff repaints below.
            app._on_resize = lambda: None
            try:
                with app.input.detach(), nullcontext() if atomic else app.input.cooked_mode():
                    yield handoff
            finally:
                del app._on_resize
                app.renderer.reset()
                if handoff.top_row is not None and handoff.rows_written is not None:
                    rows = app.output.get_size().rows
                    row = min(handoff.top_row + handoff.rows_written, rows)
                    app.renderer._min_available_height = rows - row + 1
                    # Sent with the cursor still on the editor's top row, as the
                    # renderer requires; the reply only refines the guess above.
                    app._request_absolute_cursor_position()
                    app._running_in_terminal = False
                    app._redraw()
                    app.output.write_raw(SYNC_END)
                    app.output.flush()
                else:
                    if atomic:
                        # Nothing to hold the frame for across the report round trip.
                        app.output.write_raw(SYNC_END)
                        app.output.flush()
                    app._request_absolute_cursor_position()
                    try:
                        # Input is attached again, so the report can be read here.
                        # Keep modified keys encoded while this wait delays repaint.
                        # Rendering stays disabled meanwhile: an invalidation from a
                        # keystroke or the spinner would otherwise paint the early frame.
                        if app.output.responds_to_cpr:
                            await app.renderer.wait_for_cpr_responses()
                    finally:
                        app._running_in_terminal = False
                        app._redraw()
    finally:
        if not done.done():
            done.set_result(None)


class ReflowAwareRenderer(Renderer):
    """Erase every physical row tmux produced from the last layout on narrowing.

    prompt_toolkit paints full-width rows with autowrap disabled, so tmux does
    not flag them as wrapped. On a narrowing resize tmux still splits any row
    whose used cells exceed the new width, before the app sees SIGWINCH, and
    keeps the cursor inside its split row. The stock erase then moves up by the
    old logical row count and leaves the top of the old layout on screen. Count
    the split rows instead. Partial clears never shrink tmux's used-cell count;
    only the column-zero full erase does, so track the widest write per row
    since the last erase.
    """

    def __init__(self, *args, **kwargs) -> None:
        self._row_extents: dict[int, int] = {}
        super().__init__(*args, **kwargs)

    def reset(self, _scroll: bool = False, leave_alternate_screen: bool = True) -> None:
        self._row_extents = {}
        super().reset(_scroll, leave_alternate_screen)

    def render(self, app, layout, is_done: bool = False) -> None:
        super().render(app, layout, is_done)
        screen, size, has_style = self._last_screen, self._last_size, self._style_string_has_style
        if screen is None or size is None or has_style is None:
            return
        for y in range(screen.height):
            extent = 0
            for x, cell in screen.data_buffer[y].items():
                if cell.char != " " or has_style[cell.style]:
                    extent = max(extent, min(x, size.columns - 1) + (cell.width or 1))
            if extent > self._row_extents.get(y, 0):
                self._row_extents[y] = extent

    def _reflowed_rows_above_cursor(self) -> int | None:
        """Physical rows above the cursor after tmux narrowed the pane, else None."""
        if not os.environ.get("TMUX") or self._last_size is None or self._in_alternate_screen:
            return None
        columns = self.output.get_size().columns
        if columns < 1 or columns >= self._last_size.columns:
            return None
        cursor = self._cursor_pos
        rows = 0
        for y in range(cursor.y):
            rows += max(1, -(-self._row_extents.get(y, 0) // columns))
        extent = self._row_extents.get(cursor.y, 0)
        if extent > columns:
            # tmux keeps the cursor in its split piece, or after the last one.
            rows += cursor.x // columns if cursor.x < extent else (extent - 1) // columns
        return rows

    def erase(self, leave_alternate_screen: bool = True) -> None:
        rows = self._reflowed_rows_above_cursor()
        if rows is None:
            super().erase(leave_alternate_screen)
            return
        output = self.output
        output.cursor_backward(self._cursor_pos.x)
        output.cursor_up(rows)
        output.erase_down()
        output.reset_attributes()
        output.enable_autowrap()
        output.flush()
        self.reset(leave_alternate_screen=leave_alternate_screen)


def editor_mode_label(app: Application) -> str:
    """Read vi state at render time, without showing a badge for Emacs editing."""
    if app.editing_mode != EditingMode.VI:
        return ""
    if app.current_buffer.selection_state:
        return " VISUAL "
    return {
        InputMode.INSERT: " INSERT ",
        InputMode.INSERT_MULTIPLE: " INSERT ",
        InputMode.NAVIGATION: " NORMAL ",
        InputMode.REPLACE: " REPLACE ",
        InputMode.REPLACE_SINGLE: " REPLACE ",
    }[app.vi_state.input_mode]


def editor_mode_badge(app: Application) -> list[tuple[str, str]]:
    """Keep the badge's text and color tied to the same live editor state."""
    label = editor_mode_label(app)
    if not label:
        return []
    return [(f"class:editor.mode.{label.strip().lower()}", label)]


def install_reflow_renderer(app: Application) -> None:
    """Install the reflow-aware erase; only needed without resize regeneration.

    Regeneration clears the screen and scrollback before replaying, so the
    careful physical-row accounting is wasted work when it is enabled.
    """
    app.renderer = ReflowAwareRenderer(
        app._merged_style,
        app.output,
        full_screen=False,
        mouse_support=False,
        cpr_not_supported_callback=app.cpr_not_supported_callback,
    )


# Frames a paced backlog takes to drain, so a settled block rolls out row by
# row when small and lands within about a second however big it is.
PACED_DRAIN_FRAMES = 30
# Typed prose advances every TYPED_STEP_FRAMES frames (every frame: 30 a
# second). A step reveals TYPED_CHARS_PER_STEP characters (about 360 a second,
# near a model's own pace, so a steady stream reads as one), sped up so a
# backlog types out within TYPED_DRAIN_STEPS steps (about two seconds).
TYPED_STEP_FRAMES = 1
TYPED_CHARS_PER_STEP = 12
TYPED_DRAIN_STEPS = 60

# Rich writes only SGR styles and OSC 8 hyperlinks. prompt_toolkit's ANSI parser
# knows SGR but would print an OSC's payload as text, so the live row drops them.
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ESCAPES = re.compile(OSC.pattern + r"|\x1b\[[0-?]*[ -/]*[@-~]")
# Blocks whose rows only make sense whole: typing them out character by
# character would show a half-drawn rule, table border, or code line.
UNTYPED_TOKENS = frozenset({"fence", "code_block", "table_open", "html_block", "hr"})


def typed_prose(objects: tuple) -> bool:
    """Whether a write is prose that reads well typed out rather than rolled in."""
    if not objects:
        return False
    for obj in objects:
        if isinstance(obj, Markdown):
            tokens = obj.parsed
        elif isinstance(obj, ThinkingMarkdown):
            tokens = Markdown(obj.source).parsed
        else:
            return False
        if any(token.type in UNTYPED_TOKENS for token in tokens):
            return False
    return True


@dataclass
class QueuedRow:
    """One rendered row waiting for scrollback.

    ``visible`` counts what a reader sees, without escapes or the trailing
    padding Rich adds, so a blank row costs nothing to reveal. ``lead`` is its
    leading indentation, shown at once so a centred heading or indented list
    item does not spend frames typing spaces. Both are computed on first use:
    a replay queues every row but writes them all without asking.
    """

    text: str
    typed: bool = False

    @cached_property
    def _plain(self) -> str:
        return ESCAPES.sub("", self.text).rstrip()

    @property
    def visible(self) -> int:
        return len(self._plain)

    @property
    def lead(self) -> int:
        return len(self._plain) - len(self._plain.lstrip())


def split_rows(text: str) -> list[str]:
    """Split rendered output into rows, each keeping its newline.

    Only ``\\n`` ends a row; ``str.splitlines`` would also split on control
    characters that escape sequences never contain but text could.
    """
    rows = text.split("\n")
    last = rows.pop()
    result = [row + "\n" for row in rows]
    if last:
        result.append(last)
    return result


class RowCounter:
    """Tee for the Rich console's file that counts the rows a batch advances.

    Rich wraps every line to the width it is given and ends it with a newline,
    so newlines are terminal rows as long as Rich and the terminal agree on
    cell widths; see ``suspended_editor`` for what a disagreement costs.
    """

    def __init__(self, file):
        self.file = file
        self.rows = 0

    def write(self, text: str) -> int:
        self.rows += text.count("\n")
        return self.file.write(text)

    def __getattr__(self, name):
        return getattr(self.file, name)


def settled_lines(source: str) -> int:
    """How many of ``source``'s leading lines are Markdown blocks more text cannot change.

    What streams in after them can only start a new block, so they can be
    written to scrollback now and the rest held until it settles too.
    """
    lines = source.splitlines(keepends=True)
    blocks = [token for token in Markdown(source).parsed if token.level == 0 and token.map]
    if not blocks:
        return 0
    last = blocks[-1]
    # Retain the last container: blank lines can belong to lists, quotes,
    # indented code, or fenced code. A following top-level block settles it.
    end = last.map[0]
    if lines[-1].strip() == "" and last.type in {
        "paragraph_open",
        "heading_open",
        "table_open",
        "hr",
    }:
        end = len(lines)
    elif last.type == "fence":
        closing = lines[last.map[1] - 1].rstrip("\r\n")
        if last.map[1] - last.map[0] > 1 and re.fullmatch(
            r" {0,3}" + re.escape(last.markup[0]) + "{" + str(len(last.markup)) + r",} *",
            closing,
        ):
            end = last.map[1]
    return end


class TerminalOutput:
    """Commit completed Markdown blocks once; buffer unfinished text.

    All writes run through one batched terminal handoff. Never hold the handoff
    across a network await: the editor must keep receiving input while streaming.
    """

    def __init__(
        self,
        console: Console,
        app: Application,
        *,
        code_theme=None,
        rich_theme=None,
    ):
        self.console = console
        self.app = app
        self.tail = ""
        self.streamed = False
        self.code_theme = code_theme or (lambda: "ansi_dark")
        self.rich_theme = rich_theme or PALETTES["dark"].rich_theme
        self.pending: list[tuple[tuple[object, ...], str, bool]] = []
        self.transient_pending: list[tuple[tuple[object, ...], str, bool]] = []
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.commit_user = self.user
        self.commit_message = self.message
        self.commit_thinking = lambda text: self.print(
            ThinkingMarkdown(text, code_theme=self.code_theme(), style="dim"), end=""
        )
        self._thinking_tail = ""
        self._thinking_streamed = False
        self._regenerate = None
        self.resize_replay = None
        # Another view drawn in the conversation's place, e.g. a /btw thread
        # (`pcode.btw_view`): the replay that draws it. While set, the
        # conversation's writes wait in ``pending`` (its log keeps them, so the
        # replay that closes the view draws them), every rebuild redraws the
        # view, and ``view_print`` writes below it.
        self.view = None
        self.view_pending: list[tuple[tuple, str, bool]] = []
        # The conversation's notes that were queued as rows when a view took the
        # screen: notes are not in its log, so they come back with it.
        self._held_notes: list[tuple[tuple, str, bool]] = []
        # `held_tail`'s rows for the held writes, kept until another arrives:
        # ``_writes`` counts every conversation write.
        self._tail_cache: tuple | None = None
        self._writes = 0
        # Rendered rows not yet written. Pacing rolls a settled block out over
        # successive frames instead of landing it in one; off writes them all.
        self.rows: list[QueuedRow] = []
        self.paced = False
        # Typing reveals prose rows a few characters per frame in the live area
        # and writes each to scrollback once it is complete. Needs ``paced``.
        self.typed = False
        # Characters of ``rows[0]`` already shown in the live area.
        self._typed = 0
        self._preview: tuple[tuple[str, int], StyleAndTextTuples] | None = None
        # Frames skipped since the last typing step; see TYPED_STEP_FRAMES.
        self._skipped = 0
        # Rows and characters per step for the current backlog. Fixed when rows
        # arrive rather than recomputed as they leave, or the tail would crawl.
        self._reveal_rate = 0
        self._type_rate = 0
        # Transient writes already rendered into ``rows`` but not yet written,
        # with the absolute index just past their last row. A replay drops the
        # queued rows, so these must join it, just as ``transient_pending`` does.
        self._queued_transient: list[tuple[tuple, int]] = []
        # Absolute index of ``rows[0]``: rows written or dropped so far.
        self._row_base = 0

    def regenerate(self, replay) -> None:
        # Coalesce requests. Snapshot only inside the handoff so arrivals during
        # the CPR await are included exactly once, including queued writes.
        self._regenerate = replay
        self.changed.set()

    def open_view(self, replay) -> None:
        """Draw ``replay``'s rows in the conversation's place, or redraw the view shown."""
        self.view = replay
        self.view_pending = []
        self._tail_cache = None
        self.regenerate(replay)

    def view_print(self, entries: list[tuple[tuple, str, bool]]) -> None:
        """Write below the open view; nothing while the conversation is shown."""
        if self.view is not None and entries:
            self.view_pending.extend(entries)
            self.changed.set()

    def close_view(self, replay) -> None:
        """Give the screen back to the conversation, rebuilt by ``replay``."""
        if self.view is None:
            return
        self.view = None
        self.view_pending = []
        self._tail_cache = None
        self.regenerate(replay)

    def _queue(self) -> list:
        """The writes waiting for the screen: the open view's, else the conversation's."""
        return self.view_pending if self.view is not None else self.pending

    def print(self, *objects, end="\n", transient=False) -> None:
        entry = (objects, end, False)
        self.pending.append(entry)
        self._writes += 1
        if transient:
            self.transient_pending.append(entry)
        self.changed.set()
        if self.view is not None and (invalidate := getattr(self.app, "invalidate", None)):
            # Shown under the view (`held_tail`), even with no turn to redraw it.
            invalidate()

    @property
    def view_drawn(self) -> bool:
        """Whether a view is on screen: opened, and its rebuild already written."""
        return self.view is not None and self._regenerate is None

    def begin_turn(self, prompt: str) -> None:
        # The live row shows the running tool, not the prompt, so the quote goes
        # to scrollback as soon as the turn starts rather than waiting for output.
        self.commit_user(prompt)

    def user(self, prompt: str) -> None:
        self.print()
        self.print(TaskPrompt(prompt))
        self.print()

    def message(self, source: str) -> None:
        self.print(Markdown(safe_text(source), code_theme=self.code_theme()))
        self.print()

    def end_turn(self) -> None:
        self.finish_thinking()
        self.finish()

    def _commit(self, source: str) -> None:
        if source.strip():
            self.commit_message(source)

    def _commit_thinking(self, source: str) -> None:
        if source.strip():
            self.commit_thinking(source)

    def thinking_delta(self, text: str) -> None:
        """Buffer incomplete Markdown containers, just like the public answer."""
        if not text:
            return
        self._thinking_streamed = True
        for part in text.splitlines(keepends=True):
            self._thinking_tail += part
            if part.endswith("\n"):
                self._commit_blocks(thinking=True)

    def finish_thinking(self, fallback: str = "") -> None:
        if not self._thinking_streamed and fallback:
            self.thinking_delta(fallback)
        if self._thinking_streamed:
            self._commit_thinking(self._thinking_tail.rstrip("\n") + "\n\n")
        self._thinking_tail = ""
        self._thinking_streamed = False

    def delta(self, text: str) -> None:
        if not text:
            return
        self.streamed = True
        text = safe_text(text)
        # Examine boundaries independently of provider chunk sizes. Do not parse
        # every token: a newline can complete a block, a partial line cannot.
        for part in text.splitlines(keepends=True):
            self.tail += part
            if part.endswith("\n"):
                self._commit_blocks()
        self.changed.set()

    def _commit_blocks(self, *, thinking: bool = False) -> None:
        source = self._thinking_tail if thinking else self.tail
        lines = source.splitlines(keepends=True)
        end = settled_lines(source)
        if end:
            if thinking:
                self._commit_thinking("".join(lines[:end]))
                self._thinking_tail = "".join(lines[end:])
            else:
                self._commit("".join(lines[:end]))
                self.tail = "".join(lines[end:])

    def finish(self, fallback: str = "") -> None:
        # Message is a completion marker, not a second copy of streamed text.
        if not self.streamed and fallback:
            self.delta(fallback)
        if self.streamed:
            self._commit(self.tail)
        self.tail = ""
        self.streamed = False
        self.changed.set()

    def _enqueue(self, pending: list, width: int, *, replay: bool = False) -> None:
        """Render writes into rows, marking the prose ones to type out.

        A row is a self-contained unit (Rich closes styles and links per
        segment), so any prefix of the queue can be written now and the rest on
        later frames without splitting an escape. Each write renders on its own
        so its rows know whether they came from prose. A replay is written whole
        at once, so it skips both the prose check and transient tracking.
        """
        if self.view is not None:
            # The view's writes are never notes; the conversation's stay held.
            transient = []
        else:
            transient = [] if replay else self.transient_pending
            self.transient_pending = []
        pieces = [
            (entry, piece, self.typed and not replay and typed_prose(entry[0]))
            for entry, piece in zip(pending, self._render(pending, width), strict=True)
        ]
        # A write can end mid-row and the next complete it; the row is typed
        # only if every write it holds is.
        text, typed = "", True
        for entry, piece, piece_typed in pieces:
            for part in split_rows(piece):
                text, typed = text + part, typed and piece_typed
                if text.endswith("\n"):
                    self.rows.append(QueuedRow(text, typed))
                    text, typed = "", True
            if any(entry is note for note in transient):
                # Its last character is in the open row, or the one just closed.
                last = len(self.rows) + (1 if text else 0)
                self._queued_transient.append((entry, self._row_base + last))
        if text:
            self.rows.append(QueuedRow(text, typed))

    def _render(self, entries: list, width: int) -> list[str]:
        """Each write rendered on its own, as terminal text at ``width``."""
        prefetch_edits((obj for objects, _, _ in entries for obj in objects), width)
        pieces = []
        previous_file = self.console._file
        try:
            with self.console.use_theme(self.rich_theme()):
                for objects, end, soft_wrap in entries:
                    self.console.file = rendered = StringIO()
                    with self.console:
                        self.console.print(*objects, end=end, soft_wrap=soft_wrap, width=width)
                    pieces.append(rendered.getvalue())
        finally:
            self.console.file = previous_file
        return pieces

    def held_tail(self, width: int, count: int) -> list[str]:
        """The conversation's newest rows while a view hides it, newest last.

        What it wrote since the view opened, then the text still streaming
        that has not settled into a block yet, as terminal text at ``width``.
        """
        if self.view is None:
            return []

        def shown(piece: str) -> list[str]:
            return [row for row in split_rows(piece) if ESCAPES.sub("", row).strip()]

        # Rendered again only as it changes: the unsettled tail on every
        # streamed line, the held writes only when another one arrives.
        streaming = []
        if self.tail.strip():
            streaming = shown(self._render([((Markdown(self.tail),), "\n", False)], width)[0])
        if len(streaming) >= count:
            return streaming[-count:]
        key = (self._writes, width, id(self.rich_theme()))
        if self._tail_cache is None or self._tail_cache[0] != key:
            held: list[str] = []
            # Only as far back as the rows wanted: a long turn holds many writes.
            for objects, end, soft_wrap in reversed(self.pending):
                if any(isinstance(obj, EditTranscript) for obj in objects):
                    # A diff runs delta: never inside a frame. Its tool row says so.
                    continue
                held[:0] = shown(self._render([(objects, end, soft_wrap)], width)[0])
                if len(held) >= count:
                    break
            self._tail_cache = (key, held[-count:])
        return (self._tail_cache[1] + streaming)[-count:]

    def _take_pending(self) -> list:
        if self.view is not None:
            pending, self.view_pending = self.view_pending, []
        else:
            pending, self.pending = self.pending, []
        return pending

    def _written(self, count: int) -> None:
        """Drop ``count`` rows from the queue's front as written or replaced."""
        self.rows = self.rows[count:]
        self._row_base += count
        self._queued_transient = [
            (entry, end) for entry, end in self._queued_transient if end > self._row_base
        ]

    def _advance(self) -> tuple[int, int]:
        """One paced frame: whole rows now due, and characters shown of the next.

        Rows roll in one at a time and prose types out a few characters a step;
        both speed up so a backlog never falls far behind. Blank rows are free,
        so the gap between paragraphs does not stall the reveal, and so is a
        typed row's indentation.
        """
        self._reveal_rate = max(self._reveal_rate, -(-len(self.rows) // PACED_DRAIN_FRAMES))
        if self._typed and self.rows[0].typed and self._skipped < TYPED_STEP_FRAMES - 1:
            # Mid-row and between steps: nothing moves this frame.
            self._skipped += 1
            return 0, self._typed
        self._skipped = 0
        backlog = sum(row.visible for row in self.rows if row.typed) - self._typed
        self._type_rate = max(
            self._type_rate, TYPED_CHARS_PER_STEP, -(-backlog // TYPED_DRAIN_STEPS)
        )
        rows, chars, typed = self._reveal_rate, self._type_rate, self._typed
        due = 0
        for row in self.rows:
            if row.typed:
                typed = max(typed, row.lead)
                if row.visible - typed > chars:
                    return due, typed + chars
                chars -= row.visible - typed
                typed = 0
            elif row.visible:
                if not rows:
                    break
                rows -= 1
            due += 1
        return due, 0

    def typing_fragments(self) -> StyleAndTextTuples:
        """The prose row being typed out, as far as it has got."""
        if not self._typed or not self.rows:
            return []
        row = self.rows[0]
        key = (row.text, self._typed)
        if self._preview is None or self._preview[0] != key:
            fragments, left = [], self._typed
            for style, text in ANSI(OSC.sub("", row.text.rstrip("\n"))).__pt_formatted_text__():
                if left <= 0:
                    break
                fragments.append((style, text[:left]))
                left -= len(text)
            self._preview = (key, fragments)
        return self._preview[1]

    async def flush(self, *, drain: bool = False) -> None:
        """Write queued output; ``drain`` writes it all, as before a popup or exit."""
        async with self.lock:
            paced = self.paced and not drain
            due = typed = 0
            if paced and self._regenerate is None:
                # Decide what this frame reveals before paying for a handoff: a
                # frame that only types further repaints the live row instead.
                if self._queue():
                    width = max(1, self.app.output.get_size().columns)
                    self._enqueue(self._take_pending(), width)
                if self.rows:
                    due, typed = self._advance()
                if not due and typed != self._typed:
                    self._typed = typed
                    self.app.invalidate()
            if self._regenerate is not None or due or not paced and (self._queue() or self.rows):
                # Renderer.reset() shows the cursor at the transcript position
                # both when erasing and before repainting. Suppress those shows
                # until the handoff has restored the editor and its cursor.
                with self.app.output.hidden_cursor():
                    async with suspended_editor(self.app, atomic=True) as handoff:
                        # Snapshot after entering: input/model events can arrive while
                        # the handoff waits for CPR, but not during these sync writes.
                        width = max(1, self.app.output.get_size().columns)
                        if self._regenerate is not None:
                            # Transient writes are not in the replay; keep those
                            # still queued as rows as well as those not rendered.
                            notes = [entry for entry, _ in self._queued_transient]
                            if self.view is not None:
                                # Any rebuild while a view is open redraws the
                                # view; the conversation's writes keep waiting,
                                # but its queued rows go, so their notes wait too.
                                self._held_notes += notes
                                pending = self.view()
                                self.view_pending = []
                            else:
                                pending = (
                                    self._regenerate()
                                    + self._held_notes
                                    + notes
                                    + self.transient_pending
                                )
                                self._held_notes = []
                                self.pending.clear()
                            self._regenerate = None
                            # Replay covers the rows still queued; drop them.
                            self._written(len(self.rows))
                            self._queued_transient = []
                            # The handoff has erased the editor. Clear the
                            # normal-screen history and home before replay; its
                            # exit will request fresh CPR and restore the draft.
                            self.app.output.write_raw("\x1b[H\x1b[2J\x1b[3J")
                            self.app.output.flush()
                            handoff.top_row = 1
                            self._enqueue(pending, width, replay=True)
                            # A rebuilt screen lands whole; pacing is for new output.
                            due, typed = len(self.rows), 0
                        else:
                            self._enqueue(self._take_pending(), width)
                            if not paced:
                                due, typed = len(self.rows), 0
                        # Rows arriving during the handoff queue behind this frame's.
                        chunk = self.rows[:due]
                        self._written(due)
                        self._typed = typed
                        counter = RowCounter(self.console.file)
                        counter.write("".join(row.text for row in chunk))
                        counter.flush()
                        handoff.rows_written = counter.rows
                # The handoff already repainted the editor on exit.
            self.changed.clear()
            if self.rows:
                # Keep the run loop ticking at its frame rate until drained.
                self.changed.set()
            else:
                self._reveal_rate = self._type_rate = self._typed = self._skipped = 0

    async def run(self) -> None:
        size = self.app.output.get_size()
        resized_at = None
        while True:
            # Poll only when resize replay is enabled. Debounce resize storms;
            # the renderer continues handling the editor normally meanwhile.
            if self.resize_replay is None:
                await self.changed.wait()
            else:
                # Not asyncio.wait_for: on 3.11 it swallows a cancel that lands
                # as the event fires, so this loop outlived the editor and
                # Ctrl+D never exited.
                try:
                    async with asyncio.timeout(0.1):
                        await self.changed.wait()
                except TimeoutError:
                    pass
                # Height changes can scroll pieces of the live preview into
                # history too; replay must clear those just like width reflow.
                current = self.app.output.get_size()
                if current != size:
                    size, resized_at = current, monotonic()
                elif resized_at is not None and monotonic() - resized_at >= 0.25:
                    self.regenerate(self.resize_replay)
                    resized_at = None
            await asyncio.sleep(1 / 30)
            await self.flush()


# Rows of the conversation's newest output shown under a /btw thread that has
# the screen (`PromptLayout.conversation_rows`): enough to follow a turn.
CONVERSATION_ROWS = 3

PROMPT_PREFIX = "❯ "
# Same width as PROMPT_PREFIX, so wrapping is unchanged when the draft flips.
SHELL_PROMPT_PREFIX = "$ "
CONTINUATION_PREFIX = "· "


def prompt_prefix(text: str) -> str:
    """The prompt marker for a draft: `$` once it is a `!command`, else the chevron."""
    return SHELL_PROMPT_PREFIX if text.lstrip().startswith(SHELL_PREFIX) else PROMPT_PREFIX


def _completes_while_typing() -> bool:
    text = get_app().current_buffer.text
    return (
        (text.startswith("/") and "\n" not in text)
        # A leading `$MODEL` or `+EFFORT` while it is still the only word.
        or (text.startswith(("$", "+")) and not any(char.isspace() for char in text))
        or reference_fragment(get_app().current_buffer.document.text_before_cursor) is not None
    )


def _fit_editor(session: PromptSession) -> Window:
    """The session's editor window, sized to its content and without search."""
    # Retain PromptSession's editor/processors, but give its frame a content-sized
    # height. The default frame expands into the CPR-reported space below the
    # cursor, which can be almost the whole pane after a tmux split.
    editor = session.layout.current_window
    editor.height = None
    editor.dont_extend_height = Always()
    # No incremental search here: dropping the editor's search control makes
    # prompt_toolkit's `control_is_searchable` false, so Ctrl+R, Ctrl+S, and vi's
    # `/` and `?` never open an `I-search:` prompt this layout has no room for.
    editor.content._search_buffer_control = None
    return editor


def _per_render(method):
    """Cache a layout callback for the length of one redraw.

    Layout callbacks are queried repeatedly during a single synchronous redraw.
    Never retain their results across redraws: editor/menu/CPR and mutable
    activity state can all change without going through one revision counter.
    """

    @wraps(method)
    def cached(self, *args):
        if self.render_cache is None:
            return method(self, *args)
        key = (method, self.size(), args)
        if key not in self.render_cache:
            self.render_cache[key] = method(self, *args)
        return self.render_cache[key]

    return cached


def _preview_body(diff: bool, body: str, width: int, theme: str):
    # Only the most recent body is retained. Titles and height/tail allocation
    # stay outside this cache; width, kind and syntax theme affect rendering.
    if diff:
        return edit_preview_rows(body, width, theme)
    return [
        ("class:bottom-toolbar.text", row.plain)
        for row in Text(command_text(body)).wrap(
            Console(width=width), width, overflow="fold", no_wrap=False
        )
    ]


def _spinner_rows(fragments, height) -> VSplit:
    """Chrome rows outside a frame: spinners, notices and queued prompts.

    One column of left padding, so the rows sit off the terminal edge and
    their text starts where the frames' contents do.
    """
    return VSplit(
        [
            Window(width=1),
            Window(
                FormattedTextControl(fragments, show_cursor=False),
                height=height,
                wrap_lines=False,
                dont_extend_height=True,
            ),
        ],
        height=height,
    )


class PromptLayout:
    """The live block under scrollback: activity rows, tasks, previews and the editor.

    Row callbacks read the session's current app on every call, since a
    transcript prompt swaps in its own Application after the layout is built.
    """

    def __init__(
        self,
        session: PromptSession,
        activity: Activity,
        transcript: "Transcript | None",
        shortcuts: PrefixKeys,
        session_title: Callable[[], str] = lambda: "",
    ):
        self.session = session
        self.activity = activity
        self.transcript = transcript
        self.shortcuts = shortcuts
        self.session_title = session_title
        self.editor = _fit_editor(session)
        self.render_cache = None
        self.animation_task = None
        self.preview_body = lru_cache(maxsize=1)(_preview_body)
        self.live_delta = LiveDeltaPreview(lambda: self.session.app.invalidate())
        # The status row and side questions (waits on a model) share one
        # spinner; running tool calls and the terminal's own waits (a command)
        # share another, so the two kinds of wait look different at a glance.
        # Plan steps use a static marker, so motion only ever means
        # "the turn is waiting on this". Who owns the work is the badge and colour.
        self.spinner = status_spinner()
        self.tool_spinner = status_spinner("tool_spinner")
        self.thought_max_rows = parse_height(load_preferences().get("thinking_max_lines")) or float(
            THOUGHT_ROWS
        )
        self.menu = CompletionsMenu(
            max_height=20, scroll_offset=1, extra_filter=has_focus(session.default_buffer)
        )
        self.menu.content.dont_extend_height = Always()
        from pcode.btw_view import ThreadView

        # A /btw thread in the conversation's place, while one is open.
        self.btw = (
            ThreadView(activity, transcript, shortcuts.label)
            if shortcuts is not None
            else ThreadView(activity, transcript)
        )
        # Whether the open menu made the layout taller than the measured rows.
        self.menu_overflowed = False
        # The band's held height, when it last filled it, and the pane size
        # it was held at (`band_rows`).
        self.band_hold = (0, 0.0, None)

    def size(self):
        size = self.session.app.output.get_size()
        # Every layout read comes through here, so the task widget's size rule
        # follows a resize or split before anything is measured against it.
        self.activity.fit_screen(size.columns, size.rows)
        return size

    # Heights and rows, each computed at most once per redraw.

    @_per_render
    def frame_height(self) -> int:
        """The editor box, including its band and any tasks drawn inside it."""
        tasks = self.attached_height()
        band = self.band_height()
        live = self.preview_layout()
        if live is not None:
            return live[3] + tasks + band
        size = self.size()
        gap = self.top_gap()
        available = max(
            1,
            size.rows - 4 - gap - self.activity_height() - tasks - band - len(self.queue_rows()),
        )
        cap = self.activity.height_cap(size.rows)
        if cap is not None:
            available = min(available, max(1, cap - 2 - gap - band - self.tasks_height()))
        text_height = self.editor.preferred_height(max(1, size.columns - 2), available).preferred
        return min(text_height, available) + 2 + gap + tasks + band

    def refresh_interval(self) -> float:
        """Seconds until the next frame."""
        return min(self.spinner.interval, self.tool_spinner.interval) / 1000

    @_per_render
    def spinner_frame(self) -> str:
        """This redraw's model-wait frame, shared so every such spinner turns together."""
        return self.spinner.render(monotonic()).plain

    @_per_render
    def tool_spinner_frame(self) -> str:
        """This redraw's frame for running tool calls and commands."""
        return self.tool_spinner.render(monotonic()).plain

    @_per_render
    def base_plan_rows(self, budget: int | None = None):
        if budget is None:
            rows = self.size().rows
            cap = self.activity.height_cap(rows)
            budget = (
                min(10, max(1, rows // 2 - 2))
                if cap is None
                # The editor box keeps one text row inside its two borders,
                # and detached tasks leave it any blank row under its status.
                else max(
                    1,
                    cap
                    - self.task_chrome()
                    - 3
                    - self.band_height()
                    - (0 if self.activity.attach_tasks else self.top_gap(attached=False)),
                )
            )
        # The active step turns with the running commands' spinner.
        return self.activity.plan_rows(budget, self.tool_spinner_frame())

    @_per_render
    def preview_layout(self):
        """Allocate actual chrome/editor height first, then give output the remainder.

        Keep the normal task viewport unless it would leave no output at all.
        Only in that case trim task rows to preserve a one-line output tail.
        Calculate all three heights together so editor wrapping cannot create a
        circular dependency between frame_height and command_rows.
        """
        activity, transcript = self.activity, self.transcript
        if transcript is None or transcript.view_shown:
            # A /btw thread has the screen: the conversation's previews wait for it.
            return None
        edits = transcript.live_edit_previews and activity.edit_previews
        if not edits:
            # The preview ended, so the next edit never flashes this one's diff.
            self.live_delta.forget()
        # A `!command` the user typed is shown while it runs whatever the
        # scrollback setting for the model's commands says, and so is a job
        # the user asked to watch; the model's own commands follow the setting.
        commands = activity.command_outputs
        if not (transcript.command_scrollback or activity.user_command):
            commands = {
                key: event for key, event in commands.items() if key.startswith(WATCHED_PREFIX)
            }
        if not edits and not commands:
            return None
        size = self.size()
        width = max(1, size.columns - 2)
        # One terminal row stays free for the non-full-screen renderer/CPR.
        # The band rides in the editor box, but it is chrome all the same.
        band = self.band_height()
        fixed = (
            1
            + int(self.session.bottom_toolbar is not None)
            + self.status_height()
            + band
            + len(self.queue_rows())
            + self.menu.preferred_height(size.columns, size.rows).preferred
        )
        room = max(0, size.rows - fixed)
        # Parallel calls share the preview; show the most recently updated call.
        event = next(reversed((activity.edit_previews if edits else commands).values()))
        # A sandboxed snippet is pending arguments like an edit, but it is code
        # rather than a diff: no +/- coloring, and nothing has run yet.
        code = bool(edits) and event.kind == "code"
        heading = (
            block_heading(
                RUNNING,
                "Preparing code · not yet run" if code else f"Editing · {event.path}",
            )
            if edits
            else command_heading(activity, event)
        )
        plans = self.base_plan_rows()
        # Editor: two borders and at least one text row. Preview: two rule
        # lines, the command, and at least one output row. Keep one task when
        # possible.
        # Attached tasks share the editor's top border and add only a divider.
        chrome_rows = 1 if activity.attach_tasks else 2
        task_floor = 1 + chrome_rows if plans else 0
        gap = self.top_gap(attached=activity.attach_tasks and bool(plans))
        editor_room = max(1, room - 2 - gap - 4 - task_floor)
        cap = activity.height_cap(size.rows)
        if cap is not None:
            tasks = len(plans) + chrome_rows if plans else 0
            editor_room = min(editor_room, max(1, cap - 2 - gap - band - tasks))
        editor_rows = min(editor_room, self.editor.preferred_height(width, editor_room).preferred)
        editor_height = editor_rows + 2 + gap
        plan_budget = max(0, room - editor_height - 4 - chrome_rows)
        if len(plans) > plan_budget:
            plans = self.base_plan_rows(plan_budget)
            # Trimmed away entirely, attached tasks no longer stand in for the gap.
            trimmed_gap = self.top_gap(attached=activity.attach_tasks and bool(plans))
            editor_height += trimmed_gap - gap
        plan_height = len(plans) + chrome_rows if plans else 0
        # Chrome: the two rule lines, plus the indented `$ command` a shell
        # preview repeats below its heading, exactly as scrollback does.
        chrome = 2 if edits else 3
        budget = min(
            max(1, screen_rows(transcript.command_preview_lines, size.rows)),
            room - editor_height - plan_height - chrome,
        )
        if budget <= 0:
            return plans, "", [], editor_height
        body = event.text if edits else event.output
        delta = transcript.delta if edits and not code else None
        rows = (
            self.live_delta.rows(
                delta, event.call_id, event.path, body, width, transcript.code_theme
            )
            if delta is not None
            else None
        ) or self.preview_body(bool(edits) and not code, body, width, transcript.code_theme)
        if edits:
            # A diff keeps its +/- gutter flush left, as the settled block does.
            return plans, heading, rows[-budget:], editor_height
        block = [("class:plan", "$ " + command_preview(event.command)), *rows[-budget:]]
        return plans, heading, [(style, INDENT + text) for style, text in block], editor_height

    def plan_rows(self):
        live = self.preview_layout()
        return live[0] if live is not None else self.base_plan_rows()

    def preview_heading(self):
        live = self.preview_layout()
        return live[1] if live is not None else ""

    def command_rows(self):
        live = self.preview_layout()
        return live[2] if live is not None else []

    @_per_render
    def notice_rows(self):
        """Freeze the expiring notice for this render so height matches content."""
        width = self.size().columns - 1
        return self.activity.notice_rows(width)

    @_per_render
    def group_rows(self):
        """The run's group line so far, when no status row carries its tally.

        While a turn runs the count rides the status row instead, next to the
        spinner that says it is still going; the full line reaches scrollback
        when the run closes.
        """
        if self.transcript is None or self.activity.status_shown:
            return []
        row = self.transcript.pending_group_row(self.size().columns - 1)
        return [("class:activity.group", row)] if row else []

    @_per_render
    def thought_rows(self):
        """The newest thought, on fewer rows in a short pane.

        The tool rows count against the same cap, keeping at least one row
        for the thought.
        """
        screen, cap = self.size().rows, self.thought_max_rows
        # A fixed count still yields to a short pane (a quarter of it, less
        # the tool rows); a share of the screen is that share less the tool rows.
        share = screen_rows(cap, screen) if cap < 1 else screen // 4
        rows = max(1, share - len(self.tool_rows()))
        if cap >= 1:
            rows = min(int(cap), rows)
        if self.activity.attach_activity:
            budget = self.band_budget()
            if budget is not None:
                # Under a height cap the thought gives way first.
                rows = min(rows, budget - len(self.tool_rows()))
            return self.activity.thought_fragments(self.band_width(), rows, hanging=True)
        return self.activity.thought_fragments(self.size().columns - 1, rows)

    @_per_render
    def status_line(self) -> StatusLine:
        """The held status line, read once per redraw by the status row and the tool row.

        Without its tally, which the hold ignores anyway: the tally reads the
        task widget, whose height depends on `preview_layout`, which in turn
        sizes the live rows this line feeds. Like `status_gap`, it must read
        only activity state.
        """
        return self.activity.held_status_line("", STATUS_HOLD_SECONDS)

    @_per_render
    def tool_rows(self):
        """The turn's latest tool calls, under the thought.

        They share a quarter of the pane with the thought, which keeps one
        row of it when there is one to show.
        """
        limit = self.size().rows // 4 - int(self.activity.thought_shown)
        if not self.activity.attach_activity:
            return self.activity.tool_rows(
                self.status_line(), self.size().columns - 1, limit, self.tool_spinner_frame()
            )
        budget = self.band_budget()
        if budget is not None:
            if budget < 1:
                return []
            limit = min(limit, budget)
        # Indented under the thought's text, a level below the column of icons.
        indent = " " * (cell_len(THOUGHT_ICON) + 1)
        rows = self.activity.tool_rows(
            self.status_line(), self.band_width() - len(indent), limit, self.tool_spinner_frame()
        )
        return [[("", indent), *row] for row in rows]

    def band_width(self) -> int:
        """Cells across a band row: inside the box's sides, in from them like the
        tasks, and a cell short of the right one so wrapped prose has a margin
        to match the status row's."""
        return max(1, self.size().columns - 3 - self.task_padding())

    def band_budget(self) -> int | None:
        """Rows `tasks_max_height` leaves the band, or None when nothing caps it.

        The cap covers the whole box, so the band gets what remains once the
        box keeps its status row, one text row, its bottom row, the band's
        divider, and a task with its divider. Reads the plan itself rather
        than its rows, which are sized from this.
        """
        activity = self.activity
        cap = activity.height_cap(self.size().rows)
        if cap is None:
            return None
        tasks = activity.panel_shown and bool(activity.displayed_plan)
        return cap - 4 - (1 + self.task_chrome() if tasks else 0)

    @_per_render
    def band_rows(self) -> list[list[tuple[str, str]]]:
        """The activity band: the thought, then its tool calls, under the status row.

        `attach_activity` draws it in the editor box. It grows at once but
        gives rows back only once it has held fewer for `BAND_HOLD_SECONDS`,
        padding the gap with blank rows: a new thought starting over on one
        row, or a call that stops lingering, would otherwise jolt the status
        row and the tasks up and down on every step. With nothing left to
        show (the answer is streaming, or Ctrl+O hid the panel) it goes at
        once: a band of blank rows would only look stuck.
        """
        rows = (
            [*self.thought_rows(), *self.tool_rows()]
            if self.activity.attach_activity and self.activity.status_shown
            else []
        )
        size = self.size()
        if not rows:
            self.band_hold = (0, 0.0, size)
            return []
        now = monotonic()
        held, since, sized = self.band_hold
        budget = self.band_budget()
        if budget is not None:
            held = min(held, budget)
        # A resize sizes the rows afresh: a height held from a taller pane
        # could crowd the editor out of this one.
        if len(rows) >= held or now - since >= BAND_HOLD_SECONDS or sized != size:
            self.band_hold = (len(rows), now, size)
            return rows
        return rows + [[] for _ in range(held - len(rows))]

    def band_height(self) -> int:
        """Rows the band adds to the editor box: its rows and the divider under them."""
        rows = self.band_rows()
        return len(rows) + 1 if rows else 0

    @_per_render
    def aside_rows(self):
        return self.activity.aside_rows(self.spinner_frame(), self.size().columns - 1)

    @_per_render
    def wait_rows(self):
        """The terminal's own wait (the host starting, a command running there)."""
        return self.activity.wait_fragments(self.tool_spinner_frame(), self.size().columns - 1)

    @_per_render
    def typing_row(self):
        """Prose still being typed out: the next scrollback row, drawn live."""
        output = self.transcript.output if self.transcript is not None else None
        return output.typing_fragments() if output is not None else []

    @_per_render
    def queue_rows(self):
        budget = min(4, max(1, self.size().rows // 4))
        return self.activity.queue_rows(budget)

    def status_gap(self) -> bool:
        """Whether the live panel needs its own blank row above it.

        Scrollback separates blocks with a blank row, but the panel is not
        scrollback: without this the live rows, or the editor box, sit flush
        against the last tool line or the startup notes. Depend only on state
        preview_layout already reads, so asking for the gap cannot re-enter
        the layout calculation.
        """
        transcript = self.transcript
        if transcript is None:
            return False
        if not transcript.ends_blank:
            return True
        # A typed row is not written yet, so scrollback's own gap sits above it.
        shown = (
            self.activity.status_shown
            or bool(self.group_rows())
            or bool(self.notice_rows())
            or bool(self.wait_rows())
            or bool(self.aside_rows())
        )
        return shown and bool(self.typing_row())

    @_per_render
    def conversation_rows(self) -> list[StyleAndTextTuples]:
        """While a /btw thread has the screen: a divider, then the conversation's newest rows.

        The divider points up to the thread, which is scrollback now, and down
        to what the conversation is writing meanwhile, so a running turn can be
        followed without going back to it.
        """
        transcript = self.transcript
        # Not before the thread is drawn, nor over the conversation it hides.
        if transcript is None or not getattr(transcript.output, "view_drawn", False):
            return []
        from pcode.btw_view import current
        from pcode.conversation_tree import excerpt

        thread = current(self.activity)
        columns = self.size().columns - 1
        question = plain(excerpt(thread[0].question), max(10, columns // 4)) if thread else ""
        label = [
            ("class:btw.divider", f" ↑ btw: {question} "),
            ("class:frame.border", "·"),
            ("class:btw.conversation", " ↓ conversation "),
        ]
        key = self.shortcuts.label("q") if self.shortcuts is not None else "Ctrl+Q"
        keys = [
            ("class:btw.key", " Esc"),
            ("class:btw.conversation", " back · "),
            ("class:btw.key", key),
            ("class:btw.conversation", " next thread "),
        ]

        def cells(fragments) -> int:
            return sum(get_cwidth(text) for _, text in fragments)

        # The keys are dropped first when the row is too narrow for both.
        if cells(label) + cells(keys) + 4 > columns:
            keys = []
        fill = max(0, columns - 2 - cells(label) - cells(keys) - (1 if keys else 0))
        divider = [("class:frame.border", "──"), *label, ("class:frame.border", "─" * fill)]
        if keys:
            divider += [*keys, ("class:frame.border", "─")]
        tail = transcript.output.held_tail(max(1, columns - 2), CONVERSATION_ROWS)
        # prompt_toolkit's ANSI reads no OSC: hyperlinks would show as text.
        rows = [
            [("", "  "), *ANSI(OSC.sub("", row.rstrip("\n"))).__pt_formatted_text__()]
            for row in tail
        ]
        return [fit_fragments(divider, columns), *rows]

    def status_height(self) -> int:
        """Live rows above the editor box. The status rides the box's own border."""
        # Attached, the thought and tool rows are the box's band instead.
        unboxed = not self.activity.attach_activity
        return (
            len(self.conversation_rows())
            + bool(self.typing_row())
            + (len(self.thought_rows()) + len(self.tool_rows()) if unboxed else 0)
            + len(self.group_rows())
            + len(self.notice_rows())
            + len(self.wait_rows())
            + len(self.aside_rows())
            + self.status_gap()
        )

    def task_chrome(self) -> int:
        """Rows the widget adds around its tasks: a divider attached, else a frame."""
        return 1 if self.activity.attach_tasks else 2

    def tasks_height(self) -> int:
        """The widget's full height, wherever it is drawn."""
        rows = self.plan_rows()
        return len(rows) + self.task_chrome() if rows else 0

    def plan_attached(self) -> bool:
        return self.activity.attach_tasks and bool(self.plan_rows())

    def attached_height(self) -> int:
        """Rows attached tasks add to the editor box: the tasks plus a divider."""
        return len(self.plan_rows()) + 1 if self.plan_attached() else 0

    def activity_height(self) -> int:
        rows = [] if self.activity.attach_tasks else self.plan_rows()
        commands = self.command_rows()
        return (
            self.status_height()
            + (len(rows) + 2 if rows else 0)
            + (len(commands) + 2 if commands else 0)
        )

    def task_padding(self) -> int:
        return int(self.activity.task_style == "icons")

    def plan_text(self):
        return panel_fragments(self.plan_rows(), self.size().columns - 2 - self.task_padding())

    # Containers.

    def panel_rows(self, rows) -> ConditionalContainer:
        """Padded chrome rows for one row source, shown while it has any."""
        return ConditionalContainer(
            _spinner_rows(
                lambda: panel_fragments(rows(), self.size().columns - 1),
                lambda: len(rows()),
            ),
            filter=Condition(lambda: bool(rows())),
        )

    def plan_body(self) -> VSplit:
        # `task_style icons` sets rows one column off the frame's side.
        return VSplit(
            [
                Window(width=self.task_padding),
                Window(
                    FormattedTextControl(self.plan_text),
                    height=lambda: len(self.plan_rows()),
                    dont_extend_height=True,
                    wrap_lines=False,
                ),
            ]
        )

    def band_body(self) -> VSplit:
        """The band's rows, set in from the side like the tasks under them."""
        return VSplit(
            [
                Window(width=self.task_padding),
                Window(
                    FormattedTextControl(
                        lambda: panel_fragments(self.band_rows(), self.band_width()),
                        show_cursor=False,
                    ),
                    height=lambda: len(self.band_rows()),
                    dont_extend_height=True,
                    wrap_lines=False,
                ),
            ]
        )

    def status_border(self) -> Window:
        """The editor's top border carrying the status: `┌─ ⠋ Phase ── ✓ 1 tool · 4s ─┐`.

        It takes the place of the task heading while a turn runs: the tasks
        hang under what the turn is doing, and the status costs no row. The
        heading's count moves to the right-hand meta, so a plan longer than
        its window still says how much there is (`Tasks 2/14 · ✓ 1 tool`).
        """

        def fragments():
            columns = self.size().columns
            if columns < 2:
                return []
            transcript = self.transcript
            meta = [transcript.pending_tally() if transcript is not None else ""]
            activity = self.activity
            # A plan hidden only for the pane's size still says it exists.
            hidden = activity.show_tasks and activity.tasks_too_big
            if (self.plan_attached() or hidden) and activity.displayed_plan:
                meta.insert(0, activity.panel_title())
            line = replace(self.status_line(), tally=" · ".join(part for part in meta if part))
            lead = self.heading_lead()
            status = line.fragments(self.spinner_frame(), columns - 3 - len(lead), rule=True)
            if not status:
                return [("class:frame.border", "┌" + "─" * max(0, columns - 2) + "┐")]
            used = len(lead) + sum(cell_len(text) for _, text in status)
            return [
                ("class:frame.border", lead),
                *status,
                ("class:frame.border", f" {'─' * max(0, columns - used - 2)}┐"),
            ]

        return Window(FormattedTextControl(fragments, show_cursor=False), height=1)

    def plan_heading(self):
        """The task heading, without permanent shortcut instructions."""
        activity = self.activity
        # The lead, a space and at least one rule cell, the corner, and margin.
        width = self.size().columns - len(self.heading_lead()) - 5
        style = "bold"
        if activity.displayed_plan:
            style = "class:plan.heading.done" if activity.plan_done else "class:plan.heading"
        heading = [(style, activity.panel_heading())]
        # Attached, the heading takes the editor's top border, so it carries
        # the session's name too (`Tasks 2/5 · Fix the login test`). A detached
        # task box sits above an editor box already showing it.
        if self.plan_attached() and (label := self.session_label()):
            return fit_fragments([*heading, ("class:activity.meta", " · "), *label], width)
        return panel_fragments(heading, width)

    def heading_lead(self) -> str:
        """What comes before a box's heading on its top row.

        Bordered, the corner and a rule stub (`┌─ `). Shaded there is no line
        to start, so the heading lines up with the rows under it: the task
        icons and the prompt chevron, one column in (two with `task_style icons`).
        """
        if self.frame_shaded():
            return " " * (1 + self.task_padding())
        return "┌─ "

    def plan_heading_border(self) -> VSplit:
        """A top border with the task heading at the left, cut to fit the row.

        Swapped into the editor frame as well as the plan's own.
        """
        return self.heading_border(self.plan_heading)

    def heading_border(self, heading) -> VSplit:
        """A top border with `heading` at the left, drawn like pcode's ``Frame`` title."""
        return VSplit(
            [
                Window(
                    FormattedTextControl(self.heading_lead),
                    width=lambda: len(self.heading_lead()),
                    style="class:frame.border",
                ),
                Label(
                    heading,
                    style="class:frame.label",
                    dont_extend_width=True,
                ),
                Window(FormattedTextControl(" "), width=1, style="class:frame.border"),
                Window(char="─", style="class:frame.border"),
                Window(char="┐", width=1, style="class:frame.border"),
            ],
            height=1,
        )

    def commands_block(self) -> ConditionalContainer:
        # Framed the way scrollback frames the same run once it settles: the
        # heading rides the opening rule, the body is indented, a rule closes it.
        return ConditionalContainer(
            HSplit(
                [
                    VSplit(
                        [
                            Label(
                                lambda: panel_fragments(
                                    [("class:block.heading", self.preview_heading())],
                                    self.size().columns - 4,
                                ),
                                style="class:block.heading",
                                dont_extend_width=True,
                            ),
                            Window(FormattedTextControl(" "), width=1, style="class:block.rule"),
                            Window(char=RULE, style="class:block.rule"),
                        ],
                        height=1,
                    ),
                    Window(
                        FormattedTextControl(
                            lambda: panel_fragments(self.command_rows(), self.size().columns),
                            show_cursor=False,
                        ),
                        height=lambda: len(self.command_rows()),
                        dont_extend_height=True,
                        wrap_lines=False,
                    ),
                    Window(char=RULE, height=1, style="class:block.rule"),
                ],
                height=lambda: len(self.command_rows()) + 2,
            ),
            filter=Condition(lambda: bool(self.command_rows())),
        )

    def activity_panel(self) -> HSplit:
        """Everything live between scrollback and the editor, top to bottom."""
        activity = self.activity

        # With `attach_activity off`: its own rows, so a running tool taking
        # the status never hides it; above the editor box whose border carries
        # the status, where it reads as what led to the call. Attached, the
        # box's band draws both.
        def unboxed(rows):
            return lambda: [] if activity.attach_activity else rows()

        thought = self.panel_rows(unboxed(self.thought_rows))
        # Between the thought and the status row: why, what, then how it is going.
        tool = self.panel_rows(unboxed(self.tool_rows))
        plan_frame = Frame(
            self.plan_body(), height=lambda: len(self.plan_rows()) + 2, style="class:shaded"
        )
        plan_frame.container.children[0] = self.plan_heading_border()
        plan = ConditionalContainer(
            plan_frame,
            filter=Condition(lambda: bool(self.plan_rows()) and not activity.attach_tasks),
        )
        # Keep the turn and its activity adjacent even when the root layout justifies
        # the transcript and editor across the remaining terminal height.
        commands = self.commands_block()
        status_spacer = ConditionalContainer(Window(height=1), filter=Condition(self.status_gap))
        # Flush left, where scrollback will draw the same line once the run closes.
        group = ConditionalContainer(
            Window(
                FormattedTextControl(self.group_rows, show_cursor=False),
                height=lambda: len(self.group_rows()),
                wrap_lines=False,
                dont_extend_height=True,
            ),
            filter=Condition(lambda: bool(self.group_rows())),
        )
        # Above the spinner and the thought: a notice answers the keystroke that
        # caused it without ever reaching scrollback, and vanishes on its own.
        notice = self.panel_rows(self.notice_rows)
        # This terminal's own wait on the session host, hidden while a turn row covers it.
        waits = self.panel_rows(self.wait_rows)
        # Side questions outlive the turn. Their rows stay out here, so only
        # the main status rides the editor box with its tasks.
        asides = self.panel_rows(self.aside_rows)
        # On top, right under the /btw thread it points up to.
        conversation = ConditionalContainer(
            Window(
                FormattedTextControl(
                    lambda: panel_fragments(self.conversation_rows(), self.size().columns - 1),
                    show_cursor=False,
                ),
                height=lambda: len(self.conversation_rows()),
                wrap_lines=False,
                dont_extend_height=True,
                style="class:btw.conversation",
            ),
            filter=Condition(lambda: bool(self.conversation_rows())),
        )
        return HSplit(
            [
                status_spacer,
                conversation,
                group,
                notice,
                thought,
                tool,
                waits,
                asides,
                commands,
                plan,
            ]
        )

    def queued(self) -> ConditionalContainer:
        return ConditionalContainer(
            _spinner_rows(
                lambda: panel_fragments(self.queue_rows(), self.size().columns - 1),
                lambda: len(self.queue_rows()),
            ),
            filter=Condition(lambda: bool(self.activity.queued_prompts)),
        )

    def typing(self) -> ConditionalContainer:
        # Flush left like the scrollback row it becomes, and placed above the
        # layout's justifying filler so it sits directly beneath scrollback rather
        # than jumping up a row when it is written.
        return ConditionalContainer(
            Window(
                FormattedTextControl(self.typing_row, show_cursor=False),
                height=1,
                wrap_lines=False,
                dont_extend_height=True,
            ),
            filter=Condition(lambda: bool(self.typing_row())),
        )

    def session_name(self) -> str:
        """The session's /rename name or model-given title, on one line; else empty.

        Never a fallback such as the first prompt, so a new session keeps a
        plain rule.
        """
        return " ".join((self.session_title() or "").split())

    def session_label(self, leads: bool = False) -> list[tuple[str, str]]:
        """The last turn's mark, then the session's name; either may be absent.

        A new session, with no name and no finished turn, gets nothing. When
        the label `leads` its row, a name with no outcome known here (a resumed
        session, a /tree jump) gets a muted `•` in either frame style, so the
        title row always leads with a mark like a finished turn's; after
        `Tasks 1/1 ·` it would only be noise.
        """
        label = []
        outcome = self.activity.turn_outcome
        name = self.session_name()
        if mark := TURN_MARKS.get(outcome):
            label.append((f"class:session.{outcome}", mark))
        elif name and leads:
            label.append(("class:session.unknown", UNKNOWN_TURN_MARK))
        if name:
            if label:
                label.append(("", " "))
            label.append(("class:session.name", name))
        return label

    def idle_title(self) -> list[tuple[str, str]]:
        """The idle editor box's heading: how the last turn ended, and the name.

        Coming back to a finished turn, it says what the session is about and
        whether the turn worked; while a turn runs, the status row takes the
        border instead.
        """
        # Corners, stub, a space either side, and at least one rule cell.
        # Shaded, `heading_border` draws it: the lead, a space, a rule cell, the corner.
        chrome = len(self.heading_lead()) + 3 if self.frame_shaded() else TITLE_CHROME + 1
        return fit_fragments(self.session_label(leads=True), self.size().columns - chrome)

    def top_row_filled(self) -> bool:
        """Whether the editor's top row carries text: a status, tasks, or a title."""
        return self.activity.status_shown or self.plan_attached() or bool(self.idle_title())

    def top_gap(self, attached: bool | None = None) -> int:
        """Shaded, a blank row (1) between a filled top row and the text, else 0.

        A new session's empty top row reads as padding, so the prompt sits
        between it and the bottom row. Once a status or the session's name
        fills that row, the prompt directly beneath it looks crowded, so the
        box grows a row and keeps the prompt between two blank ones. Drawn
        borders already separate the two, and attached tasks and the band
        bring dividers of their own. `preview_layout` passes `attached`, since
        asking `plan_attached` there would re-enter it.
        """
        if attached is None:
            attached = self.plan_attached()
        return int(
            self.frame_shaded()
            and not attached
            and not self.band_rows()
            and (self.activity.status_shown or bool(self.idle_title()))
        )

    def frame_shaded(self) -> bool:
        """Whether the boxes are drawn as shades rather than lines (`frame_style`)."""
        return (
            self.activity.frame_style == "shaded"
            and self.transcript is not None
            and self.transcript.frame_shade() is not None
        )

    def editor_frame(self) -> Frame:
        session = self.session
        editor_frame = Frame(
            self.editor, title=self.idle_title, height=self.frame_height, style="class:shaded"
        )
        # Replace only the bottom border: the badge must not add a row or alter CPR sizing.
        editor_frame.container.children[-1] = VSplit(
            [
                Window(char="└", width=1, style="class:frame.border"),
                Window(char="─", style="class:frame.border"),
                ConditionalContainer(
                    Label(
                        lambda: editor_mode_badge(session.app),
                        dont_extend_width=True,
                    ),
                    filter=Condition(lambda: session.app.editing_mode == EditingMode.VI),
                ),
                Window(FormattedTextControl("─┘"), width=2, style="class:frame.border"),
            ],
            height=1,
        )
        side = partial(Window, char="│", width=1, style="class:frame.border")
        top, middle, bottom = editor_frame.container.children
        editor_frame.container.children[:] = [
            top,
            ConditionalContainer(
                VSplit([side(), Window(), side()], height=1),
                filter=Condition(lambda: bool(self.top_gap())),
            ),
            middle,
            bottom,
        ]
        # The top border carries the running status, else the attached tasks'
        # heading. Attached tasks sit under it, and a divider separates them
        # from the text; frame_height counts both.
        status = Condition(lambda: self.activity.status_shown)
        attached = Condition(self.plan_attached)
        shaded = Condition(self.frame_shaded)
        from pcode.btw_view import current

        # Showing a /btw thread, the box takes the thread's colour: Enter goes
        # to the thread, not the conversation.
        editor_frame.container.style = lambda: (
            "class:frame class:shaded" + (" class:btw.reply" if current(self.activity) else "")
        )
        band = Condition(lambda: bool(self.band_rows()))

        def divider() -> VSplit:
            # Shaded, the rule takes the box's own colour: a blank row.
            return VSplit(
                [
                    Window(char="├", width=1, style="class:frame.border"),
                    Window(char="─", style="class:frame.border"),
                    Window(char="┤", width=1, style="class:frame.border"),
                ],
                height=1,
            )

        editor_frame.container.children[0] = HSplit(
            [
                # Shaded, a top row with something on it is a shade off the
                # rest of the box; a new session's empty row keeps the box's
                # shade. The band hangs under the status in the same shade, so
                # the turn's live rows read as one header block.
                HSplit(
                    [
                        ConditionalContainer(self.status_border(), filter=status),
                        ConditionalContainer(self.plan_heading_border(), filter=attached & ~status),
                        ConditionalContainer(
                            top,
                            filter=~attached & ~status & ~shaded,
                        ),
                        # Shaded, the session's name lines up with the prompt chevron.
                        ConditionalContainer(
                            self.heading_border(self.idle_title),
                            filter=~attached & ~status & shaded,
                        ),
                        ConditionalContainer(VSplit([side(), self.band_body(), side()]), band),
                    ],
                    style=lambda: "class:shaded.top" if self.top_row_filled() else "",
                ),
                ConditionalContainer(divider(), filter=band),
                ConditionalContainer(
                    HSplit([VSplit([side(), self.plan_body(), side()]), divider()]),
                    filter=attached,
                ),
            ]
        )
        return editor_frame

    def bottom_toolbar(self) -> Window:
        return Window(
            FormattedTextControl(
                lambda: self.session.bottom_toolbar, style="class:bottom-toolbar.text"
            ),
            style="class:bottom-toolbar",
            height=1,
        )

    def layout(self) -> Layout:
        children = [
            self.menu,
            self.activity_panel(),
            self.queued(),
            # This prompt can be only three rows tall. A float would inherit
            # that height and clip the menu, so reserve space only while open.
            shortcut_hint(self.shortcuts),
            self.editor_frame(),
        ]
        if self.transcript is not None:
            children[:0] = [self.typing(), Window()]
        if self.session.bottom_toolbar is not None:
            children.append(self.bottom_toolbar())
        return Layout(
            HSplit(
                children,
                align=VerticalAlign.JUSTIFY if self.transcript else VerticalAlign.BOTTOM,
            ),
            focused_element=self.editor,
        )

    # Redraw hooks: the per-render cache and the spinner's animation timer.

    def needs_animation(self) -> bool:
        activity = self.activity
        return (
            activity.busy
            or activity.status_shown
            # Keep redrawing while a notice is live: nothing else will ask for
            # the frame that finally removes it.
            or activity.notice_shown
            or activity.asides_running
            or bool(activity.waits)
            or (activity.panel_shown and activity.tools.animating)
        )

    async def animate(self, app) -> None:
        await asyncio.sleep(self.refresh_interval())
        self.animation_task = None
        # Repaint unconditionally: this timer only exists because the previous
        # render was animated, and the frame that removes an expired notice or
        # a finished spinner is the one nothing else asks for.
        app.invalidate()

    def before_render(self, app) -> None:
        self.render_cache = {}
        self.btw.sync()
        transcript, activity = self.transcript, self.activity
        if transcript is None or not (
            (transcript.live_edit_previews and activity.edit_previews)
            or (transcript.command_scrollback and activity.command_outputs)
            or any(key.startswith(WATCHED_PREFIX) for key in activity.command_outputs)
        ):
            self.preview_body.cache_clear()

    def after_render(self, app) -> None:
        self.render_cache = None
        # A redraw caused by input or application events starts animation again.
        # Idle prompts have no timer; toolkit owns cancellation at app shutdown.
        if self.needs_animation() and app.is_running:
            if self.animation_task is None or self.animation_task.done():
                self.animation_task = app.create_background_task(self.animate(app))
        elif self.animation_task is not None:
            self.animation_task.cancel()
            self.animation_task = None
        self.replay_after_menu(app)

    def replay_after_menu(self, app) -> None:
        """Rebuild scrollback, as a resize does, once an overflowing menu closes.

        A completion menu taller than the rows below the editor scrolls the
        terminal, and prompt_toolkit never shrinks its screen again (each render
        keeps at least the last height), so closing the menu leaves blank rows
        under the editor where scrolled-away transcript used to be. The replay
        resets the renderer and repaints that history.
        """
        transcript, renderer = self.transcript, app.renderer
        if transcript is None or not transcript.replays_on_resize:
            return
        screen, available = renderer._last_screen, renderer._min_available_height
        if renderer._in_alternate_screen or screen is None or available <= 0:
            return
        if self.session.default_buffer.complete_state is not None:
            self.menu_overflowed |= screen.height > available
        elif self.menu_overflowed:
            self.menu_overflowed = False
            transcript.regenerate()


def create_prompt(
    registry: CommandRegistry,
    *,
    activity: Activity | None = None,
    transcript: "Transcript | None" = None,
    workspace=None,
    on_submit=None,
    on_command=None,
    on_cancel=None,
    on_effort=None,
    on_model=None,
    on_tasks=None,
    on_thinking=None,
    on_commands=None,
    on_send_mode=None,
    on_interrupt_submit=None,
    on_previous_session=None,
    on_copy_response=None,
    on_btw=None,
    on_btw_close=None,
    key_prefix: str | None = None,
    session_title: Callable[[], str] = lambda: "",
    **kwargs,
) -> PromptSession:
    configure_newline_keys()
    install_fast_layout_division()
    activity = activity or Activity()
    callbacks = PromptCallbacks(
        on_cancel=on_cancel,
        on_effort=on_effort,
        on_model=on_model,
        on_tasks=on_tasks,
        on_thinking=on_thinking,
        on_commands=on_commands,
        on_send_mode=on_send_mode,
        on_interrupt_submit=on_interrupt_submit,
        on_previous_session=on_previous_session,
        on_copy_response=on_copy_response,
        on_btw=on_btw,
        on_btw_close=on_btw_close,
    )
    keys, shortcuts = prompt_key_bindings(
        activity, transcript, callbacks, key_prefix, lambda: session.default_buffer
    )

    kwargs.setdefault("color_depth", color_depth())
    output = kwargs.pop("output", None)
    if not isinstance(output, CursorSafeOutput):
        output = CursorSafeOutput(output if output is not None else create_output())
    session = PromptSession(
        output=output,
        message=lambda: [("class:prompt", prompt_prefix(session.default_buffer.text))],
        prompt_continuation=lambda width, line, soft: [
            ("class:prompt", "  " if soft else CONTINUATION_PREFIX)
        ],
        # Every prefix is the same width, so wrapped rows all get the same
        # amount of text space.
        input_processors=[WordWrapProcessor(prefix_width=get_cwidth(PROMPT_PREFIX))],
        multiline=True,
        erase_when_done=True,
        completer=merge_completers([SlashCompleter(registry), FileReferenceCompleter(workspace)]),
        lexer=ReferenceLexer(),
        complete_while_typing=Condition(_completes_while_typing),
        reserve_space_for_menu=0,
        auto_suggest=AutoSuggestFromHistory(),
        key_bindings=shortcuts.key_bindings(keys),
        mouse_support=False,
        **kwargs,
    )
    session.shortcuts = shortcuts
    session.command_bindings = (
        PromptKeymap(
            shortcuts,
            registry,
            on_command,
            transcript.warning if transcript is not None else lambda message: None,
        )
        if on_command is not None
        else None
    )

    prompt_layout = PromptLayout(session, activity, transcript, shortcuts, session_title)
    if transcript is not None:

        def accept(buffer):
            text = buffer.text
            buffer.append_to_history()
            # True keeps the draft: something refused it with a reason shown.
            return bool(on_submit(text))

        session.default_buffer.accept_handler = accept
    session.btw_view = prompt_layout.btw
    session.layout = prompt_layout.layout()
    session.app.layout = session.layout
    if transcript is not None:
        # The app runs for the whole session, redrawn by streaming output and
        # animation; cap that at the paced output's frame rate.
        session.app.min_redraw_interval = 1 / 30
    session.app.before_render += prompt_layout.before_render
    session.app.after_render += prompt_layout.after_render
    if session.app.editing_mode == EditingMode.VI:
        # Allow terminal escape sequences to arrive, without a half-second pause.
        session.app.ttimeoutlen = 0.1
    if transcript is None or not transcript.replays_on_resize:
        install_reflow_renderer(session.app)

    return session


def tally(succeeded: int, failed: int) -> str:
    """`✓ 2 ✗ 1`, dropping a zero side, so each mark only ever counts its own outcome."""
    return " ".join(f"{mark} {count}" for mark, count in (("✓", succeeded), ("✗", failed)) if count)


def tool_count(name: str, count: int, failed: int) -> str:
    """One tool's part of a group line: `Read file ✓ 3`, `Search code ✓ 2 ✗ 1`."""
    return f"{name} {tally(count - failed, failed)}"


class Transcript:
    """Persistent Rich output: anything written here belongs in terminal scrollback.

    Mutable activity and event routing belong to the application, not this writer.
    """

    def __init__(
        self,
        console: Console,
        theme: str = "dark",
        *,
        activity: Activity | None = None,
        preferences: dict[str, str] | None = None,
        detected_theme: str | None = None,
        probe_width: bool = False,
    ) -> None:
        # `probe_width`: also measure the terminal's ambiguous-width glyphs
        # (`pcode.theme`), for the editor's terminal only: its late replies
        # need the editor's input to take them out.
        preferences = load_preferences() if preferences is None else preferences
        self.error_scrollback_lines = int(preferences.get("error_scrollback_lines", "20"))
        self.tool_error_scrollback = preferences.get("tool_error_scrollback", "off") == "on"
        self.show_edits = preferences.get("show_edits", "on") == "on"
        self.live_edits = preferences.get("live_edits", SETTINGS["live_edits"].default) == "on"
        self.command_scrollback = preferences.get("show_commands", "off") == "on"
        self.group_tools = preferences.get("group_tools", SETTINGS["group_tools"].default) == "on"
        self.command_scrollback_lines = int(preferences.get("command_scrollback_lines", "20"))
        # Rows, or a share of the screen; resolved against the pane when drawn.
        self.command_preview_lines = parse_height(
            preferences.get("command_preview_lines")
        ) or float(SETTINGS["command_preview_lines"].default)
        self.activity = activity
        self.console = console
        self.theme = theme
        self.detected_theme = (
            detect_theme(probe_width=probe_width) if detected_theme is None else detected_theme
        )
        self.syntax_themes = syntax_themes(preferences)
        self._delta = delta_from_preferences(preferences)
        self.diff_dedent = preferences.get("diff_dedent", SETTINGS["diff_dedent"].default) == "on"
        self._output: TerminalOutput | None = None
        self.regenerate_on_resize = preferences.get("regenerate_on_resize", "on") == "on"
        self.paced_scrollback = preferences.get(
            "paced_scrollback", SETTINGS["paced_scrollback"].default
        )
        self.log = TranscriptLog(
            max_chars=int(
                preferences.get("transcript_max_chars", SETTINGS["transcript_max_chars"].default)
            )
        )
        self._replay_sink: list | None = None
        self._block: str | None = None
        # A sub-agent's calls settle before its delegate does. They wait here,
        # keyed by the delegate's call id, to be written beneath it.
        self._children: dict[str, list[ToolSummary]] = {}
        # With `group_tools`, the settled calls of the run in progress: they
        # reach scrollback as one line when anything else is written or the
        # turn ends, and the live panel counts them until then.
        self._group: list[ToolSummary] = []

    @property
    def replays_on_resize(self) -> bool:
        """Whether a width change rebuilds scrollback instead of reflowing in place."""
        return self.regenerate_on_resize and self.console.is_terminal

    @property
    def output(self) -> TerminalOutput | None:
        return self._output

    @property
    def view_shown(self) -> bool:
        """Whether another view, a /btw thread, has the screen (`TerminalOutput.view`)."""
        return getattr(self._output, "view", None) is not None

    @output.setter
    def output(self, output: TerminalOutput | None) -> None:
        self._output = output
        if output is not None:
            output.commit_user = self.user
            output.commit_message = self.message
            output.commit_thinking = self.thinking
            # Pacing spreads handoffs over frames; only a real application has
            # either, so offline harnesses and stand-ins write at once.
            output.paced = (
                self.paced_scrollback != "off"
                and self.console.is_terminal
                and isinstance(output.app, Application)
            )
            output.typed = output.paced and self.paced_scrollback == "typed"
            if self.replays_on_resize:
                output.resize_replay = self.replay

    @recorded
    def print(self, *objects, end="\n", tool_line: bool = False) -> None:
        """Write scrollback, keeping tool lines one block apart from other output."""
        if self._group:
            # Anything written after a run of calls closes it, so the run's
            # line lands where the calls happened.
            self._flush_group()
        # Resolve theme-dependent renderables again on every replay.
        objects = tuple(
            Markdown(safe_text(obj.markup), code_theme=self.code_theme)
            if isinstance(obj, (Markdown, RetainedMarkdown))
            else replace(obj, code_theme=self.code_theme, delta=self.delta, dedent=self.diff_dedent)
            if isinstance(obj, EditTranscript)
            else replace(obj, code_theme=self.code_theme)
            if isinstance(obj, (TranscriptNotice, CommandTranscript))
            else obj
            for obj in objects
        )
        block = "tools" if tool_line else "blank" if self._ends_blank(objects) else "other"
        # Consecutive tool lines stay flush; entering or leaving that run gets a
        # blank row, unless the preceding write already ended with one.
        if self._block not in (None, "blank", block) and "tools" in (self._block, block):
            self._write((), "\n")
        self._block = block
        self._write(objects, end)

    @property
    def ends_blank(self) -> bool:
        """Whether scrollback already ends with a blank row.

        The live activity panel uses this to keep itself one block apart from
        the last thing written, the same way consecutive writes do.
        """
        return self._block in (None, "blank")

    @staticmethod
    def _ends_blank(objects: tuple) -> bool:
        """Report whether this write already leaves a blank row behind it.

        A bare ``print()`` is the usual separator; thinking is the one
        renderable here that pads itself, so only it needs naming.
        """
        return not objects or isinstance(objects[-1], ThinkingMarkdown)

    def _write(self, objects: tuple, end: str) -> None:
        if self._replay_sink is not None:
            self._replay_sink.append((objects, end, False))
        elif self.output is not None:
            self.output.print(*objects, end=end)
        else:
            with self.console.use_theme(self.rich_theme):
                self.console.print(*objects, end=end)

    @recorded
    def message(self, text: str) -> None:
        """Retain a Markdown block and its separator atomically, even at tiny budgets."""
        self.print(Markdown(text, code_theme=self.code_theme))
        self.print()

    @recorded
    def thinking(self, text: str) -> None:
        """Retain readable provider text, choosing visibility again on every redraw."""
        if self.activity is not None and self.activity.show_thinking:
            self.print(ThinkingMarkdown(command_text(text), code_theme=self.code_theme), end="")

    @recorded
    def tool_result(self, event: ToolSummary) -> None:
        """Retain hidden results too; choose one representation on each replay."""
        if event.parent_call_id:
            if self.summarizes(event):
                self._children.setdefault(event.parent_call_id, []).append(event)
            return
        if self.writes_tool_result(event):
            self.events((event,))
        for line in self.child_lines(self._children.pop(event.call_id, []), CHILD_INDENT):
            self.print(Padding(line, (0, 0, 0, CHILD_INDENT), expand=False), tool_line=True)

    def child_lines(self, children: list[ToolSummary], indent: int = 0) -> list[Text]:
        """A sub-agent's calls, folded the way the parent's are when grouping."""
        if self.group_tools:
            return self.group_lines(children, indent=indent)
        return [line for child in children for line in self.summary_lines(child, indent=indent)]

    def groups(self, event: ToolSummary) -> bool:
        """Report whether this call's summary line folds into the run's group line.

        A delegate heads its own calls, and a background job's exit is a
        delayed notice, not part of the run. A failure folds in like any call.
        """
        return self.group_tools and event.name != DELEGATE and event.execution != "background"

    def group_lines(self, events: list[ToolSummary], *, indent: int = 0) -> list[Text]:
        """Fold a run of calls into one line; a run of one keeps the call's own line."""
        if len(events) == 1:
            return self.summary_lines(events[0], indent=indent)
        if not events:
            return []
        return [self.group_line(events, width=max(1, self.console.width - indent))]

    @staticmethod
    def group_line(events: list[ToolSummary], *, width: int) -> Text:
        """`✓ 15 tools · Edit file ✓ 10 · Run shell ✓ 5`, most used first.

        Failures split the run's count and their tool's, so a run that hit
        one still stands out: `✓ 5 ✗ 1 tools · Read file ✓ 3 · Search code ✓ 2 ✗ 1`.
        """
        counts = Counter(label(event.name) for event in events)
        failures = Counter(label(event.name) for event in events if event.failed)
        failed = failures.total()
        parts = [tool_count(name, count, failures[name]) for name, count in counts.most_common()]
        head = tally(len(events) - failed, failed)
        line = Text(f"{head} tools · " + " · ".join(parts), style="pcode.thinking")
        line.no_wrap = True
        line.overflow = "ellipsis"
        line.truncate(width, overflow="ellipsis")
        return line

    def _flush_group(self) -> None:
        group, self._group = self._group, []
        # The retained tool results already reproduce this line on replay.
        recording, self.log.recording = self.log.recording, False
        try:
            for line in self.group_lines(group):
                self.print(line, tool_line=True)
        finally:
            self.log.recording = recording

    def settle_tools(self) -> None:
        """Write the pending group line; the turn ended with nothing after it.

        Not recorded: whatever is written next closes the run at the same
        place, and `replay` closes a trailing one unless it is still live.
        """
        if self._group:
            self._flush_group()

    def pending_group_row(self, width: int) -> str:
        """The group line so far, for the live panel; empty when nothing is pending."""
        if not self._group or width < 1:
            return ""
        line = self.group_lines(self._group)[-1].copy()
        line.truncate(width, overflow="ellipsis")
        return line.plain

    def pending_tally(self) -> str:
        """The open run's count for the status row: `✓ 7 ✗ 1 tools`, or empty."""
        if not self._group:
            return ""
        failed = sum(event.failed for event in self._group)
        noun = "tool" if len(self._group) == 1 else "tools"
        return f"{tally(len(self._group) - failed, failed)} {noun}"

    def settle_orphans(self) -> None:
        """Write sub-agent calls whose delegate never settled, e.g. a cancelled turn.

        Without a delegate row to sit under they are written flush, so the
        steps a sub-agent did take are not silently lost.
        """
        orphans, self._children = self._children, {}
        for children in orphans.values():
            for line in self.child_lines(children):
                self.print(line, tool_line=True)

    @property
    def live_edit_previews(self) -> bool:
        """Whether a not-yet-applied edit is previewed in the live panel."""
        return self.show_edits and self.live_edits

    @recorded
    def edit(self, event) -> None:
        if self.show_edits:
            self.print(EditTranscript(event))

    def replay(self) -> list:
        """Project the retained log with current settings, without recording again."""
        sink = []
        self._replay_sink = sink
        self._block = None
        # Replaying the log's own tool results rebuilds whatever is pending.
        self._children = {}
        live_group, self._group = bool(self._group), []
        self.log.recording = False
        try:
            if self.log.dropped:
                self.note("Earlier transcript entries omitted from this regenerated view.")
            for entry in self.log.entries:
                getattr(self, entry.method)(*entry.args, **entry.kwargs)
            # A run still going stays in the live panel; any other was closed.
            if not live_group:
                self.settle_tools()
        finally:
            self.log.recording = True
            self._replay_sink = None
        return sink

    @contextmanager
    def restore(self):
        """Replace history without rendering discarded entries, then redraw once.

        Saved history goes through the same recorded methods and retention budget
        as live output. Redirected output receives the retained slice once, without
        terminal escapes; subsequent redraw requests remain a no-op there.
        """
        previous = self.log
        self.log = TranscriptLog(limit=previous.limit, max_chars=previous.max_chars)
        self.log.capture_only = True
        # Restored history is settled; nothing from it is still running.
        self._group = []
        try:
            yield
        except BaseException:
            self.log = previous
            raise
        finally:
            self.log.capture_only = False
        if self.output is not None and self.console.is_terminal:
            self.regenerate()
        else:
            for objects, end, _ in self.replay():
                self._write(objects, end)

    def regenerate(self) -> None:
        """Request an atomic rebuild; never emit terminal escapes into redirected output."""
        if self.output is not None and self.console.is_terminal:
            self.output.regenerate(self.replay)

    def clear(self) -> None:
        """Drop retained scrollback and rebuild the screen from what comes next.

        The rebuild is deferred to the next flush, so writes made after this
        call are replayed onto the cleared screen rather than erased with it.
        """
        self.log.clear()
        self.regenerate()

    @property
    def resolved_theme(self) -> str:
        return self.detected_theme if self.theme == "auto" else self.theme

    @property
    def delta(self) -> Delta | None:
        """delta for diffs, told the current palette; None renders them with Rich."""
        if self._delta is None:
            return None
        return replace(self._delta, light=self.resolved_theme == "light")

    @property
    def palette(self) -> Palette:
        return PALETTES[self.resolved_theme]

    @property
    def terminal_colors(self) -> bool:
        """Whether `/syntax terminal` hands every color to the terminal's ANSI palette."""
        return self.syntax_themes[self.resolved_theme] == TERMINAL_SYNTAX

    @property
    def menu_palette(self) -> Palette:
        """The palette implied by the syntax style in use, for the popup."""
        if self.terminal_colors:
            return TERMINAL_PALETTE
        return syntax_palette(self.syntax_themes[self.resolved_theme], self.palette)

    @property
    def chrome_palette(self) -> Palette:
        """The same style read for text drawn straight onto the terminal.

        The popup brings its own background; the prompt, the plan rows and the
        frame do not, so the palette's surface stands in for the terminal's
        background and any color that would be lost against it is dropped.
        """
        if self.terminal_colors:
            return TERMINAL_PALETTE
        style = self.syntax_themes[self.resolved_theme]
        return syntax_palette(style, self.palette, self.palette.surface)

    def prompt_style(self, task_style: str = "status", frame_style: str = "borders") -> Style:
        """The prompt_toolkit style for the current theme, syntax, task and frame style."""
        shades = self.frame_shades() if frame_style == "shaded" else None
        return self.chrome_palette.prompt_style(self.menu_palette, task_style, shades)

    def frame_shade(self) -> str | None:
        """The shade for `frame_style shaded`, or None to keep the borders."""
        shades = self.frame_shades()
        return shades[0] if shades is not None else None

    def frame_shades(self) -> tuple[str, str] | None:
        """The box's shade and its header's, or None to keep the borders.

        From the background the terminal last reported when its theme is the
        one in use (an explicit `/theme light` on a dark terminal is not, nor a
        light/dark flip reported without a color), else from the palette's
        surface. `/syntax terminal` has no surface to fall back on. Off true
        color, the shade would round to the nearest of 256 colors, a visible
        blotch on a tinted background, so that keeps the borders too.
        """
        if not true_color():
            return None
        theme = self.resolved_theme
        reported = terminal_background()
        if reported is not None and reported[1] == theme:
            background = reported[0]
        elif self.terminal_colors:
            return None
        else:
            background = self.palette.surface
        return frame_shade(background, theme), header_shade(background, theme)

    @property
    def rich_theme(self) -> Theme:
        return TERMINAL_THEME if self.terminal_colors else self.palette.rich_theme()

    @property
    def code_theme(self) -> str:
        return (
            f"ansi_{self.resolved_theme}"
            if self.terminal_colors
            else self.syntax_themes[self.resolved_theme]
        )

    def welcome(self, model: str | None = None, workspace: str = "") -> None:
        self.print()
        self.print(
            Text.assemble(
                ("pcode", "pcode.brand"),
                (f"  /  {model or 'UI preview'}", "pcode.muted"),
            )
        )
        if model:
            self.retained_note(f"Coder · workspace: {workspace}")
            self.retained_note("Live model · file edits and shell tools enabled · not a sandbox")
        else:
            self.retained_note("Local only · no model connected · no files or shell tools")
        self.retained_note(
            "Type / for commands, /theme-preview for a sample response, /help for keys."
        )
        self.print()

    @recorded
    def syntax_gallery(self) -> None:
        """Preview every selectable style, marking the one in use.

        Recorded without arguments so a replay re-reads the current palette and
        saved styles: the marked row still answers "what am I looking at?"
        after `/theme` or `/syntax` rebuilds scrollback.
        """
        self.print(
            SyntaxGallery(
                SYNTAX_THEMES,
                palette=self.resolved_theme,
                dark=self.syntax_themes["dark"],
                light=self.syntax_themes["light"],
            )
        )

    @recorded
    def retained_note(self, text: str) -> None:
        """Show a notice that belongs to scrollback, so a redraw keeps it.

        Most notices answer a keystroke and are dismissed by the next redraw.
        The opening banner and what it reports about this session are history,
        not an answer, so a resize must not wipe them.
        """
        self.print(Note(text))

    def flash(self, text: str) -> None:
        """Answer a keystroke in the live panel instead of in scrollback.

        Acknowledgements ("Show thinking: off") are worth a glance and nothing
        more; writing them to scrollback leaves them between the model's output
        forever. Without a live panel there is nowhere to put one, so fall back
        to a plain notice.
        """
        if self.activity is None or self.output is None:
            self.note(text)
            return
        self.activity.flash(text)
        self.output.app.invalidate()

    def note(self, text: str) -> None:
        """Show an informational notice once, without retaining it for redraws."""
        notice = Note(text)
        if self._replay_sink is not None:
            self._replay_sink.append(((notice,), "\n", False))
        elif self.view_shown and self.activity is not None:
            # A /btw thread has the screen: a note written now would only show
            # once the conversation is back, so say it where it is seen.
            self.flash(text)
        elif self.output is not None:
            # Preserve new notices across an already queued redraw, but never
            # replay notices that have previously reached the terminal.
            self.output.print(notice, transient=True)
        else:
            with self.console.use_theme(self.rich_theme):
                self.console.print(notice)

    def _flash_behind_view(self, text: str) -> None:
        """Flash a warning or error a /btw thread on screen would otherwise hide.

        It still goes to the conversation, which shows it on return.
        """
        if self.view_shown and self._replay_sink is None and self.activity is not None:
            self.flash(text.strip().splitlines()[0] if text.strip() else text)

    @recorded
    def error(self, text: str, *, title: str = "Error") -> None:
        self._flash_behind_view(f"{title}: {text}")
        self.print(
            TranscriptNotice(text, "error", title, self.error_scrollback_lines, self.code_theme)
        )

    def streams_command(self, event: Event) -> bool:
        """Report whether this settled tool will be mirrored into scrollback.

        Captured output is the bulky part, so mirroring a failed command also
        needs the failure option; without it the completion still shows, as the
        summary line a successful call would leave.
        """
        if not isinstance(event, ToolSummary) or event.name not in COMMAND_TOOLS:
            return False
        return self.command_scrollback and (self.tool_error_scrollback or not event.failed)

    def writes_tool_result(self, event: Event) -> bool:
        """Report whether this settled tool is written to scrollback as it arrives.

        A sub-agent's call is not: it waits to be written beneath its delegate.
        """
        return (
            isinstance(event, ToolSummary) and not event.parent_call_id and self.summarizes(event)
        )

    def summarizes(self, event: Event) -> bool:
        """Report whether this settled tool reaches scrollback at all.

        Omit calls whose results already have a home: planning in the task
        panel, shown edits in their diff, and job inspection in the job's own
        completion notice. Actual tool errors still get their own entry; only
        how much of them, a summary or diagnostic, is configurable.
        """
        if not isinstance(event, ToolSummary):
            return False
        # Inspecting/waiting on a job is not another command completion. A
        # nonzero job exit sets `failed`, but the helper call itself succeeded;
        # only actual helper errors/retries need their own scrollback entry.
        if event.name in {"wait_for_job", "job_output"} and event.outcome == "success":
            return False
        # A command always leaves at least the summary line every other tool
        # leaves; `show_commands` governs only its mirrored output.
        if event.name in COMMAND_TOOLS:
            return True
        if event.failed:
            return True
        return event.name not in PLAN_TOOLS and not (event.name in EDIT_TOOLS and self.show_edits)

    @recorded
    def command_output(self, event: ToolSummary) -> bool:
        """Mirror a command and its captured output; report whether anything printed."""
        if not self.streams_command(event):
            return False
        invocation = (
            command_text(event.command) if event.command else plain(event.detail, limit=None)
        )
        # Live results arrive length-bounded from the capture step;
        # sanitize again so replayed or synthesized events cannot emit controls.
        output = command_text(event.result or event.error or "").rstrip("\n")
        # The job's closing marker repeats the heading's marker and elapsed
        # time; only its id is new, so the heading takes that and drops the rest.
        output, job = split_outcome(output)
        output = output.rstrip("\n")
        if not output.strip():
            output = "(no output)"
        title = label(event.name)
        if event.execution == "background":
            # This is the delayed exit notice, not just a tool result: keep the
            # exact outcome even when the footer is absorbed into the heading.
            title += " · " + plain(event.detail.rsplit(" → ", 1)[-1], limit=None)
        elif job:
            title += f" · {job}"
        self.print(
            CommandTranscript(
                command=invocation,
                output=output,
                # The purpose belongs in the title, not the command line: that
                # line is syntax-highlighted as shell and should stay runnable.
                title=f"{title} · {event.purpose}" if event.purpose else title,
                failed=event.failed,
                elapsed_seconds=event.elapsed_seconds,
                max_lines=self.command_scrollback_lines,
                code_theme=self.code_theme,
                shell_command=bool(event.command),
            )
        )
        return True

    @recorded
    def shell_result(
        self, command: str, output: str, *, failed: bool, elapsed_seconds: float | None
    ) -> None:
        """Mirror a `!command` the user ran; always shown, since they asked for it."""
        output = command_text(output).rstrip("\n")
        self.print(
            CommandTranscript(
                command=command_text(command),
                output=output if output.strip() else "(no output)",
                title="Shell",
                failed=failed,
                elapsed_seconds=elapsed_seconds,
                max_lines=self.command_scrollback_lines,
                code_theme=self.code_theme,
            )
        )

    def warning(self, text: str) -> None:
        self._flash_behind_view(f"Warning: {text}")
        self.print(TranscriptNotice(text, "warning", "Warning"))

    @recorded
    def cancelled(self) -> None:
        self.settle_orphans()
        self.print(TranscriptNotice("", "cancelled", "Run cancelled"))

    @recorded
    def user(self, text: str) -> None:
        self.settle_orphans()
        self.print()
        self.print(TaskPrompt(text))
        self.print()

    def summary_lines(self, event: ToolSummary, *, indent: int = 0) -> list[Text]:
        """The compact rows a settled call leaves in scrollback."""
        if event.name not in COMMAND_TOOLS and not event.command:
            return [
                Text.assemble(
                    (f"{'✗' if event.failed else '✓'} {label(event.name)}  ", "pcode.thinking"),
                    (plain(event.detail, limit=None), "pcode.thinking"),
                    (
                        f"  {event.elapsed_seconds:.1f}s"
                        if event.elapsed_seconds is not None
                        else "",
                        "pcode.thinking",
                    ),
                )
            ]
        # Scrollback shows the outcome only: the live panel already named the
        # target while the call ran. The session browser, which has no such
        # panel, passes the whole detail to the same renderer.
        result = (
            " · " + plain(event.detail.rsplit(" → ", 1)[-1], limit=60)
            if event.failed or (event.name != "run_command" and " → " in event.detail)
            else ""
        )
        return tool_summary_lines(
            event.name,
            result,
            failed=event.failed,
            elapsed_seconds=event.elapsed_seconds,
            command=event.command,
            width=max(1, self.console.width - indent),
        )

    def command_summary(self, event: ToolSummary) -> None:
        """Write a settled call's summary line, or hold it for the run's group line."""
        if self.groups(event):
            self._group.append(event)
            return
        for line in self.summary_lines(event):
            self.print(line, tool_line=True)

    @recorded
    def events(self, events: tuple[Event, ...], *, show_tools: bool = False) -> None:
        for event in events:
            if isinstance(event, CacheBust):
                # Informational, like a retained note: muted, no marker or title.
                self.print(Text(command_text(event.text), style="pcode.muted"))
            elif isinstance(event, Thinking):
                self.thinking(event.text.rstrip("\n") + "\n\n")
            elif isinstance(event, Message):
                self.message(event.markdown)
            elif isinstance(event, ToolSummary):
                if event.name in COMMAND_TOOLS:
                    # Mirroring owns command completions. A call whose
                    # captured output is withheld still reports itself.
                    if not self.command_output(event):
                        self.command_summary(event)
                    continue
                if event.failed and self.tool_error_scrollback:
                    detail = (
                        command_preview(event.command)
                        if event.command
                        else plain(event.detail, limit=None)
                    )
                    if event.error:
                        detail += "\n" + "\n".join(
                            plain(line, limit=None) for line in event.error.splitlines()
                        )
                    self.error(detail, title=f"{label(event.name)} failed")
                    continue
                if event.command:
                    if show_tools:
                        self.print(Text(f"{label(event.name)} · {plain(event.detail, limit=None)}"))
                        self.print(Text(command_text(event.command)))
                    else:
                        self.command_summary(event)
                    continue
                self.command_summary(event)

    def help(self, registry: CommandRegistry, shortcut: Callable[[str], str] | None = None) -> None:
        """List the commands, then the keys; ``shortcut`` spells the prompt's own."""
        key = shortcut or shortcut_label
        previous = key("^") + (" (Ctrl+6)" if key("^") == "Ctrl+^" else "")
        table = Table(box=None, padding=(0, 2), show_header=False)
        table.add_column(style="pcode.accent", no_wrap=True)
        table.add_column()
        for group, commands in registry.grouped():
            table.add_row(Text(group, style="pcode.muted"), "")
            for command in commands:
                name = " ".join((command.name, *command.aliases))
                table.add_row(name, command.description)
        self.print(table)
        self.print()
        self.note("Enter send · ↓ on last line or Ctrl+J newline · Tab/↑/↓ complete")
        self.note("Enter accepts a selected completion; press again to send.")
        self.note(
            f"{key('o')} tasks widget · {key('t')} thinking · {key('g')} command output "
            "(each redraws)"
        )
        self.note(
            f"{key('l')} choose model · {key('n')} raise effort · {key('p')} lower effort "
            "(next turn)"
        )
        self.note(f"{previous} back to the previous session (/switch -)")
        self.note(f"{key('y')} copy the draft · Ctrl+C discard input · Ctrl+D exit on empty input")
        self.note(
            f"During a run: Enter sends · {key('s')} picks steering/queue/interrupt for the "
            "next send. Ctrl+C discards a draft first, then cancels · Ctrl+D cancels, keeps draft."
        )
        self.note("Cancellation clears queued messages. Use terminal/tmux scrollback for history.")
        self.print()
