"""Follow-ups to side answers, asked from the /btw viewer's editor."""

import asyncio
from copy import deepcopy
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from rich.console import Console

from pcode import aside as aside_module
from pcode.agent import SideModel, create_coder
from pcode.app import PreviewApp
from pcode.aside import (
    FOLLOW_UP_FRAMING,
    Aside,
    Asides,
    SideReply,
    framed_follow_up,
)
from pcode.aside_ui import AsideBrowser, row
from pcode.live import AgentRuntime
from pcode.popup_ui import INPUT_ROWS_MAX


class Recorder(AbstractCapability):
    """Records each request: its model, conversation id, settings and messages."""

    def __init__(self, seen):
        self.seen = seen

    async def before_model_request(self, ctx, request_context):
        self.seen.append(
            {
                "model": request_context.model.model_name,
                "conversation": ctx.conversation_id,
                "settings": request_context.model_settings,
                "messages": deepcopy(list(request_context.messages)),
            }
        )
        return request_context


def prompts(messages) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


def test_a_follow_up_resends_the_side_run_and_adds_only_its_question(tmp_path):
    """The prefix is the side run as sent and answered, so its cache is reused."""
    seen = []

    async def main_model(messages, info):
        yield f"main: {prompts(messages)[-1].rpartition('Question: ')[2]}"

    async def replacement(messages, info):
        yield "replacement"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=main_model, model_name="main"),
            capabilities=[create_coder(tmp_path), Recorder(seen)],
            model_settings={"temperature": 0.5},
        )
    )

    async def run():
        [event async for event in runtime.stream("Main task")]
        reply = await runtime.aside("Why?")
        # /model swaps the conversation's agent; the thread stays where it ran.
        runtime.replace_agent(
            Agent(
                FunctionModel(stream_function=replacement, model_name="replacement"),
                capabilities=[create_coder(tmp_path), Recorder(seen)],
            )
        )
        follow = await runtime.aside("And then?", after=reply)
        return reply, follow

    reply, follow = asyncio.run(run())
    main, side, following = seen
    assert reply.answer == "main: Why?"
    assert follow.answer == "main: And then?"
    sent = following["messages"]
    assert sent[: len(reply.messages)] == reply.messages
    # One new request: the follow-up's question, framed for a follow-up.
    assert len(sent) == len(reply.messages) + 1
    assert isinstance(sent[-1], ModelRequest)
    assert prompts(sent[-1:]) == [framed_follow_up("And then?")]
    assert following["model"] == "main"
    assert following["settings"] == side["settings"] == main["settings"]
    assert following["conversation"] == side["conversation"] == runtime.conversation_id
    # The next follow-up continues from this one.
    assert follow.messages[: len(sent)] == sent
    # Nothing reached the conversation.
    assert prompts(runtime.history) == ["Main task"]
    assert len(runtime.tree.nodes) == 1


def test_a_follow_up_on_another_model_keeps_its_model_settings_and_session(tmp_path):
    seen = []

    async def main_model(messages, info):
        yield "main"

    async def other_model(messages, info):
        yield "other"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=main_model, model_name="main"),
            capabilities=[create_coder(tmp_path), Recorder(seen)],
            model_settings={"temperature": 0.5},
        )
    )
    other = SideModel(
        "fn:other", FunctionModel(stream_function=other_model, model_name="other"), {"top_p": 0.9}
    )

    async def run():
        [event async for event in runtime.stream("Main task")]
        reply = await runtime.aside("Why?", model=other)
        await runtime.aside("And then?", after=reply)
        return reply

    reply = asyncio.run(run())
    _, side, following = seen
    assert following["model"] == side["model"] == "other"
    assert following["settings"] == side["settings"]
    assert following["settings"].get("top_p") == 0.9
    # Another model's session is the thread's own, and a follow-up stays in it.
    assert reply.conversation_id.startswith(f"{runtime.conversation_id}.btw-")
    assert following["conversation"] == side["conversation"] == reply.conversation_id


