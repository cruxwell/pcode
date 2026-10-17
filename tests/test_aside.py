"""Side questions run beside a turn without joining, steering, or blocking it."""

import asyncio
from copy import deepcopy
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from rich.console import Console

from pcode.agent import create_coder
from pcode.app import PreviewApp
from pcode.aside import ASIDE_FRAMING, Aside, Asides, framed, settled_context
from pcode.live import AgentRuntime
from pcode.preferences import save_preferences
from pcode.runtime import Message
from pcode.ui import create_prompt


def test_settled_context_stops_before_an_unanswered_tool_call():
    call = ToolCallPart("read_file", {}, tool_call_id="one")
    messages = [
        ModelRequest([UserPromptPart("first")]),
        ModelResponse([call]),
        ModelRequest([ToolReturnPart("read_file", "ok", tool_call_id="one")]),
        ModelResponse([TextPart("answer")]),
        ModelRequest([UserPromptPart("second")]),
        ModelResponse([ToolCallPart("read_file", {}, tool_call_id="two")]),
    ]
    # The request in flight ends mid-tool-loop; a provider would reject it.
    assert settled_context(messages) == messages[:5]
    assert settled_context(messages[:2]) == messages[:1]
    assert settled_context([]) == []


def side_question(messages) -> str | None:
    """The question a request asks, when it is a side question's request."""
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart) and part.content.startswith(ASIDE_FRAMING):
                return part.content.rpartition("Question: ")[2]
    return None


def test_a_side_question_repeats_the_turns_request_prefix_byte_for_byte(tmp_path):
    """Instructions, tools, settings and history match, so the cache is reused."""
    requests = []

    async def model(messages, info):
        requests.append(
            {
                "aside": side_question(messages) is not None,
                "messages": deepcopy(messages),
                "instructions": info.instructions,
                "tools": [
                    (tool.name, tool.description, tool.parameters_json_schema)
                    for tool in info.function_tools
                ],
                "settings": info.model_settings,
            }
        )
        if side_question(messages) is not None:
            yield "Side answer."
        elif not any(isinstance(m, ModelResponse) for m in messages):
            yield {0: DeltaToolCall(name="read_file", json_args='{"path":"sample.txt"}')}
        else:
            yield "Main answer."

    (tmp_path / "sample.txt").write_text("marker")
    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=model),
            capabilities=[create_coder(tmp_path)],
            model_settings={"temperature": 0.5},
        )
    )

    async def run():
        [event async for event in runtime.stream("Main task")]
        assert (await runtime.aside("Why this file?")).answer == "Side answer."

    asyncio.run(run())
    *main, side = requests
    assert [request["aside"] for request in requests] == [False, False, True]
    tool_names = [name for name, _, _ in side["tools"]]
    # The full tool list, not a read-only subset: dropping one breaks the cache.
    assert {"shell", "write_file", "write_plan", "delegate_task"} <= set(tool_names)
    for request in main:
        assert side["instructions"] == request["instructions"]
        assert side["tools"] == request["tools"]
        assert side["settings"] == request["settings"]
    # The framing is in the question, not the system prompt.
    assert ASIDE_FRAMING not in (side["instructions"] or "")
    prefix = main[-1]["messages"]
    assert side["messages"][: len(prefix)] == prefix
    assert side_question(side["messages"][len(prefix) :]) == "Why this file?"


def test_a_side_question_cannot_change_the_plan_or_delegate_but_still_answers(tmp_path):
    from pydantic_ai_harness.planning import PlanItem

    results = {}

    async def model(messages, info):
        returned = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, (ToolReturnPart, RetryPromptPart))
        ]
        if not returned:
            yield {
                0: DeltaToolCall(
                    name="write_plan",
                    json_args='{"items":[{"content":"Hijacked","status":"pending"}]}',
                    tool_call_id="plan",
                ),
                1: DeltaToolCall(
                    name="delegate_task",
                    json_args='{"agent_name":"worker","task":"do it"}',
                    tool_call_id="delegate",
                ),
                2: DeltaToolCall(name="read_plan", json_args="{}", tool_call_id="read"),
            }
            return
        results.update({part.tool_call_id: part.model_response_str() for part in returned})
        yield "Answered anyway."

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        await runtime.plan_store.set_items([PlanItem(content="Real plan", status="pending")])
        assert (await runtime.aside("What is left?")).answer == "Answered anyway."
        assert [item.content for item in await runtime.plan_store.get_items()] == ["Real plan"]

    asyncio.run(run())
    assert "unavailable in a side question" in results["plan"]
    assert "unavailable in a side question" in results["delegate"]
    # Reading the plan is how a side question learns what the turn is doing.
    assert "Real plan" in results["read"]


