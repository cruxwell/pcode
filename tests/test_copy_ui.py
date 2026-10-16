import asyncio
from dataclasses import FrozenInstanceError

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import Label, TextArea

from pcode.conversation_tree import ConversationTree, TurnNode
from pcode.copy_ui import (
    Answer,
    AnswerPicker,
    CopyPicker,
    Snippet,
    SnippetPicker,
    answers,
    last_response,
    snippets,
)
from pcode.frame import Dialog
from pcode.popup_ui import popup_container, popup_mouse, popup_style
from pcode.prefix_keys import PrefixKeys


def copy_picker_dialog(choices: list[Answer], *, input=None, output=None, style=None):
    """`CopyPicker` as an application of its own, whose result is the picked Snippet."""
    app: Application
    shortcuts = PrefixKeys()
    picker = CopyPicker(
        choices,
        on_pick=lambda snippet: app.exit(result=snippet),
        on_cancel=lambda: app.exit(result=None),
        shortcuts=shortcuts,
    )
    shortcuts.set_help(picker.help)
    dialog = Dialog(
        title=lambda: picker.title,
        body=HSplit([picker.container, Label(shortcuts.summary)], padding=1),
    )
    app = Application(
        layout=Layout(
            popup_container(dialog, shortcuts), focused_element=picker.query or picker.list
        ),
        key_bindings=shortcuts.key_bindings(KeyBindings()),
        full_screen=True,
        mouse_support=popup_mouse(shortcuts),
        input=input,
        output=output,
        style=popup_style(style),
    )
    return app


def test_answers_follow_active_branch_newest_first():
    tree = ConversationTree()
    for node in [
        TurnNode("root", None, "first question", response="first answer"),
        TurnNode("old", "root", "abandoned question", response="abandoned answer"),
        TurnNode("new", "root", "current question", response="current answer"),
        TurnNode("compact", "new", "focus", response="summary", kind="compaction"),
        TurnNode("empty", "compact", "unfinished", response=" \n\t"),
    ]:
        tree.nodes[node.id] = node
    tree.active = "empty"
    assert answers(tree) == [
        Answer("current question", "current answer"),
        Answer("first question", "first answer"),
    ]
    assert last_response(tree) == "current answer"
    tree.active = "old"
    assert answers(tree)[0] == Answer("abandoned question", "abandoned answer")
    tree.active = None
    assert answers(tree) == []


def test_answers_are_cleaned_before_search_display_and_pick():
    secret = "synthetic-answer-text"
    tree = ConversationTree()
    tree.nodes["one"] = TurnNode(
        "one", None, f"\nquestion {secret}\n", response=f"\nanswer {secret}\x00\n"
    )
    tree.active = "one"
    answer = answers(tree)[0]
    assert answer == Answer(f"question {secret}", f"answer {secret}")
    assert last_response(tree) == tree.nodes["one"].response  # Legacy API stays raw.
    with pytest.raises(FrozenInstanceError):
        answer.text = "changed"
    picker = AnswerPicker([answer], lambda _: None, lambda: None)
    assert secret in picker.list.text
    assert picker.selected().text == f"answer {secret}"
    picker.query.text = "no such answer"
    assert picker.selected() is None
    # Directly supplied aside choices are cleaned the same way.
    assert Answer("a\x00b", "c") == Answer("ab", "c")


def test_answers_skip_text_that_is_blank_after_cleaning():
    tree = ConversationTree()
    tree.nodes["one"] = TurnNode("one", None, "question", response="\x00\x1b")
    tree.active = "one"
    assert answers(tree) == []


def choices():
    return [
        Answer("Newest QUESTION", "newest answer"),
        Answer("Older question " * 10, "older preview " + "padding " * 30 + "Straße hidden needle"),
        Answer("Oldest question", "oldest answer"),
    ]


