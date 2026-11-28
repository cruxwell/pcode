import asyncio
import json

import pytest
from pydantic_ai import Agent, CallToolsNode, ModelRetry, RunContext
from pydantic_ai._run_context import dispatch_event_stream
from pydantic_ai.exceptions import CallDeferred, RunCancelled, ToolFailed
from pydantic_ai.messages import CapabilityPart, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness._background_completion import make_background_result
from pydantic_ai_harness.background_tools import BackgroundTools
from pydantic_ai_harness.background_tools import _capability as upstream
from pydantic_ai_harness.sub_agents import SubAgent, SubAgents

from pcode.background_delegation import BackgroundDelegation, _failure, _shown
from pcode.claude_sdk import ClaudeConnectionError
from pcode.delegation import DelegationReporting, stream_child_activity
from pcode.live import AgentRuntime
from pcode.runtime import Message, RunStatus, ToolSummary


def delegate():
    args = {"agent_name": "explorer", "task": "Read the logs"}
    return DeltaToolCall(name="delegate_task", json_args=json.dumps(args), tool_call_id="call-1")


def last_request_text(messages) -> str:
    request = next(m for m in reversed(messages) if isinstance(m, ModelRequest))
    return "\n".join(
        str(part.content)
        for part in request.parts
        if isinstance(part, UserPromptPart | ToolReturnPart | CapabilityPart)
    )


class Harness:
    """A parent delegating to a child held open by `gate`, with steerable input."""

    def __init__(self):
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        # Holds the child's second call, once `gate` has released its first.
        self.second_gate = asyncio.Event()
        self.second_gate.set()
        self.stopped = asyncio.Event()
        self.steering: list[str] = []
        self.requests: list[str] = []
        # Errors the parent model raises, once each, on its next requests.
        self.parent_errors: list[BaseException] = []

        async def hold() -> str:
            self.started.set()
            try:
                await self.gate.wait()
            finally:
                self.stopped.set()
            return "child evidence"

        async def hold_again() -> str:
            await self.second_gate.wait()
            return "more evidence"

        async def child_model(messages, info):
            done = {p.tool_name for m in messages for p in m.parts if isinstance(p, ToolReturnPart)}
            if "hold_again" in done:
                yield "CHILD REPORT"
            elif "hold" in done:
                yield {0: DeltaToolCall(name="hold_again", json_args="{}", tool_call_id="again")}
            else:
                yield {0: DeltaToolCall(name="hold", json_args="{}", tool_call_id="held")}

        async def parent_model(messages, info):
            text = last_request_text(messages)
            self.requests.append(text)
            if self.parent_errors and len(self.requests) > 1:
                raise self.parent_errors.pop(0)
            if "Next turn" in text:
                yield "Noted"
            elif "CHILD REPORT" in text:
                yield "Final answer"
            elif "Second?" in text:
                yield "Second answer"
            elif "Status?" in text:
                yield "Still working on it"
            else:
                yield {0: delegate()}

        child = Agent(
            FunctionModel(stream_function=child_model), name="explorer", tools=[hold, hold_again]
        )
        self.runtime = AgentRuntime(
            Agent(
                FunctionModel(stream_function=parent_model),
                capabilities=[
                    SubAgents(
                        agents=[SubAgent(child)],
                        agent_folders=None,
                        event_stream_handler=stream_child_activity,
                    ),
                    DelegationReporting(),
                    BackgroundDelegation(),
                ],
            )
        )
        self.runtime.has_steering = lambda: bool(self.steering)
        self.runtime.take_steering = self.take

    def take(self) -> list[str]:
        taken, self.steering[:] = list(self.steering), []
        return taken


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def test_steering_reaches_the_parent_while_its_delegate_keeps_working():
    async def run():
        h = Harness()
        events = []

        async def consume():
            async for event in h.runtime.stream("Explore"):
                events.append(event)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(h.started.wait(), 5)
        h.steering.append("Status?")
        # The parent answers while the child is still held open.
        await wait_for(lambda: any(isinstance(e, Message) for e in events))
        assert not h.gate.is_set() and not h.stopped.is_set()
        assert "moved to the background" in h.requests[1] and "Status?" in h.requests[1]
        delegate_ends = [e for e in events if isinstance(e, ToolSummary) and not e.parent_call_id]
        assert delegate_ends == []

        # The model ended its response; the run waits for the child, and more
        # steering still gets an answer without waiting for it.
        await wait_for(
            lambda: any(isinstance(e, RunStatus) and "sub-agent" in e.text for e in events)
        )
        h.steering.append("Second?")
        await wait_for(lambda: len(h.requests) == 3)
        assert "Second?" in h.requests[2]
        assert not h.stopped.is_set()

        # While the run waits, the child's progress still reaches the screen.
        h.second_gate.clear()
        h.gate.set()
        await wait_for(
            lambda: any(isinstance(e, ToolSummary) and e.call_id == "call-1:held" for e in events)
        )
        assert len(h.requests) == 3 and not task.done()
        h.second_gate.set()
        await asyncio.wait_for(task, 5)
        assert '<tool_result tool_name="delegate_task" tool_call_id="call-1"' in h.requests[3]
        assert [e.markdown for e in events if isinstance(e, Message)] == [
            "Still working on it",
            "Second answer",
            "Final answer",
        ]
        (end,) = [e for e in events if isinstance(e, ToolSummary) and e.call_id == "call-1"]
        assert end.outcome == "ok" and not end.failed and "Completed" in end.detail
        assert "CHILD REPORT" in end.result

    asyncio.run(run())


