"""Mouse text selection inside popup panes, copied without wraps or borders.

A terminal selects screen rows: it cannot tell a wrapped line from a hard one,
or a pane's text from its frame and the pane beside it. Popups capture the
mouse anyway, so a pane selects its own text instead and copies what it knows
is there.

Rich wraps to the pane width by inserting real newlines, so `marked_rendering`
has `Text.wrap` mark the rows it split with zero-width characters, which
`unmark` reads back and strips before anything is drawn.

Positions are (row, character index) pairs, as prompt_toolkit's window hands
them to a control's mouse handler: a wide character is one position.
"""

import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from prompt_toolkit.application import get_app
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from rich._wrap import divide_line  # How Text.wrap finds its breaks.
from rich.cells import cell_len
from rich.containers import Lines
from rich.text import Text

from pcode.clipboard import copy as copy_to_clipboard

# Invisible operators: zero cells wide to Rich, and absent from anything a
# model or a tool prints in practice.
WRAP_END = "\u2063"  # This row continues on the next, broken between words.
FOLD_END = "\u2062"  # This row continues on the next, broken inside a word.
WRAP_START = "\u2064"  # A continued row's text starts here; before it is indent.
_MARKERS = re.compile(f"[{WRAP_END}{FOLD_END}{WRAP_START}]")
_ENDS = re.compile(f"[{WRAP_END}{FOLD_END}]")
# What may follow a wrapped row's text: padding, and the right edge of a box.
_TRAILING_CHROME = re.compile(r"[\s\u2500-\u257f]*")
# What a double-click word stops at besides whitespace: box and quote gutters.
_WORD = re.compile(r"[^\s\u2500-\u259f]+")
_SELECTED = "class:selected"
# Clicks closer together than this, in seconds, count as a double or triple click.
_MULTI_CLICK = 0.4

_marking: ContextVar[bool] = ContextVar("pcode_marking_wraps", default=False)
_rich_wrap = Text.wrap


def _marked_wrap(self: Text, console, width: int, **options) -> Lines:
    # Most text fits on one row; it has nothing to mark, so skip the bookkeeping.
    if not _marking.get() or ("\n" not in self.plain and cell_len(self.plain) <= width):
        return _rich_wrap(self, console, width, **options)
    # Rich splits on newlines before wrapping; wrap each hard line alone so a
    # row's marker says whether Rich broke it, not whether the text did.
    result = Lines()
    for piece in self.split("\n", allow_blank=True):
        rows = list(_rich_wrap(piece, console, width, **options))
        ends = _break_markers(piece, width, len(rows) - 1, **options)
        for index, row in enumerate(rows):
            if index:
                marked = row.blank_copy(WRAP_START)
                marked.append_text(row)
                row = marked
            if index < len(ends):
                row.append(ends[index])
            result.append(row)
    return result


def _break_markers(piece: Text, width: int, breaks: int, **options) -> list[str]:
    """Which marker ends each row Rich split ``piece`` into, but the last.

    Rich pads rows before they come back, so the break itself is found the way
    Rich found it. Should that ever disagree, a space is the likelier break.
    """
    if breaks <= 0:
        return []
    text = piece.plain.expandtabs(options.get("tab_size") or 8)
    overflow = options.get("overflow") or piece.overflow or "fold"
    offsets = divide_line(text, width, fold=overflow == "fold")
    if len(offsets) != breaks:
        return [WRAP_END] * breaks
    return [WRAP_END if text[offset - 1].isspace() else FOLD_END for offset in offsets]


Text.wrap = _marked_wrap  # type: ignore[method-assign]


@contextmanager
def marked_rendering() -> Iterator[None]:
    """Mark wrap points in whatever Rich renders on this thread meanwhile."""
    token = _marking.set(True)
    try:
        yield
    finally:
        _marking.reset(token)