def test_follow_ups_share_a_thread_and_continue_from_its_newest_answer():
    async def run():
        asides = Asides()
        release = asyncio.Event()

        def answering(text):
            async def work(aside):
                await release.wait()
                return SideReply(answer=text, messages=[text])

            return work

        async def fail(aside):
            raise ValueError("provider refused")

        root = asides.start("why?", answering("a1"))
        other = asides.start("unrelated?", answering("b1"))
        with pytest.raises(ValueError, match="Wait for this answer first"):
            asides.follows(root.thread)
        release.set()
        await asyncio.sleep(0.05)
        assert asides.follows(root.thread) is root

        release.clear()
        follow = asides.start("and then?", answering("a2"), thread=root.thread)
        assert follow.thread == root.thread
        assert [[aside.question for aside in thread] for thread in asides.threads()] == [
            ["why?", "and then?"],
            ["unrelated?"],
        ]
        with pytest.raises(ValueError, match="Wait for this answer first"):
            asides.follows(root.thread)
        release.set()
        await asyncio.sleep(0.05)
        assert asides.follows(root.thread) is follow
        # Only the newest answer keeps a copy of the conversation.
        assert root.reply is None
        assert follow.reply.answer == "a2"
        assert other.reply.answer == "b1"

        # A failed follow-up leaves the answer before it to continue from.
        failed = asides.start("again?", fail, thread=root.thread)
        await asyncio.sleep(0.05)
        assert failed.status == "failed"
        assert asides.follows(root.thread) is follow

        lonely = asides.start("broken?", fail)
        await asyncio.sleep(0.05)
        with pytest.raises(ValueError, match="No answer to follow up on"):
            asides.follows(lonely.thread)
        with pytest.raises(ValueError, match="gone"):
            asides.follows("missing")

    asyncio.run(run())


def test_old_threads_are_trimmed_whole(monkeypatch):
    monkeypatch.setattr(aside_module, "ASIDE_HISTORY", 2)

    async def run():
        asides = Asides()

        async def answer(aside):
            return None

        first = asides.start("first?", answer)
        asides.start("first again?", answer, thread=first.thread)
        await asyncio.sleep(0.01)
        asides.start("second?", answer)
        asides.start("third?", answer)
        await asyncio.sleep(0.01)
        # The oldest thread goes with its follow-up, not one question at a time.
        assert [aside.question for aside in asides.items] == ["second?", "third?"]

    asyncio.run(run())


def test_the_app_asks_a_follow_up_in_the_thread_on_its_model():
    calls = []

    class Runtime:
        session = None
        tree = None

        async def aside(self, question, report, **options):
            calls.append((question, options))
            report(f"answer to {question}", "")
            return SideReply(answer=f"answer to {question}", messages=[question])

    app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=StringIO()))

    async def settled():
        while app.asides.running:
            await asyncio.sleep(0.01)

    async def run():
        await app.controller.start_aside("why?")
        root = app.asides.items[0]
        root.model, root.label, root.effort = "q:other", "other · low", "low"
        await settled()
        app.controller.follow_up_aside(root.thread, "and then?")
        follow = app.asides.items[-1]
        assert (follow.thread, follow.model, follow.label, follow.effort) == (
            root.thread,
            "q:other",
            "other · low",
            "low",
        )
        with pytest.raises(ValueError, match="Wait for this answer first"):
            app.controller.follow_up_aside(root.thread, "too soon?")
        await settled()
        assert follow.answer == "answer to and then?"

    asyncio.run(run())
    assert calls[0] == ("why?", {})
    question, options = calls[1]
    assert question == "and then?"
    assert options["after"].answer == "answer to why?"


def answered(question: str, answer: str, **fields) -> Aside:
    aside = Aside(question=question, answer=answer, activity="", **fields)
    aside.settle("answered")
    aside.reply = SideReply(answer=answer)
    return aside


def test_a_thread_shows_as_one_row_and_one_conversation():
    asides = Asides()
    root = answered("why this file?", "Because the task named it.", label="other")
    follow = answered("and the test?", "It covers the parser.", thread=root.thread)
    asides.items.extend([root, answered("unrelated?", "No."), follow])
    assert row([root, follow]).startswith("[other] why this file?  (answered")
    assert row([root, follow]).endswith(" · 1 follow-up)")
    browser = AsideBrowser(asides, selected=follow.id, output=None, input=None)
    assert browser.selected == root.thread
    lines = browser.list.text.splitlines()
    assert len(lines) == 2
    assert "1 follow-up" in lines[0]
    body = browser.detail.text(80)
    assert body.index("why this file?") < body.index("Because the task named it.")
    assert body.index("Because the task named it.") < body.index("and the test?")
    assert body.index("and the test?") < body.index("It covers the parser.")
    assert follow.read and root.read
    # No `ask`, no editor: the read-only viewer is unchanged.
    assert browser.input is None


def test_the_editor_grows_with_the_draft_up_to_its_cap():
    browser = AsideBrowser(Asides(), ask=lambda thread, question: None, output=None, input=None)
    editor = browser.input
    assert editor.rows().preferred == 1
    editor.area.text = "one\ntwo\nthree"
    assert editor.rows().preferred == 3
    editor.area.text = "\n".join(str(line) for line in range(20))
    assert editor.rows().preferred == INPUT_ROWS_MAX


