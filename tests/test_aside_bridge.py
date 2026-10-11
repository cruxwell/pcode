"""Bringing a /btw thread into the conversation: merged into /tree, or summarized."""

import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from rich.console import Console

from pcode.agent import create_coder
from pcode.app import PreviewApp
from pcode.aside import (
    ASIDE_FRAMING,
    FOLLOW_UP_FRAMING,
    SUMMARY_FRAMING,
    Aside,
    Asides,
    Bridge,
    SideReply,
    exchanges,
    framed,
    framed_follow_up,
    framed_summary,
    summary_request,
)
from pcode.aside_ui import AsideBrowser
from pcode.live import AgentRuntime
from pcode.sessions import SavedSession, SessionError
from pcode.ui import create_prompt


def prompts(messages) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


def question_of(text: str) -> str:
    return text.rpartition("Question: ")[2]


def scripted(requests: list):
    """A model answering side questions, follow-ups and summaries by what they ask."""

    async def model(messages, info):
        requests.append(list(messages))
        last = prompts(messages[-1:])
        asked = last[-1] if last else ""
        if asked.startswith(SUMMARY_FRAMING):
            yield "Summary: the parser is LL(1)."
        elif asked.startswith((ASIDE_FRAMING, FOLLOW_UP_FRAMING)):
            yield f"Side answer to {question_of(asked)}"
        else:
            yield f"Main answer to {asked}"

    return model


def runtime_for(tmp_path, requests, session=None) -> AgentRuntime:
    return AgentRuntime(
        Agent(
            FunctionModel(stream_function=scripted(requests)), capabilities=[create_coder(tmp_path)]
        ),
        session,
    )


async def turn(runtime, prompt):
    return [event async for event in runtime.stream(prompt)]


def steps_for(thread: list[Aside], reply: SideReply):
    return [
        (aside.question, aside.answer, reply.messages[:end])
        for aside, end in exchanges(thread, reply.messages)
    ]


async def ask_thread(runtime, *questions) -> tuple[list[Aside], SideReply]:
    """Ask a question and its follow-ups, returning the thread's records and newest reply."""
    thread: list[Aside] = []
    reply = None
    for question in questions:
        reply = await runtime.aside(question, after=reply)
        thread.append(Aside(question=question, answer=reply.answer, status="answered"))
    return thread, reply


def test_exchanges_cut_the_newest_history_at_each_answered_question():
    context = [
        ModelRequest([UserPromptPart("Main task")]),
        ModelResponse([ToolCallPart("read_file", {}, tool_call_id="one")]),
        # Asked mid-turn, the question joined the request in flight.
        ModelRequest(
            [
                ToolReturnPart("read_file", "ok", tool_call_id="one"),
                UserPromptPart(framed("why?")),
            ]
        ),
    ]
    messages = [
        *context,
        ModelResponse([TextPart("because")]),
        ModelRequest([UserPromptPart(framed_follow_up("and then?"))]),
        ModelResponse([ToolCallPart("read_file", {}, tool_call_id="two")]),
        ModelRequest([ToolReturnPart("read_file", "ok", tool_call_id="two")]),
        ModelResponse([TextPart("then this")]),
    ]
    root, failed, follow = (
        Aside(question="why?"),
        Aside(question="lost?"),
        Aside(question="and then?"),
    )
    # The failed follow-up never joined the history, so it has no cut.
    assert exchanges([root, failed, follow], messages) == [(root, 4), (follow, len(messages))]
    assert exchanges([root], messages[:4]) == [(root, 4)]
    assert exchanges([Aside(question="unrelated")], messages) == []


