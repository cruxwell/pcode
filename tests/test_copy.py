import asyncio
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.conversation_tree import ConversationTree
from pcode.copy_ui import Snippet, last_response, snippets

RESPONSE = """Two things:

- Report it and paste:
  > SAML login fails for
  > **all** users.
  lazy continuation

```bash
zed schema read
```

> outer
> > nested
"""


def test_snippets_pull_top_level_quotes_and_code_without_markers():
    found = snippets(RESPONSE)
    assert found[0] == Snippet("response", RESPONSE)
    assert found[1:] == [
        Snippet("quote", "SAML login fails for **all** users. lazy continuation"),
        # No trailing newline, which would run the command when pasted in a shell.
        Snippet("code", "zed schema read", "bash"),
        Snippet("quote", "outer\n> nested"),
    ]
    assert [s.label for s in found] == ["Whole response", "Quote", "Code (bash)", "Quote"]
    assert snippets("just prose") == [Snippet("response", "just prose")]
    assert snippets("  \n") == []


def test_a_quote_opening_a_list_item_copies_without_the_list_marker():
    assert snippets("2. > quoted in list\n   > and more\n")[1:] == [
        Snippet("quote", "quoted in list and more")
    ]


def tree_with(*responses: str) -> ConversationTree:
    tree = ConversationTree()
    parent = None
    for index, response in enumerate(responses):
        identity = f"t{index}"
        tree.consume(
            {"kind": "turn_started", "run_id": identity, "parent_id": parent, "prompt": "ask"}
        )
        if response:
            tree.consume({"kind": "Message", "markdown": response})
        tree.consume({"kind": "turn_completed"})
        parent = identity
    return tree


def test_last_response_skips_turns_without_text():
    assert last_response(tree_with("first", "")) == "first"
    assert last_response(ConversationTree()) == ""


def test_copy_command_copies_a_plain_response_without_a_picker(monkeypatch):
    from pydantic_ai import Agent

    import pcode.clipboard
    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime

    copied: list[str] = []
    monkeypatch.setattr(
        pcode.clipboard, "copy", lambda text, output=None: (copied.append(text), (True, False))[1]
    )

    async def run():
        runtime = AgentRuntime(Agent("test"))
        output = StringIO()
        app = PreviewApp(runtime=runtime, console=Console(file=output))
        assert app.registry.dispatch("/copy")
        assert app.copy_requested
        await app.choose_copy(output=None, session=None)
        assert not app.copy_requested and "No response to copy" in output.getvalue()
        runtime.tree = tree_with("plain answer")
        await app.choose_copy(output=None, session=None)
        assert copied == ["plain answer"] and "Copied response." in output.getvalue()

    asyncio.run(run())


UP = "\x1b[A"


@pytest.mark.parametrize(
    ("older", "keys", "expected"),
    [
        # Rows: ask, older..., ask, newest answer (selected first), ask.
        ("older plain answer", "\r", "newest answer"),
        ("older plain answer", UP * 2 + "\r", "older plain answer"),
        ("older plain answer", UP + "\r", "ask"),
        ("older plain answer", "\x1b", None),
        ("older plain answer", "\x03", None),
        # A response's quotes and code blocks are rows of their own under it.
        (RESPONSE, UP * 2 + "\r", "outer\n> nested"),
        (RESPONSE, UP * 3 + "\r", "zed schema read"),
        (RESPONSE, UP * 4 + "\r", "SAML login fails for **all** users. lazy continuation"),
        (RESPONSE, UP * 5 + "\r", RESPONSE.strip()),
    ],
)
def test_main_copy_history_by_keyboard(monkeypatch, older, keys, expected):
    from pydantic_ai import Agent

    import pcode.clipboard
    import pcode.tree_ui as tree_ui
    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime

    copied = []
    monkeypatch.setattr(
        pcode.clipboard, "copy", lambda text, output=None: (copied.append(text), (True, False))[1]
    )
    dialogs = []

    async def run():
        runtime = AgentRuntime(Agent("test"))
        runtime.tree = tree_with(older, "newest answer", "")
        # A newer sibling must not enter the active branch's answer history.
        runtime.tree.consume(
            {"kind": "turn_started", "run_id": "sibling", "parent_id": "t0", "prompt": "other"}
        )
        runtime.tree.consume({"kind": "Message", "markdown": "excluded sibling answer"})
        runtime.tree.consume({"kind": "turn_completed"})
        runtime.tree.active = "t2"
        app = PreviewApp(runtime=runtime, console=Console(file=StringIO()))
        with create_pipe_input() as pipe:

            @asynccontextmanager
            async def popup(output, session):
                yield pipe

            monkeypatch.setattr(app, "popup", popup)
            original = tree_ui.CopyBrowser

            def build(tree, **kwargs):
                browser = original(tree, **kwargs)
                dialogs.append(browser)
                browser.app.pre_run_callables.append(lambda: pipe.send_text(keys))
                return browser

            monkeypatch.setattr(tree_ui, "CopyBrowser", build)
            session = SimpleNamespace(app=SimpleNamespace(output=DummyOutput(), style=None))
            assert app.registry.dispatch("/copy")
            await asyncio.wait_for(app.choose_copy(output=None, session=session), 5)
            assert not app.copy_requested

    asyncio.run(run())
    assert copied == ([] if expected is None else [expected])
    [browser] = dialogs
    # A newer sibling is not on the active branch, so it is not offered.
    assert all("excluded" not in label for _, label in browser.rows)