def test_aside_guard_passes_everything_but_plan_writes_and_delegation():
    from pydantic_ai.exceptions import ToolFailed

    from pcode.aside_guard import AsideGuard

    guard = AsideGuard()

    async def handler(args):
        return "ran"

    async def call(name):
        return await guard.wrap_tool_execute(
            None,
            call=ToolCallPart(name, {}),
            tool_def=None,
            args={},
            handler=handler,
        )

    async def run():
        for name in ["shell", "write_file", "edit_file", "read_plan", "list_task_worktrees"]:
            assert await call(name) == "ran"
        for name in ["write_plan", "add_task", "update_task_statuses", "remove_task"]:
            with pytest.raises(ToolFailed):
                await call(name)
        for name in ["delegate_task", "integrate_task", "discard_task"]:
            with pytest.raises(ToolFailed):
                await call(name)

    asyncio.run(run())


def test_a_failed_side_question_writes_its_frames_to_errors_log(tmp_path):
    from pcode.sessions import SavedSession

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    forever = asyncio.Event()

    class Runtime:
        session = saved
        tree = None

        async def aside(self, question, report):
            if question == "stop me":
                await forever.wait()
            raise ValueError("provider refused")

    app = PreviewApp(
        model="test:local", runtime=Runtime(), console=Console(file=StringIO(), width=140)
    )

    async def run():
        await app.controller.start_aside("why this file?")
        await asyncio.sleep(0.05)
        failed = app.asides.items[0]
        assert failed.status == "failed"
        log = (saved.directory / "errors.log").read_text()
        assert f"run aside {failed.id}" in log
        assert "Side question (failed): why this file?" in log
        assert "ValueError: provider refused" in log
        assert "Traceback" in log
        # Stopping on purpose is not a defect worth a traceback.
        await app.controller.start_aside("stop me")
        await asyncio.sleep(0)
        app.asides.cancel()
        await app.asides.close()
        assert (saved.directory / "errors.log").read_text() == log

    asyncio.run(run())
    saved.close()


def test_aside_answers_while_a_turn_runs_and_records_nothing(tmp_path):
    (tmp_path / "sample.txt").write_text("workspace marker")
    blocked = asyncio.Event()
    released = asyncio.Event()
    side_tools: set[str] = set()
    main_tools: set[str] = set()
    side_requests = []

    async def model(messages, info):
        names = {tool.name for tool in info.function_tools}
        if (question := side_question(messages)) is not None:
            side_tools.update(names)
            prompts = [
                part.content
                for message in messages
                for part in message.parts
                if isinstance(part, UserPromptPart)
            ]
            side_requests.append(sum(isinstance(m, ModelRequest) for m in messages))
            # It sees the turn in flight, and its question comes last.
            assert prompts[0] == "Main task"
            assert prompts[-1] == framed(question)
            yield f"Answer: {question}"
            return
        main_tools.update(names)
        if not blocked.is_set():
            blocked.set()
            await released.wait()
            yield {0: DeltaToolCall(name="read_file", json_args='{"path":"sample.txt"}')}
        else:
            yield "Main answer."

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        async def turn():
            return [event async for event in runtime.stream("Main task")]

        main = asyncio.create_task(turn())
        await blocked.wait()
        reports = []
        answer = await runtime.aside(
            "Why this file?", report=lambda text, activity: reports.append((text, activity))
        )
        released.set()
        events = await main
        assert answer.answer == "Answer: Why this file?"
        assert reports[-1] == ("Answer: Why this file?", "")
        assert ("Answer: Why this file?", "Responding…") in reports
        assert Message("Main answer.") in events
        # Mid-turn the question joins the request in flight rather than following
        # it as a second user message, which providers reject.
        assert side_requests == [1]
        # Idle, the conversation ends on an answer, so it is asked normally.
        assert (await runtime.aside("And now?")).answer == "Answer: And now?"
        assert side_requests == [1, 3]
        # No node, no journal record, and the question never reaches history.
        assert len(runtime.tree.nodes) == 1
        prompts = [
            part.content
            for message in runtime.history
            for part in message.parts
            if isinstance(part, UserPromptPart)
        ]
        assert prompts == ["Main task"]
        # The conversation's own tools, so the request prefix stays cached.
        assert side_tools == main_tools
        # The tokens were really spent, so they are billed to the session.
        assert runtime.input_tokens > 0

    asyncio.run(run())


