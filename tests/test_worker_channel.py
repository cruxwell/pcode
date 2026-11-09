"""The parent-worker channel: ids, briefs, questions, messages, follow-ups, stops."""

import asyncio
import json

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from pcode.agent import create_coder
from pcode.live import AgentRuntime
from pcode.runtime import WorkerStatus
from pcode.worker_channel import WorkerHub, render_brief


def call(name, call_id, **args):
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)}


def request_text(messages) -> str:
    request = next(m for m in reversed(messages) if isinstance(m, ModelRequest))
    return "\n".join(
        str(part.content)
        for part in request.parts
        if isinstance(part, UserPromptPart | ToolReturnPart)
    )


def all_text(messages) -> str:
    return "\n".join(
        str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart | ToolReturnPart)
    )


def runtime(tmp_path, parent, child):
    """A real coder; `parent` and `child` are stream functions told apart by their tools."""

    async def model(messages, info):
        role = parent if "delegate_task" in {t.name for t in info.function_tools} else child
        async for chunk in role(messages, info):
            yield chunk

    return AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )


def run(rt, prompt="Go"):
    async def collect():
        return [event async for event in rt.stream(prompt)]

    return asyncio.run(asyncio.wait_for(collect(), 60))


@pytest.mark.parametrize("background", [False, True])
def test_a_workers_question_reaches_the_parent_and_its_answer_comes_back(tmp_path, background):
    seen: list[str] = []

    async def parent(messages, info):
        text = request_text(messages)
        seen.append(text)
        if "DONE with B" in text:
            yield "Final"
        elif "Answered worker w1" in text:
            yield "Waiting for the worker."
        elif "asks: A or B?" in text:
            yield call("answer_worker", "answer", worker_id="w1", answer="B")
        elif len(seen) == 1:
            yield call(
                "delegate_task",
                "delegate",
                agent_name="worker",
                task="Fix the parser",
                context="parser.py holds it",
                done_when="tests pass",
                run_in_background=background,
            )
        else:
            yield "Waiting for the worker."

    async def child(messages, info):
        text = all_text(messages)
        assert "ask_parent" in {t.name for t in info.function_tools}
        assert "## Context\nparser.py holds it" in text
        assert "## Done when\ntests pass" in text
        if "The parent answered: B" in text:
            yield "DONE with B"
        else:
            yield call("ask_parent", "ask", question="A or B?", fallback="A")

    events = run(runtime(tmp_path, parent, child))
    question = next(text for text in seen if "asks: A or B?" in text)
    assert "Worker w1 (Fix the parser) asks: A or B?" in question
    assert "proceed with: A" in question
    if not background:
        # The waiting delegate_task moved aside so the parent could answer.
        assert "A worker asked you a question" in question
    assert "[worker w1]" in seen[-1] and "DONE with B" in seen[-1]
    statuses = [e for e in events if isinstance(e, WorkerStatus)]
    assert any(s.question == "A or B?" and s.fallback == "A" for s in statuses)
    assert any(s.answered is True for s in statuses)
    assert statuses[-1].status == "done"


