"""Word-aware soft wrapping for the editor buffer.

prompt_toolkit wraps a buffer line strictly by column: the character that no
longer fits moves to the next row, so a word straddles the wrap point. There is
no word-wrap option on ``Window``/``BufferControl`` -- the break is decided
character by character deep inside ``Window._copy_body``.

Rather than reimplement that rendering, this pads the *display* text: when a
word would straddle the wrap point, enough spaces are inserted before it to
fill the current row, so prompt_toolkit's own column wrapping lands on the word
boundary. The buffer text is untouched; the cursor mappings below keep editing
positions correct, the same technique ``TabsProcessor`` uses to expand tabs.
"""

from __future__ import annotations

from bisect import bisect_right
from functools import cache

from prompt_toolkit.document import Document
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.layout.utils import explode_text_fragments

__all__ = ["WordWrapProcessor", "display_row_step", "row_starts", "wrap_padding"]


@cache
def cell_width(char: str) -> int:
    """Columns the renderer draws ``char`` in: a tab shows as ``^I``, two wide."""
    return Char(char, "").width


def text_width(text: str) -> int:
    return sum(map(cell_width, text))


def wrap_padding(text: str, width: int) -> dict[int, int]:
    """Map index -> number of spaces to insert before ``text[index]``.

    ``width`` is the number of columns available for the text itself (the line
    prefix is already subtracted). Words wider than a full row are left alone:
    they have to be split somewhere, and padding them would only waste a row.
    """
    if width < 2 or not text:
        return {}

    padding: dict[int, int] = {}
    column = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        starts_word = not char.isspace() and (index == 0 or text[index - 1].isspace())
        # A word starting at column 0, or one whose row is already full, wraps
        # correctly on its own.
        if starts_word and 0 < column < width:
            end = index
            while end < length and not text[end].isspace():
                end += 1
            word = text_width(text[index:end])
            if column + word > width >= word:
                padding[index] = width - column
                column = 0
        char_width = cell_width(char)
        if column + char_width > width:
            column = 0
        column += char_width
        index += 1
    return padding


def row_starts(line: str, width: int) -> list[int]:
    """Indexes in ``line`` where each display row begins, as the editor wraps it.

    Mirrors the padding above plus prompt_toolkit's column wrap: a character
    that would overflow ``width`` starts the next row.
    """
    padding = wrap_padding(line, width)
    starts = [0]
    column = 0
    for index, char in enumerate(line):
        char_width = cell_width(char)
        if index in padding or (column and column + char_width > width):
            starts.append(index)
            column = 0
        column += char_width
    return starts


def display_row_step(
    document: Document, width: int, delta: int, column: int | None = None
) -> tuple[int, int] | None:
    """Vi-normal-mode cursor one display row down (``delta`` 1) or up (-1).

    Returns the new cursor index and the goal column it aimed for, so repeated
    moves keep the column across shorter rows, or None on the first/last row.
    ``column`` is that goal from the previous move; None measures the cursor's.
    """
    lines = document.lines
    number = document.cursor_position_row
    line = lines[number]
    local = document.cursor_position_col
    starts = row_starts(line, width)
    row = bisect_right(starts, local) - 1
    if column is None:
        column = text_width(line[starts[row] : local])

    if 0 <= row + delta < len(starts):
        row += delta
    elif 0 <= number + delta < len(lines):
        number += delta
        line = lines[number]
        starts = row_starts(line, width)
        row = 0 if delta > 0 else len(starts) - 1
    else:
        return None

    start = starts[row]
    end = starts[row + 1] if row + 1 < len(starts) else len(line)
    target = max(start, end - 1)
    used = 0
    for index in range(start, end):
        used += cell_width(line[index])
        if used > column:
            target = index
            break
    return document.translate_row_col_to_index(number, 0) + target, column


class WordWrapProcessor(Processor):
    """Pad display lines so soft wraps fall between words.

    :param prefix_width: Columns taken by the window's line prefix (the prompt
        and its continuation), which ``TransformationInput.width`` includes.
    """

    def __init__(self, prefix_width: int = 0) -> None:
        self.prefix_width = prefix_width

    def apply_transformation(self, ti: TransformationInput) -> Transformation:
        fragments = explode_text_fragments(ti.fragments)
        padding = wrap_padding(
            "".join(text for _, text, *_ in fragments), ti.width - self.prefix_width
        )
        if not padding:
            return Transformation(ti.fragments)

        result = []
        positions = {}
        position = 0
        for index, fragment in enumerate(fragments):
            pad = padding.get(index)
            if pad:
                result.append(("", " " * pad))
                position += pad
            positions[index] = position
            result.append(fragment)
            position += 1
        # The cursor can also sit right after the line, and one past that.
        positions[len(fragments)] = position
        positions[len(fragments) + 1] = position + 1

        def source_to_display(from_position: int) -> int:
            return positions.get(from_position, from_position)

        def display_to_source(display_position: int) -> int:
            reversed_positions = {v: k for k, v in positions.items()}
            while display_position >= 0:
                if display_position in reversed_positions:
                    return reversed_positions[display_position]
                display_position -= 1
            return 0

        return Transformation(
            result,
            source_to_display=source_to_display,
            display_to_source=display_to_source,
        )