@dataclass(frozen=True)
class Row:
    """One drawn row's plain text and where Rich's wrap left its content."""

    text: str
    # Indices into text: where a continued row's text starts, and where a
    # wrapped row's text ends; None when the row ends a line of its own.
    start: int = 0
    end: int | None = None
    # What rejoins a wrapped row to the next: a space, or nothing inside a word.
    joiner: str = " "


def unmark(line: StyleAndTextTuples) -> tuple[StyleAndTextTuples, Row]:
    """Strip wrap markers from one drawn row, returning it and what they said."""
    text = "".join(fragment[1] for fragment in line)
    if not _MARKERS.search(text):
        return line, Row(text)
    plain = _MARKERS.sub("", text)
    start = text.find(WRAP_START)
    start = len(_MARKERS.sub("", text[:start])) if start >= 0 else 0
    ends = list(_ENDS.finditer(text))
    end, joiner = None, " "
    # A cell wrapping inside a table row is not the row wrapping.
    if ends and _TRAILING_CHROME.fullmatch(text, ends[-1].end()):
        end = len(_MARKERS.sub("", text[: ends[-1].start()]))
        joiner = " " if ends[-1].group() == WRAP_END else ""
    stripped = [(style, _MARKERS.sub("", piece), *rest) for style, piece, *rest in line]
    return [fragment for fragment in stripped if fragment[1]], Row(plain, start, end, joiner)


def highlight(line: StyleAndTextTuples, start: int, end: int) -> StyleAndTextTuples:
    """``line`` with characters ``start`` up to ``end`` drawn as selected."""
    result: StyleAndTextTuples = [
        (f"{style} {_SELECTED}" if start <= index < end else style, character, *rest)
        for index, (style, character, *rest) in enumerate(explode_text_fragments(line))
    ]
    if end > len(result):
        # Past the text's end, as on a selected blank row: show the selection there.
        result.append((_SELECTED, " " * (end - max(start, len(result)))))
    return result


def copy_selected(text: str) -> str:
    """Copy a mouse selection to the clipboard, returning a notice saying how it went."""
    copied, truncated = copy_to_clipboard(text, get_app().output)
    if not copied:
        return "Could not copy the selection"
    lines = text.count("\n") + 1
    size = f"{lines} lines" if lines > 1 else f"{len(text)} characters"
    return f"Copied {size}" + (" (truncated)" if truncated else "")


def copy_on_select(area, on_select: Callable[[str], None]) -> None:
    """Hand a TextArea's mouse selection to ``on_select`` when the button comes up.

    prompt_toolkit already drags and double-clicks a buffer selection, and a
    buffer holds its text unwrapped, so only the copy is missing.
    """
    control = area.control
    handle = control.mouse_handler

    def handler(event: MouseEvent):
        result = handle(event)
        buffer = area.buffer
        if event.event_type == MouseEventType.MOUSE_UP and buffer.selection_state is not None:
            # What the highlight shows, without clearing it as copy_selection would.
            selected = buffer.document.cut_selection()[1].text.strip("\n")
            if selected:
                on_select(selected)
        return result

    control.mouse_handler = handler


Position = tuple[int, int]  # (row, character index)