def test_a_thread_that_extends_the_conversation_becomes_its_continuation(tmp_path):
    requests = []
    runtime = runtime_for(tmp_path, requests)

    async def run():
        await turn(runtime, "Main task")
        base = runtime.tree.active
        thread, reply = await ask_thread(runtime, "Why?", "And then?")
        moved = await runtime.merge_aside(steps_for(thread, reply), base)
        assert moved
        first, second = (runtime.tree.nodes[i] for i in runtime.tree.path(runtime.tree.active)[1:])
        assert (first.kind, first.parent, first.prompt) == ("aside", base, "Why?")
        assert (second.kind, second.parent, second.prompt) == ("aside", first.id, "And then?")
        assert second.response == "Side answer to And then?"
        assert runtime.history == reply.messages
        rows = [label for _, label in runtime.tree.rows()]
        assert any(label.startswith("btw: Why?") for label in rows)

        # The next turn continues from the thread's last answer.
        await turn(runtime, "Next")
        assert requests[-1][: len(reply.messages)] == reply.messages

        # Each exchange is a checkpoint of its own.
        await runtime.navigate(first.id)
        assert prompts(runtime.history)[-1] == framed("Why?")
        assert runtime.history == reply.messages[: len(runtime.history)]
        with pytest.raises(SessionError, match="came from a side thread"):
            runtime.resend_prompt()

    asyncio.run(run())


def test_a_thread_the_conversation_moved_past_becomes_a_branch(tmp_path):
    requests = []
    runtime = runtime_for(tmp_path, requests)

    async def run():
        await turn(runtime, "Main task")
        base = runtime.tree.active
        thread, reply = await ask_thread(runtime, "Why?")
        await turn(runtime, "Later work")
        later, history = runtime.tree.active, list(runtime.history)
        moved = await runtime.merge_aside(steps_for(thread, reply), base)
        assert not moved
        # The conversation stays where it was; the thread forks where it was asked.
        assert runtime.tree.active == later
        assert runtime.history == history
        (merged,) = [node for node in runtime.tree.nodes.values() if node.kind == "aside"]
        assert merged.parent == base
        await runtime.navigate(merged.id)
        assert runtime.history == reply.messages

    asyncio.run(run())


def test_a_merged_thread_survives_resume(tmp_path):
    requests = []
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = runtime_for(tmp_path, requests, saved)

    async def run():
        await turn(runtime, "Main task")
        base = runtime.tree.active
        thread, reply = await ask_thread(runtime, "Why?", "And then?")
        assert await runtime.merge_aside(steps_for(thread, reply), base)
        return runtime.tree.active, reply

    last, reply = asyncio.run(run())
    identity = saved.info.id
    saved.close()

    reopened = SavedSession.open(identity, tmp_path / "sessions")
    try:
        assert reopened.tree.active == last
        assert reopened.tree.nodes[last].kind == "aside"
        assert asyncio.run(reopened.recover()) == reply.messages
        shown = [
            (record["kind"], record.get("prompt") or record.get("markdown"))
            for record in reopened.transcript_records()
            if record["kind"] in ("turn_started", "Message")
        ]
        assert shown == [
            ("turn_started", "Main task"),
            ("Message", "Main answer to Main task"),
            ("turn_started", "Why?"),
            ("Message", "Side answer to Why?"),
            ("turn_started", "And then?"),
            ("Message", "Side answer to And then?"),
        ]
    finally:
        reopened.close()


def test_a_summary_joins_the_active_branch_as_one_exchange(tmp_path):
    requests = []
    runtime = runtime_for(tmp_path, requests)

    async def run():
        await turn(runtime, "Main task")
        before, active = list(runtime.history), runtime.tree.active
        thread, reply = await ask_thread(runtime, "Why?")
        request = summary_request(["Why?"], "keep decisions")
        summary = await runtime.summarize_aside(reply, request, "keep decisions")
        assert summary == "Summary: the parser is LL(1)."
        # Asked in the thread, after its answer, where the cache is warm.
        asked = requests[-1]
        assert asked[: len(reply.messages)] == reply.messages
        assert prompts(asked[-1:]) == [framed_summary("keep decisions")]
        # Recorded on the conversation as one exchange after what it had.
        assert runtime.history[: len(before)] == before
        assert prompts(runtime.history[len(before) :]) == [request]
        assert runtime.history[-1].parts[0].content == summary
        node = runtime.tree.nodes[runtime.tree.active]
        assert (node.kind, node.parent, node.prompt, node.response) == (
            "aside",
            active,
            request,
            summary,
        )
        await turn(runtime, "Next")
        assert requests[-1][: len(runtime.history) - 2] == runtime.history[:-2]

        # A history ending mid-turn would put two requests in a row.
        runtime.history = [ModelRequest([UserPromptPart("interrupted")])]
        with pytest.raises(SessionError, match="stopped mid-turn"):
            await runtime.summarize_aside(reply, request)

    asyncio.run(run())
    assert "Why?" in summary_request(["Why?"]) and "Focus" not in summary_request(["Why?"])