def test_asides_track_running_unread_failed_and_cancelled():
    async def run():
        asides = Asides()
        settled = []
        asides.on_settle = settled.append
        release = asyncio.Event()

        async def answer(aside):
            asides.update(aside, answer="partial", activity="Reading read_file")
            await release.wait()
            asides.update(aside, answer="done", activity="")

        async def fail(aside):
            raise ValueError("provider refused")

        first = asides.start("why", answer)
        await asyncio.sleep(0)
        assert asides.running == 1
        assert asides.unread == 0
        assert first.answer == "partial"
        assert asides.latest() is first
        asides.start("later", fail)
        await asyncio.sleep(0.05)
        assert [aside.status for aside in asides.items] == ["running", "failed"]
        assert "Run failed (ValueError)" in asides.items[1].error
        assert asides.unread == 1
        release.set()
        await asyncio.sleep(0.05)
        assert first.status == "answered"
        assert first.answer == "done"
        assert first.finished is not None
        assert asides.unread == 2
        assert asides.running == 0
        assert [aside.question for aside in settled] == ["later", "why"]
        # Reading the newest unread is what clears the footer count.
        assert asides.latest() is asides.items[1]
        asides.items[1].read = True
        assert asides.latest() is first

        forever = asyncio.Event()
        stopped = asides.start("stopped", lambda aside: forever.wait())
        await asyncio.sleep(0)
        assert asides.cancel() == 1
        await asides.close()
        assert stopped.status == "cancelled"

    asyncio.run(run())


def test_running_side_questions_get_muted_spinner_rows_above_the_editor():
    from pcode.aside import Aside
    from pcode.ui import ASIDE_ROWS, Activity

    activity = Activity()
    assert activity.aside_rows("⠋", 80) == []
    assert not activity.asides_running
    asides = Asides()
    activity.asides = asides.items
    running = Aside(question="why recursive descent?", activity="Reading parser.py")
    asides.items.append(running)
    asides.items.append(Aside(question="old", status="answered", activity=""))
    assert activity.asides_running
    rows = activity.aside_rows("⠋", 80)
    assert len(rows) == 1
    style, text = rows[0]
    assert style == "class:activity.aside"
    assert text.startswith("⠋ btw · why recursive descent? · Reading parser.py · ")
    assert text.endswith("s")
    # Narrow panes truncate rather than wrap so the editor keeps its rows.
    assert len(activity.aside_rows("⠋", 20)[0][1]) <= 20
    for index in range(ASIDE_ROWS + 1):
        asides.items.append(Aside(question=f"q{index}"))
    rows = activity.aside_rows("⠋", 80)
    assert len(rows) == ASIDE_ROWS
    assert rows[-1][1] == "… 3 more (/btw)"
    running.settle("answered")
    assert [text for _, text in activity.aside_rows("⠋", 80)][0].startswith("⠋ btw · q0")


def test_btw_command_requires_a_question_or_an_answer_to_read():
    preview = PreviewApp(console=Console(file=StringIO()))
    with pytest.raises(ValueError, match="No side questions yet"):
        asyncio.run(preview.controller.aside(""))
    with pytest.raises(ValueError, match="local UI preview"):
        asyncio.run(preview.controller.aside("why"))

    app = PreviewApp(
        model="test:local",
        runtime=AgentRuntime(Agent(TestModel())),
        console=Console(file=StringIO()),
    )
    # Unavailable commands are refused while working; this one is the exception.
    app.activity.busy = True
    app.activity.queued = 1
    start = app.controller.start_aside = AsyncMock()
    app.read_asides = AsyncMock(return_value=None)
    asyncio.run(app.controller.dispatch("/btw  why this file?  ", idle=False))
    start.assert_awaited_once_with("why this file?", [])
    app.asides.items.append(Aside(question="earlier"))
    asyncio.run(app.controller.dispatch("/btw", idle=False))
    app.read_asides.assert_awaited_once()


def test_btw_runs_in_the_background_and_reports_in_the_footer_and_transcript():
    async def run():
        output = StringIO()
        answered = asyncio.Event()

        class Runtime:
            session = None
            tree = None

            async def aside(self, question, report):
                report("", "Waiting for model…")
                await answered.wait()
                report(f"Answer to {question}", "")
                return f"Answer to {question}"

        app = PreviewApp(
            model="test:local",
            runtime=Runtime(),
            console=Console(file=output, color_system=None, width=140),
        )
        await app.controller.start_aside("why")
        await asyncio.sleep(0)
        assert app.asides.running == 1
        # A side question is not "working": input and the queue stay untouched.
        assert not app.activity.busy
        assert app.asides.items[0].activity == "Waiting for model…"
        answered.set()
        await asyncio.sleep(0.05)
        assert app.asides.items[0].answer == "Answer to why"
        assert app.asides.unread == 1
        await app.asides.close()
        text = " ".join(output.getvalue().split())
        assert "Asking beside the conversation: the turn keeps running" in text

    asyncio.run(run())


