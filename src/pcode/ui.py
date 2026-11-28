"""Editable prompt with replayable output in the terminal's normal scrollback."""

import asyncio
import os
import re
import unicodedata
from asyncio import Future
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import asdict, dataclass, field, replace
from functools import cache, cached_property, lru_cache, partial, wraps
from io import StringIO
from time import monotonic
from time import time as wall_time

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app, get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import merge_completers
from prompt_toolkit.cursor_shapes import ModalCursorShapeConfig
from prompt_toolkit.data_structures import Size
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Always, Condition, has_focus
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import (
    ConditionalContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.containers import Container, VerticalAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.renderer import Renderer
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Label
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from rich.spinner import SPINNERS as RICH_SPINNERS
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from pcode import dock, glyphs, kitty_spinner, smooth_scroll
from pcode.aside import typed_word
from pcode.block import INDENT, RULE, RUNNING, block_heading
from pcode.command_transcript import CommandTranscript
from pcode.commands import CommandRegistry, SlashCompleter
from pcode.delta import Delta
from pcode.delta import from_preferences as delta_from_preferences
from pcode.drafts import Drafts
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
from pcode.markdown import Markdown
from pcode.popup_ui import shortcut_hint
from pcode.preferences import (
    COMMAND_MODES,
    SETTINGS,
    SYNTAX_THEMES,
    TERMINAL_SYNTAX,
    canonical,
    load_preferences,
    parse_height,
    screen_rows,
)
from pcode.prefix_keys import PrefixKeys, shortcut_label
from pcode.prompt_keys import PromptCallbacks, prompt_key_bindings
from pcode.row_marks import marking, take_mark
from pcode.runtime import (
    CacheBust,
    CommandOutput,
    EditCompleted,
    Event,
    Message,
    Thinking,
    ToolSummary,
)
from pcode.shell_mode import SHELL_PREFIX
from pcode.syntax_colors import derive_colors
from pcode.task_prompt import TaskPrompt
from pcode.terminal_color import color_depth, true_color
from pcode.terminal_text import safe_text
from pcode.theme import (
    detect_theme,
    glide_supported,
    marks_supported,
    terminal_background,
)
from pcode.theme_gallery import SyntaxGallery
from pcode.thinking_markdown import ThinkingMarkdown
from pcode.tool_display import (
    COMMAND_TOOLS,
    EDIT_TOOLS,
    PLAN_TOOLS,
    command_preview,
    command_text,
    label,
    outcome_icon,
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
from pcode.transcript_notice import CancelledNotice, ErrorNotice, Note, WarningNotice
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


def shaded_styles(shade: str, top: str, btw_hue: str = "") -> dict[str, str]:
    """`frame_style shaded`: the editor and task boxes as surfaces, not outlines.

    `shade` fills the box. Border glyphs keep their cells in the shade's own
    color, so the geometry, and every height sum built on it, is the same in
    both styles.

    `top` is the editor's header block (see `header_shade`): its top row
    (status, task heading or session name), and while a turn runs the
    thought under it, so the turn's status and its reason read as one header
    without a drawn line.

    While the editor replies to a `/btw` thread (`btw.reply`), the surface and
    header take a warm tint toward `btw_hue` so the box visibly pairs with the
    `/btw` drawer.
    """
    # Appended after the palette's `frame.border`: for equal classes the later
    # rule wins, so these override its muted color.
    styles = {
        "shaded": f"bg:{shade}",
        # nodim: the terminal palette mutes borders with dim, which would
        # leave a faint line in the surface's own color.
        "shaded frame.border": f"nodim fg:{shade}",
        "shaded.top": f"bg:{top}",
        "shaded.top frame.border": f"nodim fg:{top}",
    }
    if btw_hue.startswith("#"):
        btw_shade = _mix(shade, btw_hue, 0.16)
        btw_top = _mix(top, btw_hue, 0.24)
        styles["btw.reply shaded"] = f"bg:{btw_shade}"
        styles["btw.reply shaded frame.border"] = f"nodim fg:{btw_shade}"
        styles["btw.reply shaded.top"] = f"bg:{btw_top}"
        styles["btw.reply shaded.top frame.border"] = f"nodim fg:{btw_top}"
    return styles


@cache
def frame_shade(background: str, theme: str) -> str:
    """A shade lifted off `background` toward the theme's contrast.

    Mixed from the terminal's actual background rather than fixed, so the box
    reads as a clear step on any scheme. Dark backgrounds need a larger RGB
    mix than light ones: near black, sRGB compression makes small linear steps
    nearly invisible.
    """
    if theme == "light":
        return _mix(background, "#000000", 0.055)
    return _mix(background, "#ffffff", 0.125)


@cache
def header_shade(background: str, theme: str) -> str:
    """The editor's header block: a step darker than the box, much the same
    step to the eye on either theme.

    Dark, roughly halfway back to the background, so all three zones
    (scrollback, header/thinking, tasks/editor) keep roughly even perceptual
    separation from pure black through mid-dark schemes. Light, a step further
    toward black than the box.
    """
    shade = frame_shade(background, theme)
    if theme == "light":
        return _mix(shade, "#000000", 0.045)
    return _mix(shade, background, 0.48)


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
                # A /btw thread's heading in the drawer (`pcode.btw_tail`), in
                # the hue the editor's box takes while it replies to the thread.
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
                # The tool rows above the editor box: upright where the thought is
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
                "btw.badge": f"nodim bold reverse {self.agents[1]}",
                "btw.heading": f"nodim bold {self.agents[1]}",
                "btw.prompt": f"nodim bold {self.agents[1]}",
                "btw.hint": self.muted,
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
                "bottom-toolbar.btw": f"nodim bold {self.agents[1]}",
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
                **(shaded_styles(*shades, self.agents[1]) if shades is not None else {}),
                **kitty_spinner.STYLE_CLASSES,
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
        "pcode.muted": "dim default",
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
        "markdown.hr": "dim default",
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
# Fast settle sweep along the editor's top border once startup finishes.
STARTUP_SETTLE_SECONDS = 0.35
STARTUP_FRAME_SECONDS = 0.04


# The status row is redrawn several times a second while a turn runs, so a
# longer gap means the row went away; its phase clock starts over.
PHASE_GAP_SECONDS = 1.0
# Least time the status row keeps what it says before saying something else.
# A turn can change phase or tool several times a second, and a row that
# rewrites itself that fast cannot be read. Once the hold is up the row jumps
# to whatever is current, skipping anything that came and went meanwhile.
STATUS_HOLD_SECONDS = 2.5
# Least time a thought stays on the thinking rows once it stops changing. The
# answer often starts streaming a moment after the thinking that led to it, and
# clearing the rows right then made a block of up to ten rows flash and vanish.
THOUGHT_HOLD_SECONDS = STATUS_HOLD_SECONDS
# How long a command's output must stream before its live preview opens. Most
# commands (grep, git status, ls) finish well inside this, and showing their
# output the moment it arrived made a block of up to a dozen rows flash and
# vanish when they settled. Their result is already in the tool row and
# scrollback. A watched job skips the wait: the user asked to see it.
COMMAND_PREVIEW_DELAY_SECONDS = 1.0
# How long a shrunk layout's blank rows must hold before scrollback is rebuilt
# to fill them (`PromptLayout.refill_after_shrink`), so deleting a tall draft
# line by line rebuilds once rather than per line.
REFILL_DELAY = 0.3
# Streamed deltas redraw the live panel at most this often (`redraw_streamed`).
STREAM_REDRAW_SECONDS = 1 / 20
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
# Opens every thought row, in the spinner's column: a quote, as the thought
# is the model's own words, and nothing like a frame of the braille spinner
# right above it (`∴` was, on screenshots).
THOUGHT_ICON = "❝"
# The tool row's stand-ins for common verbs (`tool_display.LABELS`), so it
# says `$ make test` rather than `Run shell · make test`. A tool not listed
# keeps its verb spelled out. Each is one cell wide, ambiguous-width or not.
# ASCII, in every font, so `tool_glyphs off` keeps it.
# A wait on a job is a shell command still running, so it shares `$`: the row
# then reads like the foreground run it stands in for (`$ j3 · make e2e`).
SHELL_LABELS = ("Run shell", "Wait for shell", "Run code", "Start command", "Check command")
PLAIN_TOOL_GLYPHS = dict.fromkeys(SHELL_LABELS, "$")
TOOL_GLYPHS = {
    **PLAIN_TOOL_GLYPHS,
    **dict.fromkeys(("Search code", "Find files", "List directory", "Find tools"), "⌕"),
    **dict.fromkeys(("Edit file", "Write file"), "✎"),
    **dict.fromkeys(("Read file", "Read results", "Read job output", "File info"), "⎘"),
}
_TOOL_ICON_SLOTS = {
    **dict.fromkeys(SHELL_LABELS, "shell"),
    **dict.fromkeys(("Search code", "Find files", "List directory", "Find tools"), "search"),
    **dict.fromkeys(("Edit file", "Write file"), "edit"),
    **dict.fromkeys(("Read file", "Read results", "Read job output", "File info"), "read"),
}


def tool_glyphs_on(setting: str) -> bool:
    """Whether the `tool_glyphs` setting (`auto`, `on`, `off`) draws the symbols.

    Almost no monospace font has `⌕` or `⎘`: outside Nerd Font mode the
    terminal draws them from a fallback font, which on macOS and most desktops
    looks fine and on the Linux console is a box, so `auto` leaves them off
    where `TERM` says it is that console (tmux on it says otherwise). A
    terminal cannot say which fonts it has, so anywhere else a missing symbol
    is the user's to turn off.
    """
    if setting == "auto":
        return os.environ.get("TERM") != "linux" and not glyphs.active()
    return setting == "on"


def _tool_glyph(label: str, setting: str) -> str | None:
    if not tool_glyphs_on(setting):
        return PLAIN_TOOL_GLYPHS.get(label)
    if slot := _TOOL_ICON_SLOTS.get(label):
        return glyphs.icon(slot)
    return None


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
    name = kitty_spinner.rich_fallback(load_preferences().get(setting, SETTINGS[setting].default))
    frames = RICH_SPINNERS.get(name, {}).get("frames", "")
    if glyphs.active() and not "".join(frames).isascii():
        # Stand-ins would freeze it on one character; keep the two kinds of
        # wait distinct with two different ASCII spinners.
        name = ASCII_SPINNERS[setting]
    spinner = Spinner(name)
    if glyphs.nerd():
        spinner.frames = glyphs.NERD_SPINNERS[setting]
        spinner.interval = 100
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

    def fragments(
        self, spinner: str | tuple[str, str], width: int, *, rule: bool = False
    ) -> list[tuple[str, str]]:
        """The row, `width` cells wide when it has meta; `rule` draws the gap as border."""
        if width < 1:
            return []
        head = (
            [spinner, ("class:activity.spinner", " ")]
            if isinstance(spinner, tuple)
            else [("class:activity.spinner", f"{spinner} ")]
        )
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


@dataclass
class _HeldThought:
    """The thought on the thinking rows, and what decides how long it may stay."""

    text: str  # What the rows show (`latest_thought`).
    since: float  # When this text first showed.
    seen: float  # Last drawn.
    turn: tuple[str, str]  # The prompt it belongs to.


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
    return (*call_parts(call), call.change, call.failed, call.exit_code, call.settled is None)


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
    fragments = [
        (style, text if style.startswith("class:kitty.spinner.") else plain(text, limit=None))
        for style, text in fragments
    ]
    if sum(cell_len(text) for _, text in fragments) <= width:
        return fragments
    fitted = []
    remaining = max(0, width)
    for style, text in fragments:
        width_cells = cell_len(text)
        # Kitty spinner placeholders are atomic multi-cell images: never crop
        # them in half or append an ellipsis styled with the image-ID color.
        if style.startswith("class:kitty.spinner."):
            if width_cells <= remaining:
                fitted.append((style, text))
                remaining -= width_cells
                if remaining > 0:
                    continue
            break
        # Strictly less: the fragment that overflows needs a cell for the ellipsis.
        if width_cells < remaining:
            fitted.append((style, text))
            remaining -= width_cells
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
    # The activity panel: the newest thought, the turn's latest tool calls,
    # the task list and a running command's output, shown and hidden together
    # (Ctrl+O). The status row is not part of it: it says the turn is running
    # whatever the panel shows. Each part has its own switch as well
    # (`show_tasks`, `/show-thinking`, `/show-commands`).
    show_activity: bool = True
    # The task list's own switch, inside the panel.
    show_tasks: bool = True
    # Hide the task list again as soon as a turn ends, without forgetting that the
    # user wants it shown while the model works.
    autohide_tasks: bool = False
    # Draw the task list as a section of the editor box instead of its own box.
    attach_tasks: bool = True
    # Draw the thought in the editor box, under its status row, instead of
    # above the box with the tool rows (see `PromptLayout.band_rows`).
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
    # Cap on the editor box, the panel in it included, plus any detached task
    # box: whole rows, or a share of the screen below 1 (0.5 is half). None
    # keeps the default layout.
    tasks_max_height: float | None = None
    tasks_autohidden: bool = False
    # Hide the panel in a pane shorter or narrower than these (0 never hides),
    # so one preference suits a full screen and a split alike. Each terminal
    # measures its own pane: see `fit_screen`.
    tasks_min_rows: int = 0
    tasks_min_columns: int = 0
    screen_small: bool = False
    # Ctrl+O showed the panel in a small pane; dropped when the pane crosses
    # the threshold, so the next split or unsplit decides afresh.
    tasks_unhidden: bool = False
    # Inline shortcut hints, such as the key that hides the task list.
    show_hints: bool = True
    # Where the model's thinking shows: `off`, `activity-panel` (its own rows
    # in the activity panel), or `scrollback`. See THINKING_MODES.
    thinking_mode: str = "activity-panel"
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
    # The /btw thread open in the drawer (`pcode.btw_view`), which the editor
    # then sends to; None while none is.
    # This terminal's own, never synced from the host.
    btw_thread: str | None = None
    # The thread last shown, which Ctrl+Q brings back from the conversation.
    btw_last: str | None = None
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
    # The thought on the thinking rows while a hold keeps it. Drawing state too.
    _held_thought: _HeldThought | None = field(default=None, init=False, repr=False, compare=False)
    # The turn's latest call on the tool row, kept there once it finishes.
    _last_call: ToolCall | None = field(default=None, init=False, repr=False, compare=False)
    # Startup animation state in the editor's top border: non-empty while
    # connecting or initializing, then a brief settle sweep once ready.
    startup_label: str = field(default="", init=False, repr=False, compare=False)
    startup_started: float = field(default=0.0, init=False, repr=False, compare=False)
    startup_done_at: float = field(default=0.0, init=False, repr=False, compare=False)
    startup_ok: bool = field(default=True, init=False, repr=False, compare=False)

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
        """Drop the thought: the answer it led to is streaming now.

        The rows may keep drawing it a moment longer (`shown_thought`).
        """
        self.thought, self.thought_done = "", True

    def shown_thought(self) -> str:
        """The thought the thinking rows show, kept at least `THOUGHT_HOLD_SECONDS`.

        A thought shows as it streams. Once forgotten (the answer started) it
        stays until it has been on screen, unchanged, for the hold, so a block
        that arrives right before its answer can be read instead of flashing.
        The hold gives way at once to a new thought, a new turn, the end of
        the turn, a hidden panel, and a gap in drawing.
        """
        if not (self.thinking_mode == "activity-panel" and self.status_shown and self.panel_shown):
            self._held_thought = None
            return ""
        text, now = latest_thought(self.thought), monotonic()
        turn = (self.prompt_kind, self.prompt)
        held = self._held_thought
        if text.strip():
            if held is None or held.text != text or held.turn != turn:
                self._held_thought = _HeldThought(text, now, now, turn)
            else:
                held.seen = now
            return text
        if (
            held is None
            or held.turn != turn
            or now - held.seen > PHASE_GAP_SECONDS
            or now - held.since >= THOUGHT_HOLD_SECONDS
        ):
            self._held_thought = None
            return ""
        held.seen = now
        return held.text

    @property
    def thought_shown(self) -> bool:
        """Whether the thinking rows have a thought to show in the activity panel."""
        return bool(self.shown_thought())

    def thought_fragments(
        self, width: int, rows: int = THOUGHT_ROWS, thought: str | None = None
    ) -> list[list[tuple[str, str]]]:
        """The thinking rows of the activity panel, in `activity-panel` mode.

        Held through the tool calls that follow a thought, which it usually
        explains, and gone once the answer streams into scrollback (after
        `THOUGHT_HOLD_SECONDS` on screen): thinking is the lead-up, not a
        caption on the result.

        Every row opens with the quote mark, as a quoted passage's lines do,
        so a wrapped thought never reads as two things; the marker stays
        upright beside the italic text.

        `thought` is `shown_thought()` as the caller already read it this
        render, so the rows agree with the space it budgeted for them.
        """
        if thought is None:
            thought = self.shown_thought()
        if not thought or width < 3:
            return []
        lead = f"{glyphs.icon('thought')} "
        return [
            # lstrip: a cut row in a very narrow pane can open on the space
            # its ellipsis was set after.
            [("class:activity.thinking.icon", lead), ("class:activity.thinking", row.lstrip())]
            for row in head_rows(thought, width - cell_len(lead), rows)
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

    def begin_startup(self, label: str) -> None:
        """Show a startup animation in the editor's top border without adding rows."""
        self.startup_label = label
        if not self.startup_started:
            self.startup_started = monotonic()
        self.startup_done_at = 0.0
        self.startup_ok = True
        _invalidate()

    def finish_startup(self, *, ok: bool = True) -> None:
        """Transition the startup border into its brief settle sweep (or clear on error)."""
        self.startup_label = ""
        self.startup_ok = ok
        self.startup_done_at = monotonic() if ok else 0.0
        _invalidate()

    @property
    def startup_animating(self) -> bool:
        if self.startup_label:
            return True
        if self.startup_done_at > 0:
            if monotonic() - self.startup_done_at < STARTUP_SETTLE_SECONDS:
                return True
            self.startup_done_at = 0.0
        return False

    def wait_fragments(self, spinner: str | tuple[str, str], width: int):
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
        if isinstance(spinner, tuple):
            rest = Text(f" {SYSTEM_BADGE} {plain(wait.label, limit=None)}")
            rest.append(f" \u00b7 {now - shown[0].started:.0f}s")
            rest.truncate(max(0, width - cell_len(spinner[1])), overflow="ellipsis")
            return [spinner, ("class:activity.system", rest.plain)]
        text = Text(f"{spinner} {SYSTEM_BADGE} {plain(wait.label, limit=None)}")
        text.append(f" \u00b7 {now - shown[0].started:.0f}s")
        text.truncate(width, overflow="ellipsis")
        return [("class:activity.system", text.plain)]

    @property
    def asides_running(self) -> bool:
        return any(aside.running for aside in self.asides)

    def aside_rows(self, spinner: str | tuple[str, str], width: int, budget: int = ASIDE_ROWS):
        """One muted spinner row per running side question, folded to the budget."""
        running = [aside for aside in self.asides if aside.running]
        if budget <= 0 or not running or width < 1:
            return []
        shown = running if len(running) <= budget else running[: max(0, budget - 1)]
        rows = []
        for aside in shown:
            if isinstance(spinner, tuple):
                parts = ["btw", plain(aside.question, limit=None)]
                if aside.label:
                    parts.insert(1, aside.label)
                if aside.activity:
                    parts.append(plain(aside.activity, limit=None))
                parts.append(f"{aside.elapsed:.0f}s")
                # An inset spinner's right cell is its own gap, which keeps the
                # text in line with the tool rows' above it.
                gap = "" if spinner[1] == kitty_spinner.INSET_ICON else " "
                rest = Text(gap + " \u00b7 ".join(parts))
                rest.truncate(max(0, width - cell_len(spinner[1])), overflow="ellipsis")
                rows.append([spinner, ("class:activity.aside", rest.plain)])
            else:
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
        # Nor on its thought, held or not: the same prompt queued again keeps
        # the turn key the hold compares.
        self._held_thought = None

    def height_cap(self, rows: int) -> int | None:
        """The editor box's row limit, panel included, on a screen this tall."""
        cap = self.tasks_max_height
        return None if cap is None else screen_rows(cap, rows)

    def fit_screen(self, columns: int, rows: int) -> None:
        """Note whether this pane is below the panel's size thresholds."""
        small = rows < self.tasks_min_rows or columns < self.tasks_min_columns
        if small != self.screen_small:
            self.screen_small = small
            self.tasks_unhidden = False

    @property
    def panel_shown(self) -> bool:
        """The activity panel is visible: enabled, and the pane fits."""
        return self.show_activity and not self.tasks_too_big

    @property
    def tasks_shown(self) -> bool:
        """The task list is visible: the panel is, the list is switched on, and it
        is not auto-hidden after the last turn."""
        return self.panel_shown and self.show_tasks and not self.tasks_autohidden

    @property
    def tasks_too_big(self) -> bool:
        """The pane is below the size thresholds and Ctrl+O has not overridden them."""
        return self.screen_small and not self.tasks_unhidden

    @property
    def panel_looks_shown(self) -> bool:
        """The panel is on screen with nothing of it auto-hidden: what Ctrl+O
        reads, so after a turn auto-hides the tasks it brings them back
        rather than hiding a panel that shows nothing of them."""
        hidden = (
            self.tasks_autohidden
            and self.show_tasks
            and bool(self.displayed_plan or self.tools.delegates)
        )
        return self.panel_shown and not hidden

    def toggle_panel(self) -> bool:
        """Ctrl+O acts on what is on screen (`panel_looks_shown`).

        In a small pane it only overrides the size rule for that pane, leaving
        `show_activity` on so a larger pane still shows the panel. Showing the
        panel brings auto-hidden tasks back with it.
        """
        shown = self.panel_looks_shown
        self.tasks_autohidden = False
        if self.screen_small and self.show_activity:
            self.tasks_unhidden = not shown
            return self.show_activity
        self.show_activity = not shown
        self.tasks_unhidden = self.show_activity and self.screen_small
        return self.show_activity

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

    def plan_rows(
        self, budget: int, spinner: str = ACTIVE_TASK_ICON, agent_spinner: str | None = None
    ):
        """`spinner` marks the step in progress while a turn runs.

        `agent_spinner`, when set, replaces each running sub-agent's `»`.
        """
        if not self.tasks_shown:
            return []
        # Persisted task status describes unfinished work, not a live request.
        # Use the turn lifecycle rather than busy, which also includes queued input.
        icon = spinner if self.status_shown else glyphs.icon("pending")
        # A configured height is room the user asked the tasks to fill.
        max_tasks = TASK_ROWS if self.tasks_max_height is None else budget
        return task_panel_rows(
            self.displayed_plan, self.tools, budget, icon, max_tasks, agent_spinner
        )

    def hidden_step(self) -> str:
        """The active plan step, when the task panel is not showing it.

        Its present-continuous form (`Adding the migration`), else its text:
        `active_form` is optional, and the status row's detail is a gerund.
        Judged by the widget's visibility alone: the row is sized from
        activity state, so a preview that squeezes the plan rows out of a
        short pane does not count as hiding it.
        """
        if self.tasks_shown:
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
            # With the tasks hidden, the plan's step says what the call is for.
            detail = self.hidden_step()
            if not self.panel_shown:
                # The tool rows went with the panel, so a lone call's own
                # detail says what runs.
                detail = (call_parts(call)[1] if running < 2 else "") or detail
            return StatusLine(phase, detail, tally=tally, elapsed=call.elapsed, call=call)
        clock, hue = phase, None
        if agents := self.tools.delegates:
            # The task list shows the agents themselves, so this row just says
            # the turn is waiting on them, unless the list is hidden. One
            # clock for the whole wait, however the count changes.
            count = len(agents)
            phase = f"Waiting for {count} agent{'s' * (count > 1)}"
            if not self.tasks_shown:
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
        """The activity panel's tool rows, above the editor box: `◜ $ ls -la`.

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
        self,
        call: ToolCall,
        line: StatusLine,
        width: int,
        spinner: str | tuple[str, str],
        repeats: int = 1,
    ) -> list[tuple[str, str]]:
        """One call's row: its state mark, then its verb's glyph (`◜ $ ls -la`).

        Every row has the same columns, in the thought mark's column or with
        none, so parallel calls line up and nothing shifts as a thought comes
        and goes: the mark (the tool spinner's frame in the muted shade while
        it runs, `✓` or `✗` once settled), then the glyph (`TOOL_GLYPHS`). A
        verb with no glyph is spelled out whenever the status row does not
        say it for this call.
        """
        label, detail = call_parts(call)
        glyph = _tool_glyph(label, self.tool_glyphs)
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
            mark = spinner if isinstance(spinner, tuple) else ("class:activity.tool", spinner)
        elif call.failed:
            mark = ("class:activity.tool.failed", glyphs.icon("fail"))
        elif call.exit_code:
            mark = ("class:activity.tool.done", glyphs.icon("nonzero"))
        else:
            mark = ("class:activity.tool.done", glyphs.icon("tool_done"))
        # A wide spinner (`dots12`) would push running rows' text past
        # settled ones'; pad the settled mark's column to its width.
        spinner_width = cell_len(spinner[1] if isinstance(spinner, tuple) else spinner)
        pad = "" if call.settled is None else " " * max(0, spinner_width - 1)
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
                "provider_wake": "Continuing",
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
        # Not flushed: the hide goes out inside the handoff's synchronized
        # frame (its erase flushes). Flushed on its own, it reached the
        # terminal ahead of the frame, possibly a cursor report round trip
        # early, and iTerm2 blinked the cursor out on every row written.
        self.output.hide_cursor()
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
    # SPIKE: a docked editor is on its own terminal; scrollback never covers it.
    # Editor-only, scrollback goes nowhere, but a popup or program taking the
    # terminal takes the editor's: that handoff is a real one.
    docked = dock.active()
    if (
        not isinstance(app, Application)
        or not app._is_running
        or (docked is not None and (atomic or not docked.owns_tty))
    ):
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
            # Restore whatever handler was installed (`install_resize_repaint`).
            resize = vars(app).get("_on_resize")
            app._on_resize = lambda: None
            try:
                with app.input.detach(), nullcontext() if atomic else app.input.cooked_mode():
                    yield handoff
            finally:
                if resize is None:
                    del app._on_resize
                else:
                    app._on_resize = resize
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


def install_resize_repaint(app: Application) -> None:
    """Repaint the editor once per resize, after the cursor report.

    prompt_toolkit's ``_on_resize`` erases and repaints before the report it
    has just requested arrives, the same race ``suspended_editor`` avoids for
    scrollback writes: the paint knows nothing about the rows below the cursor,
    so the editor lands directly under the transcript, part way up the pane,
    until the report re-renders it at the bottom ~1/30 s later. Routing the
    resize through an atomic handoff erases, waits for the report, and paints
    once, with the cursor hidden for the round trip.

    Rendering is held from the signal on, not just from the handoff's erase:
    the handoff first collects any report a streaming flush left outstanding,
    and a spinner paint in that wait would land at the new size with stale
    free rows (the jump again) and overwrite ``_last_size``, which
    ``ReflowAwareRenderer.erase`` needs at the old width. A handoff already in
    flight repaints at the terminal's size when it exits, so the repaint just
    waits for it and only runs its own if the size still differs.

    prompt_toolkit also polls the size every half second, comparing against a
    local copy SIGWINCH never updates, so one resize calls ``_on_resize`` again
    up to 500 ms later, often just after the resize replay. A size the editor
    was last painted at is ignored, and a resize storm (a border drag)
    coalesces into the running repaint, which repeats until the size holds.

    Must run before the app starts: SIGWINCH binds ``app._on_resize`` then.
    """
    task: asyncio.Task | None = None
    held = False

    def in_flight() -> Future[None] | None:
        previous = app._running_in_terminal_f
        return previous if previous is not None and not previous.done() else None

    async def repaint() -> None:
        nonlocal task, held
        try:
            while app._is_running:
                if (previous := in_flight()) is not None:
                    # It clears the hold and repaints on exit. Shielded: a
                    # cancel here must not cancel the handoff's own future.
                    await asyncio.shield(previous)
                    held = False
                    continue
                if app.output.get_size() == app.renderer._last_size:
                    break
                with getattr(app.output, "hidden_cursor", nullcontext)():
                    async with suspended_editor(app, atomic=True):
                        held = False
                if app.renderer._last_size is None:
                    break  # Nothing painted (the app is exiting); don't spin.
        finally:
            task = None
            if held:
                held = app._running_in_terminal = False
                app.invalidate()

    def on_resize() -> None:
        nonlocal task, held
        if task is not None or app.output.get_size() == app.renderer._last_size:
            return
        if in_flight() is None:
            held = app._running_in_terminal = True
        task = app.create_background_task(repaint())

    app._on_resize = on_resize


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


def install_nowrap_editor(app: Application) -> None:
    """Keep autowrap off while the editor is painted, so Ghostty never reflows it.

    Ghostty reflows the screen before the app sees SIGWINCH. Narrowing it
    splits each of the editor's full-width rows (frame borders, status line)
    in two, and it shows that frame until the resize repaint lands a cursor
    report later: the top of the editor jumps up and back. With autowrap
    (DECAWM) off, Ghostty resizes without reflowing at all, cutting rows at
    the new width instead. prompt_toolkit already paints with it off and
    turns it back on after each paint; that is held off until the renderer
    resets (every erase), so transcript written between an erase and the
    next paint still wraps. Only with resize replay on: the replay restores
    the transcript the cut hid.
    """
    renderer, output = app.renderer, app.output
    reset, render = renderer.reset, renderer.render
    enable = output.enable_autowrap
    held = False

    def hold_enable() -> None:
        if not held:
            enable()

    def nowrap_render(app, layout, is_done: bool = False) -> None:
        nonlocal held
        held = not is_done
        render(app, layout, is_done)

    def wrapping_reset(*args, **kwargs) -> None:
        nonlocal held
        reset(*args, **kwargs)
        if held:
            held = False
            enable()
            output.flush()

    output.enable_autowrap = hold_enable
    renderer.render = nowrap_render
    renderer.reset = wrapping_reset


# Frames a paced backlog takes to drain, so a settled block rolls out row by
# row when small and lands within about a second however big it is.
PACED_DRAIN_FRAMES = 30
# Typed prose advances every TYPED_STEP_FRAMES frames (every frame: 30 a
# second). A step reveals TYPED_CHARS_PER_STEP characters (about 270 a second,
# a little under a model's own pace, so each line reads as it lands), sped up
# so a backlog types out within TYPED_DRAIN_STEPS steps (about two seconds).
TYPED_STEP_FRAMES = 1
TYPED_CHARS_PER_STEP = 9
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
        if isinstance(obj, RichMarkdown):
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
    # The reader-mode mark on this row (`smooth_scroll.MARK_*`), if any, and
    # whether to arm a park before it: the prompt of a turn just submitted.
    mark: int | None = None
    park: bool = False

    @cached_property
    def _plain(self) -> str:
        return ESCAPES.sub("", self.text).rstrip()

    @property
    def visible(self) -> int:
        return len(self._plain)

    @property
    def lead(self) -> int:
        return len(self._plain) - len(self._plain.lstrip())


@dataclass
class Pacer:
    """How fast queued rows come out, frame by frame; see ``advance``.

    The rates are fixed when rows arrive rather than recomputed as they
    leave, or the tail of a backlog would crawl. ``reset`` once it drains.
    """

    reveal_rate: int = 0
    type_rate: int = 0
    # Frames skipped since the last typing step; see TYPED_STEP_FRAMES.
    skipped: int = 0

    def reset(self) -> None:
        self.reveal_rate = self.type_rate = self.skipped = 0

    def advance(self, rows: list[QueuedRow], typed: int) -> tuple[int, int]:
        """One paced frame: whole rows now due, and characters shown of the next.

        ``typed`` is how much of ``rows[0]`` is already shown. Rows roll in one
        at a time and prose types out a few characters a step; both speed up so
        a backlog never falls far behind. Blank rows are free, so the gap
        between paragraphs does not stall the reveal, and so is a typed row's
        indentation.
        """
        self.reveal_rate = max(self.reveal_rate, -(-len(rows) // PACED_DRAIN_FRAMES))
        if typed and rows[0].typed and self.skipped < TYPED_STEP_FRAMES - 1:
            # Mid-row and between steps: nothing moves this frame.
            self.skipped += 1
            return 0, typed
        self.skipped = 0
        backlog = sum(row.visible for row in rows if row.typed) - typed
        self.type_rate = max(
            self.type_rate,
            TYPED_CHARS_PER_STEP,
            -(-backlog // TYPED_DRAIN_STEPS),
        )
        reveal, chars = self.reveal_rate, self.type_rate
        due = 0
        for row in rows:
            if row.typed:
                typed = max(typed, row.lead)
                if row.visible - typed > chars:
                    return due, typed + chars
                chars -= row.visible - typed
                typed = 0
            elif row.visible:
                if not reveal:
                    break
                reveal -= 1
            due += 1
        return due, 0


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
        self._stream_redraw: tuple[asyncio.AbstractEventLoop, asyncio.TimerHandle] | None = None
        self.commit_user = self.user
        self.commit_message = self.message
        self.commit_thinking = lambda text: self.print(
            ThinkingMarkdown(text, code_theme=self.code_theme(), style="dim"), end=""
        )
        self._thinking_tail = ""
        self._thinking_streamed = False
        # Whether the current thought streamed at all, across mid-thought commits.
        self._thought_streamed = False
        self._regenerate = None
        self.resize_replay = None
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
        self._pacer = Pacer()
        # Transient writes already rendered into ``rows`` but not yet written,
        # with the absolute index just past their last row. A replay drops the
        # queued rows, so these must join it, just as ``transient_pending`` does.
        self._queued_transient: list[tuple[tuple, int]] = []
        # Absolute index of ``rows[0]``: rows written or dropped so far.
        self._row_base = 0
        # Experimental glide (`pcode.smooth_scroll`). The typing row is then
        # always reserved, the glide region's last visible row, with a blank
        # reserved row below it as the region's hidden bottom row. Settled
        # rows go to the terminal in batches (`_native_batch`), which paces
        # them itself. ``_declared`` is the region last sent and when (None:
        # cleared).
        self.gliding = False
        # SPIKE: the same glide with the editor docked on its own terminal
        # (`pcode.dock`): pcode's pane then holds only scrollback, so the
        # region is the whole pane and each batch glides in the frame that
        # writes it (`_docked_frame`), with no editor render to hook.
        self.docked_glide = False
        self._docked_declared: tuple[tuple[int, int], float] | None = None
        # The batch just written, `(rows, rows scrolled, runs)`, for
        # `_track_layout` to announce in the same frame; see `_native_runs`.
        self._native_commit: tuple[int, int, list[tuple[int, int, bool]]] | None = None
        # pcode's estimate of the terminal's batch glide still to run.
        self._native_backlog = 0.0
        self._native_rate = smooth_scroll.BATCH_MIN_RATE
        self._native_at = 0.0
        # The layout's drawn height and whether it was pinned (no filler rows),
        # after the last render; and whether that render was a handoff's, whose
        # scroll `flush` already sent.
        self._last_layout: tuple[int, bool] | None = None
        # Each drawn window's top and bottom row, counted up from the screen's
        # bottom edge (its last row is 1), after the last render.
        self._last_spans: dict[int, tuple[int, int]] = {}
        # The live panel's rows as `_row_keys` reads them, after the last render.
        self._last_keys: dict[int, str] = {}
        self._handoff_render = False
        self._declared: tuple[tuple[int, int], float] | None = None
        # The live panel's rows moving (`_glide_moves`, `_glide_panel`): pcode's
        # copy of that glide, and whether its region is declared.
        self._panel_glide = smooth_scroll.Glide()
        self._panel_declared = False
        # Reader mode (protocol 10): write each row's mark, and arm a park
        # before the next submitted prompt (`begin_turn`) once it is rendered.
        self.marks = False
        self.park = False
        # The submitted prompt's write, whose turn row takes the park.
        self._park_entry = None
        # The turn ended: once its last rows are written, end its park.
        self._park_off = False
        # Whether the pending replay keeps a reader's place (`regenerate`), and
        # the marks written since the screen was last cleared, to tell the
        # terminal how many a replay adds (`smooth_scroll.replay_end`).
        self._same_content = False
        self._marks_written = 0
        # Editor-only (`dock.EditorOnly`): nobody sees this transcript, so
        # queued writes, replays included, are dropped unrendered.
        self.discard = False

    def regenerate(self, replay, *, same_content: bool = False) -> None:
        """Clear the screen and write `replay()` on the next flush.

        `same_content` says the replay rewrites what is there at another
        size (a resize), so a reader scrolled back can be kept at their
        place. A display toggle or a restored session moves the marks that
        place is found by.
        """
        # Coalesce requests. Snapshot only inside the handoff so arrivals during
        # the CPR await are included exactly once, including queued writes.
        self._same_content = same_content and (self._regenerate is None or self._same_content)
        self._regenerate = replay
        self.changed.set()

    def print(self, *objects, end="\n", transient=False) -> None:
        entry = (objects, end, False)
        self.pending.append(entry)
        if transient:
            self.transient_pending.append(entry)
        self.changed.set()

    def begin_turn(self, prompt: str) -> None:
        # The live row shows the running tool, not the prompt, so the quote goes
        # to scrollback as soon as the turn starts rather than waiting for output.
        self._park_off = False
        start = len(self.pending)
        self.commit_user(prompt)
        # The park belongs to this prompt's own write, not to whichever turn
        # row is queued next (a steering prompt still pending, say).
        self._park_entry = (
            next(
                (
                    entry
                    for entry in self.pending[start:]
                    if any(isinstance(obj, TaskPrompt) for obj in entry[0])
                ),
                None,
            )
            if self.park
            else None
        )

    def user(self, prompt: str) -> None:
        self.print()
        self.print(TaskPrompt(prompt))
        self.print()

    def message(self, source: str) -> None:
        self.print(Markdown(safe_text(source), code_theme=self.code_theme()))
        self.print()

    def end_turn(self) -> None:
        self.finish_thinking(complete=True)
        self.finish()
        self._park_off = self.park

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
        self._thought_streamed = True
        for part in text.splitlines(keepends=True):
            self._thinking_tail += part
            if part.endswith("\n"):
                self._commit_blocks(thinking=True)

    def finish_thinking(self, fallback: str = "", *, complete: bool = False) -> None:
        """Commit the thought so far; `complete` means the thought itself has ended.

        A scrollback write in the middle of a thought commits what streamed
        before it, so the completion's `fallback` must not repeat the thought.
        """
        if not self._thought_streamed and fallback:
            self.thinking_delta(fallback)
        if self._thinking_streamed:
            self._commit_thinking(self._thinking_tail.rstrip("\n") + "\n\n")
        self._thinking_tail = ""
        self._thinking_streamed = False
        if complete:
            self._thought_streamed = False

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

    def redraw_streamed(self) -> None:
        """Redraw for a streamed delta, no sooner than `STREAM_REDRAW_SECONDS` after the last frame.

        A fast stream sends a delta every few milliseconds, and each frame
        re-renders the whole layout. Within the interval one redraw is
        scheduled for its end, and skipped if anything else (the pacer, a
        spinner, a keystroke) has drawn the delta by then.
        """
        app = self.app
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if not isinstance(app, Application) or loop is None or loop is not app.loop:
            app.invalidate()
            return
        # One pending at a time, unless it was left on a loop that stopped.
        if self._stream_redraw is not None and self._stream_redraw[0] is loop:
            return
        drawn = app._last_redraw_time
        # Clamped: a wall clock stepping back must not hold redraws for long.
        wait = min(drawn + STREAM_REDRAW_SECONDS - wall_time(), STREAM_REDRAW_SECONDS)
        if wait <= 0:
            app.invalidate()
            return

        def due() -> None:
            self._stream_redraw = None
            if app._last_redraw_time == drawn:
                app.invalidate()

        self._stream_redraw = (loop, loop.call_later(wait, due))

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
        transient = [] if replay else self.transient_pending
        self.transient_pending = []
        if replay:
            # A submitted prompt not yet written is in the replay: no park for it.
            self._park_entry = None
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
                    self._queue_row(text, typed, entry)
                    text, typed = "", True
            if any(entry is note for note in transient):
                # Its last character is in the open row, or the one just closed.
                last = len(self.rows) + (1 if text else 0)
                self._queued_transient.append((entry, self._row_base + last))
        if text:
            self._queue_row(text, typed, pieces[-1][0])

    def _queue_row(self, text: str, typed: bool, entry) -> None:
        """Queue one rendered row, its reader-mode sentinel taken out into ``mark``."""
        text, mark = take_mark(text)
        park = (
            self._park_entry is not None
            and entry is self._park_entry
            and mark == smooth_scroll.MARK_TURN
        )
        if park:
            self._park_entry = None
        self.rows.append(QueuedRow(text, typed, mark, park))

    def _row_text(self, row: QueuedRow) -> str:
        """A row as written to scrollback, led by its reader-mode mark when on."""
        if not self.marks or row.mark is None:
            return row.text
        lead = smooth_scroll.PARK if row.park and self.park else ""
        return lead + smooth_scroll.mark(row.mark) + row.text

    def _render(self, entries: list, width: int) -> list[str]:
        """Each write rendered on its own, as terminal text at ``width``."""
        prefetch_edits((obj for objects, _, _ in entries for obj in objects), width)
        pieces = []
        previous_file = self.console._file
        try:
            # Only a terminal that takes marks gets sentinels: everyone else
            # renders exactly as before.
            with self.console.use_theme(self.rich_theme()), marking(self.marks):
                for objects, end, soft_wrap in entries:
                    self.console.file = rendered = StringIO()
                    with self.console:
                        self.console.print(*objects, end=end, soft_wrap=soft_wrap, width=width)
                    pieces.append(rendered.getvalue())
        finally:
            self.console.file = previous_file
        return pieces

    def _take_pending(self) -> list:
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
        """One paced frame: whole rows now due, and characters shown of the next."""
        return self._pacer.advance(self.rows, self._typed)

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
        if self.discard:
            # A replay's function only renders: the live state it resets is current.
            self.pending.clear()
            self.transient_pending = []
            self._regenerate = None
            self._written(len(self.rows))
            self.changed.clear()
            return
        async with self.lock:
            paced = self.paced and not drain
            due = typed = 0
            if self.gliding and (drain or self._regenerate is not None):
                # A popup or a rebuilt screen must not be drawn gliding.
                self._reset_native_state()
                self._declare(None)
            # The layout's drawn height before this write, to work out how far
            # the write scrolls the screen (see `_scrolled`).
            drawn = self._drawn_height() if self.gliding and self._regenerate is None else None
            if paced and self._regenerate is None:
                # Decide what this frame reveals before paying for a handoff: a
                # frame that only types further repaints the live row instead.
                if self.pending:
                    width = max(1, self._screen_size().columns)
                    self._enqueue(self._take_pending(), width)
                if self.gliding or self.docked_glide:
                    # The terminal paces and reveals the batch itself.
                    due = self._native_batch()
                elif self.rows:
                    due, typed = self._advance()
                if not due and typed != self._typed:
                    self._typed = typed
                    self.app.invalidate()
            if self._regenerate is not None or due or not paced and (self.pending or self.rows):
                # Renderer.reset() shows the cursor at the transcript position
                # both when erasing and before repainting. Suppress those shows
                # until the handoff has restored the editor and its cursor.
                with self.app.output.hidden_cursor():
                    async with suspended_editor(self.app, atomic=True) as handoff:
                        # Snapshot after entering: input/model events can arrive while
                        # the handoff waits for CPR, but not during these sync writes.
                        width = max(1, self._screen_size().columns)
                        rebuilt = keep_place = False
                        marks_before = 0
                        if self._regenerate is not None:
                            rebuilt = True
                            if self.gliding:
                                self._reset_native_state()
                                self._declare(None)
                            # Transient writes are not in the replay; keep those
                            # still queued as rows as well as those not rendered.
                            notes = [entry for entry, _ in self._queued_transient]
                            pending = self._regenerate() + notes + self.transient_pending
                            self.pending.clear()
                            self._regenerate = None
                            # Replay covers the rows still queued; drop them.
                            self._written(len(self.rows))
                            self._queued_transient = []
                            # The handoff has erased the editor. Clear the
                            # normal-screen history and home before replay; its
                            # exit will request fresh CPR and restore the draft.
                            # With marks, the terminal keeps a reader scrolled
                            # back where they were (`REPLAY_BEGIN`).
                            keep_place = self.marks and self._same_content
                            self._same_content = False
                            marks_before, self._marks_written = self._marks_written, 0
                            begin = smooth_scroll.REPLAY_BEGIN if keep_place else ""
                            clear = begin + "\x1b[H\x1b[2J\x1b[3J"
                            if dock.active() is not None:
                                # SPIKE: the transcript's terminal, not the editor's dock.
                                if self.docked_glide:
                                    # A rebuilt screen must not be drawn gliding,
                                    # nor finish a glide queued before it.
                                    region = smooth_scroll.REGION_ID
                                    clear = smooth_scroll.region_off(region) + clear
                                    self._docked_declared = None
                                self.console.file.write(clear)
                                self.console.file.flush()
                            else:
                                self.app.output.write_raw(clear)
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
                        written = "".join(self._row_text(row) for row in chunk)
                        if self.marks:
                            self._marks_written += sum(row.mark is not None for row in chunk)
                        if rebuilt and keep_place:
                            # Rows that were still queued are on screen for the
                            # first time, their marks with them.
                            added = max(0, self._marks_written - marks_before)
                            written += smooth_scroll.replay_end(added)
                        if self._park_off and not self.rows and not self.pending:
                            written += smooth_scroll.PARK_OFF
                            self._park_off = False
                        if (docked := dock.active()) is not None:
                            written = self._docked_frame(
                                docked,
                                written,
                                self._native_runs(chunk),
                                rebuilt=rebuilt,
                                glide=self.docked_glide and paced and not rebuilt,
                            )
                        counter.write(written)
                        counter.flush()
                        handoff.rows_written = counter.rows
                        scrolled = self._scrolled(drawn, counter.rows)
                        # The exit repaint's growth is in ``scrolled`` already.
                        self._handoff_render = True
                        if (
                            not rebuilt
                            and self.gliding
                            and paced
                            and self._declared is not None
                            and counter.rows > 0
                        ):
                            # Sent in `_track_layout` right after the editor
                            # redraws and before SYNC_END so the terminal sees
                            # the final committed rows when scanning reveal spans.
                            self._native_commit = (counter.rows, scrolled, self._native_runs(chunk))
                # Ensure a commit never leaks past this handoff if `_track_layout` skipped it.
                self._native_commit = None
                # The handoff already repainted the editor on exit.
            self.changed.clear()
            if self.rows:
                # Keep the run loop ticking at its frame rate until drained.
                self.changed.set()
            else:
                self._typed = 0
                self._pacer.reset()

    def _drop_settled_regions(self, app) -> None:
        """Clear the panel region once its glide has settled.

        Left declared, a region outlives the rows it was for: the layout
        moves on, and where it then covers the typing row or the transcript
        region's hidden row it wins there (the innermost region does), so
        those rows stop gliding with the transcript and the hidden row shows.
        """
        now = monotonic()
        if self._panel_declared and not self._panel_glide.remaining(now):
            self._end_panel()

    def _end_panel(self) -> None:
        """Drop the panel region: its glide settled, or the transcript scrolls."""
        if self._panel_declared:
            self._panel_declared = False
            self._panel_glide = smooth_scroll.Glide()
            self.app.output.write_raw(smooth_scroll.region_off(smooth_scroll.PANEL_ID))

    def _glide_panel_region(self, app, top: int, bottom: int, distance: int) -> None:
        """Declare the panel region over rows ``top`` to ``bottom`` and glide it ``distance``."""
        columns = app.output.get_size().columns
        app.output.write_raw(
            smooth_scroll.visible_declaration(smooth_scroll.PANEL_ID, top, bottom, columns)
            + smooth_scroll.advance(distance, smooth_scroll.PANEL_ID)
        )
        self._panel_glide.advance(distance, monotonic())
        self._panel_declared = True

    def _declare(self, bottom: int | None) -> None:
        """Declare the glide region down to row ``bottom`` (0-based), or clear it.

        Re-sent at least every second, since switching to the alternate
        screen (a popup) clears it on the terminal's side.
        """
        columns = self.app.output.get_size().columns
        key = None if bottom is None or bottom < 1 else (bottom, columns)
        now = monotonic()
        if key is None:
            if self._declared is None:
                return
            self._declared = None
            self._reset_native_state()
            self._panel_declared = False
            self._panel_glide = smooth_scroll.Glide()
            self.app.output.write_raw(smooth_scroll.OFF)
        else:
            if self._declared is not None and self._declared[0] == key:
                if now - self._declared[1] < 1.0:
                    return
            self._declared = (key, now)
            self.app.output.write_raw(smooth_scroll.declaration(bottom, columns))
        self.app.output.flush()

    def release_screen(self) -> None:
        """Clear the glide regions before a popup or program takes the screen.

        A region hides its bottom row; whatever draws there next must show.
        The next render declares it again.
        """
        if self.gliding and isinstance(self.app, Application):
            self._declare(None)

    def _editor_pinned(self, *, just_drawn: bool = False) -> bool:
        """Whether the editor sits at the bottom, so written rows scroll the screen.

        The layout's filler takes any rows left below scrollback; none are
        left once the drawn height is no more than the layout needs.
        ``just_drawn``, right after a render, reuses the height that render
        measured (`_MeasuredSplit`) rather than measuring the layout again.
        """
        renderer = self.app.renderer
        screen = renderer._last_screen
        if screen is None or renderer._in_alternate_screen or not self.app._is_running:
            return False
        size = self.app.output.get_size()
        root = self.app.layout.container
        drawn = getattr(root, "drawn", None) if just_drawn else None
        if drawn is not None and drawn[:2] == (size.columns, size.rows):
            needed = drawn[2]
        else:
            needed = root.preferred_height(size.columns, size.rows).preferred
        return screen.height <= needed

    def _drawn_height(self) -> int | None:
        """Rows the layout was last drawn over, from the bottom of the screen."""
        renderer = self.app.renderer
        screen = renderer._last_screen
        if screen is None or renderer._in_alternate_screen or not self.app._is_running:
            return None
        return screen.height

    def _scrolled(self, drawn: int | None, written: int) -> int:
        """How far writing ``written`` rows scrolls the screen, by this frame's end.

        The layout always reaches the bottom row, so scrollback ends where it
        was drawn from. The rows and the layout's new height scroll whatever
        does not fit. Not every row written scrolls: one written as the live
        panel shrinks (a finished command's row leaving it) fills the space
        the panel gave up, and gliding it would draw it below its place.
        """
        if drawn is None or not written:
            return 0
        size = self.app.output.get_size()
        needed = self.app.layout.container.preferred_height(size.columns, size.rows).preferred
        # Rows written, plus any the live panel grew by in the same frame.
        scrolled = max(0, min(size.rows, written + min(needed, size.rows) - drawn))
        return scrolled

    def _before_render(self, app) -> None:
        """Hold the terminal's frame for the repaint and what `_after_render` adds.

        Every repaint hides the cursor, moves it to the rows that changed (the
        spinner, the typing row) and back, then shows it again. Unsynchronized,
        a terminal such as iTerm2 can draw a frame mid-sequence, so the cursor
        visibly blinks and jumps on every spinner tick and typed step.

        Gliding, a region declaration or a glide for a scroll the repaint
        caused must also land in the same frame as the repaint, or the
        terminal draws the moved rows unglided for a frame first.
        """
        app.output.write_raw(SYNC_START)

    def _after_render(self, app) -> None:
        """Keep the glide region's bottom on the reserved row, glide panel growth, end the frame.

        The layout starts with the typing row (0-based row ``top - 1``) and
        the reserved row under it, the region's hidden bottom row.
        """
        try:
            if self.gliding:
                self._track_layout(app)
        finally:
            app.output.write_raw(SYNC_END)
            app.output.flush()

    def _reset_native_state(self) -> None:
        self._native_commit = None
        self._native_backlog = 0.0

    def _drain_native_backlog(self, now: float) -> float:
        """pcode's estimate of the batch glide the terminal still has to run at ``now``."""
        elapsed = max(0.0, now - self._native_at)
        self._native_backlog = max(0.0, self._native_backlog - elapsed * self._native_rate)
        self._native_at = now
        return self._native_backlog

    @staticmethod
    def _native_runs(chunk: list[QueuedRow]) -> list[tuple[int, int, bool]]:
        """Group committed rows into contiguous `(offset, count, is_prose)` runs.

        Blank rows join the run they sit in, so the gap between paragraphs
        does not split a sweep.
        """
        runs: list[tuple[int, int, bool]] = []
        if not chunk:
            return runs
        first_vis = next((r for r in chunk if r.visible), None)
        cur_prose = bool(first_vis.typed) if first_vis is not None else False
        cur_start = 0
        for idx, row in enumerate(chunk):
            if row.visible and bool(row.typed) != cur_prose:
                if idx > cur_start:
                    runs.append((cur_start, idx - cur_start, cur_prose))
                cur_prose = bool(row.typed)
                cur_start = idx
        if len(chunk) > cur_start:
            runs.append((cur_start, len(chunk) - cur_start, cur_prose))
        return runs

    def _docked_frame(
        self,
        docked,
        written: str,
        runs: list[tuple[int, int, bool]],
        *,
        rebuilt: bool,
        glide: bool,
    ) -> str:
        """SPIKE: ``written``, for pcode's pane with the editor docked, with its glide.

        The pane holds only scrollback, so its cursor sits on the row after
        the last one written (`Dock.pane_row`) and that row is blank. The
        rows scroll in a region that stops above the docked card
        (`Dock.reserve`), so that row is the last above the card, while the
        glide region is the whole pane, its last row (under the card) the
        hidden bottom: rows gliding in come up from under the card.
        The rows, the region, the glide and the reveals go out as one
        synchronized frame, so no frame shows the rows unglided.
        """
        rows = written.count("\n")
        pane_rows = docked.pane_rows()
        # A replay cleared and homed the pane: its scroll region goes again first.
        written = docked.homed() + written if rebuilt else written
        before = docked.pane_row
        docked.wrote(rows)
        if rebuilt:
            self._native_backlog = 0.0
        if not glide or before is None or rows == 0 or pane_rows < 2:
            return written
        now = monotonic()
        head = SYNC_START + self._docked_declaration(docked, now)
        bottom = pane_rows - 1
        scrolled = max(0, before + rows - bottom)
        end_row = min(bottom, before + rows) - 1
        reach = smooth_scroll.MAX_BATCH_GLIDE
        floor = 0
        tail = ""
        if scrolled:
            backlog = min(self._drain_native_backlog(now) + scrolled, float(reach))
            self._native_backlog = backlog
            self._native_rate = smooth_scroll.batch_rate(backlog)
            flags = smooth_scroll.ADVANCE_BATCH | smooth_scroll.ADVANCE_FADE
            tail += smooth_scroll.advance(scrolled, flags=flags)
            floor = -reach
            if not self.typed:
                # `rows` mode: the glide and fade are the reveal, except for
                # rows that landed in place before the pane filled.
                in_place = rows - scrolled
                runs = [(0, in_place, False)] if in_place > 0 else []
        base_row = end_row - rows + 1
        for offset, length, prose in runs:
            start = base_row + offset
            end = min(start + length, end_row + 1)
            if end <= floor:
                continue
            start = max(floor, start)
            count = min(end - start, reach)
            if count > 0:
                flags = smooth_scroll.REVEAL_SWEEP if prose else smooth_scroll.REVEAL_CURTAIN
                tail += smooth_scroll.reveal(start, count, flags=flags)
        return head + written + tail + SYNC_END

    def _docked_declaration(self, docked, now: float) -> str:
        """SPIKE: the docked glide region over pcode's whole pane, when it is due."""
        columns = max(1, self._screen_size().columns)
        raw = docked.raw_pane_rows()
        key = (raw, columns)
        # Re-sent every second, as `_declare` does, in case the terminal dropped it.
        if (
            self._docked_declared is not None
            and self._docked_declared[0] == key
            and now - self._docked_declared[1] < 1.0
        ):
            return ""
        self._docked_declared = (key, now)
        return smooth_scroll.declaration(raw - 1, columns)

    def _docked_room(self, rows: int) -> tuple[str, str]:
        """SPIKE: the glide for ``rows`` the pane scrolls to make room for the
        docked card growing (`Dock.glide`): the text before and after the scroll.

        A plain advance, like any scroll of a few rows; while a batch glide
        runs, the terminal adds the rows to it, and so does pcode's estimate.
        """
        docked = dock.active()
        if not self.docked_glide or docked is None or rows <= 0:
            return "", ""
        now = monotonic()
        if self._drain_native_backlog(now) > 0:
            self._native_backlog += rows
        return self._docked_declaration(docked, now), smooth_scroll.advance(rows)

    def _native_batch_cap(self) -> int:
        """Most rows one handoff may write.

        Pinned, every row scrolls into the batch glide, which reaches back
        `MAX_BATCH_GLIDE` rows. Before the screen fills, rows land in place
        instead, so no more than the transcript rows that can be seen.
        """
        if self.docked_glide:
            # SPIKE: docked, the pane is full once its cursor reaches the bottom.
            docked = dock.active()
            row = docked.pane_row if docked is not None else None
            pane_rows = docked.pane_rows() if docked is not None else 0
            if row is None or row >= pane_rows - 1:
                return smooth_scroll.MAX_BATCH_GLIDE
            return max(1, min(smooth_scroll.MAX_BATCH_GLIDE, pane_rows - 1 - row))
        if self._editor_pinned():
            return smooth_scroll.MAX_BATCH_GLIDE
        size_rows = max(1, self.app.output.get_size().rows)
        if self._declared is not None:
            vis_cap = max(1, self._declared[0][0] - 1)
        else:
            vis_cap = max(1, size_rows - 4)
        return min(smooth_scroll.MAX_BATCH_GLIDE, vis_cap, size_rows)

    def _native_batch(self) -> int:
        """Queued rows to hand to the terminal now, mixed prose and code alike.

        Waits while the terminal's glide is too far behind to take a useful
        batch, rather than trickling a row or two at a time.
        """
        if not self.rows:
            return 0
        backlog = self._drain_native_backlog(monotonic())
        cap = self._native_batch_cap()
        room = min(cap, int(smooth_scroll.MAX_BATCH_GLIDE - backlog))
        if room <= 0:
            return 0
        total = len(self.rows)
        if total > room and room < min(16, max(1, cap // 2)):
            return 0
        return min(total, room)

    def _emit_native_commit(self, app) -> None:
        """Send the batch glide and reveals for the rows just committed.

        The rows end on the transcript's last row (`end_row`). Scrolled rows
        glide in on the terminal's conveyor; reveals may start above the
        region (negative rows) as far back as the glide reaches. In `rows`
        mode, scrolled rows get no reveal beyond a curtain over the rows that
        filled the space above the editor.
        """
        commit, self._native_commit = self._native_commit, None
        if commit is None or self._declared is None:
            return
        written, scrolled, runs = commit
        end_row = self._declared[0][0] - 2
        if end_row < 0:
            return
        reach = smooth_scroll.MAX_BATCH_GLIDE
        base_row = end_row - written + 1
        floor = 0
        if scrolled > 0:
            self._end_panel()
            backlog = min(self._drain_native_backlog(monotonic()) + scrolled, float(reach))
            self._native_backlog = backlog
            self._native_rate = smooth_scroll.batch_rate(backlog)
            flags = smooth_scroll.ADVANCE_BATCH | smooth_scroll.ADVANCE_FADE
            app.output.write_raw(smooth_scroll.advance(scrolled, flags=flags))
            floor = -reach
            if not self.typed:
                # `rows` mode: the glide and fade are the reveal, except for
                # rows that filled the space above the editor.
                in_place = written - scrolled
                runs = [(0, in_place, False)] if in_place > 0 else []
        for offset, length, prose in runs:
            start = base_row + offset
            end = min(start + length, end_row + 1)
            if end <= floor:
                continue
            start = max(floor, start)
            count = min(end - start, reach)
            if count > 0:
                flags = smooth_scroll.REVEAL_SWEEP if prose else smooth_scroll.REVEAL_CURTAIN
                app.output.write_raw(smooth_scroll.reveal(start, count, flags=flags))

    def _track_layout(self, app) -> None:
        top = layout_top_row(app)
        screen = app.renderer._last_screen
        pinned = self._editor_pinned(just_drawn=True)
        if top is not None and screen is not None and pinned:
            # Pinned, the layout fills the bottom ``screen.height`` rows. Not
            # `layout_top_row`: after a write that scrolled, it rests on the
            # handoff's guess, one row off until the cursor report lands, and
            # a bottom flipping in and out under the typing row jolts it.
            top = app.output.get_size().rows - screen.height + 1
        if top is not None and not app._running_in_terminal:
            self._drop_settled_regions(app)
            self._declare(top)
            self._emit_native_commit(app)
        else:
            self._native_commit = None
        self._glide_growth(app, pinned)

    def _glide_growth(self, app, pinned: bool) -> None:
        """Glide the transcript up when the live panel grows while pinned.

        A pinned layout has no filler rows to grow into, so a command or task
        row appearing scrolls the transcript up, which the terminal would show
        as a jump. Only growth from pinned to pinned counts: a layout with
        filler rows (or a height that only reflects a corrected cursor report)
        moves nothing on screen.
        """
        screen = app.renderer._last_screen
        handoff, self._handoff_render = self._handoff_render, False
        if screen is None or app.renderer._in_alternate_screen:
            self._last_layout = None
            self._last_spans = {}
            self._last_keys = {}
            return
        last, self._last_layout = self._last_layout, (screen.height, pinned)
        spans = {
            id(window): (screen.height - pos.ypos, screen.height - pos.ypos - pos.height + 1)
            for window, pos in screen.visible_windows_to_write_positions.items()
            if pos.height > 0
        }
        last_spans, self._last_spans = self._last_spans, spans
        keys = _row_keys(screen)
        last_keys, self._last_keys = self._last_keys, keys
        if handoff or last is None or self._declared is None:
            return
        grown = screen.height - last[0]
        if not (pinned and last[1] and grown > 0):
            # Nothing scrolled: rows may still have moved within the panel.
            self._glide_moves(app, screen.height, keys, last_keys, grown=grown)
            return
        app.output.write_raw(smooth_scroll.advance(grown))
        self._glide_panel(app, screen.height, grown, spans, last_spans)

    def _glide_moves(
        self,
        app,
        height: int,
        keys,
        last_keys,
        shaded: set[int] | frozenset[int] | None = None,
        grown: int = 0,
    ) -> None:
        """Glide live panel rows that moved up or down since the last render.

        ``shaded`` defaults to the last screen's `_shaded_rows`, worked out only
        once rows did move: it resolves a style per row, and this runs after
        nearly every render.

        A row leaving the panel drops the rows above it into its place, and
        one arriving while the screen has room lifts them. Rows are matched by
        text (`_row_keys`), counted up from the bottom, which the layout always
        reaches. The rows that moved by the most common distance glide by it,
        unless something between them changed or stayed: then it is not one
        block moving, and the rows land at once as before.
        """
        move = _block_move(keys, last_keys)
        if move is None:
            if grown:
                self._end_panel()
            return
        distance, low, high = move
        if shaded is None:
            shaded = _shaded_rows(app.renderer._last_screen, getattr(app, "_merged_style", None))
        rows = app.output.get_size().rows
        top, bottom = rows - high, rows - low  # 0-based screen rows, where they are now
        floor = rows - height + 2
        if distance > 0:
            bottom += distance  # what lifted them: rows that rise in below
            # Pull unkeyed shaded rows (blank top border, top gap) into an
            # upward glide so the uncovered top edge opens above the box.
            while (
                top > floor
                and (rows - top) in shaded
                and (rows - (top - 1)) in shaded
                and (rows - (top - 1)) not in keys
            ):
                top -= 1
        else:
            top += distance  # the rows they left, now empty
        # Never over the typing and hidden rows: what starts there comes in
        # from the region's top edge instead.
        top = max(top, floor)
        if bottom >= rows or bottom < top:
            if grown:
                self._end_panel()
            return
        # Ghostty fills the edge a glide uncovers (top when rising, bottom when
        # dropping) with the terminal's default background. If that edge lies
        # inside a shaded box, gliding paints a scrollback-colored stripe across
        # the box and splits it in two; snap instead.
        if (distance > 0 and (rows - top) in shaded and (rows - (top - 1)) in shaded) or (
            distance < 0 and (rows - bottom) in shaded and (rows - (bottom + 1)) in shaded
        ):
            self._end_panel()
            return
        self._glide_panel_region(app, top, bottom, distance)

    def _glide_panel(self, app, height: int, grown: int, spans, last_spans) -> None:
        """Glide the live panel's rows above the ones just added, and those rows in.

        Everything above the new rows moved up ``grown``; everything below
        them (the status line, the editor) stayed. The new rows end at the
        lowest window that is new or whose top rose by ``grown``. The panel
        region runs from under the transcript region's hidden row to there.
        """
        risen = [
            bottom
            for window, (top, bottom) in spans.items()
            if window not in last_spans or last_spans[window][0] + grown == top
        ]
        if not risen:
            return
        rows = app.output.get_size().rows
        last_row = rows - min(risen)
        first_row = rows - height + 2  # below the typing row and the hidden row
        if last_row < first_row:
            return
        self._glide_panel_region(app, first_row, last_row, grown)

    async def run(self) -> None:
        if not isinstance(self.app, Application):
            await self._run_frames()
            return
        self.app.before_render += self._before_render
        self.app.after_render += self._after_render
        try:
            await self._run_frames()
        finally:
            self.app.before_render -= self._before_render
            self.app.after_render -= self._after_render
            self._declare(None)

    def _screen_size(self):
        """The transcript's terminal: the editor's, unless the editor is docked (SPIKE)."""
        if dock.active() is not None:
            columns, rows = os.get_terminal_size(self.console.file.fileno())
            return Size(rows=rows, columns=columns)
        return self.app.output.get_size()

    async def _run_frames(self) -> None:
        size = self._screen_size()
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
                current = self._screen_size()
                if dock.active() is not None and current.columns == size.columns:
                    # SPIKE: docked, the pane holds only scrollback, which a
                    # height change doesn't rewrap; only a width change reflows.
                    current = size
                if current != size:
                    size, resized_at = current, monotonic()
                elif resized_at is not None and monotonic() - resized_at >= 0.25:
                    self.regenerate(self.resize_replay, same_content=True)
                    resized_at = None
            await asyncio.sleep(1 / 30)
            await self.flush()


PROMPT_PREFIX = "❯ "
# Same width as PROMPT_PREFIX, so wrapping is unchanged when the draft flips.
SHELL_PROMPT_PREFIX = "$ "
BTW_PROMPT_PREFIX = "btw ❯ "
CONTINUATION_PREFIX = "· "


def prompt_prefix(text: str) -> str:
    """The prompt marker for a draft: `$` once it is a `!command`, else the chevron."""
    return SHELL_PROMPT_PREFIX if text.lstrip().startswith(SHELL_PREFIX) else PROMPT_PREFIX


def _completes_while_typing() -> bool:
    document = get_app().current_buffer.document
    text = document.text
    before = document.text_before_cursor
    return (
        (text.startswith("/") and "\n" not in text)
        # A `$MODEL` or `+EFFORT` word anywhere outside a `!command`; one
        # that matches nothing (`$HOME`) just shows no menu.
        or (not text.lstrip().startswith(SHELL_PREFIX) and typed_word(before) is not None)
        or reference_fragment(before) is not None
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


class _MeasuredSplit(HSplit):
    """The layout root, remembering the height it was measured at for its last frame.

    The renderer measures the root and then draws it. Code that runs right
    after a render (`TerminalOutput._track_layout`, on every frame while
    gliding) reads that measurement from `drawn` instead of walking the whole
    layout again, uncached once the redraw is over.
    """

    _measured: tuple[int, int, int] | None = None
    # (width, max height, preferred height) the last drawn frame was measured at.
    drawn: tuple[int, int, int] | None = None

    def begin_frame(self) -> None:
        """Forget measurements taken between redraws; called before each one."""
        self._measured = None

    def preferred_height(self, width: int, max_available_height: int):
        dimension = super().preferred_height(width, max_available_height)
        self._measured = (width, max_available_height, dimension.preferred)
        return dimension

    def write_to_screen(self, *args, **kwargs) -> None:
        self.drawn, self._measured = self._measured, None
        super().write_to_screen(*args, **kwargs)


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


# A row's leading mark: one or two symbol characters before a space, such as
# `√` (a math symbol, not stripped as `So`) or an ASCII spinner's frame.
_LEADING_MARK = re.compile(r"^[^\w\s$]{1,2}\s+")


def _row_keys(screen) -> dict[int, str]:
    """The live layout's rows under the typing and hidden rows, by their text.

    Keyed by row counted up from the bottom (the last row is 1), the edge the
    layout always reaches. Symbols are left out, and so is a leading mark,
    so a spinner's frame or a call's mark turning `√` does not make a row
    look like a different one. Blank rows are left out.
    """
    keys = {}
    for y in range(2, screen.height):
        cells = screen.data_buffer.get(y)
        if not cells:
            continue
        text = "".join(
            cell.char
            for _, cell in sorted(cells.items())
            if unicodedata.category(cell.char[:1] or " ") not in ("So", "Co")
        ).strip()
        text = _LEADING_MARK.sub("", text)
        if text:
            keys[screen.height - y] = text
    return keys


def _shaded_rows(screen, style) -> set[int]:
    """Live layout rows (counted up from the bottom) painted with a surface shade.

    Matches `_row_keys`'s coordinate system (`screen.height - y`). Rows on the
    terminal's default background (or `bottom-toolbar`, which sets `bg:default`)
    are omitted.
    """
    if style is None:
        return set()
    shaded = set()
    for y in range(2, screen.height):
        cells = screen.data_buffer.get(y)
        if not cells:
            continue
        cell = cells.get(0)
        if cell is None or not cell.style:
            continue
        bg = style.get_attrs_for_style_str(cell.style).bgcolor
        if bg and bg != "default":
            shaded.add(screen.height - y)
    return shaded


def _block_move(keys: dict[int, str], last_keys: dict[int, str]) -> tuple[int, int, int] | None:
    """Rows that moved together since `last_keys`: (distance up, lowest, highest).

    Rows count up from the bottom; a negative distance moved them down. The
    distance is the one most moved rows agree on, and every row between the
    lowest and highest of them must have moved by it, so a block that only
    partly moved, or a row in it that changed, does not glide. At most
    `smooth_scroll.MAX_GLIDE` rows: the terminal jumps the rest anyway.
    Only rows whose text is unique in both frames vote on the distance; a
    repeated row (the same command twice) counts as moved if it fits it.
    """
    where: dict[str, list[int]] = {}
    for row, key in last_keys.items():
        where.setdefault(key, []).append(row)
    counts = Counter(keys.values())
    candidates = {
        row: {row - old for old in where.get(key, ())}
        for row, key in keys.items()
        if last_keys.get(row) != key
    }
    votes = Counter(
        d
        for row, distances in candidates.items()
        if counts[keys[row]] == 1 and len(where.get(keys[row], ())) == 1
        for d in distances
    )
    if not votes:
        return None
    distance = votes.most_common(1)[0][0]
    moved = [row for row, distances in candidates.items() if distance in distances]
    low, high = min(moved), max(moved)
    if abs(distance) > smooth_scroll.MAX_GLIDE or len(moved) != sum(
        low <= row <= high for row in keys
    ):
        return None
    return distance, low, high


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
        kitty_spinners: kitty_spinner.KittySpinners | None = None,
        composer_state: Callable[[], dict] | None = None,
    ):
        self.session = session
        self.activity = activity
        self.transcript = transcript
        self.shortcuts = shortcuts
        self.session_title = session_title
        self.kitty_spinners = kitty_spinners
        self.composer_state = composer_state
        self._composer_sent: dict | None = None
        self.editor = _fit_editor(session)
        self.render_cache = None
        self.root: _MeasuredSplit | None = None  # set by `layout()`
        self.animation_task = None
        self.preview_body = lru_cache(maxsize=1)(_preview_body)
        self.live_delta = LiveDeltaPreview(lambda: self.session.app.invalidate())
        # When each running command's output was first drawn for, by call id,
        # and whether one is still waiting out COMMAND_PREVIEW_DELAY_SECONDS.
        self.command_seen: dict[str, float] = {}
        self.command_waiting = False
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
        # The terminal's own completion list, where it draws one (Ghostty Smooth).
        self.native = self._native_completions()
        self.native_style: tuple[object, dict[str, str]] | None = None
        # The completions the terminal's list last showed.
        self.native_completions: list | None = None
        # Whether the terminal shows notices as toasts (`drawer.toast`), and the
        # notice last sent: (text, expiry).
        self.native_toasts = self._native_toasts()
        self.toast_sent: tuple[str, float] | None = None
        self.frame: Container | None = None  # set by `editor_frame()`
        self.menu = CompletionsMenu(
            max_height=20,
            scroll_offset=1,
            extra_filter=has_focus(session.default_buffer)
            & ~Condition(lambda: self.native is not None and self.native.active),
        )
        self.menu.content.dont_extend_height = Always()
        from pcode.btw_view import ThreadView

        # The /btw thread in the drawer, while one is open.
        self.btw = ThreadView(activity, transcript)
        # Whether the open menu made the layout taller than the measured rows.
        self.menu_overflowed = False
        # Blank rows the layout holds where scrolled-away transcript used to
        # be (`refill_after_shrink`), when that last changed, and the pending
        # debounced rebuild. `_refilled` stops a rebuild that left the same
        # gap from being retried until the gap changes.
        self._gap = 0
        self._gap_changed = 0.0
        self._refill_task: asyncio.Task | None = None
        self._refilled = False

    def size(self):
        # Read once per redraw, as prompt_toolkit's renderer does: layout
        # callbacks ask over a hundred times a frame, each a syscall.
        cache = self.render_cache
        if cache is not None and (size := cache.get("size")) is not None:
            return size
        size = self.session.app.output.get_size()
        if (docked := dock.active()) is not None:
            # SPIKE: budget rows against the whole pane, not the dock's current
            # height, or the dock never asks for more than it has.
            size = docked.layout_size(size)
        # Every layout read comes through here, so the activity panel's size rule
        # follows a resize or split before anything is measured against it.
        self.activity.fit_screen(size.columns, size.rows)
        if cache is not None:
            cache["size"] = size
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
        btw = self.btw_bar_height()
        available = max(
            1,
            size.rows
            - 4
            - gap
            - btw
            - self.activity_height()
            - tasks
            - band
            - len(self.queue_rows()),
        )
        cap = self.activity.height_cap(size.rows)
        if cap is not None:
            available = min(available, max(1, cap - 2 - gap - btw - band - self.tasks_height()))
        text_height = self.editor.preferred_height(max(1, size.columns - 2), available).preferred
        return min(text_height, available) + 2 + gap + btw + tasks + band

    def refresh_interval(self) -> float:
        """Seconds until the next frame."""
        ks = self.kitty_spinners
        status_iv = (
            0.5 if ks is not None and ks.active_for("status") else self.spinner.interval / 1000
        )
        tool_iv = (
            (0.25 if self.activity.tools.delegates else 0.5)
            if ks is not None and ks.active_for("tool")
            else self.tool_spinner.interval / 1000
        )
        interval = min(status_iv, tool_iv)
        if self.startup_border_active():
            return min(interval, STARTUP_FRAME_SECONDS)
        return interval

    @_per_render
    def spinner_frame(self) -> str:
        """This redraw's model-wait frame, shared so every such spinner turns together."""
        return self.spinner.render(monotonic()).plain

    @_per_render
    def tool_spinner_frame(self) -> str:
        """This redraw's frame for running tool calls and commands."""
        return self.tool_spinner.render(monotonic()).plain

    def status_spinner_mark(self) -> str | tuple[str, str]:
        if self.kitty_spinners is not None and self.kitty_spinners.active_for("status"):
            return self.kitty_spinners.fragment("status")
        return self.spinner_frame()

    def tool_spinner_mark(self) -> str | tuple[str, str]:
        if self.kitty_spinners is not None and self.kitty_spinners.active_for("tool"):
            return self.kitty_spinners.fragment("tool")
        return self.tool_spinner_frame()

    def aside_spinner_mark(self) -> str | tuple[str, str]:
        if self.kitty_spinners is not None and self.kitty_spinners.active_for("aside"):
            return self.kitty_spinners.fragment("aside")
        return self.spinner_frame()

    def wait_spinner_mark(self) -> str | tuple[str, str]:
        if self.kitty_spinners is not None and self.kitty_spinners.active_for("accent"):
            return self.kitty_spinners.fragment("accent")
        return self.tool_spinner_frame()

    def plan_spinner_icon(self) -> str:
        if self.kitty_spinners is not None and self.kitty_spinners.active_for("task"):
            return kitty_spinner.INSET_ICON
        if self.kitty_spinners is not None and self.kitty_spinners.active_for("accent"):
            return kitty_spinner.PLACEHOLDER
        return self.tool_spinner_frame()

    def agent_spinner_icon(self) -> str | None:
        """The main agent's spinner, for sub-agents, which are agents too."""
        if self.kitty_spinners is not None and self.kitty_spinners.active_for("agent"):
            return self.kitty_spinners.fragment("agent")[1]
        return None

    def startup_spinner_mark(self, elapsed: float) -> str | tuple[str, str]:
        if self.kitty_spinners is not None and self.kitty_spinners.active_for("startup"):
            return self.kitty_spinners.fragment("startup")
        return "◆" if int(elapsed * 4) % 2 == 0 else "◇"

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
                    - self.btw_bar_height(attached=self.activity.attach_tasks)
                    - (0 if self.activity.attach_tasks else self.top_gap(attached=False)),
                )
            )
        # The active step turns with the running commands' spinner.
        return self.activity.plan_rows(budget, self.plan_spinner_icon(), self.agent_spinner_icon())

    @_per_render
    def preview_layout(self):
        """Allocate actual chrome/editor height first, then give output the remainder.

        Keep the normal task viewport unless it would leave no output at all.
        Only in that case trim task rows to preserve a one-line output tail.
        Calculate all three heights together so editor wrapping cannot create a
        circular dependency between frame_height and command_rows.
        """
        activity, transcript = self.activity, self.transcript
        from pcode.ui_surface import native_transcript_active

        if transcript is None or self.btw_docked() or native_transcript_active():
            if self.btw_docked() or native_transcript_active():
                self.command_waiting = False
                self.live_delta.forget()
            return None
        edits = transcript.live_edit_previews and activity.edit_previews
        if not edits:
            # The preview ended, so the next edit never flashes this one's diff.
            self.live_delta.forget()
        # A `!command` the user typed is shown while it runs whatever
        # `show_commands` says, and so is a job the user asked to watch; the
        # model's own commands follow the setting, and hide with the panel.
        commands = activity.command_outputs
        if not ((transcript.command_preview and activity.panel_shown) or activity.user_command):
            commands = {
                key: event for key, event in commands.items() if key.startswith(WATCHED_PREFIX)
            }
        commands = self.settled_commands(commands)
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
        btw = self.btw_bar_height(attached=activity.attach_tasks and bool(plans))
        editor_room = max(1, room - 2 - gap - btw - 4 - task_floor)
        cap = activity.height_cap(size.rows)
        if cap is not None:
            tasks = len(plans) + chrome_rows if plans else 0
            editor_room = min(editor_room, max(1, cap - 2 - gap - btw - band - tasks))
        editor_rows = min(editor_room, self.editor.preferred_height(width, editor_room).preferred)
        editor_height = editor_rows + 2 + gap + btw
        plan_budget = max(0, room - editor_height - 4 - chrome_rows)
        if len(plans) > plan_budget:
            plans = self.base_plan_rows(plan_budget)
            # Trimmed away entirely, attached tasks no longer stand in for the gap.
            trimmed_attached = activity.attach_tasks and bool(plans)
            trimmed_gap = self.top_gap(attached=trimmed_attached)
            trimmed_btw = self.btw_bar_height(attached=trimmed_attached)
            editor_height += (trimmed_gap + trimmed_btw) - (gap + btw)
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

    def settled_commands(self, commands: dict[str, CommandOutput]) -> dict[str, CommandOutput]:
        """The commands whose output has streamed for COMMAND_PREVIEW_DELAY_SECONDS.

        Timed from the first redraw that saw the output, so a command that is
        silent for a while and then prints and exits at once never flashes
        either. Every running command is timed, shown or not, so toggling the
        panel on mid-command shows a long one straight away.
        """
        now, seen = monotonic(), self.command_seen
        running = self.activity.command_outputs
        for key in [key for key in seen if key not in running]:
            del seen[key]
        for key in running:
            seen.setdefault(key, now)
        settled = {
            key: event
            for key, event in commands.items()
            if key.startswith(WATCHED_PREFIX) or now - seen[key] >= COMMAND_PREVIEW_DELAY_SECONDS
        }
        self.command_waiting = len(settled) < len(commands)
        return settled

    def plan_rows(self):
        if self.btw_docked():
            return []
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
        if self.native_toasts:
            return []  # The terminal shows it (`sync_native_toast`).
        width = self.size().columns - 1
        return self.activity.notice_rows(width)

    @_per_render
    def group_rows(self):
        """The run's group line so far, when no status row carries its tally.

        While a turn runs the count rides the status row instead, next to the
        spinner that says it is still going; the full line reaches scrollback
        when the run closes.
        """
        from pcode.ui_surface import native_transcript_active

        if self.transcript is None or self.activity.status_shown or native_transcript_active():
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
                # Under a height cap the thought gives way to the editor.
                rows = min(rows, budget)
            return self.activity.thought_fragments(self.band_width(), rows, self.shown_thought())
        return self.activity.thought_fragments(self.size().columns - 1, rows, self.shown_thought())

    @_per_render
    def shown_thought(self) -> str:
        """The thought on the thinking rows, read once per redraw.

        Its hold can run out between two reads of the clock, and the tool rows
        must not give up a row for a thought the band then leaves out.
        """
        return self.activity.shown_thought()

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
        """The turn's latest tool calls, on plain rows above the editor box.

        Outside the box whatever `attach_activity` says: on the terminal's own
        background they read as the scrollback lines they become once settled,
        apart from the thought drawn in the box's header. They share a quarter
        of the pane with the thought, which keeps one row of it when there is
        one to show.

        When the Swift native transcript is active, running tools and live
        command output render directly in the transcript instead so the main
        status line stays pinned as the top line of the docked editor.
        """
        from pcode.ui_surface import native_transcript_active

        if native_transcript_active():
            return []
        limit = self.size().rows // 4 - int(bool(self.shown_thought()))
        return self.activity.tool_rows(
            self.status_line(), self.size().columns - 1, limit, self.tool_spinner_mark()
        )

    def band_width(self) -> int:
        """Cells across a band row: inside the box's sides, in from them like the
        tasks, and a cell short of the right one so wrapped prose has a margin
        to match the status row's."""
        return max(1, self.size().columns - 3 - self.task_padding())

    def band_budget(self) -> int | None:
        """Rows `tasks_max_height` leaves the thought in the box, or None when
        nothing caps it.

        The cap covers the whole box, so the band gets what remains once the
        box keeps its status row, one text row, its bottom row, the band's
        divider, and a task with its divider. Reads the plan itself rather
        than its rows, which are sized from this.
        """
        activity = self.activity
        cap = activity.height_cap(self.size().rows)
        if cap is None:
            return None
        # Running sub-agents get task rows with no plan at all.
        tasks = activity.tasks_shown and bool(activity.displayed_plan or activity.tools.delegates)
        return cap - 4 - int(self.btw_active()) - (1 + self.task_chrome() if tasks else 0)

    @_per_render
    def band_rows(self) -> list[list[tuple[str, str]]]:
        """The activity band: the newest thought, in the editor box under the
        status row and in its header's shade (`attach_activity`).

        Exactly as tall as the thought, never padded: the box is anchored to
        the bottom, so a shorter thought only moves the status row down a
        row, while held blank rows read as a gap between thought and tasks.
        """
        if self.btw_docked():
            return []
        from pcode.ui_surface import native_transcript_active

        if native_transcript_active():
            return []
        attached = self.activity.attach_activity and self.activity.status_shown
        return self.thought_rows() if attached else []

    def band_height(self) -> int:
        """Rows the band adds to the editor box: its rows and any divider under them."""
        rows = self.band_rows()
        return len(rows) + self.band_divided() if rows else 0

    def band_divided(self) -> bool:
        """Whether a divider row closes the band.

        Shaded, the divider is a blank row in the box's colour. Over attached
        tasks the step from the header's shade to the box's already parts the
        two, and a blank row only read as a gap between thought and tasks;
        over the draft it keeps the prompt off the thought. Borders draw it as
        the line it is. Read from activity state alone, as `preview_layout`
        asks for the band's height before the task rows exist.
        """
        activity = self.activity
        tasks = (
            activity.attach_tasks
            and activity.tasks_shown
            and bool(activity.displayed_plan or activity.tools.delegates)
        )
        return not (self.frame_shaded() and tasks)

    @_per_render
    def aside_rows(self):
        return self.activity.aside_rows(self.aside_spinner_mark(), self.size().columns - 1)

    @_per_render
    def wait_rows(self):
        """The terminal's own wait (the host starting, a command running there)."""
        return self.activity.wait_fragments(self.wait_spinner_mark(), self.size().columns - 1)

    @_per_render
    def typing_row(self):
        """Prose still being typed out: the next scrollback row, drawn live."""
        output = self.transcript.output if self.transcript is not None else None
        return output.typing_fragments() if output is not None else []

    @_per_render
    def queue_rows(self):
        if self.btw_docked():
            return []
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
        if transcript is None or self.gliding() or dock.active() is not None:
            # Gliding, the reserved row below the typing row is the gap. One
            # that came and went with each line would resize the layout
            # under the glide region and jolt it. Docked, the card is already
            # separate from scrollback on its own surface.
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

    def status_height(self) -> int:
        """Live rows above the editor box. The status rides the box's own border."""
        from pcode.ui_surface import native_transcript_active

        # Attached (or native Swift transcript), the thought never sits above the box.
        unboxed = not self.activity.attach_activity and not native_transcript_active()
        return (
            # Gliding, the typing row and the reserved row are always there.
            (2 if self.gliding() else bool(self.typing_row()))
            + (len(self.thought_rows()) if unboxed else 0)
            + len(self.tool_rows())
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
        return not self.btw_docked() and self.activity.attach_tasks and bool(self.plan_rows())

    def attached_height(self) -> int:
        """Rows attached tasks add to the editor box: the tasks plus a divider."""
        return len(self.plan_rows()) + 1 if self.plan_attached() else 0

    def activity_height(self) -> int:
        if self.btw_docked():
            return 0
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

    def band_body(self, side) -> ConditionalContainer:
        """The band between the box's sides, its rows set in from them like the
        tasks under it."""
        body = VSplit(
            [
                side(),
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
                side(),
            ]
        )
        return ConditionalContainer(body, Condition(lambda: bool(self.band_rows())))

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
            hidden = activity.show_activity and activity.show_tasks and activity.tasks_too_big
            if (self.plan_attached() or hidden) and activity.displayed_plan:
                meta.insert(0, activity.panel_title())
            line = replace(self.status_line(), tally=" · ".join(part for part in meta if part))
            lead = self.heading_lead()
            status = line.fragments(self.status_spinner_mark(), columns - 3 - len(lead), rule=True)
            if not status:
                return [("class:frame.border", "┌" + "─" * max(0, columns - 2) + "┐")]
            used = len(lead) + sum(cell_len(text) for _, text in status)
            return [
                ("class:frame.border", lead),
                *status,
                ("class:frame.border", f" {'─' * max(0, columns - used - 2)}┐"),
            ]

        return Window(FormattedTextControl(fragments, show_cursor=False), height=1)

    def startup_border_active(self, attached: bool | None = None) -> bool:
        """Whether the editor's top border is currently showing the startup animation."""
        if (
            self.btw_docked()
            or self.native_composer_chrome()
            or not self.activity.startup_animating
            or self.activity.status_shown
        ):
            return False
        if attached is None:
            attached = self.plan_attached()
        if attached:
            return False
        if self.activity.startup_label:
            return True
        return not self.frame_shaded() and not self.btw_active() and not self.idle_title()

    def startup_border(self) -> Window:
        """Animated top border while starting up, then a fast light sweep once ready."""

        def _shimmer_rule(length: int, pos: int, pulse: int, style: str) -> list[tuple[str, str]]:
            if length <= 0:
                return []
            start = max(0, pos)
            end = min(length, pos + pulse)
            parts: list[tuple[str, str]] = []
            if start > 0:
                parts.append(("class:frame.border", "─" * start))
            if end > start:
                parts.append((style, "─" * (end - start)))
            if end < length:
                parts.append(("class:frame.border", "─" * (length - end)))
            return parts

        def fragments():
            columns = self.size().columns
            if columns < 2:
                return []
            activity = self.activity
            shaded = self.frame_shaded()
            if activity.startup_label:
                elapsed = max(0.0, monotonic() - activity.startup_started)
                # Animated startup mark while booting, keeping the status
                # spinner reserved for active model turns.
                mark = self.startup_spinner_mark(elapsed)
                mark_frags = (
                    [mark, ("class:activity.spinner", " ")]
                    if isinstance(mark, tuple)
                    else [("class:activity.spinner", f"{mark} ")]
                )
                label = plain(activity.startup_label, limit=None)
                lead = self.heading_lead()
                if shaded:
                    head = [
                        ("", lead),
                        *mark_frags,
                        ("class:activity.phase", label),
                    ]
                    return fit_fragments(head, columns)
                max_text = max(0, columns - len(lead) - 2)
                head = fit_fragments([*mark_frags, ("class:activity.phase", label)], max_text)
                if not head:
                    return [("class:frame.border", "┌" + "─" * max(0, columns - 2) + "┐")]
                used_text = sum(cell_len(t) for _, t in head)
                rule_len = max(0, columns - len(lead) - used_text - 2)
                pulse = min(6, max(2, rule_len // 6))
                span = rule_len + pulse
                pos = int((elapsed * 48) % max(1, span)) - pulse
                return [
                    ("class:frame.border", lead),
                    *head,
                    ("class:frame.border", " "),
                    *_shimmer_rule(rule_len, pos, pulse, "class:activity.spinner"),
                    ("class:frame.border", "┐"),
                ]
            # Fast settle sweep once ready: identical border geometry, brief accent wave.
            if shaded:
                return [("", " " * columns)]
            rule_len = max(0, columns - 2)
            elapsed = max(0.0, monotonic() - activity.startup_done_at)
            progress = min(1.0, elapsed / STARTUP_SETTLE_SECONDS)
            pulse = max(4, rule_len // 4)
            pos = int(progress * (rule_len + pulse)) - pulse
            return [
                ("class:frame.border", "┌"),
                *_shimmer_rule(rule_len, pos, pulse, "class:plan.heading.done"),
                ("class:frame.border", "┐"),
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

        from pcode.ui_surface import native_transcript_active

        # With `attach_activity off`, the thought gets rows of its own above
        # the editor box, so a running tool taking the status never hides it;
        # attached (or under the Swift native transcript), it never sits above the box.
        def unboxed_thought():
            if activity.attach_activity or native_transcript_active():
                return []
            return self.thought_rows()

        thought = self.panel_rows(unboxed_thought)
        # Just above the box whose border carries the status, on the
        # terminal's own background: what runs, then how it is going.
        tool = self.panel_rows(self.tool_rows)
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
        return HSplit(
            [
                status_spacer,
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
            filter=Condition(
                lambda: (
                    bool(self.activity.queued_prompts)
                    and not self.btw_docked()
                    and not isinstance(dock.active(), dock.EditorOnly)
                )
            ),
        )

    def glide_reserve(self) -> ConditionalContainer:
        """Gliding, a blank row under the typing row: the region's hidden bottom row."""
        return ConditionalContainer(
            Window(height=1, dont_extend_height=True),
            filter=Condition(self.gliding),
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
            # Gliding, the row is always reserved, so the layout's height
            # never changes under the region.
            filter=Condition(lambda: bool(self.typing_row()) or self.gliding()),
        )

    def gliding(self) -> bool:
        output = self.transcript.output if self.transcript is not None else None
        return output is not None and output.gliding

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
        turn_slots = {"done": "ok", "cancelled": "turn_cancelled", "failed": "fail"}
        if slot := turn_slots.get(outcome):
            label.append((f"class:session.{outcome}", glyphs.icon(slot)))
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
        if self.btw_docked() or self.native_composer_chrome():
            return []
        # Corners, stub, a space either side, and at least one rule cell.
        # Shaded, `heading_border` draws it: the lead, a space, a rule cell, the corner.
        chrome = len(self.heading_lead()) + 3 if self.frame_shaded() else TITLE_CHROME + 1
        return fit_fragments(self.session_label(leads=True), self.size().columns - chrome)

    def btw_active(self) -> bool:
        """Whether the editor currently sends to an open `/btw` thread."""
        from pcode.btw_view import current

        return bool(current(self.activity))

    def btw_docked(self) -> bool:
        """Whether the editor is docked inside the native `/btw` drawer.

        In that mode the card sits inside the drawer right under the thread,
        so the extra `/btw` prefix/tint/header and main-conversation chrome
        (status, tasks, command previews, toolbar) stay hidden.
        """
        return self.btw_active() and dock.active() is not None

    def btw_running_aside(self):
        """The open `/btw` thread's newest question while its answer is running."""
        if not self.btw_docked():
            return None
        from pcode.btw_view import current

        thread = current(self.activity)
        return thread[-1] if thread and thread[-1].running else None

    def btw_status_border(self) -> Window:
        """The docked `/btw` editor's top border while the open thread is answering.

        Matches the main editor's status bar (`status_border`): spinner, phase,
        detail, and elapsed clock on the shaded header strip.
        """

        def fragments():
            columns = self.size().columns
            if columns < 2:
                return []
            aside = self.btw_running_aside()
            if aside is None:
                return [("class:frame.border", "┌" + "─" * max(0, columns - 2) + "┐")]
            phase, detail = status_parts(aside.activity)
            line = StatusLine(phase, detail, elapsed=aside.elapsed)
            lead = self.heading_lead()
            status = line.fragments(self.status_spinner_mark(), columns - 3 - len(lead), rule=True)
            if not status:
                return [("class:frame.border", "┌" + "─" * max(0, columns - 2) + "┐")]
            used = len(lead) + sum(cell_len(text) for _, text in status)
            return [
                ("class:frame.border", lead),
                *status,
                ("class:frame.border", f" {'─' * max(0, columns - used - 2)}┐"),
            ]

        return Window(FormattedTextControl(fragments, show_cursor=False), height=1)

    def btw_bar_height(self, attached: bool | None = None) -> int:
        """1 when an open `/btw` thread needs its own strip inside the editor box.

        When idle, the `/btw` heading takes the top border (0 extra rows). While
        the top border carries a running status, startup label, or attached
        tasks, a 1-row strip above the prompt keeps the `/btw` target visible.
        """
        if not self.btw_active() or self.btw_docked():
            return 0
        if self.activity.status_shown:
            return 1
        if attached is None:
            attached = self.plan_attached()
        if attached:
            return 1
        return int(self.startup_border_active(attached=attached))

    def btw_label(self, width: int) -> list[tuple[str, str]]:
        """The open `/btw` thread badge, question excerpt, model, and escape hint.

        A terminal that draws the drawer's key footer (`popup_hints`) shows
        the keys there instead.
        """
        from pcode.btw_view import current, position
        from pcode.conversation_tree import excerpt
        from pcode.theme import native_hints_supported
        from pcode.tool_display import literal

        thread = current(self.activity)
        if not thread:
            return []
        first, newest = thread[0], thread[-1]
        number, count = position(self.activity)
        badge = f" btw {number}/{count} " if count > 1 else " btw "
        tail: list[tuple[str, str]] = []
        if newest.label:
            tail.append(("class:btw.hint", f" · {plain(newest.label, limit=40)}"))
        if not native_hints_supported():
            tail.append(("class:btw.hint", " · Esc back"))
        tail_width = sum(cell_len(text) for _, text in tail)
        q_budget = width - cell_len(badge) - tail_width - 1
        raw_question = excerpt(literal(first.question))
        question = (
            plain(raw_question, limit=max(8, q_budget)) if raw_question and q_budget >= 8 else ""
        )
        parts: list[tuple[str, str]] = [("class:btw.badge", badge)]
        if question:
            parts.append(("class:btw.heading", f" {question}"))
        parts.extend(tail)
        return fit_fragments(parts, width)

    def btw_heading(self) -> list[tuple[str, str]]:
        """The idle editor box's top-border heading while replying to a `/btw` thread."""
        chrome = len(self.heading_lead()) + 3 if self.frame_shaded() else TITLE_CHROME + 1
        return self.btw_label(self.size().columns - chrome)

    def btw_bar_fragments(self) -> list[tuple[str, str]]:
        """The 1-row `/btw` target strip inside the editor box when the top border is busy."""
        lead = " " * self.task_padding()
        return [("", lead), *self.btw_label(self.size().columns - 2 - len(lead))]

    def native_composer_chrome(self) -> bool:
        """True when Pcode.app renders the docked composer's status & steering strip natively."""
        from pcode.ui_surface import native_transcript_active

        return native_transcript_active() and not self.btw_docked()

    def native_composer_payload(self) -> dict:
        """Coarse live status + send-mode payload for Pcode.app's native composer strip."""
        activity = self.activity
        state = self.composer_state() if self.composer_state is not None else {}
        busy = bool(activity.status_shown or activity.startup_label or state.get("starting"))
        phase = ""
        if activity.startup_label:
            phase = plain(activity.startup_label, limit=None)
        elif activity.status_shown:
            if activity.prompt_kind != "user":
                phase = plain(activity.prompt, limit=None)
            elif agents := activity.tools.delegates:
                count = len(agents)
                phase = f"Waiting for {count} agent{'s' * (count > 1)}"
            elif activity.tools.active is not None and activity.tools.active.settled is None:
                running = activity.tools.running
                phase = "Working" if running < 2 else f"Running {running} tools"
            else:
                raw_phase = status_parts(activity.status)[0]
                if (
                    raw_phase.startswith("Running")
                    and not activity.tools.running
                    and not activity.user_command
                ):
                    phase = "Waiting for model"
                else:
                    phase = raw_phase or "Thinking"
        elif state.get("starting"):
            phase = "Starting"
        return {
            "busy": busy,
            "phase": phase,
            "send_mode": str(state.get("send_mode") or "steering"),
            "send_mode_once": bool(state.get("send_mode_once")),
            "starting": bool(state.get("starting")),
            "steering_pending": int(state.get("steering_pending") or 0),
            "queued": int(state.get("queued") or 0),
            "jobs": int(state.get("jobs") or 0),
            "btw_running": int(state.get("btw_running") or 0),
            "btw_ready": int(state.get("btw_ready") or 0),
            "cache_note": str(state.get("cache_note") or ""),
            # Unsent text: Pcode.app doesn't hibernate a pane that would lose it.
            "draft": bool(self.session.default_buffer.text.strip()),
        }

    def sync_native_composer(self, app) -> None:
        """Send `pcode;composer;<base64 json>` to the docked surface whenever state changes."""
        from pcode.ui_surface import native_transcript_active

        if not native_transcript_active() or app.is_done:
            return
        from pcode.drawer import composer_status

        payload = self.native_composer_payload()
        if payload == self._composer_sent:
            return
        self._composer_sent = payload
        app.output.write_raw(composer_status(payload))
        app.output.flush()

    def top_row_filled(self) -> bool:
        """Whether the editor's top row carries text: a status, tasks, or a title."""
        if self.btw_docked():
            return self.btw_running_aside() is not None
        if self.native_composer_chrome():
            return self.plan_attached()
        return (
            self.activity.status_shown
            or bool(self.activity.startup_label)
            or self.plan_attached()
            or self.btw_active()
            or bool(self.idle_title())
        )

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
        if self.btw_docked():
            return int(self.frame_shaded() and self.btw_running_aside() is not None)
        if attached is None:
            attached = self.plan_attached()
        if self.btw_bar_height(attached=attached):
            return 0
        if self.native_composer_chrome():
            return 0
        return int(
            self.frame_shaded()
            and not attached
            and not self.band_rows()
            and (
                self.activity.status_shown
                or bool(self.activity.startup_label)
                or self.btw_active()
                or bool(self.idle_title())
            )
        )

    def frame_shaded(self) -> bool:
        """Whether the boxes are drawn as shades rather than lines (`frame_style`)."""
        return (
            (self.activity.frame_style == "shaded" or dock.active() is not None)
            and self.transcript is not None
            and self.transcript.frame_shade() is not None
        )

    def editor_frame(self) -> Frame:
        session = self.session
        editor_frame = Frame(
            self.editor, title=self.idle_title, height=self.frame_height, style="class:shaded"
        )
        # The terminal's completion list sits above its top row.
        self.frame = editor_frame.container
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
                    filter=Condition(
                        lambda: (
                            session.app.editing_mode == EditingMode.VI
                            and not self.badge_on_toolbar()
                        )
                    ),
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
            ConditionalContainer(
                VSplit(
                    [
                        side(),
                        Window(
                            FormattedTextControl(self.btw_bar_fragments, show_cursor=False),
                            height=1,
                        ),
                        side(),
                    ],
                    height=1,
                ),
                filter=Condition(lambda: bool(self.btw_bar_height())),
            ),
            middle,
            bottom,
        ]
        # The top border carries the running status, else the attached tasks'
        # heading. Attached tasks sit under it, and a divider separates them
        # from the text; frame_height counts both.
        status = Condition(
            lambda: (
                self.activity.status_shown
                and not self.btw_docked()
                and not self.native_composer_chrome()
            )
        )
        btw_status = Condition(lambda: self.btw_running_aside() is not None)
        startup = Condition(self.startup_border_active)
        attached = Condition(self.plan_attached)
        shaded = Condition(self.frame_shaded)
        btw = Condition(lambda: self.btw_active() and not self.btw_docked())
        btw_top = btw & ~status & ~startup & ~attached
        idle_top = ~attached & ~status & ~btw_status & ~startup & ~btw_top

        # Showing a /btw thread inline, the box takes the thread's colour so it
        # is clear Enter goes to the thread. Docked inside the drawer itself,
        # the card keeps the drawer's normal style.
        editor_frame.container.style = lambda: (
            "class:frame class:shaded"
            + (" class:btw.reply" if self.btw_active() and not self.btw_docked() else "")
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
                        ConditionalContainer(self.btw_status_border(), filter=btw_status),
                        ConditionalContainer(self.startup_border(), filter=startup),
                        ConditionalContainer(self.plan_heading_border(), filter=attached & ~status),
                        ConditionalContainer(
                            self.heading_border(self.btw_heading),
                            filter=btw_top,
                        ),
                        ConditionalContainer(top, filter=idle_top & ~shaded),
                        # Shaded, the session's name lines up with the prompt chevron.
                        ConditionalContainer(
                            self.heading_border(self.idle_title),
                            filter=idle_top & shaded,
                        ),
                        self.band_body(side),
                    ],
                    style=lambda: "class:shaded.top" if self.top_row_filled() else "",
                ),
                ConditionalContainer(divider(), filter=band & Condition(self.band_divided)),
                ConditionalContainer(
                    HSplit([VSplit([side(), self.plan_body(), side()]), divider()]),
                    filter=attached,
                ),
            ]
        )
        return editor_frame

    def toolbar_shown(self) -> bool:
        """Whether the main conversation's bottom toolbar is shown."""
        return (
            self.session.bottom_toolbar is not None
            and not self.btw_docked()
            and not self.native_composer_chrome()
        )

    def badge_on_toolbar(self) -> bool:
        """Docked, the vi mode badge floats right on the status bar, not in the box."""
        return dock.active() is not None and self.toolbar_shown()

    def bottom_toolbar(self) -> Container:
        session = self.session
        return VSplit(
            [
                Window(
                    FormattedTextControl(
                        lambda: session.bottom_toolbar, style="class:bottom-toolbar.text"
                    ),
                    style="class:bottom-toolbar",
                ),
                ConditionalContainer(
                    Label(lambda: editor_mode_badge(session.app), dont_extend_width=True),
                    filter=Condition(
                        lambda: (
                            session.app.editing_mode == EditingMode.VI and self.badge_on_toolbar()
                        )
                    ),
                ),
            ],
            height=1,
        )

    def layout(self) -> Layout:
        children = [
            self.menu,
            ConditionalContainer(
                self.activity_panel(),
                filter=Condition(lambda: not self.btw_docked()),
            ),
            self.queued(),
            # This prompt can be only three rows tall. A float would inherit
            # that height and clip the menu, so reserve space only while open.
            shortcut_hint(self.shortcuts),
            self.editor_frame(),
        ]
        docked = dock.active() is not None
        if self.transcript is not None and not docked:
            children[:0] = [self.typing(), self.glide_reserve(), Window()]
        if self.session.bottom_toolbar is not None:
            children.append(
                ConditionalContainer(
                    self.bottom_toolbar(),
                    filter=Condition(self.toolbar_shown),
                )
            )
        self.root = _MeasuredSplit(
            children,
            align=VerticalAlign.JUSTIFY if self.transcript and not docked else VerticalAlign.BOTTOM,
        )
        return Layout(self.root, focused_element=self.editor)

    # Redraw hooks: the per-render cache and the spinner's animation timer.

    def needs_animation(self) -> bool:
        activity = self.activity
        terminal_spinner = (
            activity.busy or activity.status_shown
        ) and not self.native_composer_chrome()
        return (
            terminal_spinner
            or self.startup_border_active()
            # Keep redrawing while a notice is live: nothing else will ask for
            # the frame that finally removes it.
            # A toast fades by itself.
            or (activity.notice_shown and not self.native_toasts)
            or activity.asides_running
            or self.btw_running_aside() is not None
            or bool(activity.waits)
            or (activity.tasks_shown and activity.tools.animating)
            # Nothing else may redraw when a waiting command preview is due.
            or self.command_waiting
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
        if self.root is not None:
            self.root.begin_frame()
        self.btw.sync()
        transcript, activity = self.transcript, self.activity
        if (
            self.kitty_spinners is not None
            and self.kitty_spinners.enabled
            and transcript is not None
        ):
            self.kitty_spinners.sync(
                transcript.chrome_palette,
                transcript.palette,
                write_raw=app.output.write_raw,
                flush=app.output.flush,
                invalidate=app.invalidate,
                schedule=app.create_background_task,
            )
        if transcript is None or not (
            (transcript.live_edit_previews and activity.edit_previews)
            or (transcript.command_preview and activity.command_outputs)
            or any(key.startswith(WATCHED_PREFIX) for key in activity.command_outputs)
        ):
            self.preview_body.cache_clear()

    def after_render(self, app) -> None:
        if (docked := dock.active()) is not None:
            docked.report_rows(app)
        self.render_cache = None
        # A redraw caused by input or application events starts animation again.
        # Idle prompts have no timer; toolkit owns cancellation at app shutdown.
        if self.needs_animation() and app.is_running:
            if self.animation_task is None or self.animation_task.done():
                self.animation_task = app.create_background_task(self.animate(app))
        elif self.animation_task is not None:
            self.animation_task.cancel()
            self.animation_task = None
        self.sync_native_menu(app)
        self.sync_native_toast(app)
        self.sync_native_composer(app)
        self.replay_after_menu(app)
        self.refill_after_shrink(app)

    # The completion menu, where the terminal draws it (`native_completions`).

    def _native_completions(self):
        from pcode.native_completions import NativeCompletions
        from pcode.preferences import overlay_popups
        from pcode.theme import native_completion_supported

        if self.transcript is None or not native_completion_supported() or not overlay_popups():
            return None

        def write_raw(text: str) -> None:
            output = self.session.app.output
            output.write_raw(text)
            output.flush()

        native = NativeCompletions(write_raw, self.accept_native_completion)
        native.on_change = lambda: self.session.app.invalidate()
        return native

    def sync_native_menu(self, app) -> None:
        """Show the terminal the editor's completion menu as just drawn, or hide it."""
        from pcode.native_completions import menu_message, start_column, window_offset

        native = self.native
        if native is None:
            return
        # Opened as the editor first draws, so it is ready by the first `/`.
        native.connect()
        if not native.active:
            return
        buffer = self.session.default_buffer
        state = buffer.complete_state
        place = None
        if state is not None and state.completions and app.layout.has_focus(buffer):
            place = self._native_place(app)
        if state is None or place is None or app.is_done:
            self.native_completions = None
            native.hide()
            return
        cursor, dy = place
        selected = state.complete_index
        native.offset = window_offset(native.offset, selected, len(state.completions))
        # A click is taken only on this list: a newer one may have other rows.
        self.native_completions = state.completions
        native.show(
            menu_message(
                state.completions,
                selected,
                native.offset,
                dx=start_column(state, buffer.document),
                dy=dy,
                cursor=(cursor.x, cursor.y),
                style=self._native_style(app),
            )
        )

    def _native_toasts(self) -> bool:
        from pcode.preferences import overlay_popups
        from pcode.theme import native_toast_supported

        return self.transcript is not None and native_toast_supported() and overlay_popups()

    def sync_native_toast(self, app) -> None:
        """Show the terminal each new notice once, as a toast over the editor.

        A notice cleared before it expires (a job's progress) hides the toast.
        """
        from pcode.drawer import hide_toast, toast

        activity = self.activity
        if not self.native_toasts or app.is_done:
            return
        if not activity.notice_shown:
            sent, self.toast_sent = self.toast_sent, None
            if sent is not None and monotonic() < sent[1]:
                app.output.write_raw(hide_toast())
                app.output.flush()
            return
        notice = (activity.notice, activity.notice_expires)
        if notice == self.toast_sent:
            return
        # Placed above the editor's box: wait for a render that draws it.
        place = self._native_place(app)
        if place is None:
            return
        self.toast_sent = notice
        seconds = activity.notice_expires - monotonic()
        app.output.write_raw(toast(activity.notice, rows_above=place[1], seconds=seconds))
        app.output.flush()

    def _native_place(self, app):
        """The cursor, and rows from the editor box's top to it; None if the editor isn't drawn."""
        screen = app.renderer._last_screen
        cursor = screen.cursor_positions.get(self.editor) if screen is not None else None
        if cursor is None:
            return None
        positions = screen.visible_windows_to_write_positions
        tops = [
            positions[window].ypos
            for window in (walk(self.frame) if self.frame is not None else ())
            if window in positions
        ]
        return cursor, (max(0, cursor.y - min(tops)) if tops else 0)

    def _native_style(self, app) -> dict[str, str]:
        from pcode.pick_ui import native_style

        key = app.style.invalidation_hash()
        if self.native_style is None or self.native_style[0] != key:
            self.native_style = (key, native_style(app.style))
        return self.native_style[1]

    def accept_native_completion(self, index: int) -> None:
        """A row of the terminal's list was clicked: take it, as Enter on it would."""
        buffer = self.session.default_buffer
        state = buffer.complete_state
        if (
            state is None
            or state.completions is not self.native_completions
            or not 0 <= index < len(state.completions)
        ):
            return
        buffer.go_to_completion(index)
        buffer.complete_state = None
        self.session.app.invalidate()

    def layout_gap(self, app) -> int:
        """Blank rows above the layout that scrolled-away transcript used to fill.

        prompt_toolkit never shrinks its screen (each render keeps at least the
        last height). When the draft or the panel grows past the rows below
        scrollback, the terminal scrolls transcript off the top; when it shrinks
        again, the justifying filler holds those rows blank. That is the screen
        height beyond both the rows the layout was given and what it now needs.
        """
        renderer = app.renderer
        screen, available = renderer._last_screen, renderer._min_available_height
        drawn = getattr(self.root, "drawn", None)
        if renderer._in_alternate_screen or screen is None or available <= 0 or drawn is None:
            return 0
        return max(0, screen.height - max(available, drawn[2]))

    def refill_after_shrink(self, app) -> None:
        """Rebuild scrollback once a shrunk layout has settled, filling its blank rows.

        Debounced: deleting a tall draft line by line (or holding backspace)
        changes the gap on every render, and each rebuild clears and repaints
        the whole screen, so wait until it has held for `REFILL_DELAY`.
        """
        transcript = self.transcript
        if transcript is None or not transcript.replays_on_resize or dock.active() is not None:
            return
        # The menu's own replay covers its close, at once. Mid-turn, the next
        # write fills the gap anyway; a rebuild then would only repaint the
        # stream, so a gap left at the turn's end is filled once it settles.
        refills = self.session.default_buffer.complete_state is None and not self.activity.busy
        gap = self.layout_gap(app) if refills else 0
        if gap != self._gap:
            self._gap, self._gap_changed, self._refilled = gap, monotonic(), False
        if gap > 0 and not self._refilled and self._refill_task is None:
            self._refill_task = app.create_background_task(self._refill())

    async def _refill(self) -> None:
        try:
            while (wait := self._gap_changed + REFILL_DELAY - monotonic()) > 0:
                await asyncio.sleep(wait)
            if self._gap > 0 and not self._refilled and self.transcript is not None:
                self._refilled = True
                self.transcript.regenerate()
        finally:
            self._refill_task = None

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
            # This replay fills the gap the menu left; no debounced one too.
            self._refilled, self._gap = True, self.layout_gap(app)


def create_prompt(
    registry: CommandRegistry,
    *,
    activity: Activity | None = None,
    transcript: "Transcript | None" = None,
    workspace=None,
    on_submit=None,
    on_command=None,
    on_cancel=None,
    on_busy_exit=None,
    on_effort=None,
    on_model=None,
    on_panel=None,
    on_thinking=None,
    on_commands=None,
    on_send_mode=None,
    on_interrupt_submit=None,
    on_previous_session=None,
    on_copy_response=None,
    on_paste_image=None,
    on_btw=None,
    on_btw_next=None,
    on_btw_close=None,
    key_prefix: str | None = None,
    session_title: Callable[[], str] = lambda: "",
    composer_state: Callable[[], dict] | None = None,
    drafts: Drafts | None = None,
    kitty_spinners: kitty_spinner.KittySpinners | None = None,
    **kwargs,
) -> PromptSession:
    configure_newline_keys()
    install_fast_layout_division()
    activity = activity or Activity()
    callbacks = PromptCallbacks(
        on_cancel=on_cancel,
        on_busy_exit=on_busy_exit,
        on_effort=on_effort,
        on_model=on_model,
        on_panel=on_panel,
        on_thinking=on_thinking,
        on_commands=on_commands,
        on_send_mode=on_send_mode,
        on_interrupt_submit=on_interrupt_submit,
        on_previous_session=on_previous_session,
        on_copy_response=on_copy_response,
        on_paste_image=on_paste_image,
        on_btw=on_btw,
        on_btw_next=on_btw_next,
        on_btw_close=on_btw_close,
    )
    # Unsent text survives a reset as a history entry and, given `drafts` with
    # a directory, a session that ends: what earlier ones left starts the history.
    drafts = drafts or Drafts()
    kwargs.setdefault("history", InMemoryHistory(drafts.load()))
    keys, shortcuts = prompt_key_bindings(
        activity, transcript, callbacks, key_prefix, lambda: session.default_buffer, drafts
    )

    def btw_replying() -> bool:
        from pcode.btw_view import current

        return on_btw_close is not None and bool(current(activity))

    def btw_inline_indicator() -> bool:
        # In terminal/undocked mode, the main editor stays under the conversation,
        # so the `btw ❯ ` prefix makes it clear Enter goes to the side thread.
        # Docked, the card moves inside the drawer itself, so the prefix isn't needed.
        return btw_replying() and dock.active() is None

    def thread_commands() -> tuple[tuple[str, str], ...]:
        from pcode.btw_view import THREAD_COMMANDS

        return THREAD_COMMANDS if btw_replying() else ()

    def prompt_message() -> list[tuple[str, str]]:
        text = session.default_buffer.text
        if text.lstrip().startswith(SHELL_PREFIX):
            return [("class:prompt", SHELL_PROMPT_PREFIX)]
        if btw_inline_indicator():
            return [("class:btw.prompt", BTW_PROMPT_PREFIX)]
        return [("class:prompt", PROMPT_PREFIX)]

    def prompt_prefix_width() -> int:
        text = session.default_buffer.text
        if text.lstrip().startswith(SHELL_PREFIX):
            return get_cwidth(SHELL_PROMPT_PREFIX)
        if btw_inline_indicator():
            return get_cwidth(BTW_PROMPT_PREFIX)
        return get_cwidth(PROMPT_PREFIX)

    def prompt_continuation(width: int, line: int, soft: bool) -> list[tuple[str, str]]:
        if soft:
            return [("class:prompt", " " * width)]
        pad = max(0, width - get_cwidth(CONTINUATION_PREFIX))
        is_shell = session.default_buffer.text.lstrip().startswith(SHELL_PREFIX)
        style = "class:btw.prompt" if btw_inline_indicator() and not is_shell else "class:prompt"
        return [(style, " " * pad + CONTINUATION_PREFIX)]

    kwargs.setdefault("color_depth", color_depth())
    # Block in vi navigation, bar in insert (and emacs), underline in replace.
    # Every renderer reset (exit, each handoff) sends CSI 0 q, so the
    # terminal's own shape is back whenever the editor isn't painting.
    kwargs.setdefault("cursor", ModalCursorShapeConfig())
    output = kwargs.pop("output", None)
    if not isinstance(output, CursorSafeOutput):
        output = CursorSafeOutput(output if output is not None else create_output())
    session = PromptSession(
        output=output,
        message=prompt_message,
        prompt_continuation=prompt_continuation,
        # Wrapped rows match the active prompt width (2 normally, 6 in `/btw` reply).
        input_processors=[WordWrapProcessor(prefix_width=prompt_prefix_width)],
        multiline=True,
        erase_when_done=True,
        completer=merge_completers(
            [SlashCompleter(registry, thread_commands), FileReferenceCompleter(workspace)]
        ),
        lexer=ReferenceLexer(),
        complete_while_typing=Condition(_completes_while_typing),
        reserve_space_for_menu=0,
        auto_suggest=AutoSuggestFromHistory(),
        key_bindings=shortcuts.key_bindings(keys),
        mouse_support=False,
        **kwargs,
    )
    session.shortcuts = shortcuts
    drafts.buffer = session.default_buffer
    session.drafts = drafts
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

    prompt_layout = PromptLayout(
        session,
        activity,
        transcript,
        shortcuts,
        session_title,
        kitty_spinners=kitty_spinners,
        composer_state=composer_state,
    )
    if transcript is not None:

        def accept(buffer):
            # True keeps the draft: something refused it with a reason shown.
            return drafts.accept(buffer, on_submit)

        session.default_buffer.accept_handler = accept
    session.btw_view = prompt_layout.btw
    session.layout = prompt_layout.layout()
    session.app.layout = session.layout
    if transcript is not None:
        # The app runs for the whole session, redrawn by streaming output and
        # animation; cap that at the paced output's frame rate.
        session.app.min_redraw_interval = 1 / 30
    docked = transcript is not None and dock.active() is not None
    if not docked:
        install_resize_repaint(session.app)
    session.app.before_render += prompt_layout.before_render
    session.app.after_render += prompt_layout.after_render
    if session.app.editing_mode == EditingMode.VI:
        # Allow terminal escape sequences to arrive, without a half-second pause.
        session.app.ttimeoutlen = 0.1
    if docked:
        # SPIKE: the dock is the editor's alone, so draw it full screen there:
        # no cursor reports, no rows above it to keep track of.
        session.app.full_screen = True
        session.app.renderer = Renderer(
            session.app._merged_style,
            session.app.output,
            full_screen=True,
            mouse_support=False,
            cpr_not_supported_callback=session.app.cpr_not_supported_callback,
        )
        app = session.app

        def repaint_docked() -> None:
            # prompt_toolkit erases on a resize and paints on a later pass,
            # leaving the cursor parked at the top in between. Full screen
            # needs no erase: repaint whole, at once, as one frame.
            if not app._is_running or app.output.get_size() == app.renderer._last_size:
                # Only pcode's own pane resized (its window): the dock's grid
                # is fixed, and already shows the right frame. The resize
                # reset the pane's scroll region, so look now, which sets it
                # again as soon as the cursor's row is in.
                if (docked := dock.active()) is not None:
                    docked.pane_rows()
                return
            # Home and clear first: `reset` assumes the cursor is at the top
            # left, and a paint from wherever it really is doubles the layout.
            app.output.write_raw(SYNC_START + "\x1b[H\x1b[2J")
            app.renderer.reset(leave_alternate_screen=False)
            app._redraw()
            app.output.write_raw(SYNC_END)
            app.output.flush()

        app._on_resize = repaint_docked
    elif transcript is None or not transcript.replays_on_resize:
        install_reflow_renderer(session.app)
    elif smooth_scroll.worth_asking():
        install_nowrap_editor(session.app)

    return session


def tally(succeeded: int, failed: int, nonzero: int = 0) -> str:
    """`✓ 2 • 1 ✗ 1`, dropping zeros, so each mark only ever counts its own outcome."""
    return " ".join(
        f"{mark} {count}"
        for mark, count in (
            (glyphs.icon("ok"), succeeded),
            (glyphs.icon("nonzero"), nonzero),
            (glyphs.icon("fail"), failed),
        )
        if count
    )


def tool_count(name: str, count: int, failed: int, nonzero: int = 0) -> str:
    """One tool's part of a group line: `Read file ✓ 3`, `Search code ✓ 2 ✗ 1`."""
    return f"{name} {tally(count - failed - nonzero, failed, nonzero)}"


def exited_nonzero(event: ToolSummary) -> bool:
    """A command that ran and exited non-zero without failing (`grep` found nothing)."""
    return not event.failed and bool(event.exit_code)


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
        probe_cursor: bool = False,
        probe_glide: bool = False,
    ) -> None:
        # `probe_width`: also measure the terminal's ambiguous-width glyphs
        # (`pcode.theme`), for the editor's terminal only: its late replies
        # need the editor's input to take them out. `probe_glide`, the same
        # for whether it glides (the ``smooth_scroll`` setting).
        preferences = load_preferences() if preferences is None else preferences
        self.error_scrollback_lines = int(preferences.get("error_scrollback_lines", "20"))
        self.tool_error_scrollback = preferences.get("tool_error_scrollback", "off") == "on"
        self.show_edits = preferences.get("show_edits", "on") == "on"
        self.live_edits = preferences.get("live_edits", SETTINGS["live_edits"].default) == "on"
        # `show_commands`: see COMMAND_MODES.
        mode = canonical("show_commands", preferences.get("show_commands", "off"))
        self.command_mode = mode if mode in COMMAND_MODES else "off"
        self.group_tools = preferences.get("group_tools", SETTINGS["group_tools"].default) == "on"
        self.command_scrollback_lines = int(preferences.get("command_scrollback_lines", "20"))
        # Rows, or a share of the screen; resolved against the pane when drawn.
        self.command_preview_lines = parse_height(
            preferences.get("command_preview_lines")
        ) or float(SETTINGS["command_preview_lines"].default)
        self.activity = activity
        self.console = console
        self.theme = theme
        # Experimental, and only for Ghostty Smooth, which the probe asks after.
        self.smooth_scroll = preferences.get("smooth_scroll", SETTINGS["smooth_scroll"].default)
        self.detected_theme = (
            detect_theme(
                probe_width=probe_width,
                probe_cursor=probe_cursor,
                # Reader mode marks need the answer too, glide or not; the
                # drawer query already makes this Ghostty-only round trip.
                probe_glide=probe_glide,
                # Asked whatever `popup_view` says, so turning it on needs no restart.
                probe_drawer=probe_glide,
                probe_kitty=probe_glide
                and preferences.get("smooth_spinner", SETTINGS["smooth_spinner"].default) == "auto",
            )
            if detected_theme is None
            else detected_theme
        )
        self.syntax_themes = syntax_themes(preferences)
        self._delta = delta_from_preferences(preferences)
        self.diff_dedent = preferences.get("diff_dedent", SETTINGS["diff_dedent"].default) == "on"
        self._output: TerminalOutput | None = None
        self.regenerate_on_resize = preferences.get("regenerate_on_resize", "on") == "on"
        self.paced_scrollback = preferences.get(
            "paced_scrollback", SETTINGS["paced_scrollback"].default
        )
        self.reader_park = preferences.get("reader_park", SETTINGS["reader_park"].default)
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
            output.gliding = output.paced and self.smooth_scroll == "auto" and glide_supported()
            output.typed = output.paced and self.paced_scrollback == "typed"
            if (docked := dock.active()) is not None:
                # SPIKE: the glide moves to pcode's own pane (`docked_glide`);
                # without it, typing would draw into the live panel, on the dock.
                output.docked_glide = output.gliding
                output.gliding = False
                docked.glide = output._docked_room
                if not output.docked_glide:
                    output.typed = False
            output.marks = self.console.is_terminal and marks_supported()
            output.park = output.marks and self.reader_park == "on"
            if self.replays_on_resize:
                output.resize_replay = self.replay
            if docked is not None and docked.owns_tty:
                # Editor-only: the transcript is shown elsewhere; nothing to pace or glide.
                output.discard = True
                output.paced = output.gliding = output.docked_glide = output.typed = False
                output.marks = output.park = False
                output.resize_replay = None
                docked.glide = None

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
            if isinstance(obj, (RichMarkdown, RetainedMarkdown))
            else replace(obj, code_theme=self.code_theme, delta=self.delta, dedent=self.diff_dedent)
            if isinstance(obj, EditTranscript)
            else replace(obj, code_theme=self.code_theme)
            if isinstance(obj, CommandTranscript)
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

    def _pop_descendants(self, call_id: str) -> list[ToolSummary]:
        """Pop all buffered child and nested descendant summaries under `call_id`."""
        direct = self._children.pop(call_id, [])
        if not direct:
            return []
        collected: list[ToolSummary] = []
        for child in direct:
            collected.append(child)
            if child.call_id and child.call_id in self._children:
                collected.extend(self._pop_descendants(child.call_id))
        return collected

    @recorded
    def tool_result(self, event: ToolSummary) -> None:
        """Retain hidden results too; choose one representation on each replay."""
        if event.parent_call_id:
            if self.summarizes(event):
                self._children.setdefault(event.parent_call_id, []).append(event)
            return
        if self.writes_tool_result(event):
            self.events((event,))
        for line in self.child_lines(self._pop_descendants(event.call_id), CHILD_INDENT):
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
        nonzeros = Counter(label(event.name) for event in events if exited_nonzero(event))
        failed, nonzero = failures.total(), nonzeros.total()
        parts = [
            tool_count(name, count, failures[name], nonzeros[name])
            for name, count in counts.most_common()
        ]
        head = tally(len(events) - failed - nonzero, failed, nonzero)
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
        nonzero = sum(exited_nonzero(event) for event in self._group)
        noun = "tool" if len(self._group) == 1 else "tools"
        return f"{tally(len(self._group) - failed - nonzero, failed, nonzero)} {noun}"

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

    def writes_edit(self, event: EditCompleted) -> bool:
        """Report whether this change's diff is written to scrollback.

        A sub-agent's is not: its edit is a line beneath its delegate, like
        its other calls, and `/diffs` has the diff.
        """
        return self.show_edits and not event.parent_call_id

    @recorded
    def edit(self, event) -> None:
        if self.writes_edit(event):
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
    def command_scrollback(self) -> bool:
        """Whether a finished command's output is mirrored into scrollback."""
        return self.command_mode == "scrollback"

    @command_scrollback.setter
    def command_scrollback(self, mirrored: bool) -> None:
        self.command_mode = "scrollback" if mirrored else "off"

    @property
    def command_preview(self) -> bool:
        """Whether a running command's output shows live in the activity panel."""
        return self.command_mode != "off"

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
        # SPIKE: docked, the card is the frame: the box goes "shaded" in the
        # terminal's own background, so its lines vanish and its rows stay.
        shaded = frame_style == "shaded" or dock.active() is not None
        shades = self.frame_shades() if shaded else None
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
        if (docked := dock.active()) is not None:
            # SPIKE: one raised surface: the dock's own background (`Dock.tint`)
            # and every box in it share the lifted shade.
            lifted = frame_shade(background, theme)
            docked.tint = lifted
            return lifted, lifted
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

    def welcome(self, model: str | None = None, workspace: str = "") -> int:
        header = Text.assemble(
            ("◆ ", "pcode.accent"),
            ("pcode", "pcode.brand"),
            (f"  /  {model or 'UI preview'}", "pcode.muted"),
        )
        notes = (
            (
                f"Coder · workspace: {workspace}",
                "Live model · file edits and shell tools enabled · not a sandbox",
            )
            if model
            else ("Local only · no model connected · no files or shell tools",)
        ) + ("Type / for commands, /theme-preview for a sample response, /help for keys.",)
        self.print()
        self.print(header)
        for text in notes:
            self.retained_note(text)
        self.print()
        options = self.console.options.update(overflow="fold")
        return (
            2
            + len(self.console.render_lines(header, options=options, pad=False))
            + sum(
                len(self.console.render_lines(Note(text), options=options, pad=False))
                for text in notes
            )
        )

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
        elif self.output is not None:
            # Preserve new notices across an already queued redraw, but never
            # replay notices that have previously reached the terminal.
            self.output.print(notice, transient=True)
        else:
            with self.console.use_theme(self.rich_theme):
                self.console.print(notice)

    @recorded
    def error(self, text: str, *, title: str = "Error") -> None:
        self.print(ErrorNotice(text, title, self.error_scrollback_lines))

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
        # job's exit status is the job's; the helper call itself succeeded;
        # only actual helper errors/retries need their own scrollback entry.
        if event.name in {"wait_for_job", "job_output"} and event.outcome == "success":
            return False
        # A command always leaves at least the summary line every other tool
        # leaves; `show_commands` governs only its mirrored output.
        if event.name in COMMAND_TOOLS:
            return True
        if event.failed:
            return True
        # A sub-agent's diff is never shown, so its edit keeps its line.
        shown_as_diff = event.name in EDIT_TOOLS and self.show_edits and not event.parent_call_id
        return event.name not in PLAN_TOOLS and not shown_as_diff

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
                exit_code=event.exit_code,
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
        self.print(WarningNotice(text))

    @recorded
    def cancelled(self) -> None:
        self.settle_orphans()
        self.print(CancelledNotice())

    @recorded
    def user(self, text: str) -> None:
        self.settle_orphans()
        self.print()
        self.print(TaskPrompt(text))
        self.print()

    def summary_lines(self, event: ToolSummary, *, indent: int = 0) -> list[Text]:
        """The compact rows a settled call leaves in scrollback."""
        if event.name not in COMMAND_TOOLS and not event.command:
            mark = outcome_icon(event.failed, event.exit_code)
            return [
                Text.assemble(
                    (f"{mark} {label(event.name)}  ", "pcode.thinking"),
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
            if event.failed
            or event.exit_code
            or (event.name != "run_command" and " → " in event.detail)
            else ""
        )
        return tool_summary_lines(
            event.name,
            result,
            failed=event.failed,
            exit_code=event.exit_code,
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
            "(next request)"
        )
        self.note(f"{previous} back to the previous session (/attach -)")
        self.note(f"{key('y')} copy the draft · Ctrl+C discard input · Ctrl+D exit on empty input")
        self.note(
            f"During a run: Enter sends · {key('s')} picks steering/queue/interrupt for the "
            "next send. Ctrl+C discards a draft first, then cancels · Ctrl+D cancels, keeps draft."
        )
        self.note("Cancellation clears queued messages. Use terminal/tmux scrollback for history.")
        self.print()
