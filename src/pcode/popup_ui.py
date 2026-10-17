"""Shared terminal-native styling for alternate-screen popups."""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import StringIO

from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, Filter, has_focus
from prompt_toolkit.formatted_text import (
    ANSI,
    AnyFormattedText,
    StyleAndTextTuples,
    to_formatted_text,
)
from prompt_toolkit.formatted_text.utils import fragment_list_to_text, split_lines
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HorizontalAlign,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import (
    AfterInput,
    ConditionalProcessor,
    Processor,
    Transformation,
    TransformationInput,
)
from prompt_toolkit.styles import Style, merge_styles
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea
from rich.console import Console
from rich.segment import Segments
from rich.style import Style as RichStyle
from rich.styled import Styled
from rich.theme import Theme

from pcode.frame import TITLE_CHROME, Frame, text_width
from pcode.input_keys import configure_newline_keys
from pcode.preferences import SETTINGS, load_preferences
from pcode.prefix_keys import PrefixKeys
from pcode.text_selection import (
    Row,
    Selection,
    copy_selected,
    highlight,
    marked_rendering,
    unmark,
)

# Rich writes Markdown links as OSC 8 hyperlinks on a terminal, but
# prompt_toolkit's ANSI parser reads CSI only and spills the rest as literal
# text ("8;id=1;https://…"). A pane cannot follow a link anyway, so drop them.
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Style meta naming a block a RichPane can highlight; Rich never draws meta.
_MARK = "pcode.mark:"


def marked(renderable, key: str) -> Styled:
    """``renderable`` tagged as block ``key``, unchanged on screen until a pane shows it.

    Tags nest, e.g. a code block inside a response, because each is its own meta key.
    """
    return Styled(renderable, RichStyle(meta={_MARK + key: True}))


# Framed selector panes: rows of list content, before the frame's own borders.
LIST_ROWS_MIN = 3
LIST_ROWS_MAX = 6
_BORDERS = 2
# Rows a docked editor grows to before it scrolls; the panes above keep the rest.
INPUT_ROWS_MAX = 6
_INPUT_PROMPT = "\u203a "
# A draft whose first word reads as a command name. A path of more than one
# part such as /etc/hosts does not, so a question opening with one is sent.
_COMMAND_WORD = re.compile(r"/[a-z-]*")

_NATIVE = "bg:default fg:default noreverse"
POPUP_STYLE = Style.from_dict(
    {
        "popup": _NATIVE,
        "popup dialog": _NATIVE,
        "popup dialog.body": _NATIVE,
        "popup text-area": _NATIVE,
        "popup frame.border": _NATIVE,
        "popup frame.label": f"{_NATIVE} bold",
        "popup shadow": _NATIVE,
        "popup text-area last-line": "nounderline",
        # A list row is already drawn in the selection's reverse video, so a
        # drag inside it would not show: underline it instead.
        "popup cursor-line selected": "underline",
    }
)
# These are fallbacks for standalone popups without a caller theme. Keep them
# before the caller's style, unlike the terminal-native surfaces above.
POPUP_ACCENTS = Style.from_dict(
    {
        "popup scrollbar.background": _NATIVE,
        "popup scrollbar.button": f"{_NATIVE} reverse",
        "popup scrollbar.arrow": f"{_NATIVE} bold",
        "popup selected": f"{_NATIVE} reverse",
        "popup cursor-line": f"{_NATIVE} reverse nounderline",
        "popup placeholder": "dim italic",
        "popup frame.footer": "dim",
        "popup hint.message": "italic",
        "popup search-match": "reverse",
    }
)
SEARCH_MATCH = "class:search-match"


def mark_matches(line: StyleAndTextTuples, words: Sequence[str]) -> StyleAndTextTuples:
    """Add SEARCH_MATCH to every case-insensitive occurrence of `words` in one line.

    A match that a wrap split across two lines is not marked.
    """
    text = "".join(fragment[1] for fragment in line)
    # Fold one character at a time: casefolding can lengthen one (ß → ss),
    # so `owner` maps each folded position back to the character it came from.
    folded, owner = [], []
    for index, character in enumerate(text):
        piece = character.casefold()
        folded.append(piece)
        owner.extend([index] * len(piece))
    folded = "".join(folded)
    marked = [False] * len(text)
    for word in words:
        start = folded.find(word) if word else -1
        while start != -1:
            for position in range(start, start + len(word)):
                marked[owner[position]] = True
            start = folded.find(word, start + len(word))
    if not any(marked):
        return line
    result: StyleAndTextTuples = []
    position = 0
    for style, fragment, *rest in line:
        run_start = 0
        for index in range(1, len(fragment) + 1):
            if index == len(fragment) or marked[position + index] != marked[position + run_start]:
                hit = marked[position + run_start]
                piece = fragment[run_start:index]
                result.append((f"{style} {SEARCH_MATCH}" if hit else style, piece, *rest))
                run_start = index
        position += len(fragment)
    return result