def settled_app(tmp_path, requests) -> PreviewApp:
    return PreviewApp(
        model="test:local",
        runtime=runtime_for(tmp_path, requests),
        console=Console(file=StringIO(), width=140),
    )


async def settle(app):
    while app.asides.running:
        await asyncio.sleep(0.01)


def test_the_app_refuses_a_bridge_it_cannot_make_yet(tmp_path):
    app = settled_app(tmp_path, [])

    async def run():
        await turn(app.runtime, "Main task")
        await app.controller.start_aside("Why?")
        root = app.asides.items[0]
        assert root.base == app.runtime.tree.active
        assert root.conversation == app.runtime.conversation_id
        with pytest.raises(ValueError, match="Wait for this answer first"):
            app.controller.check_bridge(root.thread)
        await settle(app)
        assert app.controller.check_bridge(root.thread) is root
        app.activity.busy = True
        with pytest.raises(ValueError, match="waits for the running turn"):
            app.controller.check_bridge(root.thread)
        app.activity.busy = False
        root.conversation = "another"
        with pytest.raises(ValueError, match="another conversation"):
            app.controller.check_bridge(root.thread)

    asyncio.run(run())


def test_the_app_merges_a_thread_and_shows_it(tmp_path):
    app = settled_app(tmp_path, [])

    async def run():
        await turn(app.runtime, "Main task")
        await app.controller.start_aside("Why?")
        await settle(app)
        root = app.asides.items[0]
        app.controller.follow_up_aside(root.thread, "And then?")
        await settle(app)
        await app.controller.merge_thread(root.thread)
        tree = app.runtime.tree
        assert [tree.nodes[i].prompt for i in tree.path(tree.active)] == [
            "Main task",
            "Why?",
            "And then?",
        ]
        assert app.asides.items[-1].bridged == "merged"

    asyncio.run(run())
    printed = app.transcript.console.file.getvalue()
    assert "Side answer to And then?" in printed
    assert "Merged 2 side questions into the conversation" in printed


def test_btw_summarizes_a_thread_into_the_conversation_from_the_viewer(tmp_path):
    """The whole path: the viewer returns a request, prompts wait, the summary lands."""
    requests = []
    app = settled_app(tmp_path, requests)
    runtime = app.runtime

    async def run():
        await turn(runtime, "Main task")
        await app.controller.start_aside("Why?")
        await settle(app)
        thread = app.asides.items[0].thread

        class Browser:
            def __init__(self, asides, **options):
                self.check = options["check_bridge"]

            async def run(self):
                self.check(thread)
                return Bridge(thread, "summary", "keep decisions")

        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            async def wait_for(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(0.01)

            async def drive():
                await wait_for(lambda: session is not None and session.app.is_running)
                pipe.send_text("/btw\r")
                printed = app.transcript.console.file
                await wait_for(lambda: "summary added" in printed.getvalue())
                node = runtime.tree.nodes[runtime.tree.active]
                assert node.kind == "aside"
                assert node.response == "Summary: the parser is LL(1)."
                assert app.asides.items[0].bridged == "summarized"
                assert not app.activity.busy
                pipe.send_text("/quit\r")

            with (
                patch("pcode.app.create_prompt", prompt),
                patch("pcode.aside_ui.AsideBrowser", Browser),
            ):
                await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=15)

    asyncio.run(run())
    assert prompts(requests[-1][-1:]) == [framed_summary("keep decisions")]


def answered(question: str, answer: str) -> Aside:
    aside = Aside(question=question, answer=answer, activity="")
    aside.settle("answered")
    aside.reply = SideReply(answer=answer)
    return aside


