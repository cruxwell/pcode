"""Temporary alternate-screen review of the session's git diff.

Every file's diff is one continuous scroll, with a small file index below it.
The diff pane keeps a cursor so a row can be acted on: each row knows the file
and, where it can, the line it stands for. That is what notes (review comments
that go back to the agent as a prompt) and opening the editor anchor to.

delta lays each hunk out separately, through one delta run for the whole
review, so a hunk's rows are always known. In the unified layout delta prints
one line per patch line, so rows map to lines exactly; side by side pairs lines
up, so its rows anchor to the hunk's first line instead.
"""

import asyncio
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout
from prompt_toolkit.widgets import Label, TextArea

from pcode.delta import Delta
from pcode.edit_transcript import DiffLexer, text_fragments
from pcode.edits import edit_text, patch_text
from pcode.frame import Frame
from pcode.git_diff import Review
from pcode.popup_ui import (
    bind_list_paging,
    fuzzy_match,
    list_pane_height,
    popup_container,
    popup_mouse,
    popup_style,
    steer_list_from_query,
)
from pcode.prefix_keys import PrefixKeys
from pcode.runtime import EditCompleted
from pcode.text_selection import copy_drags

NO_MATCH = "No matching files."
PROMPTS = {"paths": "Search paths: ", "diffs": "Search diff lines: "}
VIEWS = ("all", "uncommitted", "review")
VIEW_NAMES = {"all": "All changes", "uncommitted": "Uncommitted", "review": "Since review"}
HEADING_STYLE = "bold underline"
NOTE_STYLE = "italic fg:ansiyellow"
# A hunk quoted in a note keeps this many lines either side of the noted one.
QUOTE_CONTEXT = 2
QUOTE_HUNK_LINES = 12
HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def change_title(change: EditCompleted) -> str:
    return (
        f"{change.operation:9} {edit_text(change.path)} · +{change.added} −{change.removed}"
    ).replace("\n", " ↵ ")


def change_heading(change: EditCompleted) -> str:
    return (
        f"{change.operation.capitalize()} {edit_text(change.path)}"
        f" · +{change.added} −{change.removed}"
    ).replace("\n", " ↵ ")


def file_path(change: EditCompleted) -> str:
    """The path a change leaves behind: a rename's new name."""
    return change.path.rsplit(" → ", 1)[-1]


@dataclass(frozen=True)
class Hunk:
    header: str
    lines: tuple[str, ...]
    # The new-file line each body line stands for. A removed line takes the
    # line that now sits where it was.
    numbers: tuple[int, ...]

    @property
    def start(self) -> int:
        return self.numbers[0] if self.numbers else 1


def split_patch(patch: str) -> tuple[list[str], list[str], list[Hunk]]:
    """A patch's `---`/`+++` header, its other preamble lines, and its hunks."""
    header, preamble, hunks = [], [], []
    current: list | None = None
    for line in patch.splitlines():
        if line.startswith("@@"):
            match = HUNK.match(line)
            current = [line, [], int(match[1]) if match else 1]
            hunks.append(current)
        elif current is not None:
            current[1].append(line)
        elif line.startswith(("--- ", "+++ ")):
            header.append(line)
        else:
            preamble.append(line)
    parsed = []
    for line, body, start in hunks:
        numbers, new = [], max(1, start)
        for text in body:
            if text.startswith("+") or text.startswith(" ") or not text:
                numbers.append(new)
                new += 1
            elif text.startswith("\\") and numbers:
                numbers.append(numbers[-1])
            else:
                numbers.append(new)
        parsed.append(Hunk(line, tuple(body), tuple(numbers)))
    return header, preamble, parsed


def hunk_patch(header: list[str], hunk: Hunk) -> str:
    """One hunk as a patch of its own, for delta: the header names the language."""
    return "\n".join([*header, hunk.header, *hunk.lines])


@dataclass(frozen=True)
class Anchor:
    """Where a note attaches: a file, and within it a hunk and line where known."""

    path: str
    hunk: str = ""
    # Index of the noted line in the hunk's body; None notes the whole hunk.
    index: int | None = None