def test_the_viewer_sends_a_follow_up_typed_in_its_editor():
    """Real keys: letters type rather than fire shortcuts, Enter sends, Esc steps out."""

    async def run():
        asides = Asides()
        root = answered("why this file?", "Because the task named it.")
        asides.items.append(root)
        asked = []
        refusal = []

        def ask(thread, question):
            if refusal:
                raise ValueError(refusal[0])
            asked.append((thread, question))
            asides.items.append(Aside(question=question, thread=thread))

        with create_pipe_input() as pipe:
            browser = AsideBrowser(asides, ask=ask, input=pipe, output=DummyOutput())
            app = browser.app
            task = asyncio.create_task(browser.run())

            async def press(keys, wait=0.05):
                pipe.send_text(keys)
                await asyncio.sleep(wait)
                assert not task.done(), f"{keys!r} must not close the viewer"

            try:
                await asyncio.sleep(0.05)
                # Opens on the answer (a lone thread shows no list), not the editor.
                assert app.layout.has_focus(browser.detail)
                await press("\x12")  # Ctrl+R
                assert app.layout.has_focus(browser.input.area)
                assert ("Enter", "Send") in browser.help()
                # Shortcuts are chords, so every letter here is text.
                await press("can you recheck it?")
                await press("\n")  # Ctrl+J.
                await press("line two")
                assert browser.input.text == "can you recheck it?\nline two"
                assert browser.notice == ""
                await press("\r")
                assert asked == [(root.thread, "can you recheck it?\nline two")]
                assert browser.input.text == ""
                assert app.layout.has_focus(browser.input.area)
                assert "1 follow-up" in browser.list.text
                assert "can you recheck it?" in browser.detail.text(80)

                # Enter on an empty draft sends nothing and stays open.
                await press("\r")
                assert len(asked) == 1

                # A refusal keeps the draft and says why in the editor's title.
                refusal.append("Wait for this answer first")
                await press("too soon?")
                await press("\r")
                assert browser.input.text == "too soon?"
                assert "Wait for this answer first" in browser.input._title()
                await press("!")
                assert "Wait" not in browser.input._title()

                # Esc steps back out of the editor, keeping the draft; Esc there closes.
                await press("\x1b", wait=0.7)
                assert app.layout.has_focus(browser.detail)
                assert browser.input.text == "too soon?!"
                pipe.send_text("\x1b")
                await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    app.exit()
                    await task

    asyncio.run(run())


def test_a_viewer_the_user_opened_reads_in_its_editor():
    """`edit`: typing replies at once, and Esc from an empty draft leaves as from the answer."""

    async def run():
        asides = Asides()
        first = answered("first?", "One.")
        second = answered("second?", "Two.")
        asides.items.extend([first, second])
        asked = []

        def ask(thread, question):
            asked.append((thread, question))

        with create_pipe_input() as pipe:
            browser = AsideBrowser(asides, ask=ask, edit=True, input=pipe, output=DummyOutput())
            app = browser.app
            task = asyncio.create_task(browser.run())

            async def press(keys, wait=0.05):
                pipe.send_text(keys)
                await asyncio.sleep(wait)

            try:
                await asyncio.sleep(0.05)
                # Several threads: still opens in the newest one's editor, so
                # the follow-up is just typed; the list is an Esc away.
                assert not browser.listing()
                assert app.layout.has_focus(browser.input.area)
                await press("and why?\r")
                assert asked == [(second.thread, "and why?")]
                # A draft still makes Esc step out to the answer, keeping it.
                await press("draft")
                await press("\x1b", wait=0.7)
                assert app.layout.has_focus(browser.detail)
                await press("\t")
                assert app.layout.has_focus(browser.input.area)
                await press("\x7f" * len("draft"))
                # Empty, Esc goes back to the list as the answer's Esc would.
                await press("\x1b", wait=0.7)
                assert browser.listing() and app.layout.has_focus(browser.list)
                # Choosing a thread from it lands in the editor again.
                await press("\r")
                assert not browser.listing()
                assert app.layout.has_focus(browser.input.area)
                await press("\x1b", wait=0.7)
                assert not task.done()
                pipe.send_text("\x1b")
                await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    app.exit()
                    await task

        # A lone thread opens straight in the editor; one Esc closes.
        lone = Asides()
        lone.items.append(answered("only?", "Yes."))
        with create_pipe_input() as pipe:
            browser = AsideBrowser(lone, ask=ask, edit=True, input=pipe, output=DummyOutput())
            assert browser.app.layout.has_focus(browser.input.area)
            task = asyncio.create_task(browser.run())
            await asyncio.sleep(0.05)
            pipe.send_text("\x1b")
            await asyncio.wait_for(task, 2)

    asyncio.run(run())