def test_btw_asks_and_reads_while_a_turn_is_running():
    """The real command path: neither asking nor reading waits for the turn."""
    save_preferences(btw_view="popup")  # The viewer this exercises.

    async def run():
        printed = StringIO()
        streaming = asyncio.Event()
        finish = asyncio.Event()
        answered = asyncio.Event()

        class Runtime:
            session = None
            tree = None

            async def stream(self, prompt):
                streaming.set()
                await finish.wait()
                yield Message("main answer")

            async def aside(self, question, report):
                report("", "Waiting for model…")
                await answered.wait()
                report("side answer", "")
                return "side answer"

        app = PreviewApp(
            model="test:local",
            runtime=Runtime(),
            console=Console(file=printed, color_system=None, width=140),
        )
        browsers = []

        class Browser:
            def __init__(self, asides, **options):
                self.asides = asides
                self.running = False
                browsers.append(self)

            async def run(self):
                self.running = True
                await asyncio.sleep(0.05)
                self.running = False

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
                pipe.send_text("do the work\r")
                await asyncio.wait_for(streaming.wait(), 5)
                pipe.send_text("/btw why this file?\r")
                await wait_for(lambda: app.asides.running == 1)
                # The turn is untouched: nothing queued, nothing cancelled.
                assert app.activity.queued_prompts == []
                assert "btw running" in "".join(
                    value for _, value in app.toolbar() if isinstance(value, str)
                )
                answered.set()
                await wait_for(lambda: app.asides.unread == 1)
                await wait_for(lambda: "Side answer ready" in printed.getvalue())
                pipe.send_text("/btw\r")
                await wait_for(lambda: bool(browsers))
                assert browsers[0].asides is app.asides
                await wait_for(lambda: not browsers[0].running)
                finish.set()
                await wait_for(lambda: "main answer" in printed.getvalue())
                pipe.send_text("/quit\r")

            with (
                patch("pcode.app.create_prompt", prompt),
                patch("pcode.aside_ui.AsideBrowser", Browser),
            ):
                await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=15)
        assert [aside.answer for aside in app.asides.items] == ["side answer"]

    asyncio.run(run())


def test_a_ready_side_answer_opens_the_viewer_unless_auto_open_is_off():
    """The answer arrives where the reader is looking, without a second /btw."""
    save_preferences(btw_view="popup")

    async def run():
        printed = StringIO()
        answered = asyncio.Event()

        class Runtime:
            session = None
            tree = None

            async def aside(self, question, report):
                await answered.wait()
                report(f"answer to {question}", "")
                return f"answer to {question}"

        app = PreviewApp(
            model="test:local",
            runtime=Runtime(),
            console=Console(file=printed, color_system=None, width=140),
        )
        browsers = []

        class Browser:
            def __init__(self, asides, **options):
                self.edit = options["edit"]
                browsers.append(self)

            async def run(self):
                await asyncio.sleep(0)

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
                pipe.send_text("/btw why this file?\r")
                await wait_for(lambda: app.asides.running == 1)
                answered.set()
                # Nobody typed a bare /btw: the settled answer opened the viewer.
                await wait_for(lambda: len(browsers) == 1)
                await wait_for(lambda: "Opening it." in printed.getvalue())
                # Opened by itself, it keeps out of the editor; asked for, it opens there.
                assert not browsers[0].edit
                pipe.send_text("/btw\r")
                await wait_for(lambda: len(browsers) == 2)
                assert browsers[1].edit
                save_preferences(btw_auto_open="off")
                answered.clear()
                pipe.send_text("/btw and this one?\r")
                await wait_for(lambda: app.asides.running == 1)
                answered.set()
                await wait_for(lambda: "/btw opens it." in printed.getvalue())
                await asyncio.sleep(0.05)
                assert len(browsers) == 2
                pipe.send_text("/quit\r")

            with (
                patch("pcode.app.create_prompt", prompt),
                patch("pcode.aside_ui.AsideBrowser", Browser),
            ):
                await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=15)

    asyncio.run(run())


