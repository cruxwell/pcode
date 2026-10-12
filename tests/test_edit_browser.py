import asyncio
from io import StringIO
from pathlib import Path

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.styles import Style
from pygments.token import Generic
from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text

from pcode.app import PreviewApp
from pcode.delta import Delta
from pcode.edit_transcript import DiffLexer
from pcode.edit_ui import (
    DiffBrowser,
    Note,
    editor_command,
    lay_out,
    notes_prompt,
    quote_for,
    render_review,
    split_patch,
)
from pcode.git_diff import Review
from pcode.runtime import EditCompleted

PATCH = "--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n x\n-old\n+new\n y\n@@ -10,2 +10,3 @@\n p\n+q\n r"


def change(path, call_id="one", patch="@@ -1 +1 @@\n-old\n+new", **kwargs):
    return EditCompleted(call_id, path, "edited", patch, added=1, removed=1, **kwargs)


def review(changes, **kwargs):
    return Review(kwargs.pop("title", "Net"), list(changes), kwargs.pop("empty", "none"), **kwargs)


def browser(changes=(), pipe=None, **kwargs):
    options = {
        k: kwargs.pop(k) for k in ("delta", "reload", "mark", "rendered", "width") if k in kwargs
    }
    output = kwargs.pop("output", DummyOutput())
    return DiffBrowser(review(changes, **kwargs), input=pipe, output=output, **options)


def select_file(ui, index):
    ui.files.buffer.cursor_position = ui.files.document.translate_row_col_to_index(index, 0)


def current_path(ui):
    row = ui.current_row()
    return row.change.path if row and row.change else None


def test_every_file_is_one_scroll_and_the_file_index_follows_the_cursor():
    changes = [change(f"file_{i}.py", call_id=str(i)) for i in range(3)]
    with create_pipe_input() as pipe:
        ui = browser(changes, pipe)
        text = ui.diff.text
        assert all(f"file_{i}.py" in text for i in range(3)) and text.count("-old") == 3
        assert ui.files.text.splitlines() == [f"  edited    file_{i}.py · +1 −1" for i in range(3)]
        assert current_path(ui) == "file_0.py" and ui.position() == 1
        select_file(ui, 2)  # choosing a file scrolls the diff to it
        assert current_path(ui) == "file_2.py" and ui.position() == 3
        assert ui.diff.window.vertical_scroll == ui.page.starts[2]
        # Moving in the diff moves the index with it.
        ui.go_to(ui.page.starts[1] + 1)
        assert ui.files.document.cursor_position_row == 1


def test_unavailable_and_truncated_diffs_are_explained():
    with create_pipe_input() as pipe:
        ui = browser(
            [
                change("big.py", truncated=True),
                change("binary.bin", patch="", omitted="Binary content"),
            ],
            pipe,
        )
        assert "additional diff rows omitted" in ui.diff.text
        assert "Diff unavailable: Binary content" in ui.diff.text


def test_an_empty_review_shows_its_notice():
    async def run():
        screen = StringIO()
        with create_pipe_input() as pipe:
            ui = browser(
                [],
                pipe,
                title="feature vs main",
                empty="Nothing.",
                output=Vt100_Output(screen, lambda: Size(rows=24, columns=80), enable_cpr=False),
            )
            assert ui.diff.text == "Nothing." and ui.files.text == "Nothing."
            assert ui.current_row().change is None and ui.position() == 0
            with set_app(ui.app):
                ui.app.renderer.render(ui.app, ui.app.layout)
        assert "feature vs main" in screen.getvalue()
        assert "All changes (0)" in screen.getvalue()

    asyncio.run(run())


def test_secrets_are_redacted_before_display():
    with create_pipe_input() as pipe:
        ui = browser([change("app.py", patch='+token = "sk-0123456789ab"')], pipe)
        assert "sk-0123456789ab" not in ui.diff.text
        assert "[redacted]" in ui.diff.text