@dataclass
class Note:
    anchor: Anchor
    line: int | None
    quote: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class Row:
    change: EditCompleted | None = None
    line: int | None = None
    anchor: Anchor | None = None
    note: Note | None = None


@dataclass
class Page:
    """The diff pane's document: text, delta's styled rows, and what each row is."""

    text: str
    styled: dict[int, list[tuple[str, str]]]
    rows: list[Row]
    # First row of each change, in order.
    starts: list[int]


def lay_out(
    changes: list[EditCompleted],
    rendered: dict[str, list | None],
    notes: list[Note],
    *,
    side_by_side: bool = False,
    uncommitted: frozenset[str] = frozenset(),
    empty: str = "",
    dedent: bool = True,
) -> Page:
    """Every change in one document, notes under the rows they are about.

    `rendered` holds delta's line groups by hunk patch; a hunk missing from it,
    or delta failed on, shows its own patch lines. `dedent` strips each hunk's
    shared indentation (`diff_dedent`), as `render_review` must have too.
    """
    lines: list[str] = []
    styled: dict[int, list[tuple[str, str]]] = {}
    rows: list[Row] = []
    starts: list[int] = []
    # Where each note of the file being laid out goes, worked out per file.
    targets: dict[int, Anchor] = {}

    def add(text: str, row: Row, style: list[tuple[str, str]] | None = None) -> None:
        if style is not None:
            styled[len(lines)] = style
        lines.append(text)
        rows.append(row)

    def add_notes(anchor: Anchor) -> None:
        for number, note in enumerate(notes):
            if targets.get(number) == anchor:
                text = f"  ✎ {note.text}"
                add(text, Row(rows[-1].change, note.line, anchor, note), [(NOTE_STYLE, text)])

    for change in changes:
        path = file_path(change)
        if lines:
            add("", Row())
        starts.append(len(lines))
        marker = "● " if change.path in uncommitted else ""
        heading = marker + change_heading(change)
        file_anchor = Anchor(path)
        header, preamble, hunks = (
            split_patch(patch_text(change.patch, dedent=dedent)) if change.patch else ([], [], [])
        )
        # A note goes under its line where that row is shown, else under its
        # hunk, else (another view, or a refresh changed the hunk) under the
        # file's heading. Its own anchor is kept for the views that have it.
        shown = {file_anchor}
        for hunk in hunks:
            shown.add(Anchor(path, hunk.header))
            groups = rendered.get(hunk_patch(header, hunk), None)
            if groups is None or (not side_by_side and len(groups) == len(hunk.lines)):
                shown.update(Anchor(path, hunk.header, i) for i in range(len(hunk.lines)))
        targets = {
            number: next(
                a for a in (note.anchor, Anchor(path, note.anchor.hunk), file_anchor) if a in shown
            )
            for number, note in enumerate(notes)
            if note.anchor.path == path
        }
        add(heading, Row(change, None, file_anchor), [(HEADING_STYLE, heading)])
        add_notes(file_anchor)
        for text in preamble:
            add(text, Row(change, None, file_anchor))
        for number, hunk in enumerate(hunks):
            groups = rendered.get(hunk_patch(header, hunk))
            exact = groups is not None and not side_by_side and len(groups) == len(hunk.lines)
            hunk_anchor = Anchor(path, hunk.header)
            if groups is None:
                add(hunk.header, Row(change, hunk.start, hunk_anchor))
                add_notes(hunk_anchor)
                for index, text in enumerate(hunk.lines):
                    anchor = Anchor(path, hunk.header, index)
                    add(text, Row(change, hunk.numbers[index], anchor))
                    add_notes(anchor)
                continue
            if number:
                # delta draws no hunk header here, so mark where one hunk ends.
                add("⋯", Row(change, hunk.start, hunk_anchor), [("fg:ansibrightblack", "⋯")])
            for index, group in enumerate(groups):
                anchor = Anchor(path, hunk.header, index) if exact else hunk_anchor
                line = hunk.numbers[index] if exact else hunk.start
                for row in group:
                    fragments = text_fragments(row)
                    add(
                        "".join(text for _, text in fragments), Row(change, line, anchor), fragments
                    )
                if exact:
                    add_notes(anchor)
            add_notes(hunk_anchor)
        if change.truncated:
            add("… additional diff rows omitted", Row(change, None, file_anchor))
        if change.omitted:
            add(f"Diff unavailable: {edit_text(change.omitted)}", Row(change, None, file_anchor))
    if not lines:
        add(empty, Row())
    return Page("\n".join(lines), styled, rows, starts)


