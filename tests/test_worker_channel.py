"""The parent-worker channel: ids, briefs, questions, messages, follow-ups, stops."""

import asyncio
import json

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import (
    CapabilityPart,
    ModelRequest,
    RetryPromptPart,
    ToolReturnPart,
    UserPromptPart,
)
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
        if isinstance(part, UserPromptPart | ToolReturnPart | RetryPromptPart | CapabilityPart)
    )


def all_text(messages) -> str:
    return "\n".join(
        str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart | ToolReturnPart | CapabilityPart)
    )


def runtime(tmp_path, parent, child):
    """A real coder; `parent` and `child` are stream functions told apart by their tools."""

    async def model(messages, info):
        role = parent if "task_worktree" in {t.name for t in info.function_tools} else child
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
            yield call("manage_worker", "answer", action="answer", worker_id="w1", text="B")
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
            yield call("manage_worker", "msg", action="message", worker_id="w1", text="use B")
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
            yield call("manage_worker", "stop", action="stop", worker_id="w1")
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


def test_a_finished_worker_can_be_continued_after_a_restart(tmp_path):
    from pcode.sessions import SavedSession

    root = tmp_path / "sessions"
    work = tmp_path / "work"
    work.mkdir()

    async def first_parent(messages, info):
        if "REPORT ONE" in request_text(messages):
            yield "Done"
        else:
            yield call("delegate_task", "first", agent_name="worker", task="Survey it")

    async def second_parent(messages, info):
        text = request_text(messages)
        if "REPORT TWO" in text:
            yield "Done"
        else:
            yield call(
                "delegate_task",
                "second",
                agent_name="worker",
                task="Follow up",
                continue_worker="w1",
            )

    async def child(messages, info):
        text = all_text(messages)
        if "Follow up" in text:
            # The follow-up joins the history saved before the restart.
            assert "Survey it" in text and "REPORT ONE" in str(messages)
            yield "REPORT TWO"
        else:
            yield "REPORT ONE"

    saved = SavedSession.create("test:local", work, root)
    identity = saved.info.id
    rt = runtime(work, first_parent, child)
    rt.session = saved
    try:
        run(rt)
    finally:
        saved.close()
    assert (saved.directory / "workers" / "w1.json").stat().st_mode & 0o777 == 0o600
    copy = SavedSession.fork(saved.directory)
    copy.close()
    assert (copy.directory / "workers" / "w1.json").is_file()

    # A fresh agent and hub, as after a restart.
    resumed = SavedSession.open(identity, root)
    rt = runtime(work, second_parent, child)
    rt.session = resumed
    try:
        run(rt, "Again")
    finally:
        resumed.close()
    worker = json.loads((resumed.directory / "workers" / "w1.json").read_text())
    assert worker["report"].endswith("REPORT TWO") and worker["status"] == "done"