def test_lexer_colors_match_the_scrollback_diff_theme():
    document = Document("@@ -1 +1 @@\n-old\n+new\n context")
    line = DiffLexer("monokai").lex_document(document)
    theme = Syntax.get_theme("monokai")
    expected = [Generic.Subheading, Generic.Deleted, Generic.Inserted]
    for row, token in enumerate(expected):
        style = line(row)[0][0]
        color = theme.get_style_for_token(token).color.get_truecolor().hex.lstrip("#")
        assert Style.from_dict({}).get_attrs_for_style_str(style).color == color
    assert line(1)[0][0] != line(2)[0][0]
    assert not Style.from_dict({}).get_attrs_for_style_str(line(3)[0][0]).bgcolor


def test_rows_know_their_file_line_and_hunk():
    header, preamble, hunks = split_patch(PATCH + "\n\\ No newline at end of file")
    assert header == ["--- a/a.py", "+++ b/a.py"] and preamble == []
    assert [h.numbers for h in hunks] == [(1, 2, 2, 3), (10, 11, 12, 12)]
    page = lay_out([change("a.py", patch=PATCH)], {}, [])
    lines = page.text.splitlines()
    assert lines[0] == "Edited a.py · +1 −1"
    by_text = {text: row for text, row in zip(lines, page.rows, strict=True)}
    assert (by_text["+new"].line, by_text["+new"].anchor.index) == (2, 2)
    assert by_text["-old"].line == 2  # a removed line takes the line now in its place
    assert by_text["+q"].line == 11 and by_text["@@ -10,2 +10,3 @@"].anchor.index is None
    # A deleted file's hunk starts at line 0; the editor gets line 1.
    (gone,) = split_patch("--- a/g.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye")[2]
    assert gone.numbers == (1,)


class FakeDelta(Delta):
    """Lays each hunk out as one row per patch line, or fails, recording its batches."""

    def render_all(self, patches, width):
        self.calls.append((tuple(patches), width))
        if self.args == ("fail",):
            return [None] * len(patches)
        groups = []
        for patch in patches:
            body = patch.split("\n")[3:]
            rows = body[:1] if self.args == ("pairs",) else body
            groups.append([[Text(f"D{width} {line}", style="on red")] for line in rows])
        return groups


def fake_delta(*args, layout="unified"):
    delta = FakeDelta("delta", args, layout=layout)
    object.__setattr__(delta, "calls", [])
    return delta


class SizedOutput(DummyOutput):
    columns = 84

    def get_size(self):
        return Size(rows=40, columns=self.columns)


def test_delta_lays_out_every_hunk_of_every_view_in_one_run():
    delta = fake_delta()
    a, b = change("a.py", patch=PATCH), change("b.py", patch=PATCH.replace("a.py", "b.py"))

    async def resize():
        ui.rewidth()
        assert ui.rendering  # off the event loop, at the pane's width
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not ui.rendering:
                break

    with create_pipe_input() as pipe:
        ui = browser([a, b], pipe, uncommitted=[b], delta=delta, output=SizedOutput())
        asyncio.run(resize())
        ((patches, width),) = delta.calls
        assert width == 80 and len(patches) == 4  # two hunks in each of two files
        lines = ui.diff.text.splitlines()
        assert "D80 +new" in lines and "⋯" in lines  # delta draws no hunk header
        row = lines.index("D80 +new")
        assert ui.page.rows[row].line == 2 and ui.page.rows[row].anchor.index == 2
        styled = ui.lexer.lex_document(Document(ui.diff.text))(row)
        assert any("bg:" in style for style, _ in styled)
        ui.show("uncommitted")  # another view is already rendered
        assert len(delta.calls) == 1 and "D80 +new" in ui.diff.text


def test_rows_delta_pairs_up_anchor_to_their_hunk():
    with create_pipe_input() as pipe:
        delta = fake_delta("pairs")
        ui = browser([change("a.py", patch=PATCH)], pipe, delta=delta, output=SizedOutput())
        asyncio.run(ui.rerender(80))
        row = ui.diff.text.splitlines().index("D80  x")
        assert ui.page.rows[row].line == 1 and ui.page.rows[row].anchor.index is None