def quote_for(change: EditCompleted, anchor: Anchor) -> tuple[str, ...]:
    """The diff lines a note quotes: around the noted line, else the hunk's start."""
    if not anchor.hunk:
        return ()
    # Undedented: the agent reads the code as it is. Dedenting keeps every
    # line, so the anchor's index points at the same line either way.
    _, _, hunks = split_patch(edit_text(change.patch))
    hunk = next((h for h in hunks if h.header == anchor.hunk), None)
    if hunk is None:
        return ()
    if anchor.index is None:
        body = hunk.lines[:QUOTE_HUNK_LINES]
        return (*body, "…") if len(hunk.lines) > QUOTE_HUNK_LINES else body
    low = max(0, anchor.index - QUOTE_CONTEXT)
    return hunk.lines[low : anchor.index + QUOTE_CONTEXT + 1]


def notes_prompt(notes: list[Note]) -> str:
    """Notes as a message to the agent: where, what the diff said there, and the note."""
    if not notes:
        return ""
    parts = ["Review notes on the diff:"]
    for note in notes:
        where = note.anchor.path + (f":{note.line}" if note.line else "")
        block = [where]
        if note.quote:
            # A fence longer than any backtick run the quoted code holds.
            longest = max((len(run) for run in re.findall(r"`+", "\n".join(note.quote))), default=0)
            fence = "`" * max(3, longest + 1)
            block += [f"{fence}diff", *note.quote, fence]
        block.append(note.text)
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def editor_command(path: Path, line: int | None, environ=os.environ) -> list[str]:
    """$VISUAL or $EDITOR opening `path`, at `line` where the editor can be told it."""
    args = shlex.split(environ.get("VISUAL") or environ.get("EDITOR") or "vi")
    name = Path(args[0]).name
    if not line:
        return [*args, str(path)]
    if name in {"code", "code-insiders", "codium", "cursor", "windsurf"}:
        return [*args, "--goto", f"{path}:{line}"]
    if name in {"subl", "zed", "hx", "helix"}:
        return [*args, f"{path}:{line}"]
    return [*args, f"+{line}", str(path)]


def render_review(
    review: Review,
    delta: Delta | None,
    width: int,
    known: dict[str, list | None] | None = None,
    *,
    dedent: bool = True,
) -> dict[str, list | None]:
    """delta's layout of every hunk in every view of `review`, in one delta run.

    `known` holds hunks already rendered at `width`. Safe off the event loop.
    """
    if delta is None:
        return {}
    known = known or {}
    patches = []
    for change in (*review.changes, *review.uncommitted, *(review.since_review or [])):
        if change.patch:
            header, _, hunks = split_patch(patch_text(change.patch, dedent=dedent))
            patches += [hunk_patch(header, hunk) for hunk in hunks]
    patches = list(dict.fromkeys(patches))
    missing = [patch for patch in patches if patch not in known]
    found = dict(zip(missing, delta.render_all(missing, width), strict=True)) if missing else {}
    return {patch: known[patch] if patch in known else found[patch] for patch in patches}


def matching_rows(text: str, terms: list[str]) -> list[int]:
    """Rows where every query word fuzzy-matches the line, in document order."""
    if not terms:
        return []
    return [
        row
        for row, line in enumerate(text.casefold().splitlines())
        if all(fuzzy_match(term, line) for term in terms)
    ]