def focus_overlay(app, target) -> None:
    """Focus `target` inside an overlay that has just appeared.

    prompt_toolkit finds a control's key bindings through a child-to-parent map
    it rebuilds only on redraw. Until then an overlay swapped into a
    `DynamicContainer` has no known parents, so typed-ahead or pasted keys reach
    the app's bindings instead of the overlay's: Enter closed the viewer behind
    a link picker rather than opening the link.
    """
    app.layout.update_parents_relations()
    app.layout.focus(target)


def popup_container(body, shortcuts: PrefixKeys | None = None):
    """Scope every modal surface under the same style class.

    Shortcut help floats over the content without changing its focus or size.
    """
    if shortcuts is not None:
        body = FloatContainer(body, floats=[Float(shortcut_hint(shortcuts), bottom=1, right=2)])
    return HSplit([body], style="class:popup")


def shortcut_hint(shortcuts: PrefixKeys):
    """Shared contextual help for prefix actions and read-only Ctrl+/ browsing."""

    def lines() -> list[StyleAndTextTuples]:
        rows = shortcuts.hint_rows()
        columns = [rows]
        if len(rows) > HINT_COLUMN_ROWS:
            half = -(-len(rows) // 2)
            split = [rows[:half], rows[half:]]
            # Room for the borders and a popup float's right margin, or it would wrap.
            room = get_app().output.get_size().columns - _BORDERS - 2
            if _hint_width(_hint_lines(split)) <= room:
                columns = split
        listed = _hint_lines(columns)
        if shortcuts.message:
            listed.append([("class:hint.message", f" {shortcuts.message} ")])
        return listed

    def fragments() -> StyleAndTextTuples:
        listed = lines()
        # Down/PgDn count rows, which two columns halve: clamp to what is drawn.
        shortcuts.help_offset = min(shortcuts.help_offset, max(0, len(listed) - 1))
        result: StyleAndTextTuples = []
        for index, line in enumerate(listed):
            if index:
                result.append(("", "\n"))
            result += line
        return result

    def footer() -> StyleAndTextTuples:
        result: StyleAndTextTuples = []
        for index, (key, label) in enumerate(shortcuts.hint_footer()):
            if index:
                result.append(("", " · "))
            result += [("bold", key), ("", f" {label}")]
        return result

    def frame_width() -> Dimension:
        # Frame's top and bottom borders stretch to whatever width they are
        # given, while the body hugs its text, so without an explicit width
        # the box spans the screen and the right border floats mid-row.
        fit = max(
            _hint_width(lines()) + _BORDERS,
            text_width(shortcuts.help_title) + TITLE_CHROME,
            text_width(footer()) + TITLE_CHROME,
        )
        # A maximum, not an exact width, so a narrow terminal wraps the labels.
        return Dimension(preferred=fit, max=fit)

    body = Window(
        FormattedTextControl(
            fragments,
            show_cursor=False,
            get_cursor_position=lambda: Point(0, shortcuts.help_offset),
        ),
        wrap_lines=True,
        dont_extend_height=True,
    )
    frame = Frame(body, title=lambda: shortcuts.help_title, width=frame_width, footer=footer)
    return ConditionalContainer(
        VSplit([frame], align=HorizontalAlign.LEFT),
        filter=Condition(lambda: shortcuts.visible),
    )


# More rows than this split into two columns, when the screen is wide enough.
HINT_COLUMN_ROWS = 10
_COLUMN_GAP = "   "


def _hint_lines(columns: list[list[tuple[str, str]]]) -> list[StyleAndTextTuples]:
    """Side-by-side columns of ``key  label`` rows, each column aligned on its own."""

    def pad(text: str, width: int) -> str:
        # By display width, not ljust's character count, for wide characters.
        return text + " " * (width - get_cwidth(text))

    widths = [
        (
            max((get_cwidth(key) for key, _ in rows), default=0),
            max((get_cwidth(label) for _, label in rows), default=0),
        )
        for rows in columns
    ]
    lines = []
    for index in range(max((len(rows) for rows in columns), default=0)):
        line: StyleAndTextTuples = [("", " ")]
        for number, (rows, (key_width, label_width)) in enumerate(zip(columns, widths)):
            if index >= len(rows):
                break
            key, label = rows[index]
            if number:
                line.append(("", _COLUMN_GAP))
            last = number == len(columns) - 1
            line += [
                ("class:hint.key bold", pad(key, key_width)),
                ("class:hint.label", "  " + (label if last else pad(label, label_width))),
            ]
        line.append(("", " "))
        lines.append(line)
    return lines


def _hint_width(lines: list[StyleAndTextTuples]) -> int:
    return max((get_cwidth(fragment_list_to_text(line)) for line in lines), default=0)


# The popup shortcut that hands the mouse to the terminal and back. Ctrl+M is
# Enter and Alt+M (WeeChat's toggle) arrives as an Escape that closes popups,
# so this takes the one letter free in every popup that edits nothing common:
# Ctrl+Q only displaces Emacs quoted-insert, and raw mode turns off XON/XOFF.
MOUSE_TOGGLE_KEY = "q"


def popup_mouse(shortcuts: PrefixKeys | None = None, key: str = MOUSE_TOGGLE_KEY) -> Filter:
    """Whether a popup captures the mouse: `popup_mouse`, read as it opens.

    Capturing gives clicks and wheel scrolling to the popup, but the terminal
    then stops treating a drag as a text selection. With ``shortcuts``, prefix
    `q` flips it until the popup closes, to select some text and come back;
    the renderer reads the filter every frame, so it applies at once. A popup
    that gives `q` another job passes another ``key``.
    """
    captured = load_preferences().get("popup_mouse", SETTINGS["popup_mouse"].default) == "on"
    state = {"captured": captured}
    if shortcuts is not None:

        @shortcuts.add(
            key,
            lambda: "Release mouse" if state["captured"] else "Capture mouse",
        )
        def toggle(event) -> None:
            state["captured"] = not state["captured"]
            event.app.invalidate()

    return Condition(lambda: state["captured"])


def popup_style(base=None):
    """Let the caller theme highlights, but keep popup surfaces terminal-native."""
    styles = [POPUP_ACCENTS]
    if base is not None:
        styles.append(base)
    return merge_styles([*styles, POPUP_STYLE])


def _list_page(listing) -> int:
    info = listing.window.render_info
    return max(1, info.window_height - 1) if info else 10


def fuzzy_match(term: str, text: str) -> bool:
    """Match a casefolded substring or joined word prefixes, e.g. ed_ui + edit_ui.py.

    Gaps are allowed between words, not inside them. This keeps 'anthopus'
    from matching an unrelated Sonnet via scattered letters in 'anthropic:claude',
    and a three-letter query from matching every diff line with those initials.
    """
    if term in text:
        return True
    # Separators in the query are word boundaries, so ed_ui and ed/ui both find edit_ui.
    term = "".join(re.findall(r"[a-z0-9]+", term))
    if not term:
        return False
    # Track how much of the query can be consumed by successive word prefixes.
    positions = {0}
    for word in re.findall(r"[a-z0-9]+", text):
        following = set(positions)  # Skipping a word is allowed.
        for position in positions:
            for length, character in enumerate(word, 1):
                index = position + length - 1
                if index >= len(term) or character != term[index]:
                    break
                following.add(index + 1)
        if len(term) in following:
            return True
        positions = following
    return False


def bind_list_paging(keys, listing, filter) -> None:
    """Ctrl+U/Ctrl+D move a TextArea's cursor, a list's selection, by half a page.

    Every popup pane half-pages with these keys, so a browser feels the same
    whichever split has focus. ↑/↓ and PageUp/PageDown come from the TextArea
    itself (full-screen apps load prompt_toolkit's page navigation).
    """

    def half() -> int:
        return max(1, (_list_page(listing) + 1) // 2)

    @keys.add("c-u", filter=filter)
    def previous_half(event):
        listing.buffer.cursor_up(half())

    @keys.add("c-d", filter=filter)
    def following_half(event):
        listing.buffer.cursor_down(half())


def steer_list_from_query(keys, query, listing) -> None:
    """Keep typing in the query while ↑/↓ move the selection in a TextArea list.

    A one-line query swallows vertical movement otherwise, which is what a
    filter-as-you-type list makes people expect least.
    """
    typing = has_focus(query)

    def page() -> int:
        return _list_page(listing)

    @keys.add("up", filter=typing)
    @keys.add("c-p", filter=typing)
    def previous(event):
        listing.buffer.cursor_up()

    @keys.add("down", filter=typing)
    @keys.add("c-n", filter=typing)
    def following(event):
        listing.buffer.cursor_down()

    @keys.add("pageup", filter=typing)
    def previous_page(event):
        listing.buffer.cursor_up(page())

    @keys.add("pagedown", filter=typing)
    def following_page(event):
        listing.buffer.cursor_down(page())


def _mark_spans(segments, first_line: int, spans: dict[str, tuple[int, int]]) -> None:
    """Widen ``spans`` to every line where a `marked` block drew text."""
    line = first_line
    for segment in segments:
        pieces = segment.text.split("\n")
        style = segment.style
        keys = [k[len(_MARK) :] for k in style.meta if k.startswith(_MARK)] if style else []
        for offset, piece in enumerate(pieces):
            if piece:
                for key in keys:
                    start, end = spans.get(key, (line + offset, line + offset))
                    spans[key] = (min(start, line + offset), max(end, line + offset))
        line += len(pieces) - 1


class RichPane:
    """A read-only, scrollable pane showing Rich renderables (Markdown, Text).

    Rich output and prepared lines are cached until the content or width changes.
    Scrolling only creates a lightweight UIContent with the current cursor row;
    it must not rescan the entire transcript on every frame.
    """

    def __init__(
        self,
        *,
        theme: Theme | None = None,
        color_system: str | None = "truecolor",
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.theme = theme
        self.color_system = color_system
        # Where a mouse copy reports itself, e.g. a popup's notice line.
        self.notify = notify
        self.renderables: list = []
        self._version = 0
        self._cache: tuple[int, int, list, list[int], dict[str, tuple[int, int]]] | None = None
        self._line_cache: tuple[int, int, list, list[Row]] | None = None
        # The width last drawn at: selection coordinates mean nothing at another.
        self._width = 0
        self.selection = Selection(lambda: self.rows(self._width), self._copy)
        # Casefolded words marked wherever they appear, e.g. a search query.
        self.highlight: tuple[str, ...] = ()
        # The `marked` block drawn as selected, e.g. what a copy would take.
        self.mark: str | None = None
        # Renderable index to scroll to on the next render, once the width is known.
        self._anchor: int | None = None
        # A `marked` block to scroll into view on the next render.
        self._reveal: str | None = None
        pane = self

        class Control(UIControl):
            def is_focusable(self):
                return True

            def preferred_width(self, max_available_width):
                # Rich wraps to the allocated width; measuring the entire text
                # here is both unnecessary and expensive for long transcripts.
                return max_available_width

            def preferred_height(self, width, max_available_height, wrap_lines, get_line_prefix):
                return len(pane.lines(width))

            def create_content(self, width, height):
                if width != pane._width:
                    pane.selection.clear()
                    pane._width = width
                lines = pane.lines(width)
                if pane._anchor is not None:
                    # Line offsets depend on the wrap width, which only the
                    # render knows, so a requested anchor is applied here.
                    pane.window.vertical_scroll = pane.line_offset(pane._anchor, width)
                    pane._anchor = None
                if pane._reveal is not None:
                    if span := pane.span(pane._reveal, width):
                        top = pane.window.vertical_scroll
                        # Only move when part of the block is out of view.
                        if span[0] < top or span[1] >= top + height:
                            pane.window.vertical_scroll = span[0]
                    pane._reveal = None
                # The window reads the cursor row as a real line, so keep it on
                # one. A stale scroll outlives a rewrap to fewer lines (resize,
                # the wide/narrow switch), follow() holding an offset past
                # shorter content, and an empty last renderable, whose start
                # sits past the final line once trailing newlines are stripped.
                pane.window.vertical_scroll = min(
                    pane.window.vertical_scroll, max(0, len(lines) - 1)
                )
                selected = pane.selection.columns
                # A mouse selection is what gets copied while it shows, and the
                # two would draw alike, so it replaces the mark.
                span = None
                if pane.mark is not None and pane.selection.span() is None:
                    span = pane.span(pane.mark, width)

                def get_line(row):
                    line = lines[row]
                    if span and span[0] <= row <= span[1]:
                        line = highlight(line, 0, width)
                    columns = selected(row)
                    return line if columns is None else highlight(line, *columns)

                return UIContent(
                    get_line=get_line,
                    line_count=len(lines),
                    show_cursor=False,
                    cursor_position=Point(0, pane.window.vertical_scroll),
                )

            def mouse_handler(self, mouse_event):
                return pane.selection.handle(mouse_event)

        # The window scrolls to keep the reported cursor visible on every render,
        # so a fixed row 0 would snap keyboard scrolling straight back to the top.
        self.control = Control()
        self.window = Window(
            self.control, wrap_lines=False, right_margins=[ScrollbarMargin(display_arrows=True)]
        )

    def follow(self, renderables: list) -> None:
        """Replace streaming content: stay on the tail if the reader was there, else hold."""
        info = self.window.render_info
        offset = self.window.vertical_scroll
        tailing = info is not None and offset >= max(0, info.content_height - info.window_height)
        # Streaming mostly appends, so a selection (even mid-drag) survives as
        # long as every row up to its last one is unchanged.
        selection = self.selection
        held = selection.anchor, selection.cursor, selection.active, selection.pressed
        marked = [position[0] for position in held[:2] if position is not None]
        before = self.rows(self._width)[: max(marked) + 1] if marked and self._width else None
        self.set(renderables, highlight=self.highlight)
        if before and self.rows(self._width)[: len(before)] == before:
            selection.anchor, selection.cursor, selection.active, selection.pressed = held
        if info is not None:
            rows = len(self.lines(info.window_width))
            self.window.vertical_scroll = max(0, rows - info.window_height) if tailing else offset

    def set(
        self, renderables: list, *, anchor: int | None = None, highlight: Sequence[str] = ()
    ) -> None:
        """Replace the content, scrolled to the top or to ``renderables[anchor]``."""
        self.renderables = renderables
        self.highlight = tuple(highlight)
        self._version += 1
        self.selection.clear()
        self.window.vertical_scroll = 0
        self.scroll_to(anchor)

    def scroll_to(self, index: int | None) -> None:
        """Put the start of ``renderables[index]`` on the pane's top row at the next render."""
        self._anchor = index

    def reveal(self, key: str) -> None:
        """Scroll the `marked` block ``key`` into view at the next render, if it is not."""
        self._reveal = key

    def span(self, key: str, width: int) -> tuple[int, int] | None:
        """First and last rendered line of the `marked` block ``key`` at ``width``."""
        self.fragments(width)
        return self._cache[4].get(key)

    def line_offset(self, index: int, width: int) -> int:
        """First rendered line of ``renderables[index]`` at ``width``."""
        self.fragments(width)
        offsets = self._cache[3]
        return offsets[min(max(index, 0), len(offsets) - 1)] if offsets else 0

    def fragments(self, width: int) -> list:
        key = (self._version, width)
        if self._cache is None or self._cache[:2] != key:
            console = Console(
                file=StringIO(),
                force_terminal=True,
                color_system=self.color_system,
                width=max(1, width),
                theme=self.theme,
                highlight=False,
            )
            offsets, lines, position = [], 0, 0
            spans: dict[str, tuple[int, int]] = {}
            with marked_rendering():
                for renderable in self.renderables:
                    offsets.append(lines)
                    segments = list(console.render(renderable))
                    _mark_spans(segments, lines, spans)
                    console.print(Segments(segments))
                    # Read only the new output; re-reading the whole buffer per
                    # renderable would be quadratic on a long transcript.
                    console.file.seek(position)
                    lines += console.file.read().count("\n")
                    position = console.file.tell()
            rendered = OSC.sub("", console.file.getvalue().rstrip("\n"))
            self._cache = (*key, to_formatted_text(ANSI(rendered)), offsets, spans)
        return self._cache[2]

    def lines(self, width: int) -> list:
        key = (self._version, width)
        if self._line_cache is None or self._line_cache[:2] != key:
            lines, rows = [], []
            for line in split_lines(self.fragments(width)):
                line, row = unmark(line)
                lines.append(line)
                rows.append(row)
            if self.highlight:
                lines = [mark_matches(line, self.highlight) for line in lines]
            self._line_cache = (*key, lines, rows)
        return self._line_cache[2]

    def rows(self, width: int) -> list[Row]:
        """Each drawn row's plain text and wrap points, for selecting and copying."""
        self.lines(width)
        return self._line_cache[3]

    def text(self, width: int = 80) -> str:
        """Unstyled rendering without Rich's line padding, for tests and logs."""
        return "\n".join(row.text.rstrip() for row in self.rows(width))

    def _copy(self, text: str) -> None:
        notice = copy_selected(text)
        if self.notify is not None:
            self.notify(notice)

    def __pt_container__(self):
        return self.window

    def bind_scrolling(self, keys, *, paging: Filter | None = None) -> None:
        """Arrow and page keys scroll the pane while it has focus.

        ``paging`` also gives it PageUp/PageDown from elsewhere, e.g. from a
        docked editor, so the reader can scroll an answer while replying to it.
        """
        focused = has_focus(self.window)
        pages = focused | paging if paging is not None else focused

        def scroll(direction: int, page: bool = False, half: bool = False):
            def handler(event):
                info = self.window.render_info
                if info is None:
                    return
                rows = (
                    max(1, info.window_height // 2)
                    if half
                    else (max(1, info.window_height - 1) if page else 1)
                )
                bottom = max(0, info.content_height - info.window_height)
                self.window.vertical_scroll = max(
                    0, min(bottom, self.window.vertical_scroll + direction * rows)
                )

            return handler

        keys.add("up", filter=focused)(scroll(-1))
        keys.add("down", filter=focused)(scroll(1))
        keys.add("pageup", filter=pages)(scroll(-1, page=True))
        keys.add("pagedown", filter=pages)(scroll(1, page=True))
        keys.add("c-u", filter=focused)(scroll(-1, half=True))
        keys.add("c-d", filter=focused)(scroll(1, half=True))


def list_pane_height(rows: int = LIST_ROWS_MAX) -> Dimension:
    """Height for a framed selector stacked above a scrolling detail pane.

    ``Dimension`` defaults ``preferred`` to ``min``, and ``HSplit`` gives every
    child its preferred height before letting any of them grow beyond it. With
    no explicit preferred, a long diff or tool payload squeezes the selector
    down to its minimum and the list effectively disappears, so ask for the
    rows up front.
    """
    visible = max(LIST_ROWS_MIN, min(LIST_ROWS_MAX, rows))
    return Dimension(
        min=1 + _BORDERS,
        preferred=visible + _BORDERS,
        max=LIST_ROWS_MAX + _BORDERS,
    )


class EllipsisProcessor(Processor):
    """Cut a line that overflows its window, ending it with ``…``.

    For an unwrapped list, whose rows would otherwise stop dead at the edge
    with no sign that more follows. The buffer keeps the whole line, so the
    row is as long as the pane allows at any terminal width.
    """

    def apply_transformation(self, ti: TransformationInput) -> Transformation:
        fragments = ti.fragments
        if sum(get_cwidth(text) for _, text, *_ in fragments) <= ti.width:
            return Transformation(fragments)
        room = max(ti.width - 1, 0)
        kept: StyleAndTextTuples = []
        used = chars = 0
        style = ""
        for style, text, *_ in fragments:
            for char in text:
                width = get_cwidth(char)
                if used + width > room:
                    break
                kept.append((style, char))
                used += width
                chars += 1
            else:
                continue
            break
        kept.append((style, "…"))
        return Transformation(
            kept,
            source_to_display=lambda i: min(i, chars),
            display_to_source=lambda i: min(i, chars),
        )


def fit_width(lines: Sequence[str]) -> Dimension:
    """Width for a read-only list in a dialog: its longest row, capped by the terminal.

    A ``TextArea`` states no preferred width, so a ``Dialog`` shrinks to its
    labels and clips or wraps every long row. Ask for the longest one (+1 for
    the scrollbar); the terminal's width is the cap.
    """
    return Dimension(min=40, preferred=max(map(get_cwidth, lines), default=0) + 1)


@dataclass(frozen=True)
class PopupCommand:
    """A slash command typed into a popup's editor instead of a message.

    ``run`` gets whatever followed the name, stripped, and raises
    ``ValueError`` to refuse, which keeps the draft and says why. ``argument``
    names what may follow, e.g. ``[focus]``, for the completion menu.
    """

    name: str
    help: str
    run: Callable[[str], None]
    argument: str = ""


class _CommandCompleter(Completer):
    """Complete the command name while it is the only thing typed."""

    def __init__(self, editor: "PopupInput") -> None:
        self.editor = editor

    def get_completions(self, document, complete_event):
        typed = document.text_before_cursor
        if self.editor.prompting or not typed.startswith("/") or any(c.isspace() for c in typed):
            return
        for command in self.editor.commands:
            if command.name.startswith(typed):
                # A space after a name that takes text, so typing goes on.
                yield Completion(
                    command.name + (" " if command.argument else ""),
                    start_position=-len(typed),
                    display=f"{command.name} {command.argument}".strip(),
                    display_meta=command.help,
                )


class PopupInput:
    """A message editor docked in a popup: type, Enter sends, Esc goes back.

    The popup decides what sending means. ``submit`` receives the draft and
    either accepts it, which clears the draft, or raises ``ValueError`` to
    refuse, which keeps the draft and shows why in the editor's title.

    The editor's own keys are scoped to it, so its Enter and Esc win over the
    popup's while it has focus. Never give the popup a bare letter key: it
    would outrank typing. Its shortcuts go through ``PrefixKeys`` instead,
    which keeps them working here too.

    Focus it with ``open``. A popup that can appear on its own should not
    open with focus here: it could swallow keystrokes meant for the main
    prompt, and Enter would then send them.

    ``back``, when given, is what Esc does from an empty draft instead of
    stepping out to ``home``: for a popup that opens in the editor, so one
    Esc still leaves it as it would from the reader.

    ``prompt`` borrows the editor to ask for one value, e.g. instructions for
    an action, with its own title and ``submit``; the draft set aside comes
    back once that is sent or Esc cancels it.

    Pass the popup's ``shortcuts`` so a waiting leader can switch the editor's
    own Enter and Esc off; they sit on the editor and would outrank it.

    ``commands`` lets the draft name an action instead, e.g. ``/copy``, with a
    menu that completes the name. Put ``completion_menu()`` in the popup's
    floats to show it. A ``prompt`` takes its value as typed: no commands.
    """

    def __init__(
        self,
        submit: Callable[[str], None],
        *,
        home,
        title: AnyFormattedText = "Message",
        placeholder: str = "",
        shortcuts: PrefixKeys | None = None,
        commands: Sequence[PopupCommand] = (),
        back: Callable[[object], None] | None = None,
    ) -> None:
        # Ctrl+J and Shift+Enter arrive as terminal-specific sequences; the
        # main prompt registers them too, but a popup can open without it.
        configure_newline_keys()
        self.submit = submit
        # Where Esc returns focus: the popup's list, usually. A callable picks
        # it as Esc is pressed, for a popup whose list can be hidden.
        self.home = home
        self.back = back
        self.title = title
        self.placeholder = placeholder
        # Whether Enter sends an empty draft; a prompt can treat it as a default.
        self.allow_empty = False
        self.notice = ""
        self.commands = tuple(commands)
        # What `prompt` set aside: the draft, title, placeholder, submit and
        # whether empty was allowed.
        self._saved: tuple | None = None
        self.area = TextArea(
            multiline=True,
            wrap_lines=True,
            focus_on_click=True,
            completer=_CommandCompleter(self) if self.commands else None,
            complete_while_typing=True,
            prompt=_INPUT_PROMPT,
            height=self.rows,
            input_processors=[
                ConditionalProcessor(
                    AfterInput(lambda: self.placeholder, style="class:placeholder"),
                    Condition(lambda: not self.area.text),
                )
            ],
        )
        self.area.buffer.on_text_changed += lambda _: self._clear_notice()
        self.editing = has_focus(self.area)
        keys = KeyBindings()

        menu_open = Condition(lambda: self.area.buffer.complete_state is not None)

        @keys.add("enter")
        def send(event):
            # The menu closes keeping what is in the draft: Tab or ↑↓ already
            # wrote the highlighted name there, and a unique start of one is
            # enough for `command` to run it.
            self.area.buffer.complete_state = None
            self.send()

        # Tab cycles the menu while it is open, and moves focus otherwise.
        @keys.add("tab", filter=menu_open)
        def next_completion(event):
            self.area.buffer.complete_next()

        @keys.add("s-tab", filter=menu_open)
        def previous_completion(event):
            self.area.buffer.complete_previous()

        @keys.add("c-j")
        def newline(event):
            self.area.buffer.newline(copy_margin=False)

        @keys.add("escape", eager=True)
        def leave(event):
            # The draft stays: Esc steps out to browse, it does not discard.
            # From a prompt it cancels that, bringing the draft back.
            if self.back is not None and not self.prompting and not self.area.text:
                self.back(event.app)
                return
            self._restore()
            event.app.layout.focus(self.home() if callable(self.home) else self.home)

        # Added after `leave`, so it wins while the menu is open: Esc closes
        # the menu first, keeping the draft as it stands.
        @keys.add("escape", eager=True, filter=menu_open)
        def close_menu(event):
            self.area.buffer.complete_state = None

        frame = Frame(self.area, title=self._title)
        self.container = HSplit(
            [frame], key_bindings=shortcuts.gate(keys) if shortcuts is not None else keys
        )

    def __pt_container__(self):
        return self.container

    @property
    def text(self) -> str:
        return self.area.text

    @property
    def prompting(self) -> bool:
        return self._saved is not None

    def open(self, app) -> None:
        app.layout.focus(self.area)

    def prompt(
        self,
        app,
        *,
        title: AnyFormattedText,
        placeholder: str,
        submit: Callable[[str], None],
        allow_empty: bool = True,
    ) -> None:
        """Ask for one value in the editor, setting the current draft aside."""
        self._restore()
        self._saved = (self.area.text, self.title, self.placeholder, self.submit, self.allow_empty)
        self.title, self.placeholder, self.submit = title, placeholder, submit
        self.allow_empty = allow_empty
        self.area.buffer.reset()
        self.notice = ""
        self.open(app)

    def completion_menu(self) -> Float:
        """The float that shows command completions at the cursor."""
        return Float(
            xcursor=True,
            ycursor=True,
            content=CompletionsMenu(
                max_height=len(self.commands) or 1,
                scroll_offset=1,
                extra_filter=has_focus(self.area),
            ),
        )

    def command(self, text: str) -> Callable[[], None] | None:
        """What running ``text`` as a command would do; None for a message.

        Refuses, with ``ValueError``, a draft that names a command this
        editor does not have.
        """
        if not self.commands or self.prompting or not text:
            return None
        name, *rest = text.split(maxsplit=1)
        if not _COMMAND_WORD.fullmatch(name):
            return None
        # The full name, or any start of exactly one: Enter can beat the menu.
        found = [command for command in self.commands if command.name == name] or [
            command for command in self.commands if command.name.startswith(name)
        ]
        if len(found) == 1:
            return lambda: found[0].run(rest[0].strip() if rest else "")
        if found:
            raise ValueError(f"{name} could be {' or '.join(c.name for c in found)}")
        raise ValueError(f"Unknown command {name}; try {' '.join(c.name for c in self.commands)}")

    def send(self) -> bool:
        """Hand the draft to ``submit``, or run the command it names; whether accepted."""
        text = self.area.text.strip()
        if not text and not self.allow_empty:
            return False
        try:
            command = self.command(text)
            if command is not None:
                command()
            else:
                self.submit(text)
        except ValueError as error:
            self.notice = str(error)
            return False
        self.area.buffer.reset()
        self._restore()
        self.notice = ""
        return True

    def _restore(self) -> None:
        """End a prompt, bringing back the draft and settings it set aside."""
        if self._saved is None:
            return
        text, self.title, self.placeholder, self.submit, self.allow_empty = self._saved
        self._saved = None
        self.area.buffer.set_document(Document(text, len(text)))

    def rows(self) -> Dimension:
        """Exactly as tall as the draft, up to ``INPUT_ROWS_MAX``, then it scrolls.

        An open-ended height would take a share of the popup's spare rows and
        sit mostly empty under the answer it is replying to.
        """
        info = self.area.window.render_info
        width = max(1, (info.window_width if info else 80) - get_cwidth(_INPUT_PROMPT))
        rows = sum(max(1, -(-get_cwidth(line) // width)) for line in self.area.text.split("\n"))
        return Dimension.exact(min(INPUT_ROWS_MAX, rows))

    def _title(self):
        title = fragment_list_to_text(to_formatted_text(self.title))
        return f"{title} · {self.notice}" if self.notice else title

    def _clear_notice(self) -> None:
        self.notice = ""