def test_a_failed_delta_shows_the_plain_patch():
    with create_pipe_input() as pipe:
        ui = browser([change("a.py", patch=PATCH)], pipe, delta=fake_delta("fail"))
        asyncio.run(ui.rerender(80))
        assert "-old" in ui.diff.text and ui.lexer.rows.keys() == {0}  # only the heading


def test_views_cover_all_uncommitted_and_new_since_review():
    a, b, c = change("a.py"), change("b.py"), change("c.py")
    with create_pipe_input() as pipe:
        ui = browser([a, b], pipe, uncommitted=[b], since_review=[a])
        # Work new since the last review is what to read first.
        assert ui.view == "review" and [x.path for x in ui.visible] == ["a.py"]
        assert ui.tabs() == "  All changes (2)     Uncommitted (1)   ▸ Since review (1)"
        ui.show(ui.next_view())
        assert ui.view == "all"
        assert ui.files.text.splitlines()[1].startswith("● ")  # b.py is uncommitted
        assert "● Edited b.py" in ui.diff.text
        ui.show(ui.next_view())
        assert [x.path for x in ui.visible] == ["b.py"] and "●" not in ui.diff.text
        ui = browser([a], pipe, since_review=[])
        assert ui.view == "all" and "Nothing new since your last review" in ui.heading()
        ui = browser([c], pipe)  # never reviewed: no since-review view to offer
        assert ui.views() == ["all", "uncommitted"]


def test_switching_views_keeps_the_file_and_line():
    net = change("a.py", patch=PATCH)
    later = change("a.py", patch="--- a/a.py\n+++ b/a.py\n@@ -10,2 +10,3 @@\n p\n+q\n r")
    with create_pipe_input() as pipe:
        ui = browser([change("0.py"), net], pipe, uncommitted=[later])
        ui.go_to(ui.diff.text.splitlines().index("+q"))
        ui.show("uncommitted")
        assert ui.current_row().line == 11 and current_path(ui) == "a.py"


def test_path_search_fuzzy_filters_the_file_list():
    changes = [
        change("src/pcode/edit_ui.py", call_id="1"),
        change("tests/test_edit_browser.py", call_id="2"),
        change("docs/commands.md", call_id="3"),
    ]
    with create_pipe_input() as pipe:
        ui = browser(changes, pipe)
        ui.query.text = "ed_ui"  # Joined word prefixes, not a plain substring.
        assert [c.path for c in ui.visible] == ["src/pcode/edit_ui.py"]
        assert current_path(ui) == "src/pcode/edit_ui.py"
        ui.query.text = "test edit"
        assert [c.path for c in ui.visible] == ["tests/test_edit_browser.py"]
        ui.query.text = "nothing-here"
        assert ui.visible == [] and current_path(ui) is None
        assert "No matching files" in ui.files.text and "No matching files" in ui.diff.text
        ui.query.text = ""
        assert len(ui.visible) == 3


def test_diff_search_filters_changes_and_jumps_between_matching_rows():
    patch = "@@ -1 +3 @@\n-old\n+self.selected_row = 1\n context\n+selected_row += 1"
    changes = [change("a.py", call_id="1", patch=patch), change("b.py", call_id="2")]
    with create_pipe_input() as pipe:
        ui = browser(changes, pipe)
        ui.search("diffs")
        assert ui.scope == "diffs" and ui.app.layout.has_focus(ui.query)
        ui.query.text = "sel_row"
        assert [c.path for c in ui.visible] == ["a.py"]
        rows = ui.diff_rows()
        assert len(rows) == 2 and ui.diff.document.cursor_position_row == rows[0]
        ui.jump(1)
        assert ui.diff.document.cursor_position_row == rows[1]
        ui.jump(1)  # Wraps around.
        assert ui.diff.document.cursor_position_row == rows[0]
        ui.jump(-1)
        assert ui.diff.document.cursor_position_row == rows[1]
        # Switching scope drops the query typed for the other pane.
        ui.search("paths")
        assert ui.query.text == "" and len(ui.visible) == 2


