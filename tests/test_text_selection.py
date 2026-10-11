"""Mouse selection in popup panes copies text, not the screen rows it wraps to."""

import asyncio
from unittest.mock import patch

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from pcode.frame import Frame
from pcode.popup_ui import RichPane
from pcode.text_selection import FOLD_END, WRAP_END, WRAP_START

MARKDOWN = """\
Para one is quite long and will definitely wrap across several lines when rendered narrow okay.
Second hard line.

- list item that is long enough to wrap around the narrow width here
- short

```python
def f(x):  # a fairly long comment that goes past the width of the pane for sure
    return x
```

> quoted text that is long enough to wrap around at the narrow width
"""


def _pane(*renderables, width=40) -> RichPane:
    pane = RichPane(color_system=None)
    pane.set(list(renderables))
    pane.control.create_content(width, 10)
    return pane


def _select(pane: RichPane, first: tuple[int, int], last: tuple[int, int]) -> str:
    pane.selection.anchor, pane.selection.cursor = first, last
    pane.selection.active = True
    return pane.selection.text()


def _row(pane: RichPane, needle: str) -> int:
    return next(index for index, row in enumerate(pane.rows(40)) if needle in row.text)


def _event(kind, row, column, button=MouseButton.LEFT) -> MouseEvent:
    return MouseEvent(Point(column, row), kind, button, frozenset())


def test_wrapped_rows_copy_as_the_lines_they_came_from():
    pane = _pane(Markdown(MARKDOWN))
    end = _row(pane, "line.")
    assert _select(pane, (0, 0), (end, 39)) == (
        "Para one is quite long and will definitely wrap across several lines "
        "when rendered narrow okay. Second hard line."
    )
    # A list item's hanging indent and a quote's gutter are not text.
    item = _row(pane, "list item")
    assert _select(pane, (item, 0), (item + 2, 39)) == (
        " • list item that is long enough to wrap around the narrow width here\n • short"
    )
    quote = _row(pane, "quoted")
    assert _select(pane, (quote, 0), (quote + 1, 39)) == (
        "▌ quoted text that is long enough to wrap around at the narrow width"
    )
    # Code keeps its hard lines; only the wrapped one rejoins.
    code = _row(pane, "def f")
    selected = _select(pane, (code, 0), (_row(pane, "return x"), 39))
    assert selected.splitlines()[0].endswith(
        "comment that goes past the width of the pane for sure"
    )
    assert selected.splitlines()[1] == "     return x"


def test_a_word_folded_across_rows_rejoins_without_a_space():
    pane = _pane(Text("x" * 100 + " tail"))
    assert _select(pane, (0, 0), (len(pane.rows(40)) - 1, 39)) == "x" * 100 + " tail"


def test_a_wrapped_value_rejoins_but_a_cell_mid_row_does_not_join_the_row():
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row("Path", "/a/very/long/path/that/has/to/fold/across/rows.txt")
    pane = _pane(grid)
    last = len(pane.rows(40)) - 1
    # The label column is indent to the continued rows, as a hanging indent is.
    assert _select(pane, (0, 0), (last, 39)) == (
        "Path  /a/very/long/path/that/has/to/fold/across/rows.txt"
    )
    middle = Table.grid(padding=(0, 2))
    middle.add_row("a first cell long enough to wrap", "end")
    pane = _pane(middle)
    assert all(row.end is None for row in pane.rows(40))
    assert all(marker not in pane.text(40) for marker in (WRAP_END, FOLD_END, WRAP_START))


def test_markers_never_reach_the_screen_or_other_rendering():
    pane = _pane(Markdown(MARKDOWN))
    drawn = "".join(text for line in pane.lines(40) for _, text, *_ in line)
    assert all(marker not in drawn for marker in (WRAP_END, FOLD_END, WRAP_START))
    console = Console(width=40, color_system=None)
    with console.capture() as capture:
        console.print(Markdown(MARKDOWN))
    assert all(marker not in capture.get() for marker in (WRAP_END, FOLD_END, WRAP_START))


