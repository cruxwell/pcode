import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput
from rich.text import Text

from pcode.popup_ui import RichPane, fuzzy_match


@pytest.mark.parametrize(
    ("term", "text", "expected"),
    [
        ("edit_ui", "src/pcode/edit_ui.py", True),  # Plain substring.
        ("ed_ui", "src/pcode/edit_ui.py", True),  # Separators split the query into prefixes.
        ("pc/edui", "src/pcode/edit_ui.py", True),
        ("edui", "src/pcode/edit_ui.py", True),
        ("dtui", "src/pcode/edit_ui.py", False),  # Gaps inside a word do not count.
        ("sel_row", "+self.selected_row = 1", True),
        ("_", "anything", False),
    ],
)
def test_fuzzy_match_uses_substrings_or_joined_word_prefixes(term, text, expected):
    assert fuzzy_match(term, text) is expected


@pytest.mark.parametrize("prefix", ["ctrl", "ctrl+x"])
def test_standalone_copy_help_gates_picker_keys(monkeypatch, prefix):
    from pcode.copy_ui import Snippet, snippet_dialog
    from pcode.prefix_keys import PrefixKeys

    shortcuts = PrefixKeys(prefix)
    monkeypatch.setattr("pcode.copy_ui.PrefixKeys", lambda: shortcuts)
    choices = [Snippet("response", "response"), Snippet("quote", "quote")]

    async def wait_for(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    async def run():
        with create_pipe_input() as pipe:
            app = snippet_dialog(choices, input=pipe, output=DummyOutput())
            task = asyncio.create_task(app.run_async())
            try:
                await wait_for(lambda: app.is_running)
                if prefix != "ctrl":
                    pipe.send_text("\x18")
                    await wait_for(lambda: shortcuts.pending)
                    assert ("Ctrl+/", "all keys") in shortcuts.hint_footer()
                pipe.send_text("\x1f")
                await wait_for(lambda: shortcuts.browsing)
                assert ("Enter", "Copy selected snippet") in shortcuts.hint_rows()
                pipe.send_text("\x1b[A\r\x1f")
                await wait_for(lambda: not shortcuts.visible)
                assert not task.done()
                pipe.send_text("\r")
                assert await asyncio.wait_for(task, 3) == choices[1]
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_nested_copy_picker_keeps_host_help_provider():
    from pcode.copy_ui import Snippet, SnippetPicker
    from pcode.prefix_keys import PrefixKeys

    shortcuts = PrefixKeys("ctrl")

    def provider():
        return [("Enter", "Host context")]

    shortcuts.set_help(provider)
    picker = SnippetPicker(
        [Snippet("response", "text")], lambda _: None, lambda: None, shortcuts=shortcuts
    )
    assert shortcuts.help_provider is provider
    assert ("Enter", "Copy selected snippet") in picker.help()
    assert len(picker.container.children) == 1


def test_rich_pane_scroll_reuses_prepared_lines():
    pane = RichPane()
    pane.set([Text("\n".join(f"Line {i}" for i in range(1000)))])
    first = pane.control.create_content(80, 20)
    with patch("pcode.popup_ui.split_lines", side_effect=AssertionError("Resplit content")):
        for row in range(10):
            pane.window.vertical_scroll = row
            pane.control.preferred_width(80)
            assert pane.control.preferred_height(80, 20, False, None) == 1000
            content = pane.control.create_content(80, 20)
            assert content.get_line(500) is first.get_line(500)
            assert content.cursor_position.y == row
            # Wheel events should go straight to Window, not scan the text.
            assert (
                pane.control.mouse_handler(
                    MouseEvent(
                        Point(0, row), MouseEventType.SCROLL_DOWN, MouseButton.NONE, frozenset()
                    )
                )
                is NotImplemented
            )


def test_rich_pane_invalidates_lines_on_resize_and_content_change():
    pane = RichPane()
    pane.set([Text("abcdefghij" * 4)])
    wide = pane.control.create_content(40, 10)
    narrow = pane.control.create_content(10, 10)
    assert wide.line_count == 1
    assert narrow.line_count == 4
    pane.window.vertical_scroll = 3
    pane.set([Text("replacement")])
    updated = pane.control.create_content(40, 10)
    assert pane.window.vertical_scroll == 0
    assert updated.line_count == 1
    assert "replacement" == "".join(text for _, text in updated.get_line(0))


def test_rich_pane_keeps_a_stale_scroll_on_a_real_line():
    """A scroll past the content must not reach the window as the cursor row.

    Window reads ``get_line(cursor_position.y)`` on every render, so a row the
    content no longer has raised IndexError mid-render.
    """

    def draw(width):
        pane.window.write_to_screen(
            Screen(), MouseHandlers(), WritePosition(0, 0, width, 10), "", False, None
        )
        return pane.window.render_info

    pane = RichPane()
    pane.set([Text("word " * 400)])
    # Scrolled near the bottom, then widened: the rewrap has far fewer lines.
    pane.window.vertical_scroll = draw(30).content_height - 3
    info = draw(120)
    assert info.ui_content.cursor_position.y < info.content_height
    # Settles on the last full page, not a lone last line.
    assert pane.window.vertical_scroll == max(0, info.content_height - info.window_height)

    # A reader scrolled mid-way (not tailing) keeps the offset across follow(),
    # which is how the stale row arises: 20 is past the one-line content.
    pane.set([Text("line\n" * 50)])
    draw(40)
    pane.window.vertical_scroll = 20
    pane.follow([Text("short")])
    assert pane.window.vertical_scroll == 20
    assert draw(40).ui_content.cursor_position.y == 0


@pytest.mark.parametrize("kind", ["sessions", "tools"])
def test_popup_content_half_pages_and_full_pages(tmp_path, kind):
    from pcode.inspection import ToolArchive
    from pcode.inspector_ui import ToolInspector
    from pcode.session_ui import SessionBrowser

    async def run():
        with create_pipe_input() as pipe:
            options = {"input": pipe, "output": DummyOutput()}
            if kind == "sessions":
                popup = SessionBrowser([], root=tmp_path, workspace=tmp_path, **options)
            else:
                popup = ToolInspector(ToolArchive(), **options)
            popup.list.buffer.set_document(
                Document("\n".join(f"Row {i}" for i in range(300)), 0), bypass_readonly=True
            )
            popup.detail.set([Text("\n".join(f"Line {i}" for i in range(300)))])
            popup.app.layout.focus(popup.detail.window)
            task = asyncio.create_task(popup.app.run_async())
            try:
                await asyncio.sleep(0.05)
                window = popup.detail.window
                height = window.render_info.window_height

                async def press(keys):
                    pipe.send_text(keys)
                    await asyncio.sleep(0.05)
                    assert not task.done(), "Scrolling must not close the popup"

                await press("\x04")  # Ctrl+D, half page down.
                assert window.vertical_scroll == max(1, height // 2)
                await press("\x15")  # Ctrl+U, half page up.
                assert window.vertical_scroll == 0
                await press("\x1b[6~")  # Page Down.
                assert window.vertical_scroll == height - 1
                await press("\x1b[6~")
                await press("\x1b[5~")  # Page Up goes back one page, not to the top.
                assert window.vertical_scroll == height - 1
                await press("\x1b[B")
                assert window.vertical_scroll == height
                await press("\x1b[A")
                assert window.vertical_scroll == height - 1
                await press("\x04" * 50)
                assert window.vertical_scroll == 300 - height
                await press("\x15" * 50)
                assert window.vertical_scroll == 0
                # The same keys half-page the list, whether it or the query has focus.
                for focus in (popup.list, popup.query):
                    popup.app.layout.focus(focus)
                    await asyncio.sleep(0.05)
                    rows = popup.list.window.render_info.window_height
                    half = max(1, rows // 2)
                    start = popup.list.document.cursor_position_row
                    await press("\x04")
                    assert popup.list.document.cursor_position_row == start + half
                    await press("\x15")
                    assert popup.list.document.cursor_position_row == start
                assert popup.query.text == "", "Paging keys must not type into the query"
                pipe.send_text("\x1b")
                await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    popup.app.exit()
                    await task

    asyncio.run(run())


def _popups(tmp_path, options):
    """Every alternate-screen popup, with each keyboard-scrollable TextArea it shows."""
    from pcode.aside import Aside, Asides
    from pcode.aside_ui import AsideBrowser
    from pcode.conversation_tree import ConversationTree
    from pcode.edit_ui import DiffBrowser
    from pcode.git_diff import Review
    from pcode.inspection import ToolArchive
    from pcode.inspector_ui import ToolInspector
    from pcode.links_ui import links_dialog
    from pcode.runtime import EditCompleted
    from pcode.session_ui import SessionBrowser, session_info_dialog
    from pcode.tree_ui import TreeBrowser

    links = links_dialog([], **options)
    info = session_info_dialog([("Model", "test:local")], **options)
    edits = DiffBrowser(
        Review("t", [EditCompleted("1", "a.py", "edited", "+x", added=1)], "none"), **options
    )
    tree = TreeBrowser(ConversationTree(), **options)
    # Two threads, since a lone one reads full width with no list to page.
    records = Asides()
    records.items.extend([Aside(question="one?"), Aside(question="two?")])
    asides = AsideBrowser(records, **options)
    sessions = SessionBrowser([], root=tmp_path, workspace=tmp_path, **options)
    tools = ToolInspector(ToolArchive(), **options)
    # The links picker opens in its search line; the read-only window is the list.
    links_list = next(
        window
        for window in links.layout.find_all_windows()
        if isinstance(window.content, BufferControl) and window.content.buffer.read_only()
    )
    return {
        "links": (links, [links_list]),
        "session info": (info, [info.layout.current_window]),
        "edits": (edits.app, [edits.files.window, edits.diff.window]),
        "tree": (tree.app, [tree.list.window]),
        "asides": (asides.app, [asides.list.window]),
        "sessions": (sessions.app, [sessions.list.window]),
        "tools": (tools.app, [tools.list.window]),
    }


POPUPS = ["links", "session info", "edits", "tree", "asides", "sessions", "tools"]


@pytest.mark.parametrize("name", [name for name in POPUPS if name != "session info"])
def test_every_popup_hands_the_mouse_to_the_terminal_and_back(tmp_path, name):
    """Ctrl+Q flips mouse capture while the popup stays open, for a native text selection.

    The /btw viewer's Ctrl+Q goes back to the conversation, so it flips on Ctrl+G.
    """
    toggle = "\x07" if name == "asides" else "\x11"

    async def run():
        with create_pipe_input() as pipe:
            app, _ = _popups(tmp_path, {"input": pipe, "output": DummyOutput()})[name]
            task = asyncio.create_task(app.run_async())
            try:
                await asyncio.sleep(0.05)
                assert app.renderer.mouse_support()
                for captured in (False, True):
                    pipe.send_text(toggle)
                    await asyncio.sleep(0.05)
                    assert app.renderer.mouse_support() is captured
                    assert not task.done()
            finally:
                if not task.done():
                    app.exit()
                    await task

    asyncio.run(run())


def test_popup_mouse_toggle_starts_from_the_setting(monkeypatch):
    from prompt_toolkit.application import Application

    from pcode import popup_ui
    from pcode.prefix_keys import PrefixKeys

    monkeypatch.setattr(popup_ui, "load_preferences", lambda: {"popup_mouse": "off"})
    shortcuts = PrefixKeys("ctrl+p")
    captured = popup_ui.popup_mouse(shortcuts)
    assert not captured()
    # Action labels stay in contextual help, not the summary footer.
    assert "Keys" in shortcuts.summary()
    assert ("Ctrl+P q", "Capture mouse") in shortcuts.hint_rows()
    toggle = next(s for s in shortcuts.shortcuts if s.key == popup_ui.MOUSE_TOGGLE_KEY)
    toggle.handler(SimpleNamespace(app=Application()))
    assert captured()
    assert "Keys" in shortcuts.summary()
    assert ("Ctrl+P q", "Release mouse") in shortcuts.hint_rows()
    # Without shortcuts there is nothing to toggle, only the setting.
    assert not popup_ui.popup_mouse()()


@pytest.mark.parametrize("name", POPUPS)
def test_every_popup_pane_pages_with_the_same_keys(tmp_path, name):
    """↑↓, PgUp/PgDn and Ctrl+U/D move every TextArea pane and never close the popup."""

    async def run():
        with create_pipe_input() as pipe:
            app, windows = _popups(tmp_path, {"input": pipe, "output": DummyOutput()})[name]
            task = asyncio.create_task(app.run_async())
            try:
                await asyncio.sleep(0.05)

                async def press(keys):
                    pipe.send_text(keys)
                    await asyncio.sleep(0.05)
                    assert not task.done(), f"{keys!r} must not close the popup"

                for window in windows:
                    buffer = window.content.buffer
                    text = "\n".join(f"Row {i}" for i in range(300))
                    buffer.set_document(Document(text, 0), bypass_readonly=True)
                    app.layout.focus(window)
                    await asyncio.sleep(0.05)

                    def row():
                        return buffer.document.cursor_position_row

                    await press("\x1b[B")  # Down.
                    assert row() == 1
                    await press("\x1b[A")  # Up.
                    assert row() == 0
                    await press("\x04")  # Ctrl+D, half a page.
                    half = row()
                    assert half > 1
                    await press("\x1b[6~")  # PageDown, a whole page.
                    paged = row()
                    assert paged > half * 2 - 2
                    await press("\x1b[5~")  # PageUp.
                    assert row() < paged
                    before = row()
                    await press("\x15")  # Ctrl+U, half a page back.
                    assert row() == max(0, before - half)
                pipe.send_text("\x1b")
                await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    app.exit()
                    await task

    asyncio.run(run())


@pytest.mark.parametrize("name", POPUPS)
def test_popup_mouse_capture_is_default(tmp_path, name):
    """On by default so the wheel scrolls; `popup_mouse off` restores native selection."""
    from pcode.preferences import update_preferences

    with create_pipe_input() as pipe:
        options = {"input": pipe, "output": DummyOutput()}
        app, _ = _popups(tmp_path, options)[name]
        assert app.mouse_support()
        update_preferences({"popup_mouse": "off"})
        app, _ = _popups(tmp_path, options)[name]
        assert not app.mouse_support()


def test_model_picker_pages_its_selection():
    from pcode.model_ui import ModelPicker

    models = [f"test:model-{i:02}" for i in range(40)]

    async def run():
        with create_pipe_input() as pipe:
            picker = ModelPicker(models, {"test"}, input=pipe, output=DummyOutput())
            task = asyncio.create_task(picker.run())
            try:
                await asyncio.sleep(0.05)

                async def press(keys):
                    pipe.send_text(keys)
                    await asyncio.sleep(0.05)
                    assert not task.done(), f"{keys!r} must not close the picker"

                await press("\x04")  # Ctrl+D.
                half = picker.selected
                assert half > 1
                await press("\x1b[6~")
                assert picker.selected > half * 2 - 2
                await press("\x1b[5~")
                assert picker.selected == half
                await press("\x15")
                assert picker.selected == 0
                await press("\x1b[6~" * 20)
                assert picker.selected == len(models) - 1
                pipe.send_text("\r")
                assert await asyncio.wait_for(task, 2) == models[-1]
            finally:
                if not task.done():
                    picker.app.exit()
                    await task

    asyncio.run(run())


def test_anchor_on_an_empty_last_renderable_stays_on_a_real_line():
    pane = RichPane(color_system=None)
    pane.set([Text("one"), Text("two"), Text("")], anchor=2)
    content = pane.control.create_content(40, 10)
    # Trailing newlines are stripped, so the empty block starts past the end.
    assert content.line_count == 2
    assert content.cursor_position.y == 1
    content.get_line(content.cursor_position.y)


@pytest.mark.parametrize("pane", ["files", "diff"])
def test_diff_browser_panes_copy_a_drag_but_never_a_click(tmp_path, pane):
    """Drags copy even while the search line has focus; clicks never copy."""
    from pcode.edit_ui import DiffBrowser
    from pcode.git_diff import Review
    from pcode.runtime import EditCompleted

    def event(kind, column, row=0, button=MouseButton.LEFT):
        return MouseEvent(Point(column, row), kind, button, frozenset())

    def click(column, row=0):
        handle(event(MouseEventType.MOUSE_DOWN, column, row))
        handle(event(MouseEventType.MOUSE_UP, column, row, MouseButton.NONE))

    async def run():
        nonlocal handle
        with create_pipe_input() as pipe:
            browser = DiffBrowser(
                Review("t", [EditCompleted("1", "a.py", "edited", "+x", added=1)], "none"),
                input=pipe,
                output=DummyOutput(),
            )
            area = getattr(browser, pane)
            task = asyncio.create_task(browser.app.run_async())
            try:
                await asyncio.sleep(0.05)
                assert not browser.app.layout.has_focus(area)
                handle = area.control.mouse_handler
                first = area.text.splitlines()[0]
                with patch(
                    "pcode.text_selection.copy_to_clipboard", return_value=(True, False)
                ) as copy:
                    handle(event(MouseEventType.MOUSE_DOWN, 0))
                    handle(event(MouseEventType.MOUSE_MOVE, 2))
                    handle(event(MouseEventType.MOUSE_UP, 2, button=MouseButton.NONE))
                    assert copy.call_args.args[0] == first[:3]
                    assert browser.notice == "Copied 3 characters"
                    word = first.index(first.split()[0]) + 1
                    await asyncio.sleep(0.5)  # Past the double-click window.
                    click(word)
                    click(word)
                    assert copy.call_args.args[0] == first.split()[0]
                    await asyncio.sleep(0.5)
                    browser.app.layout.focus(area)
                    # A list rebuilt on a timer moves the cursor under a click.
                    handle(event(MouseEventType.MOUSE_DOWN, word))
                    area.buffer.cursor_position = 0
                    handle(event(MouseEventType.MOUSE_UP, word, button=MouseButton.NONE))
                    # Quick clicks on two different spots are not a double-click.
                    click(word + 1)
                    click(word + 2)
                    # A drag from another pane ending here selects nothing in the buffer.
                    handle(event(MouseEventType.MOUSE_MOVE, 3))
                    handle(event(MouseEventType.MOUSE_UP, 3, button=MouseButton.NONE))
                    assert area.buffer.selection_state is None
                    # The wheel still scrolls mid-drag: the window handles it.
                    handle(event(MouseEventType.MOUSE_DOWN, 0))
                    wheel = event(MouseEventType.SCROLL_DOWN, 0, button=MouseButton.NONE)
                    assert handle(wheel) is NotImplemented
                    handle(event(MouseEventType.MOUSE_UP, 0, button=MouseButton.NONE))
                assert copy.call_count == 2
            finally:
                browser.app.exit()
                await task

    handle = None
    asyncio.run(run())


def test_a_selection_inside_the_cursor_row_still_shows():
    """Selection and the cursor row share one reverse highlight; the overlap underlines."""
    from prompt_toolkit.styles import Style

    from pcode.popup_ui import popup_style

    style = popup_style(Style.from_dict({"selected": "reverse", "cursor-line": "reverse"}))
    attrs = style.get_attrs_for_style_str("class:popup class:cursor-line class:selected")
    assert attrs.underline and attrs.reverse