def test_diff_search_matches_the_redacted_text():
    with create_pipe_input() as pipe:
        ui = browser([change("app.py", patch='+token = "sk-0123456789ab"')], pipe)
        ui.search("diffs")
        ui.query.text = "sk-0123456789ab"
        assert ui.visible == []
        ui.query.text = "redacted"
        assert len(ui.visible) == 1 and ui.diff_rows()


def test_notes_sit_under_their_line_and_become_a_prompt():
    with create_pipe_input() as pipe:
        ui = browser([change("a.py", patch=PATCH)], pipe)
        lines = ui.diff.text.splitlines()
        ui.go_to(lines.index("+new"))
        ui.start_note()
        assert ui.app.layout.has_focus(ui.note) and "a.py:2" in ui.note_prompt()
        ui.save_note("  rename   this ")
        lines = ui.diff.text.splitlines()
        assert lines[lines.index("+new") + 1] == "  ✎ rename this"
        assert "1 note" in ui.heading()
        ui.go_to(0)  # a note on the file itself quotes nothing
        ui.start_note()
        ui.save_note("split this file")
        assert notes_prompt(ui.notes) == (
            "Review notes on the diff:\n\n"
            "a.py:2\n```diff\n x\n-old\n+new\n y\n```\nrename this\n\n"
            "a.py\nsplit this file"
        )
        # The note's own row edits it, and an empty note deletes it.
        ui.go_to(ui.diff.text.splitlines().index("  ✎ rename this"))
        ui.start_note()
        assert ui.note.text == "rename this"
        ui.save_note("")
        assert [n.text for n in ui.notes] == ["split this file"]
        assert "✎ rename this" not in ui.diff.text
        ui.go_to(ui.page.starts[0] - 1 if ui.page.starts[0] else 0)
    assert notes_prompt([]) == ""


def test_notes_on_hunks_another_view_lacks_stay_with_their_file():
    note = Note(anchor=None, line=11, quote=(), text="why?")
    page = lay_out([change("a.py", patch=PATCH)], {}, [])
    note.anchor = page.rows[page.text.splitlines().index("+q")].anchor
    other = lay_out([change("a.py", patch="@@ -1 +1 @@\n-a\n+b")], {}, [note])
    assert other.text.splitlines()[1] == "  ✎ why?"
    again = lay_out([change("a.py", patch=PATCH)], {}, [note])
    lines = again.text.splitlines()
    assert lines[lines.index("+q") + 1] == "  ✎ why?"  # back under its line


def test_marking_reviewed_records_the_shown_tree():
    marked = []
    with create_pipe_input() as pipe:
        ui = browser([change("a.py")], pipe, tree="a" * 40, since_review=[change("a.py")])
        ui.mark_reviewed()
        assert "Nothing to mark" in ui.notice  # no way to record it
        ui = browser(
            [change("a.py")], pipe, tree="a" * 40, since_review=[change("a.py")], mark=marked.append
        )
        ui.mark_reviewed()
        assert [c.tree for c in marked] == ["a" * 40] and ui.review.since_review == []
        assert (
            ui.notice == "Marked reviewed" and "Nothing new since your last review" in ui.diff.text
        )

        def broken(tree):
            raise OSError("read-only")

        ui = browser([change("a.py")], pipe, tree="a" * 40, mark=broken)
        ui.mark_reviewed()
        assert ui.notice == "Could not mark reviewed: read-only"


def test_refresh_reloads_keeping_the_view_the_line_and_the_notes():
    later = change("a.py", patch=PATCH.replace("+new", "+newer"))

    async def run():
        with create_pipe_input() as pipe:
            ui = browser(
                [change("a.py", patch=PATCH)],
                pipe,
                uncommitted=[change("a.py", patch=PATCH)],
                reload=lambda: review([later], uncommitted=[later]),
            )
            ui.show("uncommitted")
            ui.go_to(ui.diff.text.splitlines().index("+q"))
            ui.start_note()
            ui.save_note("keep me")
            await ui.refetch()
            assert ui.view == "uncommitted" and "+newer" in ui.diff.text
            assert ui.current_row().line == 11 and ui.notice == "Refreshed"
            assert "✎ keep me" in ui.diff.text

            def failing():
                raise RuntimeError("boom")

            ui.reload = failing
            await ui.refetch()
            assert ui.notice == "Refresh failed: boom" and "+newer" in ui.diff.text

    asyncio.run(run())


