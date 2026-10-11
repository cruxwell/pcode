"""Temporary alternate-screen tool browser, separate from the inline editor."""

import json
import subprocess
import sys
import threading
from functools import lru_cache

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, VSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Label, TextArea
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from pcode.clipboard import copy as copy_to_clipboard
from pcode.delta import Delta
from pcode.edit_transcript import EditTranscript, created_markdown
from pcode.edits import patch_text
from pcode.frame import Frame
from pcode.inspection import InspectedCall, ToolArchive
from pcode.popup_ui import (
    RichPane,
    bind_list_paging,
    list_pane_height,
    popup_container,
    popup_mouse,
    popup_style,
    steer_list_from_query,
)
from pcode.prefix_keys import PrefixKeys
from pcode.text_selection import copy_drags

STATE_STYLES = {"failed": "bold red", "succeeded": "bold green", "running": "bold yellow"}

# Argument keys whose values are code rather than prose, and the lexer for each.
CODE_KEYS = {"command": "bash", "content": None, "new_text": None, "old_text": None}


def parse_json(text: str) -> object:
    """The decoded payload when it is JSON, else None; captured payloads are often not."""
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def code_block(text: str, lexer: str | None, code_theme: str) -> Syntax:
    return Syntax(text, lexer or "text", theme=code_theme, word_wrap=True)