def test_follow_up_framing_is_only_in_the_question():
    assert framed_follow_up("why?").startswith(FOLLOW_UP_FRAMING)
    assert framed_follow_up("why?").endswith("Question: why?")


def test_the_editor_runs_slash_commands_and_completes_their_names(monkeypatch):
    """A typed /copy acts like Ctrl+Y; a path is still a question; a typo is refused."""
    from pcode import aside_ui
    from pcode.aside import Bridge

    copies = []
    monkeypatch.setattr(
        aside_ui,
        "copy_to_clipboard",
        lambda text, output=None: (copies.append(text), (True, False))[1],
    )

    async def run():
        asides = Asides()
        root = answered("why this file?", "Because the task named it.")
        asides.items.append(root)
        asked = []

        with create_pipe_input() as pipe:
            browser = AsideBrowser(
                asides,
                ask=lambda thread, question: asked.append(question),
                check_bridge=lambda thread: None,
                input=pipe,
                output=DummyOutput(),
            )
            editor = browser.input
            task = asyncio.create_task(browser.run())

            async def press(keys, wait=0.1):
                pipe.send_text(keys)
                await asyncio.sleep(wait)

            await asyncio.sleep(0.05)
            await press("\x12")  # Ctrl+R
            # The menu offers names as soon as "/" starts one.
            await press("/co")
            assert [c.text for c in editor.area.buffer.complete_state.completions] == ["/copy"]
            # Esc closes the menu, staying in the editor with the draft.
            await press("\x1b", wait=0.6)
            assert editor.area.buffer.complete_state is None
            assert browser.editing() and editor.text == "/co"
            # The start of one name is enough, menu or not.
            await press("\r")
            assert copies == ["Because the task named it."]
            assert browser.notice == "Copied answer"
            assert editor.text == "" and asked == []

            # Tab walks the menu, writing the name in; Enter runs that one.
            await press("/")
            await press("\t")
            await press("\t")
            assert editor.text == "/links"
            await press("\r")
            assert browser.notice == "No links in this side thread"
            assert editor.text == "" and asked == []

            # A start shared by several names, or a bare "/", says which it could be.
            await press("/s\r")
            assert "/s could be /summarize or /stop" in editor._title()
            editor.area.text = ""
            await press("/\r")
            assert editor.text == "/" and asked == []
            editor.area.text = ""

            # A path is not a command name, so it is asked as written.
            await press("/etc/hosts: what is in it?\r")
            assert asked == ["/etc/hosts: what is in it?"]

            # A name it does not know is refused, and the draft is kept to fix.
            await press("/bogus\r")
            assert editor.text == "/bogus"
            assert "Unknown command /bogus" in editor._title()
            assert asked == ["/etc/hosts: what is in it?"]
            editor.area.text = ""

            # What follows /summarize is its focus, and it closes the viewer.
            await press("/summarize keep the API notes\r")
            assert await asyncio.wait_for(task, 2) == Bridge(
                root.thread, "summary", "keep the API notes"
            )

    asyncio.run(run())


def test_ctrl_c_stops_running_answers_before_it_closes_the_viewer():
    async def run():
        asides = Asides()
        running = Aside(question="still going?", answer="So far")
        asides.items.append(running)
        stopped = []

        def stop():
            stopped.append(True)
            running.settle("cancelled")

        with create_pipe_input() as pipe:
            browser = AsideBrowser(
                asides,
                ask=lambda thread, question: None,
                stop=stop,
                input=pipe,
                output=DummyOutput(),
            )
            # Stop is listed only while something runs.
            assert ("Ctrl+K", "Stop") in browser.prefix_keys.hint_rows()
            task = asyncio.create_task(browser.run())
            await asyncio.sleep(0.05)
            pipe.send_text("\x03")
            await asyncio.sleep(0.1)
            assert stopped == [True] and not task.done()
            assert browser.notice == "Stopping 1 running answer"
            assert not any(label == "Stop" for _, label in browser.prefix_keys.hint_rows())
            pipe.send_text("\x03")
            assert await asyncio.wait_for(task, 2) is None
            assert stopped == [True]

    asyncio.run(run())
