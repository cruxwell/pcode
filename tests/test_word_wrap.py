"""Editor soft wraps land between words, not in the middle of one."""

import asyncio
from io import StringIO

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode.commands import CommandRegistry
from pcode.preferences import save_preferences
from pcode.prompt_keys import _wrap_width
from pcode.ui import Activity, Transcript, create_prompt
from pcode.word_wrap import display_row_step, row_starts, wrap_padding


def rows(text: str, width: int) -> list[str]:
    """Render `text` the way prompt_toolkit wraps it, padding included."""
    padding = wrap_padding(text, width)
    padded = "".join(" " * padding.get(i, 0) + c for i, c in enumerate(text))
    return [padded[i : i + width] for i in range(0, len(padded), width)]


def test_word_moves_to_next_row_whole():
    assert rows("alpha bravo charlie", 12) == ["alpha bravo ", "charlie"]


def test_word_wider_than_a_row_is_still_split():
    assert wrap_padding("x supercalifragilistic", 10) == {}


def test_no_padding_when_everything_fits():
    assert wrap_padding("alpha bravo", 40) == {}
    assert wrap_padding("", 40) == {}


def test_word_ending_exactly_at_the_edge_is_left_alone():
    assert wrap_padding("alpha bravo charlie", 11) == {}


def test_every_row_holds_only_complete_words():
    text = "one two three four five six seven eight nine ten"
    words = set(text.split())
    for row in rows(text, 14):
        assert set(row.split()) <= words


def test_padding_counts_columns_not_characters():
    # "ab " leaves 5 columns; the 6-column word needs the whole next row.
    assert wrap_padding("ab 日本語", 8) == {3: 5}


def test_row_starts_follow_word_wrap():
    # Rows: "alpha bravo " / "charlie"; a long word is split by column.
    assert row_starts("alpha bravo charlie", 12) == [0, 12]
    assert row_starts("abcdefghij", 4) == [0, 4, 8]
    assert row_starts("", 4) == [0]
    assert row_starts("ab 日本語", 8) == [0, 3]
    # Tabs render as ^I, two columns, though get_cwidth calls them zero.
    assert row_starts("ab\tcd\tef\tgh", 4) == [0, 3, 6, 9]


def step(text: str, cursor: int, width: int, delta: int, column=None):
    return display_row_step(Document(text, cursor), width, delta, column)


def test_display_row_step_moves_between_wrapped_rows():
    text = "alpha bravo charlie\nxy"
    # From "b" of bravo (column 6) down to column 6 of "charlie".
    assert step(text, 6, 12, 1) == (18, 6)
    assert step(text, 18, 12, -1) == (6, 6)
    # The last wrapped row steps to the next line, clamped to its last char.
    assert step(text, 18, 12, 1) == (21, 6)
    # The sticky column comes back on a longer row.
    assert step(text, 21, 12, -1, 6) == (18, 6)
    # Up from the next line lands on the previous line's last wrapped row.
    assert step(text, 20, 12, -1) == (12, 0)


def test_display_row_step_stops_at_the_edges():
    text = "alpha bravo charlie"
    assert step(text, 3, 12, -1) is None
    assert step(text, 14, 12, 1) is None


@pytest.mark.parametrize("setting", ["on", "off"])
def test_vi_jk_follow_wrapped_rows(setting):
    save_preferences(vi_wrapped_rows=setting)
    text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike"

    async def run(keys):
        stream = StringIO()
        snapshots = []
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=30), enable_cpr=False)

            def submit(_text):
                snapshots.append((prompt.default_buffer.cursor_position, _wrap_width(prompt.app)))
                prompt.app.exit()

            prompt = create_prompt(
                CommandRegistry(),
                vi_mode=True,
                transcript=Transcript(Console(file=stream)),
                on_submit=submit,
                input=pipe,
                output=output,
            )
            pipe.send_text(text + "\x1b0" + keys + "\r")
            await asyncio.wait_for(prompt.app.run_async(), timeout=3)
        return snapshots[0]

    cursor, width = asyncio.run(run("j"))
    starts = row_starts(text, width)
    assert len(starts) > 2
    assert cursor == (starts[1] if setting == "on" else 0)
    assert asyncio.run(run("2j"))[0] == (starts[2] if setting == "on" else 0)
    assert asyncio.run(run("jk"))[0] == 0
    # A count past the last row stops there rather than spilling into history.
    assert asyncio.run(run("99j"))[0] == (starts[-1] if setting == "on" else 0)


def test_editor_renders_words_unsplit():
    async def run():
        stream = StringIO()
        activity = Activity()
        transcript = Transcript(Console(file=stream), activity=activity)
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=30), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
            )
            app = session.app
            with set_app(app):
                try:
                    session.default_buffer.insert_text("alpha bravo charlie delta echo")
                    stream.seek(0)
                    stream.truncate()
                    app.renderer.render(app, app.layout)
                    data = stream.getvalue()
                    for word in ("alpha", "bravo", "charlie", "delta", "echo"):
                        assert word in data
                finally:
                    await app.cancel_and_wait_for_background_tasks()

    asyncio.run(run())