def test_copy_browser_highlights_exactly_what_it_copies():
    from pcode.tree_ui import CopyBrowser

    with create_pipe_input() as pipe:
        browser = CopyBrowser(tree_with("first answer", RESPONSE), input=pipe, output=DummyOutput())
        pane = browser.detail

        def highlighted(width=60):
            first, last = pane.span(pane.mark, width)
            return "\n".join(row.text.rstrip() for row in pane.rows(width)[first : last + 1])

        # It opens on the newest response, the whole of it marked.
        assert browser.selected == ("t1", False, 0)
        assert "Two things:" in highlighted() and "zed schema read" in highlighted()
        assert "first answer" not in highlighted()
        labels = [label for _, label in browser.rows]
        assert labels[-3:] == [
            "  ├─ Quote: SAML login fails for **all** users. lazy continuation",
            "  ├─ Code (bash): zed schema read",
            "  └─ Quote: outer > nested",
        ]
        browser.list.buffer.cursor_down(2)
        assert browser.chosen() == Snippet("code", "zed schema read", "bash")
        assert highlighted().strip() == "zed schema read"
        browser.list.buffer.cursor_up(1)
        assert "SAML login fails for" in highlighted() and "Two things" not in highlighted()
        browser.list.buffer.cursor_up(2)
        assert browser.chosen() == Snippet("prompt", "ask") and highlighted().strip() == "▌ ask"
        # A mouse selection, which copies on release, shows instead of the mark,
        # and moving through the list brings the mark back.
        pane.control.create_content(60, 20)  # A new width clears any selection.
        pane.selection.anchor, pane.selection.cursor, pane.selection.active = (0, 0), (0, 3), True
        content = pane.control.create_content(60, 20)
        selected = [any("selected" in style for style, *_ in content.get_line(r)) for r in range(4)]
        assert selected == [True, False, False, False]
        browser.list.buffer.cursor_down(1)
        assert pane.selection.span() is None


def test_tree_copy_opens_a_picker_for_quotes(monkeypatch):
    import pcode.tree_ui as tree_ui
    from pcode.tree_ui import TreeBrowser

    copied: list[str] = []
    monkeypatch.setattr(
        tree_ui,
        "copy_to_clipboard",
        lambda text, output=None: (copied.append(text), (True, False))[1],
    )
    with create_pipe_input() as pipe:
        browser = TreeBrowser(tree_with(RESPONSE), input=pipe, output=DummyOutput())
        browser.copy()
        assert browser.picker is not None and not copied
        # The picker starts on the first quote, the reason it opened.
        assert browser.picker.selected().kind == "quote"
        browser.picker.list.buffer.cursor_down()
        chosen = browser.picker.selected()
        assert chosen.kind == "code"
        browser.picker.on_pick(chosen)
        assert browser.picker is None
        assert copied == ["zed schema read"] and browser.notice == "Copied code"


def test_ctrl_y_with_an_empty_editor_copies_the_last_response():
    from pcode.commands import CommandRegistry
    from pcode.ui import create_prompt

    asked: list[bool] = []

    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(),
                on_submit=lambda text: None,
                on_copy_response=lambda: asked.append(True),
                input=pipe,
                output=DummyOutput(),
            )

            async def feed():
                pipe.send_text("\x19")
                while not asked:
                    await asyncio.sleep(0.01)
                prompt.app.exit(result="")

            task = asyncio.ensure_future(feed())
            try:
                await asyncio.wait_for(prompt.prompt_async(), timeout=5)
            finally:
                task.cancel()

    asyncio.run(run())
    assert asked == [True]


def test_quotes_copy_soft_wrapped_lines_joined_as_they_render():
    quote = "> one\n> two  \n> hard\\\n> break\n>\n> - item\n>   wraps\n>\n> ```\n> a\n> b\n> ```\n"
    assert snippets(quote)[1].text == "one two\nhard\nbreak\n\n- item wraps\n\n```\na\nb\n```"