class DiffBrowser:
    """The diff fills the screen; the file index stays a small pane below it.

    `review` is shown at once; `reload` loads it again (the `g` shortcut, and
    after the editor closes), off the event loop. `mark` records the review's
    checkpoint. Notes survive reloads and are read from `notes` once the popup
    closes.
    """

    def __init__(
        self,
        review: Review,
        *,
        reload: Callable[[], Review] | None = None,
        mark: Callable[[str], None] | None = None,
        code_theme: str = "monokai",
        delta: Delta | None = None,
        rendered: dict[str, list | None] | None = None,
        width: int = 0,
        dedent: bool = True,
        key_prefix: str | None = None,
        handoff: Callable[[], AbstractContextManager] = nullcontext,
        **app_options,
    ) -> None:
        self.review = review
        # Wraps $EDITOR: whatever the terminal must stop sending while it runs.
        self.handoff = handoff
        self.reload = reload
        self.mark = mark
        self.delta = delta
        self.dedent = dedent
        # `rendered` is delta's layout of the review's hunks at `width`, made
        # before the popup opened so it never flashes the plain patch first.
        self.width = width
        self.rendered: dict[str, list | None] = dict(rendered or {})
        self.rendering = False
        self.loads = 0
        self.notes: list[Note] = []
        self.notice = ""
        self.view = "all"
        if review.since_review:
            self.view = "review"
        elif review.since_review == []:
            self.notice = "Nothing new since your last review"
        self.scope = "paths"
        self.visible: list[EditCompleted] = []
        self.page = Page("", {}, [], [])
        self.editing: Note | Row | None = None
        self._syncing = False
        self.query = TextArea(height=1, prompt=lambda: PROMPTS[self.scope], multiline=False)
        self.note = TextArea(height=1, prompt=self.note_prompt, multiline=False)
        self.files = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        copy_drags(self.files, lambda text: setattr(self, "notice", text))
        self.files.window.cursorline = Always()
        self.lexer = DiffLexer(code_theme)
        self.diff = TextArea(read_only=True, wrap_lines=False, scrollbar=True, lexer=self.lexer)
        copy_drags(self.diff, lambda text: setattr(self, "notice", text))
        self.diff.window.cursorline = Always()
        self.query.buffer.on_text_changed += lambda _: self.refresh(first_match=True)
        self.files.buffer.on_cursor_position_changed += lambda _: self.follow_files()
        self.diff.buffer.on_cursor_position_changed += lambda _: self.follow_diff()
        keys = KeyBindings()
        noting = has_focus(self.note)

        @keys.add("escape", eager=True, filter=~noting)
        @keys.add("c-c", filter=~noting)
        def close(event):
            event.app.exit()

        @keys.add("escape", eager=True, filter=noting)
        @keys.add("c-c", filter=noting)
        def cancel_note(event):
            self.editing = None
            event.app.layout.focus(self.diff)

        @keys.add("enter", filter=noting)
        def save_note(event):
            self.save_note(self.note.text)
            event.app.layout.focus(self.diff)

        # Tab only toggles the panes; the query line is entered with f and left with Enter.
        @keys.add("tab", filter=~noting)
        @keys.add("s-tab", filter=~noting)
        def toggle(event):
            focused = event.app.layout.has_focus(self.diff)
            event.app.layout.focus(self.files if focused else self.diff)

        steer_list_from_query(keys, self.query, self.files)
        bind_list_paging(keys, self.files, has_focus(self.files) | has_focus(self.query))
        bind_list_paging(keys, self.diff, has_focus(self.diff))

        @keys.add("enter", filter=has_focus(self.query))
        def search_done(event):
            event.app.layout.focus(self.files if self.scope == "paths" else self.diff)

        self.prefix_keys = shortcuts = PrefixKeys(key_prefix)
        shortcuts.set_help(
            lambda: [
                ("↑/↓", "Select file / move in the diff"),
                ("PgUp/PgDn", "Page"),
                ("Ctrl+U/D", "Half page"),
                ("Type", "Search in the search field"),
                ("Enter", "Leave the search field / save a note"),
                ("Tab/Shift+Tab", "Switch files / diff"),
                ("Esc/Ctrl+C", "Close (notes go to the prompt)"),
            ]
        )

        @shortcuts.add("f", "Search the focused pane")
        def search(event):
            if event.app.layout.has_focus(self.files):
                self.search("paths")
            elif event.app.layout.has_focus(self.diff):
                self.search("diffs")
            else:
                self.search(self.scope)

        # Emacs's incremental search keys: s forward, r in reverse.
        @shortcuts.add("s", "Next match", group="Next / previous match")
        def next_match(event):
            self.jump(1)

        @shortcuts.add("r", "Previous match", group="Next / previous match")
        def previous_match(event):
            self.jump(-1)

        @shortcuts.add("v", lambda: f"View: {VIEW_NAMES[self.next_view()]}")
        def next_view(event):
            self.show(self.next_view())

        @shortcuts.add("n", "Note on this line (sent to the prompt on close)")
        def add_note(event):
            self.start_note()

        @shortcuts.add("a", "Mark reviewed", filter=Condition(lambda: self.mark is not None))
        def mark_reviewed(event):
            self.mark_reviewed()

        @shortcuts.add("e", "Open in $EDITOR")
        def open_editor(event):
            self.open_editor()

        @shortcuts.add("g", "Refresh", filter=Condition(lambda: self.reload is not None))
        def refresh(event):
            self.app.create_background_task(self.refetch())

        root = HSplit(
            [
                Label(self.tabs),
                Label(self.heading),
                self.query,
                Frame(self.diff, title="Diff"),
                ConditionalContainer(self.note, filter=Condition(lambda: self.editing is not None)),
                Frame(
                    self.files,
                    title="Files",
                    height=lambda: list_pane_height(len(self.changes())),
                ),
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
        self.app.before_render += self.rewidth
        self.refresh()

    # What is shown

    def changes(self, view: str | None = None) -> list[EditCompleted]:
        view = view or self.view
        if view == "uncommitted":
            return self.review.uncommitted
        if view == "review":
            return self.review.since_review or []
        return self.review.changes

    def views(self) -> list[str]:
        """The views this review offers: since review only once something was reviewed."""
        return [v for v in VIEWS if v != "review" or self.review.since_review is not None]

    def next_view(self) -> str:
        views = self.views()
        return views[(views.index(self.view) + 1) % len(views)] if self.view in views else "all"

    def tabs(self) -> str:
        return "   ".join(
            ("▸ " if view == self.view else "  ")
            + f"{VIEW_NAMES[view]} ({len(self.changes(view))})"
            for view in self.views()
        )

    def heading(self) -> str:
        if self.view == "uncommitted":
            title = "what the next commit would take in, against HEAD"
        elif self.view == "review":
            title = "changed since you last marked the diff reviewed"
        else:
            title = self.review.title
        position = self.position()
        where = ""
        if (row := self.current_row()) is not None and row.change is not None:
            line = f":{row.line}" if row.line else ""
            where = f" · {position}/{len(self.visible)} {file_path(row.change)}{line}"
        notes = f" · {len(self.notes)} note{'s' * (len(self.notes) != 1)}" if self.notes else ""
        notice = f" · {self.notice}" if self.notice else ""
        return f"{title}{where}{notes}{notice}"

    def empty(self) -> str:
        if self.view == "uncommitted":
            return "No uncommitted changes."
        if self.view == "review":
            return "Nothing new since your last review."
        return self.review.empty

    def show(self, view: str) -> None:
        keep = self.current_row()
        self.view = view
        self.notice = ""
        self.refresh(keep=keep)

    # Laying out

    def side_by_side(self) -> bool:
        return self.delta is not None and self.delta.side_by_side(self.width)

    def render(self, width: int, review: Review | None = None) -> dict[str, list | None]:
        known = self.rendered if width == self.width else {}
        return render_review(review or self.review, self.delta, width, known, dedent=self.dedent)

    def pane_width(self) -> int:
        # The frame's two borders, the scrollbar, and a spare column: every
        # padded delta row is exactly the width it was rendered for.
        return max(1, self.app.output.get_size().columns - 4)

    def rewidth(self, _app=None) -> None:
        """delta lays a diff out for one width, so a resize renders it again."""
        width = self.pane_width()
        if self.delta is None or width == self.width or self.rendering:
            return
        self.rendering = True
        self.app.create_background_task(self.rerender(width))

    async def rerender(self, width: int) -> None:
        review = self.review
        try:
            rendered = await asyncio.to_thread(self.render, width, review)
        finally:
            self.rendering = False
        if self.review is not review:
            # A reload landed meanwhile; the next render lays the new one out.
            self.app.invalidate()
            return
        keep = self.current_row()
        self.rendered, self.width = rendered, width
        self.refresh(keep=keep)
        self.app.invalidate()

    async def refetch(self) -> None:
        """Load the review again, keeping the view, the place, and the notes."""
        if self.reload is None:
            return
        self.notice = "Refreshing…"
        self.app.invalidate()
        width = self.width or self.pane_width()
        # Only the newest reload is applied, whichever finishes last.
        self.loads += 1
        generation = self.loads

        def load():
            review = self.reload()
            return review, self.render(width, review)

        try:
            review, rendered = await asyncio.to_thread(load)
        except Exception as error:  # a failed reload must not take the popup down
            if generation == self.loads:
                self.notice = f"Refresh failed: {error}"
                self.app.invalidate()
            return
        if generation != self.loads:
            return
        keep = self.current_row()
        self.review, self.rendered, self.width = review, rendered, width
        if self.view not in self.views():
            self.view = "all"
        self.notice = "Refreshed"
        self.refresh(keep=keep)
        self.app.invalidate()

    def refresh(self, keep: Row | None = None, *, first_match: bool = False) -> None:
        """Lay the visible changes out again, staying on `keep`'s file and line.

        `first_match` moves to the first row a diff search matches instead.
        """
        keep = keep if keep is not None else self.current_row()
        self.visible = [change for change in self.changes() if self.matches(change)]
        # In the uncommitted view every file is uncommitted, so none is marked.
        uncommitted = frozenset(
            () if self.view == "uncommitted" else (c.path for c in self.review.uncommitted)
        )
        self.page = lay_out(
            self.visible,
            self.rendered,
            self.notes,
            side_by_side=self.side_by_side(),
            uncommitted=uncommitted,
            empty=NO_MATCH if self.changes() and not self.visible else self.empty(),
            dedent=self.dedent,
        )
        self.lexer.rows = self.page.styled
        row = self.find(keep)
        self._syncing = True
        self.diff.buffer.set_document(
            Document(self.page.text, self.row_index(row)), bypass_readonly=True
        )
        listing = [
            ("● " if change.path in uncommitted else "  ") + change_title(change)
            for change in self.visible
        ]
        text = "\n".join(listing) or self.page.text
        self.files.buffer.set_document(Document(text, 0), bypass_readonly=True)
        self._syncing = False
        if (first_match or keep is None) and (rows := self.diff_rows()):
            self.go_to(rows[0])
        self.follow_diff()

    def find(self, keep: Row | None) -> int:
        """The row showing `keep`'s file and line, else its file, else the top."""
        if keep is None or keep.change is None:
            return 0
        path = file_path(keep.change)
        fallback = None
        for number, row in enumerate(self.page.rows):
            if row.change is None or file_path(row.change) != path:
                continue
            if fallback is None:
                fallback = number
            if keep.note is not None and row.note is keep.note:
                return number
            if keep.line is not None and row.line == keep.line and row.note is None:
                return number
        return fallback or 0

    def row_index(self, row: int) -> int:
        return Document(self.page.text).translate_row_col_to_index(row, 0)

    def go_to(self, row: int) -> None:
        self.diff.buffer.cursor_position = self.diff.document.translate_row_col_to_index(row, 0)
        self.diff.window.vertical_scroll = row

    def current_row(self) -> Row | None:
        number = self.diff.document.cursor_position_row
        return self.page.rows[number] if number < len(self.page.rows) else None

    def position(self) -> int:
        row = self.diff.document.cursor_position_row
        return sum(1 for start in self.page.starts if start <= row) if self.visible else 0

    def follow_files(self) -> None:
        """Selecting a file scrolls the diff to it."""
        if self._syncing or not self.visible:
            return
        index = self.files.document.cursor_position_row
        if index < len(self.page.starts):
            self._syncing = True
            self.go_to(self.page.starts[index])
            self._syncing = False

    def follow_diff(self) -> None:
        """Moving in the diff keeps the file index on the file under the cursor."""
        if self._syncing or not self.visible:
            return
        index = max(0, self.position() - 1)
        self._syncing = True
        self.files.buffer.cursor_position = self.files.document.translate_row_col_to_index(index, 0)
        self._syncing = False

    # Searching

    def terms(self) -> list[str]:
        return self.query.text.casefold().split()

    def search(self, scope: str) -> None:
        """Retarget the query line; a query typed for the other scope is dropped."""
        if scope != self.scope:
            self.scope = scope
            self.query.text = ""
        self.app.layout.focus(self.query)

    def matches(self, change: EditCompleted) -> bool:
        terms = self.terms()
        if self.scope == "paths":
            return all(fuzzy_match(term, change.path.casefold()) for term in terms)
        # Match the text as shown, so sanitizing cannot hide the row a query matched.
        text = "\n".join([change_heading(change), patch_text(change.patch, dedent=self.dedent)])
        return not terms or bool(matching_rows(text, terms))

    def diff_rows(self) -> list[int]:
        """Diff-pane rows matching a diff search; the path search never highlights rows."""
        if self.scope != "paths":
            return matching_rows(self.page.text, self.terms())
        return []

    def jump(self, direction: int) -> None:
        """Move the diff cursor to the next or previous matching row, wrapping."""
        rows = self.diff_rows()
        if not rows:
            return
        current = self.diff.document.cursor_position_row
        if direction > 0:
            self.go_to(next((row for row in rows if row > current), rows[0]))
        else:
            self.go_to(next((row for row in reversed(rows) if row < current), rows[-1]))

    # Acting on a row

    def note_prompt(self) -> str:
        row = self.editing.anchor if isinstance(self.editing, Note) else None
        if isinstance(self.editing, Row) and self.editing.anchor is not None:
            row = self.editing.anchor
        line = self.editing.line if self.editing is not None else None
        where = (row.path if row else "") + (f":{line}" if line else "")
        return f"Note on {where} (Enter saves, empty deletes, Esc cancels): "

    def start_note(self) -> None:
        row = self.current_row()
        if row is None or row.anchor is None:
            self.notice = "Move to a diff line to add a note"
            return
        self.editing = row.note or row
        self.note.text = row.note.text if row.note else ""
        self.note.buffer.cursor_position = len(self.note.text)
        self.app.layout.focus(self.note)

    def save_note(self, text: str) -> None:
        editing, self.editing = self.editing, None
        text = " ".join(text.split())
        if isinstance(editing, Note):
            if text:
                editing.text = text
                current = self.current_row()
                keep = Row(current.change if current else None, editing.line, None, editing)
            else:
                self.notes.remove(editing)
                keep = self.current_row()
        elif isinstance(editing, Row) and text and editing.anchor is not None:
            note = Note(
                editing.anchor, editing.line, quote_for(editing.change, editing.anchor), text
            )
            self.notes.append(note)
            keep = replace(editing, note=note)
        else:
            return
        self.refresh(keep=keep)

    def mark_reviewed(self) -> None:
        checkpoint = self.review.checkpoint
        if self.mark is None or checkpoint is None:
            self.notice = "Nothing to mark reviewed"
            return
        try:
            self.mark(checkpoint)
        except Exception as error:  # the review stays usable without a checkpoint
            self.notice = f"Could not mark reviewed: {error}"
            return
        self.review = replace(self.review, since_review=[])
        self.notice = "Marked reviewed"
        self.refresh()

    def open_editor(self) -> None:
        row = self.current_row()
        if row is None or row.change is None or self.review.root is None:
            self.notice = "Move to a file to open it"
            return
        path = self.review.root / file_path(row.change)
        if not path.exists():
            self.notice = f"{file_path(row.change)} no longer exists"
            return
        command = editor_command(path, row.line)

        async def edit():
            try:
                with self.handoff():
                    await run_in_terminal(
                        lambda: subprocess.run(command, cwd=self.review.root, check=False),
                        in_executor=True,
                    )
            except OSError as error:
                self.notice = f"Could not start {command[0]}: {error}"
                return
            # The file may have changed; show what it holds now.
            await self.refetch()

        self.app.create_background_task(edit())

    async def run(self) -> None:
        await self.app.run_async()
