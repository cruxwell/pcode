import asyncio
import json

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaThinkingPart, DeltaToolCall, FunctionModel
from pydantic_ai_harness.subagents import SubAgent, SubAgents

from pcode.delegation import DelegationReporting, stream_child_activity
from pcode.live import AgentRuntime
from pcode.runtime import (
    ChildPlan,
    ChildText,
    Message,
    TextDelta,
    ToolStarted,
    ToolSummary,
    WorkerStatus,
)
from pcode.stream_display import present_stream_event
from pcode.ui import Activity
from pcode.worker_ui import WorkerBrowser, details, header, row, tokens
from pcode.workers import Workers


def started(call_id="d1", task="fix parser", **fields):
    return ToolStarted(
        "delegate_task", f"worker · {task}", call_id, agent="worker", task=task, **fields
    )


def test_worker_prose_reaches_the_viewer_but_never_the_transcript():
    async def child_model(messages, info):
        yield {0: DeltaThinkingPart(content="pondering")}
        yield "private worker "
        yield "prose"

    calls = 0

    async def parent_model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: DeltaToolCall(
                    name="delegate_task",
                    json_args=json.dumps({"agent_name": "explorer", "task": "Look around"}),
                    tool_call_id="parent-0",
                )
            }
        else:
            yield "Parent done"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=parent_model),
            capabilities=[
                SubAgents(
                    agents=[
                        SubAgent(Agent(FunctionModel(stream_function=child_model), name="explorer"))
                    ],
                    agent_folders=None,
                    event_stream_handler=stream_child_activity,
                ),
                DelegationReporting(),
            ],
        )
    )

    async def run():
        return [event async for event in runtime.stream("Explore")]

    events = asyncio.run(run())
    child = [e for e in events if isinstance(e, ChildText)]
    assert {e.call_id for e in child} == {"parent-0"}
    assert "".join(e.text for e in child if not e.thinking) == "private worker prose"
    assert "".join(e.text for e in child if e.thinking) == "pondering"
    visible = [e for e in events if isinstance(e, (TextDelta, Message))]
    assert all("private" not in getattr(e, "text", getattr(e, "markdown", "")) for e in visible)


def test_workers_fold_their_stream_in_order_and_settle():
    workers = Workers()
    workers.record(started())
    workers.record(ChildText("d1", "thinking", thinking=True, start=True))
    workers.record(ChildText("d1", "Hello ", start=True))
    workers.record(ChildText("d1", "there"))
    workers.record(ToolStarted("shell", "make test", "d1:c1", parent_call_id="d1"))
    workers.record(ToolSummary("shell", "make test → ok", call_id="d1:c1", parent_call_id="d1"))
    workers.record(ChildPlan("d1", [{"content": "Patch", "status": "in_progress"}]))
    workers.record(ChildText("other", "stray"))  # Not a known worker: ignored.
    worker = workers.latest()
    assert [(e.kind, e.text) for e in worker.entries[:2]] == [
        ("thinking", "thinking"),
        ("text", "Hello there"),
    ]
    assert isinstance(worker.entries[2].tool, ToolSummary)
    assert len(worker.entries) == 3 and worker.plan[0]["content"] == "Patch"
    assert worker.running and workers.running() == 1
    workers.record(ToolSummary("delegate_task", "worker · fix parser → Completed", call_id="d1"))
    assert worker.state() == "Done" and workers.running() == 0

    workers.record(started("d2", "second"))
    workers.end_turn()
    assert workers.get("d2").state() == "Interrupted"


def test_stream_display_routes_child_text_to_workers_only():
    activity = Activity()
    output = type("Output", (), {"app": type("App", (), {"invalidate": lambda self: None})()})()
    present_stream_event(
        ChildText("d1", "hi", start=True),
        output=output,
        transcript=None,
        activity=activity,
        present=lambda events: None,
    )
    assert activity.workers.items == []  # No delegate recorded yet, so nothing to attach to.
    activity.workers.record(started())
    present_stream_event(
        ChildText("d1", "hi", start=True),
        output=output,
        transcript=None,
        activity=activity,
        present=lambda events: None,
    )
    assert activity.workers.latest().entries[0].text == "hi"


def test_viewer_shows_plan_prose_and_calls_and_hides_thinking_by_default():
    workers = Workers()
    workers.record(started())
    workers.record(ChildPlan("d1", [{"content": "Patch parser", "status": "in_progress"}]))
    workers.record(ChildText("d1", "secret reasoning", thinking=True, start=True))
    workers.record(ChildText("d1", "Found **it**", start=True))
    workers.record(ToolStarted("read_file", "src/parser.py", "d1:c1", parent_call_id="d1"))
    worker = workers.latest()
    rendered = " ".join(
        str(getattr(b, "markup", b))
        for b in details(worker, code_theme="ansi_dark", show_thinking=False)
    )
    assert "Tasks 0/1" in rendered and "↺ Patch parser" in rendered
    assert "Found **it**" in rendered and "src/parser.py" in rendered
    assert "secret reasoning" not in rendered
    shown = " ".join(str(b) for b in details(worker, code_theme="ansi_dark", show_thinking=True))
    assert "secret reasoning" in shown

    with create_pipe_input() as pipe:
        browser = WorkerBrowser(workers, input=pipe, output=DummyOutput())
        assert browser.selected == "d1"
        assert "worker" in browser.list.text and "fix parser" in browser.list.text