def test_drag_highlights_and_copies_on_release_and_a_click_copies_nothing():
    pane = _pane(Text("alpha beta gamma\nsecond line"))
    selection = pane.selection
    with patch("pcode.popup_ui.copy_selected", return_value="Copied") as copy:
        selection.handle(_event(MouseEventType.MOUSE_DOWN, 0, 6))
        selection.handle(_event(MouseEventType.MOUSE_UP, 0, 6, MouseButton.NONE))
        copy.assert_not_called()
        selection.handle(_event(MouseEventType.MOUSE_DOWN, 0, 6))
        selection.handle(_event(MouseEventType.MOUSE_MOVE, 1, 5))
        selection.handle(_event(MouseEventType.MOUSE_UP, 1, 5, MouseButton.NONE))
        copy.assert_called_once_with("beta gamma\nsecond")
    content = pane.control.create_content(40, 10)
    selected = [text for style, text in content.get_line(0) if "class:selected" in style]
    assert "".join(selected) == "beta gamma"
    # The wheel still belongs to the window.
    assert selection.handle(_event(MouseEventType.SCROLL_DOWN, 0, 0, MouseButton.NONE)) is (
        NotImplemented
    )


def test_double_click_takes_a_word_and_triple_click_the_whole_wrapped_line():
    pane = _pane(Text("one two " * 10 + "\nnext"))
    copied = []
    with patch("pcode.popup_ui.copy_selected", side_effect=copied.append):
        for _ in range(3):
            pane.selection.handle(_event(MouseEventType.MOUSE_DOWN, 1, 1))
            pane.selection.handle(_event(MouseEventType.MOUSE_UP, 1, 1, MouseButton.NONE))
    word, line = copied
    assert pane.rows(40)[1].text[:1] in word
    assert line == ("one two " * 10).strip()


def test_a_new_content_or_width_drops_the_selection_but_streaming_keeps_it():
    pane = _pane(Text("alpha beta"))
    _select(pane, (0, 0), (0, 4))
    pane.follow([Text("alpha beta\nmore")])
    assert pane.selection.span() == ((0, 0), (0, 4))
    # Rows above the selection changed: its positions now point elsewhere.
    pane.follow([Text("new first\nalpha beta\nmore")])
    assert pane.selection.span() is None
    _select(pane, (0, 0), (0, 4))
    pane.control.create_content(30, 10)
    assert pane.selection.span() is None
    _select(pane, (0, 0), (0, 4))
    pane.set([Text("other")])
    assert pane.selection.span() is None


def test_streaming_mid_drag_still_copies_on_release():
    pane = _pane(Text("alpha beta"))
    with patch("pcode.popup_ui.copy_selected", return_value="Copied") as copy:
        pane.selection.handle(_event(MouseEventType.MOUSE_DOWN, 0, 0))
        pane.selection.handle(_event(MouseEventType.MOUSE_MOVE, 0, 2))
        pane.follow([Text("alpha beta\nmore")])
        pane.selection.handle(_event(MouseEventType.MOUSE_MOVE, 0, 4))
        pane.selection.handle(_event(MouseEventType.MOUSE_UP, 0, 4, MouseButton.NONE))
    copy.assert_called_once_with("alpha")


def test_a_release_outside_the_pane_ends_the_drag_where_it_last_was():
    """Hovering back in after a release elsewhere must not keep extending it."""
    pane = _pane(Text("alpha beta gamma"))
    with patch("pcode.popup_ui.copy_selected", return_value="Copied") as copy:
        pane.selection.handle(_event(MouseEventType.MOUSE_DOWN, 0, 0))
        pane.selection.handle(_event(MouseEventType.MOUSE_MOVE, 0, 4))
        # The release went to another pane; the next report is a plain hover.
        pane.selection.handle(_event(MouseEventType.MOUSE_MOVE, 0, 12, MouseButton.NONE))
        pane.selection.handle(_event(MouseEventType.MOUSE_MOVE, 0, 14, MouseButton.NONE))
    copy.assert_called_once_with("alpha")
    assert not pane.selection.pressed and pane.selection.span() == ((0, 0), (0, 4))


