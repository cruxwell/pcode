"""Copy a response, or one quote or code block out of it, to the clipboard.

The transcript draws a Markdown quote with a `▌` rail and hard-wraps it to the
terminal, so selecting it with the mouse picks up both. The source still has
the text as written: this pulls each top-level quote and fenced code block out
of it, for `/copy`'s rows (see `tree_ui.CopyBrowser`, which `SnippetMarkdown`
highlights them for) and for the small picker in `/tree`.
"""

import re
from dataclasses import dataclass, field

from markdown_it import MarkdownIt
from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Label, TextArea
from rich.console import Group
from rich.markdown import BlockQuote, CodeBlock, Markdown

from pcode.frame import Dialog
from pcode.popup_ui import (
    bind_list_paging,
    marked,
    popup_container,
    popup_mouse,
    popup_style,
)
from pcode.prefix_keys import PrefixKeys

_QUOTE_MARKER = re.compile(r"^[ \t]*> ?")
_ROW_WIDTH = 80


@dataclass(frozen=True)
class Answer:
    """A question and full answer, safe for display, search, and copying."""

    prompt: str
    text: str

    def __post_init__(self) -> None:
        from pcode.session_ui import literal

        object.__setattr__(self, "prompt", literal(self.prompt))
        object.__setattr__(self, "text", literal(self.text))


def answers(tree) -> list[Answer]:
    """Nonempty answers on the active branch, newest first, without compactions."""
    found = []
    for identity in reversed(tree.path(tree.active)):
        node = tree.nodes[identity]
        if node.kind != "compaction" and node.response.strip():
            answer = Answer(node.prompt, node.response)
            if answer.text.strip():
                found.append(answer)
    return found


@dataclass(frozen=True)
class Snippet:
    kind: str  # "prompt", "response", "quote", or "code"
    text: str
    language: str = ""
    # Source lines [start, end) of a quote or code block, for `SnippetMarkdown`.
    lines: tuple[int, int] | None = field(default=None, compare=False)

    @property
    def label(self) -> str:
        if self.kind == "response":
            return "Whole response"
        if self.language:
            return f"{self.kind.title()} ({self.language})"
        return self.kind.title()


def _unquote(lines: list[str]) -> str:
    # A lazy continuation line has no marker; it only loses its indentation.
    return "\n".join(
        _QUOTE_MARKER.sub("", line, count=1) if _QUOTE_MARKER.match(line) else line.lstrip()
        for line in lines
    ).strip("\n")


_HARD_BREAK = re.compile(r"(  |\\)$")


def _unwrap(markdown: str) -> str:
    """``markdown`` with each paragraph's soft line breaks joined by spaces.

    Markdown draws a lone newline inside a paragraph as a space, so a quote
    copied as written would break where nothing on screen does. Hard breaks,
    blank lines, code, and nested quotes keep their lines.
    """
    lines = markdown.splitlines()
    depth = 0
    joined: dict[int, int] = {}  # paragraph's first line -> its end
    for token in MarkdownIt("commonmark").parse(markdown):
        if token.type == "blockquote_open":
            depth += 1
        elif token.type == "blockquote_close":
            depth -= 1
        elif token.type == "paragraph_open" and depth == 0 and token.map:
            joined[token.map[0]] = token.map[1]
    out: list[str] = []
    index = 0
    while index < len(lines):
        end = joined.get(index, index + 1)
        paragraph = lines[index:end]
        line = paragraph[0]
        for following in paragraph[1:]:
            if _HARD_BREAK.search(line):
                out.append(_HARD_BREAK.sub("", line).rstrip())
                line = following
            else:
                line = f"{line.rstrip()} {following.lstrip()}"
        out.append(line)
        index = end
    return "\n".join(out)


def snippets(markdown: str) -> list[Snippet]:
    """The whole response, then each top-level quote and code block in order."""
    if not markdown.strip():
        return []
    found = [Snippet("response", markdown)]
    lines = markdown.splitlines()
    depth = 0
    for token in MarkdownIt("commonmark").parse(markdown):
        if token.type == "blockquote_open":
            if depth == 0 and token.map:
                start, end = token.map
                # Its first line can open inside a list item, e.g. "2. > text":
                # with no enclosing quote, the first `>` is this quote's own.
                quoted = lines[start:end]
                if (marker := quoted[0].find(">")) > 0:
                    quoted[0] = quoted[0][marker:]
                if text := _unwrap(_unquote(quoted)):
                    found.append(Snippet("quote", text, lines=(start, end)))
            depth += 1
        elif token.type == "blockquote_close":
            depth -= 1
        elif token.type == "fence" and depth == 0 and token.content.strip():
            language = token.info.split()[0] if token.info.strip() else ""
            # No trailing newline: pasted into a shell, it would run the command.
            text = token.content.rstrip("\n")
            found.append(Snippet("code", text, language, tuple(token.map) if token.map else None))
    return found