def test_a_side_run_reports_the_main_status_rows_words(tmp_path):
    from pydantic_ai import (
        FunctionToolCallEvent,
        PartDeltaEvent,
        PartStartEvent,
        TextPart,
        ThinkingPart,
        ThinkingPartDelta,
        ToolCallPart,
    )

    async def events():
        yield PartStartEvent(index=0, part=ThinkingPart(content="hm"))
        yield PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" more"))
        yield FunctionToolCallEvent(ToolCallPart("shell", {"command": "ls"}, tool_call_id="a"))
        yield PartStartEvent(index=1, part=TextPart(content="Done"))

    runtime = AgentRuntime(Agent(TestModel(), capabilities=[create_coder(tmp_path)]))
    reports = []
    asyncio.run(runtime._aside_answer(events(), lambda _, activity: reports.append(activity)))
    # One report per change of phase: a thought's deltas are not news.
    assert reports == ["Thinking…", "Run shell · ls", "Responding…", ""]


def test_aside_browser_streams_the_answer_and_marks_it_read():
    from pcode.aside_ui import AsideBrowser

    asides = Asides()
    running = Aside(question="why this file?", answer="Because", activity="Read file · app.py")
    asides.items.append(running)
    browser = AsideBrowser(asides, output=None, input=None)

    def status():
        return "".join(text for _, text in browser.status_fragments())

    assert "why this file?" in browser.list.text
    assert "(running" in browser.list.text
    body = browser.detail.text(80)
    assert "why this file?" in body
    assert "Because" in body
    # Progress is the main prompt's status row, docked under the answer.
    assert "Read file" not in body
    assert "Read file · app.py" in status()
    assert ("class:activity.phase", "Read file") in browser.status_fragments()
    assert any(style == "class:activity.spinner" for style, _ in browser.status_fragments())
    # A running answer is not read yet, so the footer keeps announcing it.
    assert not running.read
    asides.update(running, answer="Because the task named it.", activity="")
    running.settle("answered")
    browser.refresh()
    assert "Because the task named it." in browser.detail.text(80)
    assert status() == ""
    assert running.read
    assert "(answered" in browser.list.text


def test_aside_browser_copies_the_selected_answer(monkeypatch):
    from pcode import aside_ui
    from pcode.aside_ui import AsideBrowser

    copies: list[str] = []

    def fake_copy(text, output=None):
        copies.append(text)
        return True, False

    monkeypatch.setattr(aside_ui, "copy_to_clipboard", fake_copy)
    asides = Asides()
    browser = AsideBrowser(asides, output=None, input=None)
    browser.copy()
    assert browser.notice == "No answer to copy"
    assert copies == []

    aside = Aside(question="why?", answer="Because **it** works.")
    aside.settle("answered")
    asides.items.append(aside)
    browser.refresh()
    browser.copy()
    assert copies == ["Because **it** works."]
    assert browser.notice == "Copied answer"

    monkeypatch.setattr(aside_ui, "copy_to_clipboard", lambda text, output=None: (False, False))
    browser.copy()
    assert browser.notice == "Could not copy answer"


def test_aside_copy_skips_answers_that_are_empty_after_sanitizing(monkeypatch):
    from pcode import aside_ui
    from pcode.aside_ui import AsideBrowser

    copies = []
    monkeypatch.setattr(
        aside_ui,
        "copy_to_clipboard",
        lambda text, output=None: (copies.append(text), (True, False))[1],
    )
    asides = Asides()
    first = Aside(question="control-only", answer="\x00\x1b")
    first.settle("answered")
    asides.items.append(first)
    browser = AsideBrowser(asides, output=None, input=None)
    browser.copy()
    assert browser.notice == "No answer to copy"
    assert not copies and browser.picker is None

    follow_up = Aside(question="readable", answer="real answer", thread=first.thread)
    follow_up.settle("answered")
    asides.items.append(follow_up)
    browser.refresh()
    browser.copy()
    assert copies == ["real answer"]
    assert browser.picker is None  # One usable answer bypasses the history picker.


def test_aside_browser_copies_through_the_copy_browser(monkeypatch):
    from pcode import aside_ui
    from pcode.aside_ui import AsideBrowser
    from pcode.tree_ui import CopyBrowser

    copies: list[str] = []
    monkeypatch.setattr(
        aside_ui,
        "copy_to_clipboard",
        lambda text, output=None: (copies.append(text), (True, False))[1],
    )
    asides = Asides()
    aside = Aside(question="how?", answer="Run this:\n\n```sh\nmake test\n```\n")
    aside.settle("answered")
    asides.items.append(aside)
    browser = AsideBrowser(asides, output=None, input=None)
    browser.copy()
    assert isinstance(browser.copying, CopyBrowser)
    assert browser.app.layout.has_focus(browser.copying.list)
    assert [label for _, label in browser.copying.rows] == [
        "user: how?",
        "assistant: Run this: ```sh make test ```",
        "  └─ Code (sh): make test",
    ]
    assert copies == []
    # The same browser as /copy: it starts on the newest response.
    assert browser.copying.chosen().kind == "response"
    browser.copying.list.buffer.cursor_down()
    browser.copying.accept()
    assert copies == ["make test"]
    assert browser.copying is None
    assert browser.notice == "Copied code"