@pytest.mark.parametrize("prefix", ["ctrl", "ctrl+p"])
def test_the_viewer_asks_for_optional_summary_instructions_and_merges(prefix):
    """Real keys: t merges, s prompts in the editor, Esc gives the draft back."""

    def shortcut(letter):
        # Ctrl+letter arrives as its control code; a leader is Ctrl+P, then the letter.
        return chr(ord(letter) - 96) if prefix == "ctrl" else "\x10" + letter

    async def run():
        asides = Asides()
        root = answered("why this file?", "Because the task named it.")
        asides.items.append(root)
        refusal: list[str] = []

        def check(thread):
            if refusal:
                raise ValueError(refusal[0])

        async def viewer(*keys, wait=0.05):
            with create_pipe_input() as pipe:
                browser = AsideBrowser(
                    asides,
                    ask=lambda thread, question: None,
                    check_bridge=check,
                    key_prefix=prefix,
                    input=pipe,
                    output=DummyOutput(),
                )
                task = asyncio.create_task(browser.run())
                await asyncio.sleep(0.05)
                try:
                    for key in keys:
                        if callable(key):
                            key(browser)
                            continue
                        pipe.send_text(key)
                        await asyncio.sleep(0.7 if key == "\x1b" else wait)
                    return browser, await asyncio.wait_for(task, 1)
                finally:
                    if not task.done():
                        browser.app.exit()
                        await task

        _, result = await viewer(shortcut("t"))
        assert result == Bridge(root.thread, "merge")

        _, result = await viewer(shortcut("s"), "keep the decisions", "\r")
        assert result == Bridge(root.thread, "summary", "keep the decisions")

        # Nothing typed summarizes as is.
        _, result = await viewer(shortcut("s"), "\r")
        assert result == Bridge(root.thread, "summary", "")

        def prompting(browser):
            assert browser.input.prompting
            assert browser.prefix_keys.summary() == (
                "Ctrl+/ Keys" if prefix == "ctrl" else "Ctrl+P Keys"
            )
            assert browser.prefix_keys.browsing
            assert ("Enter", "Summarize (empty: as is)") in browser.prefix_keys.hint_rows()
            assert [shortcut.key for shortcut in browser.prefix_keys.available()] == [
                "r",
                "y",
                "o",
                "s",
                "t",
                "q",
            ]

        def restored(browser):
            assert not browser.input.prompting
            assert browser.input.text == "half a follow-up"
            assert browser.app.layout.has_focus(browser.detail)
            assert "Summarize" not in browser.input._title()

        # A follow-up draft is set aside for the prompt, and Esc brings it back.
        # The shortcut works from the editor, without typing its letter there.
        browser, result = await viewer(
            shortcut("r"),
            "half a follow-up",
            shortcut("s"),
            "\x1f",  # Ctrl+/ opens contextual help for the summary prompt.
            prompting,
            "\x1f",  # Dismiss help without cancelling the prompt.
            "x",
            "\x1b",
            restored,
            "\x1b",
        )
        assert result is None

        refusal.append("Adding to the conversation waits for the running turn")
        browser, result = await viewer(shortcut("t"), shortcut("s"), "\x1b")
        assert result is None
        assert browser.notice == refusal[0]
        assert not browser.input.prompting

    asyncio.run(run())


def test_the_viewer_without_bridging_has_no_bridge_keys():
    asides = Asides()
    asides.items.append(answered("why?", "because"))
    browser = AsideBrowser(asides, output=None, input=None)
    assert browser.input is None
    assert [shortcut.key for shortcut in browser.prefix_keys.shortcuts] == ["y", "o", "k", "q"]
    assert [shortcut.key for shortcut in browser.prefix_keys.available()] == ["y", "o", "q"]
    assert browser.prefix_keys.summary() == "Ctrl+/ Keys"
    assert not any(
        label in {"Summarize", "Merge into conversation"}
        for _, label in browser.prefix_keys.hint_rows()
    )
    keys = {binding.keys for binding in browser.app.key_bindings.bindings if binding.filter()}
    assert ("c-s",) not in keys and ("c-t",) not in keys