def test_picker_defaults_newest_and_filters_full_text_or_question():
    items = choices()
    picker = AnswerPicker(items, lambda _: None, lambda: None)
    assert picker.selected() == items[0]
    assert "Q: Newest QUESTION  A: newest answer" in picker.list.text
    assert "older preview" in picker.list.text
    assert "hidden needle" not in picker.list.text
    picker.query.text = "NEWEST question"
    assert picker.visible == items[:1]
    picker.query.text = "STRASSE HIDDEN NEEDLE"
    assert picker.visible == items[1:2]
    assert picker.selected() == items[1]
    picker.query.text = "unmatched"
    assert picker.visible == []
    assert picker.selected() is None
    assert picker.list.text == "No matching answers."
    picker.query.text = ""
    assert picker.visible == items
    assert picker.selected() == items[0]
    picker.list.buffer.cursor_down()
    assert picker.selected() == items[1]
    picker.refresh()
    assert picker.selected() == items[1]


@pytest.mark.parametrize(
    ("keys", "index"),
    [("\r", 0), ("\x1b[B\r", 1), ("hidden NEEDLE\r", 1), ("\x03", None)],
)
def test_copy_dialog_plain_answers_real_keys(keys, index):
    async def run():
        with create_pipe_input() as pipe:
            items = choices()
            app = copy_picker_dialog(items, input=pipe, output=DummyOutput())
            pipe.send_text(keys)
            result = await asyncio.wait_for(app.run_async(), 3)
            assert result == (Snippet("response", items[index].text) if index is not None else None)

    asyncio.run(run())


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


RICH_RESPONSE = "Intro\n\n> quoted words\n\n```sh\nprintf hello\n```"


@pytest.mark.parametrize("focus_query", [True, False])
@pytest.mark.parametrize("back_key", ["\x1b", "\x03"])
@pytest.mark.parametrize("snippet_keys,index", [("\x1b[A\r", 0), ("\r", 1), ("\x1b[B\r", 2)])
def test_copy_picker_back_preserves_screen_query_selection_and_focus(
    focus_query, back_key, snippet_keys, index
):
    async def run():
        with create_pipe_input() as pipe:
            shortcuts = PrefixKeys("ctrl+x")
            items = [
                Answer("excluded", "plain"),
                Answer("match first", RICH_RESPONSE),
                Answer("match second", RICH_RESPONSE + "\nsecond"),
            ]
            picked, cancelled = [], []
            picker = CopyPicker(
                items, picked.append, lambda: cancelled.append(True), shortcuts=shortcuts
            )
            answer_picker = picker.picker
            container = picker.container
            shortcuts.set_help(picker.help)
            app = Application(
                layout=Layout(
                    popup_container(HSplit([TextArea(text="host"), container]), shortcuts),
                    focused_element=picker.query,
                ),
                key_bindings=shortcuts.key_bindings(KeyBindings()),
                input=pipe,
                output=DummyOutput(),
                full_screen=True,
            )
            task = asyncio.create_task(app.run_async())
            try:
                await wait_for(lambda: app.is_running)
                pipe.send_text("match\x1b[B" + ("" if focus_query else "\t"))
                target = answer_picker.query if focus_query else answer_picker.list
                await wait_for(
                    lambda: answer_picker.selected() == items[2] and app.layout.has_focus(target)
                )
                pipe.send_text("\r")
                await wait_for(lambda: isinstance(picker.picker, SnippetPicker))
                assert picker.query is None
                assert app.layout.has_focus(picker.list)
                assert picker.title == "Copy — match second"
                assert picker.help()[-1] == ("Esc / Ctrl+C", "Back to answers")
                pipe.send_text(back_key)
                await wait_for(lambda: picker.picker is answer_picker)
                assert picker.container is container
                assert picker.query.text == "match"
                assert answer_picker.selected() == items[2]
                assert app.layout.has_focus(target)
                assert picker.title == "Copy answer"
                assert not picked and not cancelled
                # Typed-ahead keys cross the dynamic boundary before any redraw.
                pipe.send_text("\x1b[A\r" + snippet_keys)
                await wait_for(lambda: bool(picked))
                assert picked == [snippets(items[1].text)[index]]
                assert app.layout.has_focus(picker.list)
            finally:
                if not task.done():
                    app.exit()
                await asyncio.wait_for(task, 3)

    asyncio.run(run())