def test_aside_browser_opens_a_link_from_the_selected_thread(monkeypatch):
    from pcode import aside_ui
    from pcode.aside_ui import AsideBrowser
    from pcode.links_ui import LinkPicker

    opened: list[str] = []
    monkeypatch.setattr(aside_ui, "open_link", opened.append)
    asides = Asides()
    browser = AsideBrowser(asides, output=None, input=None)
    browser.choose_link()
    assert browser.notice == "No links in this side thread"
    assert browser.picker is None

    aside = Aside(
        question="see https://q.test?", answer="Read [the docs](https://docs.test) first."
    )
    aside.settle("answered")
    asides.items.append(aside)
    browser.refresh()
    assert [link.url for link in browser.links()] == ["https://q.test", "https://docs.test"]
    browser.choose_link()
    assert isinstance(browser.picker, LinkPicker)
    # Newest first, so the answer's link is selected; typing filters.
    assert browser.picker.selected() == "https://docs.test"
    browser.picker.query.text = "q.test"
    assert browser.picker.selected() == "https://q.test"


@pytest.mark.parametrize(
    ("prefix", "copy_key", "link_key", "follow_up_key"),
    [("ctrl", "\x19", "\x0f", "\x12"), ("ctrl+p", "\x10y", "\x10o", "\x10r")],
)
def test_aside_browser_pickers_by_keyboard(monkeypatch, prefix, copy_key, link_key, follow_up_key):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from pcode import aside_ui
    from pcode.aside_ui import AsideBrowser

    opened: list[str] = []
    copies: list[str] = []
    monkeypatch.setattr(aside_ui, "open_link", opened.append)
    monkeypatch.setattr(
        aside_ui,
        "copy_to_clipboard",
        lambda text, output=None: (copies.append(text), (True, False))[1],
    )
    asides = Asides()
    aside = Aside(
        question="see https://q.test", answer="Docs: https://docs.test\n\n```sh\nmake test\n```"
    )
    aside.settle("answered")
    asides.items.append(aside)

    async def run():
        with create_pipe_input() as pipe:
            browser = AsideBrowser(
                asides,
                ask=lambda thread, question: None,
                key_prefix=prefix,
                input=pipe,
                output=DummyOutput(),
            )
            task = asyncio.create_task(browser.run())

            async def wait_until(condition):
                async with asyncio.timeout(5):
                    while not condition():
                        if task.done():
                            task.result()
                            pytest.fail("Aside browser exited before the expected state")
                        await asyncio.sleep(0.01)

            processed = 0

            def after_key_press(sender):
                nonlocal processed
                processed += 1

            browser.app.key_processor.after_key_press += after_key_press

            async def send(keys):
                # All inputs here are single-character keys, not escape sequences.
                # Wait for consumption even when an unavailable shortcut is inert.
                expected = processed + len(keys)
                pipe.send_text(keys)
                await wait_until(lambda: processed >= expected)

            await wait_until(lambda: browser.app.is_running)
            await send(link_key)
            await wait_until(lambda: browser.picker is not None)
            # Other shortcuts are inert under the picker: no follow-up editor opens.
            await send(follow_up_key)
            assert not browser.editing()
            if prefix != "ctrl":
                # An unavailable shortcut leaves the action menu pending.
                # Cancel it before typing into the nested link picker.
                await wait_until(lambda: browser.prefix_keys.pending)
                await send("\x03")
                await wait_until(lambda: not browser.prefix_keys.pending)
                assert browser.picker is not None
            await send("q.test\r")
            await wait_until(lambda: bool(opened) and browser.picker is None)
            assert opened == ["https://q.test"]
            # Either cancel key closes the copy browser, back where focus was.
            for cancel in ("\x1b", "\x03"):
                focus = browser.app.layout.current_control
                await send(copy_key)
                await wait_until(lambda: browser.copying is not None)
                await send(cancel)
                await wait_until(lambda: browser.copying is None)
                assert browser.app.layout.current_control is focus
                assert not copies and not task.done()
            await send(copy_key)
            await wait_until(lambda: browser.copying is not None)
            # Viewer shortcuts are inert under the copy browser too.
            await send(follow_up_key)
            assert not browser.editing() and browser.copying is not None
            if prefix != "ctrl":
                await wait_until(lambda: browser.prefix_keys.pending)
                await send("\x03")
                await wait_until(lambda: not browser.prefix_keys.pending)
            # It starts on the response; the code block is the row under it.
            pipe.send_text("\x1b[B")
            await wait_until(lambda: browser.copying.chosen().kind == "code")
            await send("\r")
            await wait_until(lambda: bool(copies) and browser.copying is None)
            assert copies == ["make test"]
            assert browser.notice == "Copied code"
            # Esc backs out of a picker, leaving the viewer open.
            await send(link_key)
            await wait_until(lambda: browser.picker is not None)
            await send("\x1b")
            await wait_until(lambda: browser.picker is None)
            assert not task.done()
            pipe.send_text("\x1b")
            assert await asyncio.wait_for(task, 5) is None

    asyncio.run(run())