@lru_cache(maxsize=128)
def format_command(command: str) -> str:
    """Format for display only; never execute or change the copied command."""
    try:
        result = subprocess.run(
            ["shfmt", "-ln", "bash", "-i", "2"],
            input=command,
            capture_output=True,
            text=True,
            timeout=0.25,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired, UnicodeError:
        pass
    else:
        if result.returncode == 0 and result.stdout.strip():
            # shfmt adds a final newline; don't display an extra empty prompt.
            return "\n".join("$ " + line for line in result.stdout.removesuffix("\n").split("\n"))
    return _fallback_format_command(command)


def _fallback_format_command(command: str) -> str:
    """A shell command with a display-only prompt marker on each logical line.

    `;` becomes a line break; `&&` and `||` keep the operator, add a backslash
    continuation, and indent the next command so the chain reads as one
    statement. Separators inside quotes or parentheses are left alone, and a
    command the model already spread over lines keeps its layout. Each line
    starts with `$ `, before any indentation. Only the display changes: copying
    still takes the command verbatim. Soft-wrapped rows are not new lines.
    """
    if "\n" in command.strip():
        return "\n".join("$ " + line for line in command.split("\n"))
    lines: list[str] = []
    current: list[str] = []
    indent = ""
    quote: str | None = None
    depth = 0
    i = 0
    while i < len(command):
        char = command[i]
        if quote:
            current.append(char)
            if char == "\\" and quote == '"' and i + 1 < len(command):
                current.append(command[i + 1])
                i += 1
            elif char == quote:
                quote = None
        elif char == "\\" and i + 1 < len(command):
            current.extend(command[i : i + 2])
            i += 1
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(depth - 1, 0)
            current.append(char)
        elif depth == 0 and command.startswith(("&&", "||"), i) and "".join(current).strip():
            lines.append(indent + "".join(current).strip() + f" {command[i : i + 2]} \\")
            current = []
            indent = "  "
            i += 1
        elif char == ";" and command.startswith(";;", i):
            current.extend(";;")
            i += 1
        elif depth == 0 and char == ";":
            if "".join(current).strip():
                lines.append(indent + "".join(current).strip())
            current = []
            indent = ""
        else:
            current.append(char)
        i += 1
    if "".join(current).strip():
        lines.append(indent + "".join(current).strip())
    return "\n".join("$ " + line for line in (lines or [command]))


def heading(title: str) -> list:
    return [Text(""), Markdown(f"### {title}")]


def arguments_renderables(text: str, code_theme: str) -> list:
    """One row per argument; multi-line or code-like values get their own block."""
    parsed = parse_json(text)
    if not isinstance(parsed, dict) or not parsed:
        return [code_block(text, "json" if parsed is not None else None, code_theme)]
    blocks: list = []
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold")
    later: list[tuple[str, str, str | None]] = []
    for key, value in parsed.items():
        if isinstance(value, str) and (key in CODE_KEYS or "\n" in value):
            later.append((key, value, CODE_KEYS.get(key)))
        else:
            shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            grid.add_row(key, Text(shown))
    if grid.row_count:
        blocks.append(grid)
    for key, value, lexer in later:
        blocks.append(Text(key, style="bold"))
        if key == "command":
            value = format_command(value)
        blocks.append(code_block(value, lexer, code_theme))
    return blocks


def result_renderables(text: str, code_theme: str) -> list:
    """Results get the block treatment arguments already have, never Markdown.

    JSON is highlighted; anything else is verbatim in a plain block, so command
    output and the command that produced it read as the same kind of thing.
    """
    return [code_block(text, "json" if parse_json(text) is not None else None, code_theme)]


class ToolInspector:
    def __init__(
        self,
        archive: ToolArchive,
        *,
        failed: bool = False,
        edits: bool = False,
        rich_theme: Theme | None = None,
        code_theme: str = "ansi_dark",
        delta: Delta | None = None,
        dedent: bool = True,
        color_system: str | None = "truecolor",
        key_prefix: str | None = None,
        **app_options,
    ) -> None:
        self.archive = archive
        self.dedent = dedent
        self.failed = failed
        self.edits = edits
        self.code_theme = code_theme
        self.delta = delta
        self.tool = "All"
        self.names = ["All", *sorted({call.name for call in archive.calls})]
        self.visible = []
        self.selected = None
        self.notice = ""
        self._refreshing = False
        self.query = TextArea(height=1, prompt="Search tools/commands: ", multiline=False)
        self.list = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        copy_drags(self.list, lambda text: setattr(self, "notice", text))
        self.list.window.cursorline = Always()
        self.detail = RichPane(
            theme=rich_theme,
            color_system=color_system,
            notify=lambda text: setattr(self, "notice", text),
        )
        self.query.buffer.on_text_changed += lambda _: self.refresh()
        self.list.buffer.on_cursor_position_changed += lambda _: self.select()
        keys = KeyBindings()
        self.detail.bind_scrolling(keys)

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        def close(event):
            event.app.exit()

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)
        steer_list_from_query(keys, self.query, self.list)
        bind_list_paging(keys, self.list, has_focus(self.list) | has_focus(self.query))

        @keys.add("enter", filter=has_focus(self.query))
        def search_done(event):
            event.app.layout.focus(self.list)

        self.prefix_keys = shortcuts = PrefixKeys(key_prefix)
        shortcuts.set_help(
            lambda: [
                ("Type", "Search in the search field"),
                ("↑/↓", "Select call / scroll details"),
                ("PgUp/PgDn", "Page"),
                ("Ctrl+U/D", "Half page"),
                ("Enter", "Focus calls (from search field)"),
                ("Tab/Shift+Tab", "Change focus"),
                ("Esc/Ctrl+C", "Close"),
            ]
        )

        @shortcuts.add("f", "Search")
        def search(event):
            event.app.layout.focus(self.query)

        @shortcuts.add("x", "Failures only")
        def failures(event):
            self.failed = not self.failed
            self.refresh()

        @shortcuts.add("e", "Edits only")
        def edits_only(event):
            self.edits = not self.edits
            self.refresh()

        @shortcuts.add("t", "Tool filter")
        def tool(event):
            self.tool = self.names[(self.names.index(self.tool) + 1) % len(self.names)]
            self.refresh()

        @shortcuts.add("y", "Copy command")
        def copy_command(event):
            self.copy("command", event.app.output)

        @shortcuts.add("o", "Copy output")
        def copy_output(event):
            self.copy("output", event.app.output)

        header = Label(
            lambda: (
                f"Tool inspector · {len(self.visible)}/{len(self.archive.calls)} calls · "
                f"Status: {'Failed' if self.failed else 'All'} · Tool: {self.tool}"
                + (" · Edits only" if self.edits else "")
                + (f" · {self.notice}" if self.notice else "")
            )
        )
        wide = VSplit(
            [
                Frame(self.list, title="Calls", width=Dimension(weight=2)),
                Frame(self.detail, title="Details", width=Dimension(weight=3)),
            ]
        )
        narrow = HSplit(
            [
                Frame(self.list, title="Calls", height=lambda: list_pane_height(len(self.visible))),
                Frame(self.detail, title="Details"),
            ]
        )
        body = DynamicContainer(
            lambda: wide if get_app().output.get_size().columns >= 100 else narrow
        )
        root = HSplit(
            [
                header,
                self.query,
                body,
                Label(shortcuts.summary),
            ]
        )
        self.app = Application(
            # Open in the search line, so typing filters straight away.
            layout=Layout(popup_container(root, shortcuts), focused_element=self.query),
            key_bindings=shortcuts.key_bindings(keys),
            full_screen=True,
            mouse_support=popup_mouse(shortcuts),
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.prefetched_width = 0
        self.prefetching: threading.Thread | None = None
        self.app.after_render += self.prefetch
        self.refresh()

    def prefetch(self, _app=None) -> None:
        """Render every patch through one background delta run, at the Details width.

        delta lays a diff out for one width, which only a render knows, so this
        waits for one and runs again when a resize or the wide/narrow switch
        changes it. Selecting an edit then reads delta's cache instead of
        starting delta on the event loop.
        """
        info = self.detail.window.render_info
        if self.delta is None or info is None or info.window_width == self.prefetched_width:
            return
        self.prefetched_width = width = info.window_width
        patches = [
            # As EditTranscript shows it, or the prefetched render is never used.
            patch_text(change.patch, dedent=self.dedent)
            for call in self.archive.calls
            for change in call.changes
            if change.patch and created_markdown(change) is None
        ]
        if patches:
            self.prefetching = threading.Thread(
                target=self.delta.prefetch, args=(patches, width), daemon=True
            )
            self.prefetching.start()

    def refresh(self) -> None:
        previous = self.selected
        query = self.query.text.casefold()
        self.visible = [
            call
            for call in reversed(self.archive.calls)
            if (not self.failed or call.state == "failed")
            and (not self.edits or call.changes)
            and (self.tool == "All" or call.name == self.tool)
            and query in call.title().casefold()
        ]
        selected = next((i for i, c in enumerate(self.visible) if c is previous), 0)
        lines = [call.title().replace("\n", " ↵ ") for call in self.visible]
        text = "\n".join(lines) or "No matching tool calls."
        position = sum(len(line) + 1 for line in lines[:selected])
        self._refreshing = True
        self.list.buffer.set_document(Document(text, position), bypass_readonly=True)
        self._refreshing = False
        self.select()

    def select(self) -> None:
        if self._refreshing:
            return
        row = self.list.document.cursor_position_row
        call = self.visible[row] if row < len(self.visible) else None
        if call is self.selected and call is not None:
            return
        self.selected = call
        self.notice = ""
        self.detail.set(self.details(call))

    def payload(self, call: InspectedCall, what: str) -> tuple[str, str]:
        """The text to copy for "command" or "output", and what to call it.

        A call without a command copies its whole arguments payload: the point
        is to get the call out of the inspector, not to hold out for a shell.
        """
        if what == "output":
            return "output", call.result.read()
        parsed = parse_json(call.arguments.read())
        if isinstance(parsed, dict) and isinstance(parsed.get("command"), str):
            return "command", parsed["command"]
        return "arguments", call.arguments.read()

    def copy(self, what: str, output=None) -> None:
        call = self.selected
        if call is None:
            self.notice = "Nothing to copy"
            return
        name, text = self.payload(call, what)
        if not text:
            self.notice = f"No {name} to copy"
            return
        copied, truncated = copy_to_clipboard(text, output)
        limit = " (truncated)" if truncated else ""
        self.notice = f"Copied {name}{limit}" if copied else f"Could not copy {name}"

    def details(self, call: InspectedCall | None) -> list:
        """Rich renderables for the Details pane: metadata grid, then each payload."""
        if call is None:
            return [Text("No matching tool calls.")]
        title = Text(call.name, style="bold")
        title.append(" · ")
        title.append(call.state, style=STATE_STYLES.get(call.state, "bold"))
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", no_wrap=True)
        grid.add_column(overflow="fold")
        for label, value in call.metadata(self.archive.calls):
            grid.add_row(label, Text(value))
        arguments = [
            *heading("Arguments"),
            *arguments_renderables(call.arguments.read(), self.code_theme),
        ]
        result = [
            *heading("Returned result / error"),
            *result_renderables(call.result.read(), self.code_theme),
        ]
        if call.changes:
            # What the call did to the files leads; the raw arguments that
            # asked for it stay below for debugging. The pane scrolls, so the
            # scrollback's row cap does not apply.
            changes = [
                EditTranscript(
                    change,
                    code_theme=self.code_theme,
                    max_rows=sys.maxsize,
                    delta=self.delta,
                    dedent=self.dedent,
                )
                for change in call.changes
            ]
            # Each change heads itself with a rule naming the file.
            payloads = [Text(""), *changes, *result, *arguments]
        else:
            payloads = [*arguments, *result]
        return [
            title,
            grid,
            *payloads,
            Text(""),
            Text(
                "Only captured tool output is shown; tool-side truncation cannot be recovered.",
                style="dim",
            ),
        ]

    async def run(self) -> None:
        await self.app.run_async()