def test_editor_command_puts_the_cursor_on_the_line():
    path = Path("/w/a.py")
    assert editor_command(path, 4, {"EDITOR": "nvim"}) == ["nvim", "+4", "/w/a.py"]
    assert editor_command(path, 4, {"VISUAL": "code -w", "EDITOR": "vi"}) == [
        "code",
        "-w",
        "--goto",
        "/w/a.py:4",
    ]
    assert editor_command(path, 4, {"EDITOR": "/usr/bin/hx"}) == ["/usr/bin/hx", "/w/a.py:4"]
    assert editor_command(path, None, {}) == ["vi", "/w/a.py"]


def test_paging_keys_scroll_the_focused_pane_and_escape_closes():
    async def run():
        long = "\n".join(f"+line {i:03}" for i in range(200))
        with create_pipe_input() as pipe:
            ui = browser([change("long.py", patch=long)], pipe)
            task = asyncio.create_task(ui.run())
            await asyncio.sleep(0.05)
            # It opens in the search line, where PageDown pages the file list, not the diff.
            pipe.send_text("\x1b[6~")
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.query)
            assert ui.diff.document.cursor_position_row == 0
            pipe.send_text("\t")
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.diff)
            pipe.send_text("\x1b[6~")
            await asyncio.sleep(0.05)
            scrolled = ui.diff.document.cursor_position_row
            assert scrolled > 0
            pipe.send_text("\x15")  # Ctrl+U
            await asyncio.sleep(0.05)
            assert ui.diff.document.cursor_position_row < scrolled
            pipe.send_text("\x04")  # Ctrl+D scrolls; it no longer closes the browser.
            await asyncio.sleep(0.05)
            assert not task.done()
            pipe.send_text("\x1b")
            await asyncio.wait_for(task, 2)

    asyncio.run(run())


def test_note_keys_escape_cancels_the_note_not_the_popup():
    async def run():
        with create_pipe_input() as pipe:
            ui = browser([change("a.py", patch=PATCH)], pipe)
            task = asyncio.create_task(ui.run())
            await asyncio.sleep(0.05)
            pipe.send_text("\t\x1b[B\x1b[B\x1b[B\x0e")  # to the diff, down to -old, Ctrl+N
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.note)
            pipe.send_text("draft\x1b")
            # A lone Escape is only taken as one after the input's flush timeout.
            for _ in range(100):
                await asyncio.sleep(0.02)
                if not ui.app.layout.has_focus(ui.note):
                    break
            assert not task.done() and ui.notes == [] and ui.app.layout.has_focus(ui.diff)
            pipe.send_text("\x0ewhy?\r")
            await asyncio.sleep(0.05)
            assert [n.text for n in ui.notes] == ["why?"] and ui.notes[0].line == 2
            pipe.send_text("\x1b")
            await asyncio.wait_for(task, 2)

    asyncio.run(run())


def test_search_shortcut_targets_the_focused_pane_and_enter_returns_to_it():
    async def run():
        with create_pipe_input() as pipe:
            ui = browser([change("x.py")], pipe)
            task = asyncio.create_task(ui.run())
            await asyncio.sleep(0.05)
            # It opens searching paths, so typing filters.
            assert ui.scope == "paths" and ui.app.layout.has_focus(ui.query)
            pipe.send_text("nx")
            await asyncio.sleep(0.05)
            assert ui.query.text == "nx" and ui.visible == []
            pipe.send_text("\x7f\x7f\r")
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.files)
            pipe.send_text("\x06")  # Ctrl+F
            await asyncio.sleep(0.05)
            assert ui.scope == "paths" and ui.app.layout.has_focus(ui.query)
            pipe.send_text("\r")
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.files)
            pipe.send_text("\t\x06")
            await asyncio.sleep(0.05)
            assert ui.scope == "diffs" and ui.app.layout.has_focus(ui.query)
            pipe.send_text("new\r")
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.diff) and ui.query.text == "new"
            pipe.send_text("\x1b")
            await asyncio.wait_for(task, 2)

    asyncio.run(run())