def test_aside_copy_browser_holds_the_selected_thread(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from pcode import aside_ui
    from pcode.aside_ui import AsideBrowser

    copied = []
    monkeypatch.setattr(
        aside_ui,
        "copy_to_clipboard",
        lambda text, output=None: (copied.append(text), (True, False))[1],
    )
    asides = Asides()
    first = Aside(question="original question", answer="Older plain answer")
    code = Aside(
        question="shell question", answer="Run:\n```sh\nmake history\n```", thread=first.id
    )
    newest = Aside(question="latest question", answer="Newest answer", thread=first.id)
    other = Aside(question="unrelated question", answer="Excluded answer")
    for aside in (first, code, newest, other):
        aside.settle("answered")
        asides.items.append(aside)

    async def run():
        with create_pipe_input() as pipe:
            browser = AsideBrowser(
                asides,
                selected=first.id,
                ask=lambda thread, question: None,
                input=pipe,
                output=DummyOutput(),
            )
            task = asyncio.create_task(browser.run())

            async def wait_until(condition):
                async with asyncio.timeout(5):
                    while not condition():
                        if task.done():
                            task.result()
                            pytest.fail("Aside browser exited before the expected state")
                        await asyncio.sleep(0.01)

            processed = 0

            def after_key_press(sender):
                nonlocal processed
                processed += 1

            browser.app.key_processor.after_key_press += after_key_press

            async def send(keys):
                expected = processed + len(keys)
                pipe.send_text(keys)
                await wait_until(lambda: processed >= expected)

            try:
                await wait_until(lambda: browser.app.is_running)
                await send("\x19")
                await wait_until(lambda: browser.copying is not None)
                # Only the selected thread, oldest first, as /copy lists a branch.
                assert [label for _, label in browser.copying.rows] == [
                    "user: original question",
                    "assistant: Older plain answer",
                    "user: shell question",
                    "assistant: Run: ```sh make history ```",
                    "  └─ Code (sh): make history",
                    "user: latest question",
                    "assistant: Newest answer",
                ]
                # Ctrl+Y again copies the highlighted text, as in /copy.
                await send("\x19")
                await wait_until(lambda: browser.copying is None)
                assert copied == [newest.answer]
                assert browser.app.layout.has_focus(browser.list)

                # /copy from the editor puts the cursor back in the draft on cancel.
                await send("\x12")
                await wait_until(browser.editing)
                await send("/copy\r")
                await wait_until(lambda: browser.copying is not None)
                await send("\x1b")
                await wait_until(lambda: browser.copying is None)
                assert browser.app.layout.has_focus(browser.input.area)
                await send("unfinished follow-up")
                cursor = browser.input.area.buffer.cursor_position
                await send("\x19")
                await wait_until(lambda: browser.copying is not None)
                await send("\x03")
                await wait_until(lambda: browser.copying is None)
                assert browser.app.layout.has_focus(browser.input.area)
                assert browser.input.area.text == "unfinished follow-up"
                assert browser.input.area.buffer.cursor_position == cursor
                assert len(copied) == 1
                await send("\x15")

                # An unfinished answer is copyable, while an empty follow-up is skipped.
                live = Aside(question="still working", answer="Live partial", thread=first.id)
                asides.items.extend([live, Aside(question="no text yet", thread=first.id)])
                browser.refresh()
                await send("/copy\r")
                await wait_until(lambda: browser.copying is not None)
                labels = [label for _, label in browser.copying.rows]
                assert labels[-2:] == ["user: still working", "assistant: Live partial"]
                await send("\r")
                await wait_until(lambda: browser.copying is None)
                assert copied[-1] == "Live partial"
                assert browser.app.layout.has_focus(browser.input.area)
                assert not task.done()
            finally:
                if not task.done():
                    browser.app.exit()
                await asyncio.wait_for(task, 5)

    asyncio.run(run())


def test_aside_browser_enter_reads_a_thread_without_the_list():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from pcode.aside_ui import AsideBrowser

    asides = Asides()
    first = Aside(question="first?", answer="One.")
    first.settle("answered")
    asides.items.append(first)

    async def run():
        with create_pipe_input() as pipe:
            browser = AsideBrowser(asides, input=pipe, output=DummyOutput())
            # A lone thread has nothing to choose between, so no list.
            assert not browser.listing()
            assert browser.app.layout.has_focus(browser.detail)
            task = asyncio.create_task(browser.run())

            async def send(keys, wait=0.1):
                pipe.send_text(keys)
                await asyncio.sleep(wait)

            await asyncio.sleep(0.1)
            second = Aside(question="second?", answer="Two.")
            second.settle("answered")
            asides.items.append(second)
            await asyncio.sleep(0.5)
            # A second thread does not pull the list in beside the answer being
            # read; Esc brings it, and Enter reads the selected thread full width.
            assert not browser.listing()
            assert ("Esc", "Questions") in browser.help()
            await send("\x1b", wait=0.6)
            assert browser.listing() and not task.done()
            assert browser.app.layout.has_focus(browser.list)
            assert browser.selected == first.thread
            await send("\x1b[B")  # Down, to the second thread.
            assert browser.selected == second.thread
            await send("\x1b[A")  # Up, back to the first.
            assert browser.selected == first.thread
            await send("\r")
            assert not browser.listing() and not task.done()
            assert browser.app.layout.has_focus(browser.detail)
            assert "One." in browser.detail.text(80)
            # Esc steps back to the list, a second Esc closes.
            await send("\x1b", wait=0.6)
            assert browser.listing() and not task.done()
            assert browser.app.layout.has_focus(browser.list)
            pipe.send_text("\x1b")
            assert await asyncio.wait_for(task, 2) is None

    asyncio.run(run())


def test_side_is_an_alias_for_btw():
    app = PreviewApp(
        model="test:local",
        runtime=AgentRuntime(Agent(TestModel())),
        console=Console(file=StringIO()),
    )
    app.activity.busy = True
    start = app.controller.start_aside = AsyncMock()
    asyncio.run(app.controller.dispatch("/side why this file?", idle=False))
    start.assert_awaited_once_with("why this file?", [])


def test_tree_browser_is_read_only_while_a_turn_runs():
    from pcode.conversation_tree import ConversationTree
    from pcode.tree_ui import TreeBrowser

    tree = ConversationTree()
    tree.consume({"kind": "turn_started", "run_id": "a", "parent_id": None, "prompt": "question"})
    tree.consume({"kind": "Message", "markdown": "answer"})
    browser = TreeBrowser(tree, navigable=False, output=None, input=None)
    labels = " ".join(
        fragment_list_to_text(to_formatted_text(window.content.text))
        for window in browser.app.layout.find_all_windows()
        if hasattr(window.content, "text")
    )
    assert "read-only while working" in labels
    assert "Forking waits for the running turn" in labels
    exits = []
    browser.app.exit = lambda **kwargs: exits.append(kwargs)
    enter = next(
        binding for binding in browser.app.key_bindings.bindings if binding.keys == (Keys.ControlM,)
    )
    event = SimpleNamespace(app=browser.app)
    enter.handler(event)
    assert exits == []
    browser.navigable = True
    enter.handler(event)
    assert exits == [{"result": ("a", False)}]


def test_keys_typed_ahead_of_a_pickers_first_frame_reach_the_picker(monkeypatch):
    """A paste or typeahead lands before any redraw; Enter must open the link,
    not fall through to the viewer and close it."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from pcode import aside_ui
    from pcode.aside_ui import AsideBrowser

    opened: list[str] = []
    monkeypatch.setattr(aside_ui, "open_link", opened.append)
    asides = Asides()
    aside = Aside(question="see https://q.test", answer="Docs: https://docs.test")
    aside.settle("answered")
    asides.items.append(aside)

    async def run():
        with create_pipe_input() as pipe:
            browser = AsideBrowser(asides, key_prefix="ctrl", input=pipe, output=DummyOutput())
            task = asyncio.create_task(browser.run())
            async with asyncio.timeout(5):
                while not browser.app.is_running:
                    await asyncio.sleep(0.01)
                # One write: prompt_toolkit handles the whole batch before redrawing.
                pipe.send_text("\x0fq.test\r")
                while not opened and not task.done():
                    await asyncio.sleep(0.01)
            assert opened == ["https://q.test"]
            assert not task.done()
            pipe.send_text("\x1b")
            assert await asyncio.wait_for(task, 5) is None

    asyncio.run(run())