def test_an_attached_hub_numbers_after_saved_workers_and_forgets_on_switch(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    hub = WorkerHub()
    hub.attach(tmp_path / "a")
    first = hub.claim("c1", {"agent_name": "worker", "task": "A"})
    first.status, first.messages = "done", []
    hub.save(first)
    running = hub.claim("c2", {"agent_name": "worker", "task": "B"})
    assert running.id == "w2"

    fresh = WorkerHub()
    fresh.attach(tmp_path / "a")
    assert fresh.claim("c3", {"agent_name": "worker"}).id == "w2"  # w2 never finished.
    assert fresh.get("w1").task == "A" and fresh.total_tokens() == 0

    other = WorkerHub()
    other.attach(tmp_path / "b")
    theirs = other.claim("b1", {"agent_name": "worker", "task": "B's"})
    theirs.status, theirs.tokens = "done", 50
    other.save(theirs)

    # A switch detaches the running w2: reachable by its call, not by its id.
    hub.attach(tmp_path / "b")
    assert hub.handles() == [] and hub.for_call("c2") is running
    assert hub.get("w1").task == "B's" and hub.total_tokens() == 0
    with pytest.raises(ModelRetry, match="Known workers: w1"):
        hub.require("w2")
    assert hub.claim("c4", {"agent_name": "worker"}).id == "w2"
    # The detached worker saves with the session that started it.
    running.status = "done"
    hub.save(running)
    assert (tmp_path / "a" / "workers" / "w2.json").is_file()
    assert json.loads((tmp_path / "b" / "workers" / "w1.json").read_text())["task"] == "B's"
    # Switching away after loading never takes the budget below zero.
    hub.attach(tmp_path / "a")
    assert hub.total_tokens() == 0 and hub.for_call("c2") is None
    assert hub.get("../a/workers/w1") is None


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


def test_a_refused_follow_up_leaves_the_worker_as_it_was(tmp_path):
    seen: list[str] = []

    async def parent(messages, info):
        text = request_text(messages)
        seen.append(text)
        if '"worker_id": "w1"' in text or "'worker_id': 'w1'" in text:
            yield "Final"
        elif "Unknown worker 'w9'" in text:
            yield call("manage_worker", "list", action="list")
        elif "REPORT ONE" in text:
            yield call(
                "delegate_task",
                "second",
                agent_name="worker",
                task="Follow up",
                continue_worker="w1",
                include_reports=["w9"],
            )
        else:
            yield call("delegate_task", "first", agent_name="worker", task="Survey it")

    async def child(messages, info):
        yield "REPORT ONE"

    run(runtime(tmp_path, parent, child))
    listing = seen[-1]
    assert '"status": "done"' in listing or "'status': 'done'" in listing
    assert "Survey it" in listing and "Follow up" not in listing


def test_the_hub_validates_before_claiming_and_releases_a_refused_claim():
    hub = WorkerHub()
    with pytest.raises(ModelRetry, match="Unknown worker 'w9'"):
        hub.claim("c1", {"agent_name": "worker", "include_reports": ["w9"]})
    assert hub.handles() == []
    first = hub.claim("c1", {"agent_name": "worker", "task": "A"})
    first.status, first.messages, first.review = "stopped", ["history"], "rejected"
    first.stop_requested = True
    resumed = hub.claim("c2", {"agent_name": "worker", "task": "B", "continue_worker": "w1"})
    assert resumed is first and first.running and not first.stop_requested
    assert first.claimed == 2 and first.review == ""
    hub.release(first)
    assert (first.status, first.review, first.call_id) == ("stopped", "rejected", "c1")
    assert hub.for_call("c1") is first and hub.for_call("c2") is None
    fresh = hub.claim("c3", {"agent_name": "worker", "task": "C"})
    hub.release(fresh)
    assert hub.get(fresh.id) is None


def test_a_workers_tokens_include_its_final_response(tmp_path):
    async def parent(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "Final"
        else:
            yield call("delegate_task", "first", agent_name="worker", task="Survey it")

    async def child(messages, info):
        yield "REPORT ONE"

    events = run(runtime(tmp_path, parent, child))
    final = [e for e in events if isinstance(e, WorkerStatus) and e.status == "done"][-1]
    assert final.tokens and final.tokens > 0


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"action": "answer"}, "action='answer' needs a worker_id"),
        ({"action": "message", "worker_id": "w1"}, "action='message' needs text"),
    ],
)
def test_manage_worker_says_which_argument_is_missing(tmp_path, args, error):
    seen: list[str] = []

    async def parent(messages, info):
        text = request_text(messages)
        seen.append(text)
        if "needs" in text:
            yield "Final"
        else:
            yield call("manage_worker", "manage", **args)

    async def child(messages, info):
        yield "unused"

    run(runtime(tmp_path, parent, child))
    assert error in seen[-1]