@pytest.mark.parametrize(
    "items,keys,expected",
    [
        (
            [Answer("plain", "text"), Answer("rich", RICH_RESPONSE)],
            "\r",
            Snippet("response", "text"),
        ),
        ([Answer("rich", RICH_RESPONSE)], "\r", Snippet("quote", "quoted words")),
        ([Answer("rich", RICH_RESPONSE)], "\x1b[A\r", Snippet("response", RICH_RESPONSE)),
        ([Answer("rich", RICH_RESPONSE)], "\x1b[B\r", Snippet("code", "printf hello", "sh")),
        ([Answer("rich", RICH_RESPONSE)], "\x1b", None),
        ([Answer("rich", RICH_RESPONSE)], "\x03", None),
        ([Answer("rich", RICH_RESPONSE), Answer("plain", "text")], "\x1b", None),
        ([Answer("rich", RICH_RESPONSE), Answer("plain", "text")], "\x03", None),
        ([Answer("rich", RICH_RESPONSE), Answer("plain", "text")], "\r\x03\x03", None),
        ([Answer("plain", "text")], "\r", Snippet("response", "text")),
        ([], "\x03", None),
    ],
)
def test_copy_dialog_real_keys(items, keys, expected):
    async def run():
        with create_pipe_input() as pipe:
            app = copy_picker_dialog(items, input=pipe, output=DummyOutput())
            pipe.send_text(keys)
            assert await asyncio.wait_for(app.run_async(), 3) == expected

    asyncio.run(run())


def test_copy_picker_single_answer_starts_at_snippets():
    picker = CopyPicker([Answer("question", RICH_RESPONSE)], lambda _: None, lambda: None)
    assert isinstance(picker.picker, SnippetPicker)
    assert picker.query is None
    assert picker.list is picker.picker.list
    assert picker.title == "Copy — question"
    assert picker.help()[-1] == ("Esc / Ctrl+C", "Cancel")


@pytest.mark.parametrize("prefix", ["ctrl", "ctrl+x"])
def test_embedded_picker_search_no_matches_and_prefix_gating(prefix):
    async def run():
        with create_pipe_input() as pipe:
            shortcuts = PrefixKeys(prefix)
            picked = []
            cancelled = []
            items = choices()
            picker = AnswerPicker(
                items, picked.append, lambda: cancelled.append(True), shortcuts=shortcuts
            )

            def provider():
                return picker.help()

            shortcuts.set_help(provider)
            # A focusable sibling models the aside reader behind the modal overlay.
            app = Application(
                layout=Layout(
                    popup_container(HSplit([TextArea(text="host"), picker.container]), shortcuts),
                    focused_element=picker.list,
                ),
                key_bindings=shortcuts.key_bindings(KeyBindings()),
                input=pipe,
                output=DummyOutput(),
                full_screen=True,
            )
            task = asyncio.create_task(app.run_async())
            try:
                await wait_for(lambda: app.is_running)
                assert shortcuts.help_provider is provider
                pipe.send_text("\t")
                await wait_for(lambda: app.layout.has_focus(picker.query))
                pipe.send_text("no matches\r")
                await wait_for(lambda: picker.query.text == "no matches")
                # A key after Enter confirms the no-op was processed.
                pipe.send_text("!")
                await wait_for(lambda: picker.query.text.endswith("!"))
                assert picked == []
                picker.query.text = ""
                if prefix != "ctrl":
                    pipe.send_text("\x18")
                    await wait_for(lambda: shortcuts.pending)
                pipe.send_text("\x1f")
                await wait_for(lambda: shortcuts.browsing)
                pipe.send_text("\x1b[B\r\x1f")
                await wait_for(lambda: not shortcuts.visible)
                assert picked == [] and cancelled == []
                assert picker.selected() == items[0]
                pipe.send_text("hidden needle\t")
                await wait_for(lambda: app.layout.has_focus(picker.list))
                pipe.send_text("\r")
                await wait_for(lambda: len(picked) == 1)
                assert picked == items[1:2]
                pipe.send_text("\x03")
                await wait_for(lambda: cancelled == [True])
            finally:
                if not task.done():
                    app.exit()
                await asyncio.wait_for(task, 3)

    asyncio.run(run())
