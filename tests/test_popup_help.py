"""Context help follows the active pane instead of occupying permanent hint rows."""

import asyncio

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pcode.aside import Aside, Asides
from pcode.aside_ui import AsideBrowser, thread_tree
from pcode.links_ui import LinkPicker
from pcode.tree_ui import CopyBrowser


def test_aside_help_follows_reader_editor_and_nested_picker_focus():
    asides = Asides()
    for question in ("First?", "Second?"):
        aside = Aside(question=question, answer="Answer")
        aside.settle("answered")
        asides.items.append(aside)
    with create_pipe_input() as pipe:
        browser = AsideBrowser(asides, ask=lambda *_: None, input=pipe, output=DummyOutput())
        with set_app(browser.app):
            assert ("↑/↓", "Select question") in browser.help()
            browser.input.open(browser.app)
            assert (
                "Esc",
                "Close completions, otherwise return to questions",
            ) in browser.help()
            browser.app.layout.focus(browser.detail)
            assert ("↑/↓", "Scroll answer") in browser.help()
            assert ("Enter", "Read selected thread") in browser.help()
            browser.read(browser.app)
            assert ("Esc", "Questions") in browser.help()
            browser.input.open(browser.app)
            assert ("Enter", "Send") in browser.help()
            assert ("Ctrl+J", "Newline") in browser.help()
            assert not any(key == "Ctrl+U/D" for key, _ in browser.help())
            browser.input.prompt(
                browser.app, title="Summarize", placeholder="", submit=lambda _: None
            )
            assert ("Enter", "Summarize (empty: as is)") in browser.help()
            browser.picker = LinkPicker([], lambda _: None, lambda: None)
            assert browser.help() == browser.picker.help()
            browser.picker = None
            browser.copying = CopyBrowser(
                thread_tree(browser.current()), finish=lambda _: None, shortcuts=browser.prefix_keys
            )
            assert ("Enter", "Copy highlighted text") in browser.help()
            assert ("Esc/Ctrl+C", "Cancel") in browser.help()


def test_aside_help_describes_interrupt_only_while_answers_are_running():
    asides = Asides()
    with create_pipe_input() as pipe:
        browser = AsideBrowser(asides, input=pipe, output=DummyOutput())
        with set_app(browser.app):
            assert ("Ctrl+C", "Close") in browser.help()
            asides.items.append(Aside(question="Working?"))
            assert ("Ctrl+C", "Stop running answers") in browser.help()


def test_session_help_lists_delete_binding(tmp_path):
    from pcode.session_ui import SessionBrowser

    with create_pipe_input() as pipe:
        browser = SessionBrowser(
            [], root=tmp_path, workspace=tmp_path, input=pipe, output=DummyOutput()
        )
        with set_app(browser.app):
            browser.prefix_keys.browsing = True
            assert (
                "Delete",
                "Delete selected session (twice; list focused)",
            ) in browser.prefix_keys.hint_rows()
            assert browser.prefix_keys.summary().endswith("Keys")
            assert "Delete" not in browser.prefix_keys.summary()


@pytest.mark.parametrize("picker", [None, "links", "copy"])
def test_help_is_modal_in_reader_and_nested_pickers(picker):
    async def run():
        asides = Asides()
        aside = Aside(question="Question?", answer="Answer\n\n> quoted")
        aside.settle("answered")
        asides.items.append(aside)
        with create_pipe_input() as pipe:
            browser = AsideBrowser(asides, input=pipe, output=DummyOutput(), key_prefix="ctrl+b")
            if picker == "links":
                browser.picker = LinkPicker(
                    [], lambda _: None, lambda: None, shortcuts=browser.prefix_keys
                )
                browser.app.layout.focus(browser.picker.query)
            elif picker == "copy":
                browser.copy()
                assert browser.app.layout.has_focus(browser.copying.list)
            focused = browser.app.layout.current_control
            rendered = asyncio.Event()
            browser.app.after_render += lambda _: rendered.set()
            task = asyncio.create_task(browser.app.run_async())

            async def send(text):
                rendered.clear()
                pipe.send_text(text)
                await asyncio.wait_for(rendered.wait(), 2)

            try:
                await asyncio.wait_for(rendered.wait(), 2)
                await send("\x1f")  # Ctrl+/ opens contextual help even inside a modal picker.
                assert browser.prefix_keys.browsing
                with set_app(browser.app):
                    assert all(row in browser.prefix_keys.hint_rows() for row in browser.help())
                await send("\r")
                assert not task.done()
                assert browser.app.layout.current_control is focused
                await send("\x03")  # Dismiss help without closing its host.
                assert not browser.prefix_keys.browsing
                assert not task.done()
                assert browser.app.layout.current_control is focused
            finally:
                if not task.done():
                    browser.app.exit()
                await asyncio.wait_for(task, 2)

    asyncio.run(run())