def test_a_background_worker_reads_parent_messages_before_its_next_step(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    released = asyncio.Event()
    seen: list[str] = []

    async def parent(messages, info):
        text = request_text(messages)
        seen.append(text)
        if "REPORT B" in text:
            yield "Final"
        elif "Queued for worker w1" in text:
            released.set()
            yield "Waiting."
        elif "Started in the background (worker w1" in text:
            yield call("message_worker", "msg", worker_id="w1", message="use B")
        else:
            yield call(
                "delegate_task",
                "delegate",
                agent_name="worker",
                task="Do it",
                run_in_background=True,
            )

    async def child(messages, info):
        if "[Message from your parent agent] use B" in all_text(messages):
            yield "REPORT B"
            return
        await released.wait()
        yield call("read_file", "read", path="notes.txt")

    run(runtime(tmp_path, parent, child))
    assert "REPORT B" in seen[-1]


def test_reports_feed_later_briefs_and_a_finished_worker_can_be_continued(tmp_path):
    seen: list[str] = []

    async def parent(messages, info):
        text = request_text(messages)
        seen.append(text)
        if "REPORT THREE" in text:
            yield "Final"
        elif "REPORT TWO" in text:
            yield call(
                "delegate_task",
                "third",
                agent_name="worker",
                task="Build it",
                include_reports=["w1"],
            )
        elif "REPORT ONE" in text:
            yield call(
                "delegate_task",
                "second",
                agent_name="worker",
                task="Follow up",
                continue_worker="w1",
            )
        else:
            yield call("delegate_task", "first", agent_name="worker", task="Survey it")

    async def child(messages, info):
        text = all_text(messages)
        if "Build it" in text:
            assert "Survey it" not in text  # A fresh worker, not the continued one.
            assert "## Reports from earlier workers" in text
            assert "### w1" in text and "REPORT TWO" in text
            yield "REPORT THREE"
        elif "Follow up" in text:
            # The follow-up joins the first run's own history.
            assert "Survey it" in text and "REPORT ONE" in str(messages)
            yield "REPORT TWO"
        else:
            yield "REPORT ONE"

    run(runtime(tmp_path, parent, child))
    assert seen[1].startswith("[worker w1]\nREPORT ONE") or "[worker w1]\nREPORT ONE" in seen[1]
    assert "[worker w1]\nREPORT TWO" in seen[2]
    assert "[worker w2]\nREPORT THREE" in seen[3]


def test_a_background_worker_can_be_stopped(tmp_path):
    forever = asyncio.Event()
    started = asyncio.Event()
    seen: list[str] = []

    async def parent(messages, info):
        text = request_text(messages)
        seen.append(text)
        if "stopped as requested" in text:
            yield "Final"
        elif "Stopping worker w1" in text:
            yield "Waiting."
        elif "Started in the background" in text:
            await started.wait()
            yield call("stop_worker", "stop", worker_id="w1")
        else:
            yield call(
                "delegate_task",
                "delegate",
                agent_name="worker",
                task="Spin",
                run_in_background=True,
            )

    async def child(messages, info):
        started.set()
        await forever.wait()
        yield "never"

    events = run(runtime(tmp_path, parent, child))
    assert "worker w1" in seen[-1]
    assert [e for e in events if isinstance(e, WorkerStatus)][-1].status == "stopped"


def test_the_hub_numbers_workers_and_refuses_bad_continuations():
    hub = WorkerHub()
    first = hub.claim("c1", {"agent_name": "worker", "task": "A"})
    assert first.id == "w1" and hub.claim("c1", {}) is first
    assert hub.get("1") is first
    with pytest.raises(ModelRetry, match="still running"):
        hub.claim("c2", {"agent_name": "worker", "continue_worker": "w1"})
    first.status = "done"
    with pytest.raises(ModelRetry, match="no history"):
        hub.claim("c2", {"agent_name": "worker", "continue_worker": "w1"})
    first.messages = ["history"]
    with pytest.raises(ModelRetry, match="is a 'worker'"):
        hub.claim("c2", {"agent_name": "explorer", "continue_worker": "w1"})
    first.workspace_mode = "isolated"
    with pytest.raises(ModelRetry, match="isolated"):
        hub.claim("c2", {"agent_name": "worker", "continue_worker": "w1"})
    with pytest.raises(ModelRetry, match="Known workers: w1"):
        hub.require("w9")


def test_questions_are_delivered_once():
    from pcode.worker_channel import Question

    hub = WorkerHub()
    handle = hub.claim("c1", {"agent_name": "worker", "task": "A", "purpose": "fixing"})
    handle.question = Question("Which?", "both")
    assert hub.has_undelivered()
    [message] = hub.take_questions()
    assert "w1 (fixing) asks: Which?" in message and "proceed with: both" in message
    assert not hub.has_undelivered() and hub.take_questions() == []


def test_a_brief_keeps_only_the_sections_it_was_given():
    assert render_brief("Do it") == "Do it"
    brief = render_brief("Do it", boundaries="not tests/", done_when="")
    assert brief == "Do it\n\n## Boundaries\nnot tests/"