def test_list_reports_a_live_worker_mid_model_call_as_live(tmp_path):
    in_model_call = asyncio.Event()
    release = asyncio.Event()
    listings: list[str] = []

    async def parent(messages, info):
        text = request_text(messages)
        if "REPORT DONE" in text:
            yield "Final"
        elif "liveness" in text:
            listings.append(text)
            release.set()
            yield "Waiting."
        elif "Started in the background" in text:
            await in_model_call.wait()
            yield call("manage_worker", "list", action="list")
        else:
            yield call(
                "delegate_task",
                "delegate",
                agent_name="worker",
                task="Survey it",
                run_in_background=True,
            )

    async def child(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            # A long model call: no events reach the parent until it answers.
            in_model_call.set()
            await release.wait()
            yield "REPORT DONE"
        else:
            yield call("list_files", "look", path=".")

    run(runtime(tmp_path, parent, child))
    (listing,) = listings
    # The tool return reaches the model as the list's repr.
    assert "'tool_calls': 1" in listing
    assert "'tokens': None" in listing
    assert "'running_tools': []" in listing
    assert "In a model call" in listing and "Live: keep waiting" in listing


def test_liveness_flags_a_long_silence_and_running_tools(monkeypatch):
    from pcode import worker_channel
    from pcode.worker_channel import QUEUED, SETTING_UP, STALL_SECONDS, Question, WorkerHandle

    clock = [1000.0]
    monkeypatch.setattr(worker_channel, "monotonic", lambda: clock[0])
    handle = WorkerHandle(id="w1", call_id="c1", task="Review")
    handle.mark_started()
    handle.running_tools = ["run_command"]
    clock[0] += 30
    summary = handle.summary()
    assert summary["elapsed_seconds"] == 30 and summary["seconds_since_progress"] == 30
    assert summary["liveness"].startswith("Running run_command; last progress 30s ago.")
    assert "keep waiting" in summary["liveness"]

    handle.running_tools = []
    clock[0] += STALL_SECONDS
    assert "may be stuck; tell the user" in handle.summary()["liveness"]

    handle.activity = SETTING_UP
    assert handle.summary()["liveness"].startswith(f"{SETTING_UP}; last progress")

    # A queued worker waits on others: never called stuck, however long.
    handle.activity = QUEUED
    assert "stuck" not in handle.summary()["liveness"]

    handle.question = Question(text="Which branch?")
    assert "worker_id='w1'" in handle.summary()["liveness"]

    handle.status = "done"
    assert "liveness" not in handle.summary()


def rate_limited(retry_after=None):
    from pydantic_ai.exceptions import ModelHTTPError

    return ModelHTTPError(429, "test-model", retry_after=retry_after)


def test_a_worker_that_fails_partway_keeps_its_history_and_can_be_resumed(tmp_path):
    (tmp_path / "notes.txt").write_text("NOTE CONTENT")
    seen: list[str] = []

    async def parent(messages, info):
        text = request_text(messages)
        seen.append(text)
        if "REPORT RESUMED" in text:
            yield "Final"
        elif "continue_worker='w1'" in text:
            yield call(
                "delegate_task",
                "second",
                agent_name="worker",
                task="Carry on",
                continue_worker="w1",
            )
        else:
            yield call("delegate_task", "first", agent_name="worker", task="Survey it")

    async def child(messages, info):
        text = all_text(messages)
        if "Carry on" in text:
            # The resumed run carries the failed run's prompt and tool result.
            assert "Survey it" in text and "NOTE CONTENT" in text
            assert "Your last run was cut off (failed)" in text
            yield "REPORT RESUMED"
        elif "NOTE CONTENT" in text:
            # Asked to wait longer than a worker does: it fails at once.
            raise rate_limited(retry_after=3600)
        else:
            yield call("read_file", "read", path="notes.txt")

    events = run(runtime(tmp_path, parent, child))
    failure = next(text for text in seen if "continue_worker='w1'" in text)
    assert "Worker w1 kept its history" in failure
    assert "[worker w1]\nREPORT RESUMED" in seen[-1]
    failed = [e for e in events if isinstance(e, WorkerStatus) and e.status == "failed"]
    # The failed run's one answered request still counts.
    assert failed and failed[-1].tokens and failed[-1].tokens > 0


def test_a_rate_limited_worker_waits_and_resends(tmp_path, monkeypatch):
    from pcode import delegation

    monkeypatch.setattr(
        delegation, "rate_limit_wait", lambda error, waits: 0.0 if waits < 1 else None
    )
    calls = 0

    async def parent(messages, info):
        if "REPORT" in request_text(messages):
            yield "Final"
        else:
            yield call("delegate_task", "first", agent_name="worker", task="Survey it")

    async def child(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise rate_limited()
        yield "REPORT"

    events = run(runtime(tmp_path, parent, child))
    assert calls == 2
    assert [e for e in events if isinstance(e, WorkerStatus)][-1].status == "done"


def test_rate_limit_wait_rules():
    from pydantic_ai.exceptions import ModelHTTPError

    from pcode.delegation import RATE_LIMIT_BACKOFF, rate_limit_wait

    assert 0.8 * 15 <= rate_limit_wait(rate_limited(), 0) <= 1.2 * 15
    assert rate_limit_wait(rate_limited(5), 0) == 5
    assert rate_limit_wait(rate_limited(0), 0) == 1.0
    assert rate_limit_wait(rate_limited(3600), 0) is None
    assert rate_limit_wait(rate_limited(), len(RATE_LIMIT_BACKOFF)) is None
    assert rate_limit_wait(ModelHTTPError(529, "m"), 0) is not None
    assert rate_limit_wait(ModelHTTPError(400, "m"), 0) is None
    quota = ModelHTTPError(429, "m", body={"error": {"code": "insufficient_quota"}})
    assert rate_limit_wait(quota, 0) is None
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = rate_limited(5)
    assert rate_limit_wait(wrapped, 0) == 5
    assert rate_limit_wait(ValueError("x"), 0) is None


def test_a_worker_stopped_while_waiting_out_a_rate_limit_keeps_its_history(tmp_path, monkeypatch):
    from pcode import delegation

    (tmp_path / "notes.txt").write_text("NOTE CONTENT")
    monkeypatch.setattr(delegation, "rate_limit_wait", lambda error, waits: 3600.0)
    limited = asyncio.Event()
    seen: list[str] = []

    async def parent(messages, info):
        text = request_text(messages)
        seen.append(text)
        if "stopped as requested" in text:
            yield "Final"
        elif "Stopping worker w1" in text:
            yield "Waiting."
        elif "Started in the background" in text:
            await limited.wait()
            yield call("manage_worker", "stop", action="stop", worker_id="w1")
        else:
            yield call(
                "delegate_task",
                "delegate",
                agent_name="worker",
                task="Survey",
                run_in_background=True,
            )

    async def child(messages, info):
        if "NOTE CONTENT" in all_text(messages):
            limited.set()
            raise rate_limited()
        yield call("read_file", "read", path="notes.txt")

    run(runtime(tmp_path, parent, child))
    assert "continue_worker='w1'" in seen[-1]


def test_contained_crash_writes_to_errors_log_and_records_inherited_model(tmp_path):
    from pcode.sessions import SavedSession

    root = tmp_path / "sessions"
    work = tmp_path / "work"
    work.mkdir()

    async def parent(messages, info):
        if "continue_worker='w1'" in request_text(messages) or "crashed:" in request_text(messages):
            yield "Done"
        else:
            yield call("delegate_task", "first", agent_name="worker", task="Survey it")

    async def child(messages, info):
        raise rate_limited(retry_after=3600)
        yield "unreachable"  # pragma: no cover

    saved = SavedSession.create("test:local", work, root)
    rt = runtime(work, parent, child)
    rt.session = saved
    try:
        run(rt)
    finally:
        saved.close()

    errors_log = (saved.directory / "errors.log").read_text()
    assert "run worker:w1" in errors_log
    assert "ModelHTTPError" in errors_log
    worker = json.loads((saved.directory / "workers" / "w1.json").read_text())
    assert worker["status"] == "failed"
    assert worker["model"]


def test_manage_worker_list_includes_saved_workers_after_restart(tmp_path):
    from pcode.sessions import SavedSession

    root = tmp_path / "sessions"
    work = tmp_path / "work"
    work.mkdir()

    async def first_parent(messages, info):
        if "REPORT ONE" in request_text(messages):
            yield "Done"
        else:
            yield call(
                "delegate_task",
                "first",
                agent_name="worker",
                task="Survey it",
                purpose="initial survey",
            )

    seen_listings: list[str] = []

    async def second_parent(messages, info):
        text = request_text(messages)
        if "REPORT TWO" in text:
            yield call("manage_worker", "list2", action="list")
        elif "initial survey" in text and "second pass" in text:
            seen_listings.append(text)
            yield "Final"
        elif "initial survey" in text:
            seen_listings.append(text)
            yield call(
                "delegate_task",
                "second",
                agent_name="worker",
                task="Follow up",
                purpose="second pass",
            )
        else:
            yield call("manage_worker", "list1", action="list")

    async def child(messages, info):
        if "Follow up" in all_text(messages):
            yield "REPORT TWO"
        else:
            yield "REPORT ONE"

    saved = SavedSession.create("test:local", work, root)
    identity = saved.info.id
    rt = runtime(work, first_parent, child)
    rt.session = saved
    try:
        run(rt)
    finally:
        saved.close()

    reopened = SavedSession.open(identity, root)
    rt2 = runtime(work, second_parent, child)
    rt2.session = reopened
    try:
        run(rt2, "Check workers")
    finally:
        reopened.close()

    assert len(seen_listings) == 2
    assert "'worker_id': 'w1'" in seen_listings[0]
    assert "'status': 'done'" in seen_listings[0]
    assert "'worker_id': 'w1'" in seen_listings[1]
    assert "'worker_id': 'w2'" in seen_listings[1]
    assert seen_listings[1].index("'worker_id': 'w1'") < seen_listings[1].index("'worker_id': 'w2'")


def test_resume_hint_for_isolated_worker_names_worktree_task_id():
    from pcode.worker_channel import WorkerHandle, resume_hint

    with_task = WorkerHandle(
        id="w3", call_id="c3", workspace_mode="isolated", worktree_task_id="abc123"
    )
    hint = resume_hint(with_task)
    assert "w3" in hint
    assert "abc123" in hint
    assert "task-abc123" in hint
    assert "task_worktree(action='list')" in hint

    without_task = WorkerHandle(id="w4", call_id="c4", workspace_mode="isolated")
    assert resume_hint(without_task) == ""