def test_without_steering_a_delegation_returns_in_the_foreground():
    async def run():
        h = Harness()
        task = asyncio.create_task(_collect(h.runtime.stream("Explore")))
        await asyncio.wait_for(h.started.wait(), 5)
        h.gate.set()
        events = await asyncio.wait_for(task, 5)
        assert len(h.requests) == 2
        assert "CHILD REPORT" in h.requests[1]
        assert "<tool_result" not in h.requests[1]
        (end,) = [e for e in events if isinstance(e, ToolSummary) and e.call_id == "call-1"]
        assert end.outcome == "ok"

    asyncio.run(run())


def test_cancelling_the_turn_cancels_a_detached_child():
    async def run():
        h = Harness()
        events = []

        async def consume():
            async for event in h.runtime.stream("Explore"):
                events.append(event)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(h.started.wait(), 5)
        h.steering.append("Status?")
        await wait_for(lambda: any(isinstance(e, Message) for e in events))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert h.stopped.is_set()
        assert len(h.requests) == 2
        assert h.runtime.context.abandoned_delegations == ["call-1"]

        # The model was promised a result; the next turn says it won't come.
        await _collect(h.runtime.stream("Next turn"))
        assert "call-1 was stopped before it reported" in h.requests[-1]
        assert h.runtime.context.abandoned_delegations == []

    asyncio.run(run())


def test_a_retried_turn_learns_its_detached_child_was_lost(monkeypatch):
    monkeypatch.setattr("pcode.retries.RETRY_DELAY", 0)

    async def run():
        h = Harness()
        h.parent_errors.append(ClaudeConnectionError("test", "Connection dropped (ECONNRESET)"))
        task = asyncio.create_task(_collect(h.runtime.stream("Explore")))
        await asyncio.wait_for(h.started.wait(), 5)
        h.steering.append("Status?")
        events = await asyncio.wait_for(task, 5)
        # The failed run took its child down with it; the retry is told so,
        # and does not wait for a result that cannot come.
        assert h.stopped.is_set()
        assert "call-1 was stopped before it reported" in h.requests[-1]
        assert "Status?" in h.requests[-1]
        assert [e.markdown for e in events if isinstance(e, Message)] == ["Still working on it"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "error, detail",
    [
        (ModelRetry("logs unreadable"), "logs unreadable"),
        (RunCancelled("stopped by the child"), "cancelled"),
        (CallDeferred(), "CallDeferred"),
        (ToolFailed("guard refused"), "guard refused"),
    ],
)
def test_a_detached_failure_the_call_would_have_reported_is_a_message(error, detail):
    completion = _failure("call-1", error, 3, (1, 0))
    assert completion.retry is None and not completion.succeeded
    result = completion.result
    assert (result.tool_call_id, result.outcome) == ("call-1", "failed")
    assert detail in _shown(completion) and "tool_result" not in _shown(completion)


def test_a_detached_crash_still_ends_the_run():
    crash = RuntimeError("bug")
    assert _failure("call-1", crash, 3, (1, 0)) is crash


async def _collect(stream):
    return [event async for event in stream]


def test_upstream_internals_this_relies_on():
    """`BackgroundDelegation` reaches into these; a Harness upgrade must fail here first."""
    assert {"_task_group", "_live", "_send", "_outcomes"} <= {
        f for f in BackgroundTools.__dataclass_fields__
    }
    assert callable(BackgroundTools._arrived)
    assert callable(BackgroundTools._background_mode)
    assert callable(BackgroundTools._deliver)
    for name in (
        "_Completion",
        "_format_background_failure",
        "_format_background_result",
        "_position_in_response",
    ):
        assert callable(getattr(upstream, name))
    # Built positionally here, with `retry` and `succeeded` by keyword.
    fields = list(upstream._Completion.__dataclass_fields__)
    assert fields[:4] == ["tool_name", "max_retries", "result", "dispatched"]
    assert {"retry", "succeeded"} <= set(fields)
    assert callable(make_background_result)
    # The end-of-run wait reads which node a stream belongs to and drains its buffer.
    assert "_next_node" in CallToolsNode.__dataclass_fields__
    assert "_event_stream_buffer" in RunContext.__dataclass_fields__
    assert callable(dispatch_event_stream)