class Selection:
    """A pane's mouse selection over rows from ``rows()``, copied on release."""

    def __init__(self, rows: Callable[[], list[Row]], on_select: Callable[[str], None]):
        self.rows = rows
        self.on_select = on_select
        self.anchor: Position | None = None
        self.cursor: Position | None = None
        # Whether anything is selected: a drag that moved, or a word or line
        # clicked, which can be a single character with anchor == cursor.
        self.active = False
        self.pressed = False
        self._clicks: list[tuple[float, Position]] = []

    def clear(self) -> None:
        self.anchor = self.cursor = None
        self.active = self.pressed = False

    def span(self) -> tuple[Position, Position] | None:
        """The selection's first and last positions, both inclusive."""
        if not self.active or self.anchor is None or self.cursor is None:
            return None
        first, last = sorted((self.anchor, self.cursor))
        return first, last

    def columns(self, row: int) -> tuple[int, int] | None:
        """Selected character indices on ``row``, end exclusive."""
        span = self.span()
        rows = self.rows()
        if span is None or not span[0][0] <= row <= span[1][0] or row >= len(rows):
            return None
        (first_row, first_index), (last_row, last_index) = span
        start = first_index if row == first_row else 0
        if row == last_row:
            return start, last_index + 1
        # Through the end of a middle row's text, so a blank row shows selected too.
        return start, max(start + 1, len(rows[row].text.rstrip()))

    def text(self) -> str:
        """The selected text: wrapped rows rejoined, indent and padding dropped."""
        span = self.span()
        if span is None:
            return ""
        rows = self.rows()
        (first_row, first_index), (last_row, last_index) = span
        pieces: list[str] = []
        for number in range(first_row, min(last_row, len(rows) - 1) + 1):
            row = rows[number]
            continued = number > 0 and rows[number - 1].end is not None
            # A continued row's indent is never text, wherever the drag began.
            start = row.start if continued else 0
            if number == first_row:
                start = max(start, first_index)
            end = len(row.text) if row.end is None else row.end
            if number == last_row:
                end = min(end, last_index + 1)
            # Rich pads a row to the width; the break itself becomes the joiner.
            piece = row.text[start:end].rstrip()
            if continued and rows[number - 1].joiner:
                # Centred or right-aligned rows are padded on the left too.
                piece = piece.lstrip()
            if row.end is not None and number < last_row and end > start:
                piece += row.joiner
            if continued and number > first_row and pieces:
                pieces[-1] += piece
            else:
                pieces.append(piece)
        return "\n".join(pieces).strip("\n")

    def handle(self, event: MouseEvent):
        """Track a press, drag and release; NotImplemented for anything else (the wheel)."""
        position = (event.position.y, event.position.x)
        kind = event.event_type
        # urxvt's encoding reports no button on a press.
        if kind == MouseEventType.MOUSE_DOWN and event.button in (
            MouseButton.LEFT,
            MouseButton.UNKNOWN,
        ):
            self.anchor = self.cursor = position
            self.active, self.pressed = False, True
            return None
        if kind == MouseEventType.MOUSE_MOVE and self.pressed:
            # Any-motion tracking reports hovering too. A move with no button
            # held means the release landed outside this pane: end the drag
            # where it last was, not where the pointer came back in.
            if event.button == MouseButton.NONE:
                self._release(self.cursor or position, click=False)
            else:
                self.cursor = position
                self.active = self.active or position != self.anchor
            return None
        if kind == MouseEventType.MOUSE_UP and self.pressed:
            self._release(position, click=True)
            return None
        return NotImplemented

    def _release(self, position: Position, *, click: bool) -> None:
        self.pressed = False
        self.cursor = position
        self.active = self.active or position != self.anchor
        clicks = self._count_click(position) if click else 1
        if not self.active and clicks > 1:
            self._select_around(position, whole_line=clicks > 2)
        selected = self.text()
        if selected:
            self.on_select(selected)

    def _count_click(self, position: Position) -> int:
        now = time.monotonic()
        self._clicks = [
            (moment, spot)
            for moment, spot in self._clicks
            if now - moment < _MULTI_CLICK and spot == position
        ][-2:]
        self._clicks.append((now, position))
        return len(self._clicks)

    def _select_around(self, position: Position, *, whole_line: bool) -> None:
        rows = self.rows()
        number, index = position
        if not 0 <= number < len(rows):
            return
        if whole_line:
            first = last = number
            while first and rows[first - 1].end is not None:
                first -= 1
            while last < len(rows) - 1 and rows[last].end is not None:
                last += 1
            text = rows[last].text.rstrip()
            end = len(text) if rows[last].end is None else rows[last].end
            self.anchor, self.cursor = (first, 0), (last, max(0, end - 1))
            self.active = True
            return
        for match in _WORD.finditer(rows[number].text):
            if match.start() <= index < match.end():
                self.anchor, self.cursor = (number, match.start()), (number, match.end() - 1)
                self.active = True
                return