def test_positions_count_characters_so_wide_text_selects_what_is_drawn():
    pane = _pane(Text("日本語 text here"))
    # prompt_toolkit reports character indices: "text" is 4..7, not columns 7..10.
    assert _select(pane, (0, 4), (0, 7)) == "text"
    content = pane.control.create_content(40, 10)
    selected = [text for style, text in content.get_line(0) if "class:selected" in style]
    assert "".join(selected) == "text"


def test_a_one_character_word_and_a_drag_from_the_gutter():
    pane = _pane(Text("I am here"))
    copied = []
    with patch("pcode.popup_ui.copy_selected", side_effect=copied.append):
        for _ in range(2):
            pane.selection.handle(_event(MouseEventType.MOUSE_DOWN, 0, 0))
            pane.selection.handle(_event(MouseEventType.MOUSE_UP, 0, 0, MouseButton.NONE))
    assert copied == ["I"]
    quote = _pane(Markdown("> quoted text that is long enough to wrap around at the narrow width"))
    # Starting on the second row's gutter copies from its text, as highlighted.
    row = _row(quote, "wrap around")
    assert _select(quote, (row, 0), (row, 39)) == "wrap around at the narrow width"


def test_centred_wrapped_text_copies_without_its_padding():
    pane = _pane(Text("a centred line long enough to wrap around", justify="center"), width=20)
    last = len(pane.rows(20)) - 1
    pane.selection.anchor, pane.selection.cursor, pane.selection.active = (0, 0), (last, 19), True
    assert pane.selection.text() == "a centred line long enough to wrap around"


def test_a_drag_in_a_framed_pane_copies_neither_the_border_nor_the_neighbour():
    """End to end: SGR reports through a real app, as a mouse-enabled terminal sends them."""
    words = " ".join(f"word{index}" for index in range(30))
    copied = []
    pane = RichPane(color_system=None, notify=copied.append)
    pane.set([Text(words)])
    layout = VSplit([Window(FormattedTextControl("LEFT PANE"), width=10), Frame(pane)])

    async def run():
        with create_pipe_input() as pipe:
            app = Application(
                layout=Layout(layout),
                full_screen=True,
                mouse_support=True,
                input=pipe,
                output=DummyOutput(),
            )
            task = asyncio.create_task(app.run_async())
            try:
                await asyncio.sleep(0.05)
                # 1-based: column 12 is the pane's first text cell, row 2 its first row.
                pipe.send_text("\x1b[<0;12;2M")
                pipe.send_text("\x1b[<32;31;3M")
                pipe.send_text("\x1b[<0;31;3m")
                await asyncio.sleep(0.05)
            finally:
                app.exit()
                await task

    with patch("pcode.text_selection.copy_to_clipboard", return_value=(True, False)) as copy:
        asyncio.run(run())
    text = copy.call_args.args[0]
    assert text.startswith("word0 word1") and "\n" not in text
    assert "│" not in text and "LEFT" not in text
    assert words.startswith(text)
    assert copied == [f"Copied {len(text)} characters"]


def test_a_read_only_text_area_copies_its_drag_selection():
    from prompt_toolkit.widgets import TextArea

    from pcode.text_selection import copy_on_select

    area = TextArea(text="Session  abc123\nPath     /tmp/somewhere", read_only=True)
    copied = []
    copy_on_select(area, copied.append)

    async def run():
        with create_pipe_input() as pipe:
            app = Application(
                layout=Layout(area),
                full_screen=True,
                mouse_support=True,
                input=pipe,
                output=DummyOutput(),
            )
            task = asyncio.create_task(app.run_async())
            try:
                await asyncio.sleep(0.05)
                pipe.send_text("\x1b[<0;10;2M\x1b[<32;24;2M\x1b[<0;24;2m")
                await asyncio.sleep(0.05)
            finally:
                app.exit()
                await task

    asyncio.run(run())
    assert copied == ["/tmp/somewhere"]