def test_abandoned_message_formats_rich_entries_and_preserves_plain_strings():
    from pcode.background_delegation import abandoned_message

    plain = abandoned_message(["call-1", "call-2"])
    assert "The background delegation call-1, call-2 was stopped before it reported" in plain
    assert "Delegate again if you still need it" in plain

    resumable = abandoned_message(
        [
            {
                "task_id": "call-3",
                "worker_id": "w2",
                "purpose": "fix parser",
                "workspace_mode": "shared",
                "resumable": True,
                "worktree_task_id": "",
                "detached": True,
            }
        ]
    )
    assert "worker w2 (fix parser)" in resumable
    assert "delegate_task(continue_worker='w2', task=...)" in resumable

    isolated = abandoned_message(
        [
            {
                "task_id": "call-4",
                "worker_id": "w3",
                "purpose": "",
                "workspace_mode": "isolated",
                "resumable": False,
                "worktree_task_id": "deadbeef",
                "detached": False,
            }
        ]
    )
    assert "worker w3" in isolated
    assert "deadbeef" in isolated
    assert "task-deadbeef" in isolated
    assert "task_worktree(action='list')" in isolated

    unresumable = abandoned_message(
        [
            {
                "task_id": "call-5",
                "worker_id": "w4",
                "purpose": "quick check",
                "workspace_mode": "shared",
                "resumable": False,
                "worktree_task_id": "",
                "detached": True,
            }
        ]
    )
    assert "worker w4 (quick check)" in unresumable
    assert "Delegate again if you still need it" in unresumable
    assert "continue_worker" not in unresumable


@pytest.mark.parametrize("background", [False, True])
def test_cancelled_worker_abandoned_delegation_persists_across_restore_and_navigate(
    tmp_path, background
):
    from pcode.agent import create_coder
    from pcode.sessions import SavedSession

    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.txt").write_text("NOTE CONTENT\n")
    sessions_root = tmp_path / "sessions"

    blocked = asyncio.Event()
    requests: list[str] = []

    def make_runtime(session):
        async def model(messages, info):
            if "task_worktree" in {t.name for t in info.function_tools}:
                text = last_request_text(messages)
                requests.append(text)
                if "Continue" in text or "delegate_task(continue_worker='w1'" in text:
                    yield "Recovered"
                elif "Started in the background" in text:
                    yield "Waiting on worker"
                else:
                    args = {
                        "agent_name": "worker",
                        "task": "Read notes",
                        "purpose": "survey notes",
                        "run_in_background": background,
                    }
                    yield {
                        0: DeltaToolCall(
                            name="delegate_task",
                            json_args=json.dumps(args),
                            tool_call_id="call-w1",
                        )
                    }
            else:
                done = {
                    p.tool_name for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
                }
                if "read_file" in done:
                    blocked.set()
                    await asyncio.Event().wait()
                    yield "Unreachable"
                else:
                    yield {
                        0: DeltaToolCall(
                            name="read_file",
                            json_args=json.dumps({"path": "notes.txt"}),
                            tool_call_id="read-1",
                        )
                    }

        return AgentRuntime(
            Agent(FunctionModel(stream_function=model), capabilities=[create_coder(work)]),
            session,
        )

    async def scenario():
        saved = SavedSession.create("test:local", work, sessions_root)
        identity = saved.info.id
        rt1 = make_runtime(saved)
        try:
            task = asyncio.create_task(_collect(rt1.stream("Start")))
            await asyncio.wait_for(blocked.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            cancelled_run_id = rt1.tree.active
            assert len(rt1.context.abandoned_delegations) == 1
            entry = rt1.context.abandoned_delegations[0]
            assert isinstance(entry, dict)
            assert entry["worker_id"] == "w1"
            assert entry["purpose"] == "survey notes"
            assert entry["workspace_mode"] == "shared"
            assert entry["resumable"] is True
            assert entry["detached"] is background
        finally:
            saved.close()

        worker_disk = json.loads((saved.directory / "workers" / "w1.json").read_text())
        assert worker_disk["status"] == "stopped"
        assert worker_disk["report"] == "Interrupted before it reported."
        assert worker_disk["model"]
        assert worker_disk["messages"]

        reopened = SavedSession.open(identity, sessions_root)
        rt2 = make_runtime(reopened)
        try:
            await rt2.restore()
            assert rt2.context.abandoned_delegations == [entry]

            await _collect(rt2.stream("Continue"))
            assert "worker w1 (survey notes)" in requests[-1]
            assert "delegate_task(continue_worker='w1', task=...)" in requests[-1]
            assert rt2.context.abandoned_delegations == []

            # Navigating back to the cancelled turn re-seeds abandoned_delegations;
            # navigating to conversation start clears them.
            await rt2.navigate(cancelled_run_id)
            assert rt2.context.abandoned_delegations == [entry]
            await rt2.navigate(None)
            assert rt2.context.abandoned_delegations == []
        finally:
            reopened.close()

    asyncio.run(scenario())