class _Marked:
    """A Markdown element tagged with its snippet's pane mark, when it has one."""

    mark: str | None = None

    @classmethod
    def create(cls, markdown, token):
        element = super().create(markdown, token)
        element.mark = markdown.marks.get(tuple(token.map)) if token.map else None
        return element

    def __rich_console__(self, console, options):
        rendered = super().__rich_console__(console, options)
        if self.mark is None:
            yield from rendered
        else:
            yield marked(Group(*rendered), self.mark)


class _MarkedCodeBlock(_Marked, CodeBlock):
    pass


class _MarkedBlockQuote(_Marked, BlockQuote):
    pass


class SnippetMarkdown(Markdown):
    """Markdown whose quotes and code blocks carry their snippets' `marked` keys.

    ``marks`` maps a snippet's source lines to its key. Rich parses the same
    source as `snippets`, so a block's line map identifies the snippet.
    """

    elements = {
        **Markdown.elements,
        "fence": _MarkedCodeBlock,
        "blockquote_open": _MarkedBlockQuote,
    }

    def __init__(self, markup: str, marks: dict[tuple[int, int], str], **options) -> None:
        super().__init__(markup, **options)
        self.marks = marks


def last_response(tree) -> str:
    """The newest assistant text on the active branch, skipping compactions."""
    for identity in reversed(tree.path(tree.active)):
        node = tree.nodes[identity]
        if node.kind != "compaction" and node.response.strip():
            return node.response
    return ""


def snippet_row(snippet: Snippet) -> str:
    text = " ".join(snippet.text.split())
    row = f"{snippet.label}: {text}"
    return row if len(row) <= _ROW_WIDTH else row[: _ROW_WIDTH - 1] + "…"


class SnippetPicker:
    """A list of snippets; Enter picks one, Esc backs out.

    ``container`` and ``key_bindings`` let a popup host it as an overlay, which
    `/tree` does; ``snippet_dialog`` wraps it in an application of its own.
    """

    def __init__(self, choices: list[Snippet], on_pick, on_cancel, *, shortcuts=None) -> None:
        self.choices = choices
        self.on_pick = on_pick
        self.list = TextArea(
            read_only=True,
            wrap_lines=False,
            scrollbar=True,
            width=Dimension(min=40, preferred=_ROW_WIDTH + 1),
            height=Dimension(min=1, preferred=len(choices)),
        )
        self.list.window.cursorline = Always()
        rows = [snippet_row(snippet) for snippet in choices]
        # Start on the first quote or block: it is why the picker opened.
        start = 1 if len(choices) > 1 else 0
        position = sum(len(row) + 1 for row in rows[:start])
        self.list.buffer.set_document(Document("\n".join(rows), position), bypass_readonly=True)
        keys = self.key_bindings = KeyBindings()
        bind_list_paging(keys, self.list, Always())

        @keys.add("enter", eager=True)
        def pick(event):
            on_pick(self.selected())

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        def cancel(event):
            on_cancel()

        self.container = HSplit(
            [self.list],
            # Gated by the host's `PrefixKeys`, so a waiting leader owns Enter and Esc.
            key_bindings=shortcuts.gate(keys) if shortcuts else keys,
            modal=True,
        )

    def help(self) -> list[tuple[str, str]]:
        return [
            ("↑/↓", "Select snippet"),
            ("PgUp/PgDn", "Page"),
            ("Ctrl+U/D", "Half page"),
            ("Enter", "Copy selected snippet"),
            ("Esc / Ctrl+C", "Cancel"),
        ]

    def selected(self) -> Snippet:
        row = self.list.document.cursor_position_row
        return self.choices[min(row, len(self.choices) - 1)]


def snippet_dialog(choices: list[Snippet], *, input=None, output=None, style=None):
    """A popup application whose result is the picked snippet, or None."""
    app: Application
    shortcuts = PrefixKeys()

    picker = SnippetPicker(
        choices,
        on_pick=lambda snippet: app.exit(result=snippet),
        on_cancel=lambda: app.exit(result=None),
        shortcuts=shortcuts,
    )
    shortcuts.set_help(picker.help)
    dialog = Dialog(
        title="Copy",
        body=HSplit([picker.container, Label(shortcuts.summary)], padding=1),
    )
    app = Application(
        layout=Layout(popup_container(dialog, shortcuts), focused_element=picker.list),
        key_bindings=shortcuts.key_bindings(KeyBindings()),
        full_screen=True,
        mouse_support=popup_mouse(shortcuts),
        input=input,
        output=output,
        style=popup_style(style),
    )
    return app