def test_file_list_keeps_its_rows_when_the_diff_is_long():
    """A long diff must not squeeze the Files pane down to its minimum height."""

    async def run():
        long = "\n".join(f"+line {i:03}" for i in range(500))
        changes = [change(f"file_{i}.py", call_id=str(i), patch=long) for i in range(10)]
        with create_pipe_input() as pipe:
            ui = browser(
                changes,
                pipe,
                output=Vt100_Output(
                    StringIO(), lambda: Size(rows=24, columns=80), enable_cpr=False
                ),
            )
            with set_app(ui.app):
                ui.app.renderer.render(ui.app, ui.app.layout)
                assert ui.files.window.render_info.window_height == 6
                assert ui.diff.window.render_info.window_height > 6

    asyncio.run(run())


def test_slash_command_dispatches_and_collects_changes():
    app = PreviewApp(console=Console(file=StringIO()))
    assert not app.diffs_requested
    app.handle("/diffs")
    assert app.diffs_requested
    edit = change("kept.py")
    app.present_events((edit,))
    assert app.recorded_edits() == [edit]
    app.controller.new("")
    assert app.recorded_edits() == []


def test_only_the_newest_reload_lands_and_a_stale_rerender_is_dropped():
    first, second = change("a.py", patch=PATCH), change("b.py", patch=PATCH)

    async def run():
        with create_pipe_input() as pipe:
            gate = asyncio.Event()
            loop = asyncio.get_running_loop()

            def slow():
                asyncio.run_coroutine_threadsafe(gate.wait(), loop).result()
                return review([first])

            ui = browser([change("0.py")], pipe, reload=slow)
            older = asyncio.create_task(ui.refetch())
            await asyncio.sleep(0.05)
            ui.reload = lambda: review([second])
            await ui.refetch()
            gate.set()
            await older
            assert [c.path for c in ui.visible] == ["b.py"] and ui.notice == "Refreshed"

            delta = fake_delta()
            ui = browser([first], pipe, delta=delta, output=SizedOutput())
            stale = asyncio.create_task(ui.rerender(80))
            await asyncio.sleep(0)  # it has taken the review it renders
            ui.review = review([second])
            await stale
            assert ui.width == 0 and ui.rendered == {}  # laid out again on the next render

    asyncio.run(run())


def test_quotes_holding_backticks_get_a_longer_fence():
    note = Note(anchor=None, line=1, quote=("+x = '```'",), text="hm")
    note.anchor = lay_out([change("a.py")], {}, []).rows[0].anchor
    assert notes_prompt([note]).splitlines()[3:6] == ["````diff", "+x = '```'", "````"]


def test_the_review_dedents_hunks_like_the_scrollback_unless_turned_off():
    nested = "@@ -1,2 +1,2 @@\n         keep = 1\n-        old = 2\n+        new = 2"
    deep = change("a.py", patch=nested)
    with create_pipe_input() as pipe:
        ui = browser([deep], pipe)
        assert "-old = 2" in ui.page.text and "         keep" not in ui.page.text
        ui = DiffBrowser(review([deep]), dedent=False, input=pipe, output=DummyOutput())
        assert "-        old = 2" in ui.page.text
    # delta is asked for, and looked up by, the dedented hunk.
    delta = fake_delta()
    rendered = render_review(review([deep]), delta, 80)
    assert all("\n-old = 2" in patch for patch in rendered)
    # A note still quotes the code as it is in the file.
    note_rows = [row for row in lay_out([deep], {}, []).rows if row.anchor and row.anchor.index]
    assert any("        old" in line for line in quote_for(deep, note_rows[0].anchor))