def dashboard_workers() -> Workers:
    workers = Workers()
    arguments = json.dumps(
        {
            "agent_name": "worker",
            "task": "fix parser",
            "plan_step": "Patch parser",
            "model": "sonnet",
            "workspace_mode": "isolated",
        },
        indent=2,
    )
    workers.record(started(arguments=arguments))
    workers.record(WorkerStatus("d1", "w1", status="running", tokens=182_400))
    workers.record(started("d2", "second", arguments="not json"))
    workers.record(WorkerStatus("d2", "w2", tokens=900))
    return workers


def test_worker_status_folds_into_the_dashboard_fields():
    workers = dashboard_workers()
    worker = workers.get("d1")
    assert (worker.worker_id, worker.plan_step, worker.model, worker.workspace_mode) == (
        "w1",
        "Patch parser",
        "sonnet",
        "isolated",
    )
    assert worker.tokens == 182_400 and workers.tokens() == 183_300
    version = worker.version
    workers.record(WorkerStatus("d1", "w1", question="Which branch?", fallback="use main"))
    assert worker.version > version
    assert worker.state() == "? asks parent" and workers.waiting() == 1
    assert (worker.question, worker.fallback) == ("Which branch?", "use main")
    workers.record(WorkerStatus("d1", "w1", answered=False))
    assert worker.question == "" and worker.fallback == "" and workers.waiting() == 0
    assert worker.tokens == 182_400  # None fields leave values alone.

    workers.record(WorkerStatus("d1", "w1", status="done"))
    assert not worker.running and worker.state() == "Done"
    workers.record(WorkerStatus("d1", "w1", review="accepted"))
    assert worker.state() == "Done · reviewed"
    workers.record(WorkerStatus("d1", "w1", review="rejected"))
    assert worker.state() == "Done · rejected"
    workers.record(WorkerStatus("d2", "w2", status="stopped"))
    assert workers.get("d2").state() == "Stopped"
    workers.record(WorkerStatus("missing", "w9", tokens=5))  # Unknown worker: ignored.
    assert workers.get("d2").plan_step == ""  # Unparseable arguments leave options empty.


def test_dashboard_row_and_header_formatting():
    workers = dashboard_workers()
    line = row(workers.get("d1"))
    assert line.split()[:4] == ["w1", "Patch", "parser", "sonnet"]
    assert "182k" in line and " iso " in line and line.endswith("fix parser")
    assert row(workers.get("d2")).split()[:3] == ["w2", "—", "—"]
    assert [tokens(n) for n in (0, 950, 182_400, 1_250_000)] == ["—", "950", "182k", "1.2M"]
    assert header(workers) == "Agents · 2 this session · 2 running · 183k tokens"
    workers.record(WorkerStatus("d2", "w2", question="Ok?"))
    assert "· 1 waiting on answer ·" in header(workers)


def run_browser(workers: Workers, keys: str) -> tuple[WorkerBrowser, str | None]:
    async def run(browser):
        return await browser.run()

    with create_pipe_input() as pipe:
        browser = WorkerBrowser(workers, input=pipe, output=DummyOutput())
        pipe.send_text(keys)
        return browser, asyncio.run(run(browser))


def test_answer_and_message_keys_return_a_prompt_prefill():
    workers = dashboard_workers()
    workers.record(WorkerStatus("d1", "w1", question="Which branch?", fallback="main"))
    browser, prefill = run_browser(workers, "a")
    # The newest running worker (w2) is selected, but only w1 is asking.
    assert browser.current().worker_id == "w2" and prefill == "Answer for w1: "
    _, prefill = run_browser(workers, "m")
    assert prefill == "Message for w2: "
    _, prefill = run_browser(workers, "\r")
    assert prefill is None


def test_answer_without_a_question_stays_open_with_a_notice():
    workers = dashboard_workers()
    with create_pipe_input() as pipe:
        browser = WorkerBrowser(workers, input=pipe, output=DummyOutput())
        assert browser.asking() is None and browser.prefill("answer") is None
        assert browser.notice == "no question to answer"
        workers.record(WorkerStatus("d2", "w2", status="done"))
        assert browser.prefill("message") is None and browser.notice == "w2 has finished"


def test_report_key_jumps_to_the_last_prose():
    workers = Workers()
    workers.record(started())
    with create_pipe_input() as pipe:
        browser = WorkerBrowser(workers, input=pipe, output=DummyOutput())
        browser.show_report()
        assert browser.notice == "no report yet"
        workers.record(ChildText("d1", "Early note", start=True))
        workers.record(ToolStarted("read_file", "a.py", "d1:c1", parent_call_id="d1"))
        workers.record(ChildText("d1", "Final report", start=True))
        browser.select(force=True)
        browser.show_report()
        assert "Final report" in str(browser.detail.renderables[browser.detail._anchor].markup)


def test_a_continued_worker_counts_its_tokens_once():
    workers = Workers()
    workers.record(started("d1"))
    workers.record(WorkerStatus("d1", "w1", tokens=1000, status="done"))
    # Continued under a new call: its count already includes the first run.
    workers.record(started("d2"))
    workers.record(WorkerStatus("d2", "w1", tokens=1500, status="running"))
    workers.record(started("d3"))
    workers.record(WorkerStatus("d3", "w2", tokens=200))
    assert workers.tokens() == 1700
    assert workers.get("d1").state() == "Done" and workers.get("d2").running
