"""Let a steering message reach the parent while its delegate keeps working.

A foreground `delegate_task` holds the parent's tool call until the child
finishes, and steering only rides the next model request, so a message sent
mid-delegation used to wait out the whole child. `shell` solves the same problem
by handing its wait back as a job handle; this does it for delegation.

The call starts its child as a task in Harness's `BackgroundTools` task group and
waits on it as before. If the user steers first, the call returns now, the child
carries on, and its result is enqueued for a later request. Harness then keeps
the run alive until every detached child has reported: the model can answer the
user, do other work, or end its response and be woken by the result.

Reaches into `BackgroundTools`' private task group, outcome stream, live count
and result framing; `tests/test_background_delegation.py` pins them.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

import anyio
import anyio.lowlevel
from pydantic import ValidationError
from pydantic_ai import (
    CallToolsNode,
    CapabilityEvent,
    FinalResultEvent,
    ModelRequestNode,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
)
from pydantic_ai._run_context import dispatch_event_stream
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    RunCancelled,
    ToolFailed,
    ToolFailedError,
    ToolRetryError,
)
from pydantic_ai.messages import TextContent, UserPromptPart
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai_harness._background_completion import make_background_result
from pydantic_ai_harness.background_tools import BackgroundTools
from pydantic_ai_harness.background_tools._capability import (
    _Completion,
    _format_background_failure,
    _format_background_result,
    _position_in_response,
)

from pcode.steering import run_request, steering_pending, take_steering
from pcode.worker_channel import hub_for, resume_hint

DELEGATE = "delegate_task"
# How often a waiting delegation checks for steering; `shell` polls as often.
POLL_SECONDS = 0.2
# Detach a foreground `delegate_task` before `agy` 1.5.13's 3m0s MCP tool-call
# timeout kills it (`notifications/cancelled: context deadline exceeded`).
MCP_DETACH_SECONDS = 150.0
_MODEL_STREAM_EVENTS = (PartStartEvent, PartDeltaEvent, PartEndEvent, FinalResultEvent)


async def _take_buffered(buffer: list):
    while buffer:
        yield buffer.pop(0)


async def _close_stream(stream) -> None:
    if callable(close := getattr(stream, "aclose", None)):
        with contextlib.suppress(Exception):
            await close()


@dataclass(kw_only=True)
class DelegationDetached(CapabilityEvent, namespace="pcode_delegation", name="detached"):
    """The call returned on steering; its child is still running."""


@dataclass(kw_only=True)
class DelegationDelivered(CapabilityEvent, namespace="pcode_delegation", name="delivered"):
    """A detached child finished; `content` is what the model receives."""

    content: str


@dataclass(kw_only=True)
class DelegatesPending(CapabilityEvent, namespace="pcode_delegation", name="pending"):
    """The model ended its response; the run waits on `count` detached children."""

    count: int


WAIT_ADVICE = (
    "Do not repeat or poll it: its result will arrive automatically as a later message. "
    "Minutes without visible progress are normal: do not stop or replace it as stalled "
    "unless manage_worker list says it may be stuck, and then tell the user first. "
    "A shared-mode sub-agent is still editing the live files, so leave the files it is "
    "working on alone until it reports."
)


def _named(task_id: str, worker_id: str) -> str:
    return f"worker {worker_id}, task {task_id}" if worker_id else f"task {task_id}"


def detached_message(task_id: str, worker_id: str = "") -> str:
    return (
        f"The user sent a follow-up, so this delegation ({_named(task_id, worker_id)}) moved "
        f"to the background; the sub-agent is still working. {WAIT_ADVICE} Answer the user, "
        "continue independent work, or end your response to wait for it."
    )


def question_detached_message(task_id: str, worker_id: str = "") -> str:
    return (
        f"A worker asked you a question, so this delegation ({_named(task_id, worker_id)}) "
        f"moved to the background; it is still working. {WAIT_ADVICE} The question follows; "
        "answer it with manage_worker."
    )


def background_message(task_id: str, worker_id: str = "") -> str:
    return (
        f"Started in the background ({_named(task_id, worker_id)}). {WAIT_ADVICE} "
        "Dispatch other independent work, talk to the user, or end your response to wait."
    )


def timeout_detached_message(task_id: str, worker_id: str = "") -> str:
    return (
        f"This delegation ({_named(task_id, worker_id)}) is still running, so it moved "
        f"to the background to avoid the client tool timeout. {WAIT_ADVICE} "
        "Continue independent work or end your response to wait for it."
    )


def stopped_message(task_id: str, worker_id: str = "", hint: str = "") -> str:
    return (
        f"The background delegation ({_named(task_id, worker_id)}) was stopped as requested "
        f"before it reported. It may have left partial changes.{hint}"
    )


AbandonedItem = str | dict[str, Any]


def _abandoned_entry(task_id: str, handle: Any, *, detached: bool) -> AbandonedItem:
    if handle is None:
        return task_id
    resumable = (
        handle.workspace_mode != "isolated"
        and bool(handle.messages)
        and handle.worker_model is None
    )
    return {
        "task_id": task_id,
        "worker_id": handle.id,
        "purpose": handle.purpose,
        "workspace_mode": handle.workspace_mode,
        "resumable": resumable,
        "worktree_task_id": handle.worktree_task_id,
        "detached": detached,
    }


def _abandoned_line(item: AbandonedItem) -> str:
    if isinstance(item, str):
        return (
            f"The background delegation {item} was stopped before it reported, because the "
            "run it belonged to ended (interrupted or failed). Its result will not arrive. "
            "Delegate again if you still need it; a sub-agent may have left partial changes."
        )
    task_id = str(item.get("task_id") or "")
    worker_id = str(item.get("worker_id") or "")
    purpose = str(item.get("purpose") or "").strip()
    workspace_mode = str(item.get("workspace_mode") or "shared")
    resumable = bool(item.get("resumable"))
    worktree_task_id = str(item.get("worktree_task_id") or "")
    detached = bool(item.get("detached", True))
    kind = "background delegation" if detached else "delegation"
    if worker_id:
        named = f"worker {worker_id}"
        if purpose:
            named += f" ({purpose})"
        if task_id:
            named += f", task {task_id}"
    elif task_id:
        named = f"task {task_id}"
    else:
        named = "unknown"
    if resumable and worker_id:
        guidance = (
            f"Worker {worker_id} kept its history up to where it stopped: resume it with "
            f"delegate_task(continue_worker='{worker_id}', task=...) instead of starting a "
            "new worker."
        )
    elif workspace_mode == "isolated" and worktree_task_id:
        guidance = (
            f"Its isolated worktree (task_id='{worktree_task_id}', "
            f"branch 'task-{worktree_task_id}') preserved partial work; inspect it with "
            "task_worktree(action='list')."
        )
    else:
        guidance = "Delegate again if you still need it; a sub-agent may have left partial changes."
    return (
        f"The {kind} ({named}) was stopped before it reported, because the run it belonged "
        f"to ended (interrupted or failed). Its result will not arrive. {guidance}"
    )


def abandoned_message(items: list[AbandonedItem]) -> str:
    if all(isinstance(item, str) for item in items):
        tasks = ", ".join(items)  # type: ignore[arg-type]
        return (
            f"The background delegation {tasks} was stopped before it reported, because the "
            "run it belonged to ended (interrupted or failed). Its result will not arrive. "
            "Delegate again if you still need it; a sub-agent may have left partial changes."
        )
    return "\n".join(_abandoned_line(item) for item in items)


class AbandonedDelegations(AbstractCapability):
    """Tell a main turn which children died unreported. Per run, main turns only.

    The model was told detached results would arrive by themselves, and a
    cancelled foreground worker may have saved resumable history or a worktree.
    A turn retry after a dropped connection, Ctrl+C, /restart, or a crash ends
    the run that owned them, so the next request says they won't arrive and how
    to resume them. `ids` is the branch's own list
    (`TurnContext.abandoned_delegations`), so a retry's new run still sees it.
    """

    def __init__(self, ids: list[AbandonedItem]) -> None:
        self.ids = ids

    async def before_model_request(self, ctx, request_context):
        if self.ids:
            note = UserPromptPart(abandoned_message(self.ids))
            run_request(ctx, request_context).parts.append(note)
            self.ids.clear()
        return request_context


def _abandoned(ctx, item: AbandonedItem) -> None:
    for capability in ctx.capabilities.values():
        if isinstance(capability, AbandonedDelegations):
            capability.ids.append(item)


@dataclass
class _Child:
    done: anyio.Event = field(default_factory=anyio.Event)
    result: Any = None
    error: BaseException | None = None
    cancelled: bool = True
    detached: bool = False


# What Harness's own background tools report as a failed result (its private
# `_SignalledError`); anything else ends the run.
_REPORTED = (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    ToolFailed,
    ToolFailedError,
    ToolRetryError,
    ValidationError,
)


def _failure(
    task_id: str, error: BaseException, max_retries: int, dispatched: tuple[int, int]
) -> _Completion | BaseException:
    """What a detached child's error delivers: a failed result, or the error that ends the run.

    Errors the foreground call would have turned into a failed tool result
    become a failed result too. Anything else ends the run, as it would have
    inline: Harness's `contain_errors` already turns child crashes into retries.
    Neither counts toward `delegate_task`'s retries: the call already returned.
    `dispatched` is where the model made the call, which orders results
    requeued at a pause.
    """
    if isinstance(error, RunCancelled):
        error = ToolFailed(f"the sub-agent was cancelled. {error}".strip())
    elif not isinstance(error, _REPORTED):
        return error
    result = _format_background_failure(DELEGATE, task_id, error)
    return _Completion(DELEGATE, max_retries, result, dispatched)


def _shown(completion: _Completion) -> str:
    """A delivered result's text as the delegate row shows it.

    The framework frames it for the model.
    """
    content = completion.result.content
    if isinstance(content, str):
        return content
    return "\n".join(
        item.content if isinstance(item, TextContent) else item
        for item in content
        if isinstance(item, TextContent | str)
    )


@dataclass
class BackgroundDelegation(BackgroundTools):
    """Detach a running `delegate_task` when the user steers. Parent agent only."""

    id: str | None = "background_delegation"
    _node: Any = field(default=None, init=False, repr=False, compare=False)

    def get_instructions(self):
        # The detach message explains itself; nothing else needs to know.
        return None

    async def _background_mode(self, ctx, tool_def):
        # No tool is backgrounded up front, and no `run_in_background` flag is offered.
        return None

    async def wrap_run(self, ctx, *, handler):
        # Upstream's task group wraps whatever the run raises in an exception
        # group; turn retries, error reports and every `except` around a run
        # expect the error itself, so a lone one is unwrapped.
        try:
            return await super().wrap_run(ctx, handler=handler)
        except BaseExceptionGroup as group:
            if len(group.exceptions) != 1:
                raise
            error = group.exceptions[0]
        # Raised outside the handler, so its own cause and context are kept.
        raise error

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        if call.tool_name != DELEGATE:
            return await handler(args)
        task_id = call.tool_call_id
        # As upstream: taken while the call's response is still the last message.
        dispatched = (ctx.run_step, _position_in_response(ctx, call))
        child = _Child()
        scope = anyio.CancelScope()
        hub = hub_for(ctx)
        # Claimed before the child starts, so a receipt can name the worker.
        handle = hub.claim(task_id, args) if hub is not None else None
        worker_id = handle.id if handle is not None else ""

        def stop_requested() -> bool:
            return handle is not None and handle.stop_requested

        async def run() -> None:
            try:
                with scope:
                    try:
                        child.result = await handler(args)
                    except anyio.get_cancelled_exc_class():
                        raise
                    except BaseException as error:
                        child.error = error
                    child.cancelled = False
            finally:
                if handle is not None:
                    handle.cancel = None
                    if not handle.started:
                        # Refused before delegate_task ran (another capability, a
                        # validation error): no worker exists, so none stays running.
                        hub.release(handle)
                if child.cancelled and not stop_requested():
                    if child.detached:
                        self._live -= 1
                    if child.detached or (handle is not None and handle.started):
                        _abandoned(ctx, _abandoned_entry(task_id, handle, detached=child.detached))
                child.done.set()
            if not child.detached:
                return
            outcome: _Completion | BaseException
            if child.cancelled:
                if not stop_requested():
                    return
                # Stopped by `manage_worker(action='stop')`, not by the run ending: say so.
                stopped = stopped_message(task_id, worker_id, resume_hint(handle))
                result = make_background_result(DELEGATE, task_id, stopped, failed=True)
                outcome = _Completion(DELEGATE, ctx.max_retries, result, dispatched)
            elif child.error is not None:
                if isinstance(child.error, ToolRetryError) and ctx.tool_manager is not None:
                    # As upstream: core charged the child's retry to the step that
                    # issued the call, which already counted it a success.
                    ctx.tool_manager.failed_tools.discard(DELEGATE)
                outcome = _failure(task_id, child.error, ctx.max_retries, dispatched)
            else:
                result = _format_background_result(DELEGATE, task_id, child.result)
                outcome = _Completion(DELEGATE, ctx.max_retries, result, dispatched, succeeded=True)
            try:
                if not isinstance(outcome, BaseException):
                    # Settles the delegate row; `_live` still holds the run open.
                    await ctx.emit(DelegationDelivered(content=_shown(outcome)))
            finally:
                self._send.send_nowait(outcome)
                self._live -= 1

        # Steering already pending still starts the child (steering never skips
        # a requested tool); the first check below detaches it at once.
        from pcode.models import runs_own_loop

        self._task_group.start_soon(run, name=f"delegation ({task_id})")
        background = bool(args.get("run_in_background"))
        own_loop = runs_own_loop(getattr(ctx, "model", None))
        started = monotonic()
        try:
            while not child.done.is_set():
                # No await between this check and `detached`: the child either
                # finished first and is returned below, or sees the flag.
                # A worker's question needs a parent model request, which a
                # waiting call would block, so it detaches like steering.
                question = hub is not None and hub.has_undelivered()
                timed_out = own_loop and (monotonic() - started >= MCP_DETACH_SECONDS)
                if background or question or timed_out or steering_pending(ctx):
                    child.detached = True
                    self._live += 1
                    if handle is not None:
                        handle.cancel = scope.cancel
                    await ctx.emit(DelegationDetached())
                    if background:
                        return background_message(task_id, worker_id)
                    if question:
                        return question_detached_message(task_id, worker_id)
                    if timed_out:
                        return timeout_detached_message(task_id, worker_id)
                    return detached_message(task_id, worker_id)
                with anyio.move_on_after(POLL_SECONDS):
                    await child.done.wait()
        except BaseException:
            if not child.detached:
                # A cancelled call cancels its child, as when it ran inline,
                # and waits out the child's own cleanup (worktree records).
                scope.cancel()
                with anyio.CancelScope(shield=True):
                    await child.done.wait()
            raise
        if child.cancelled:
            # Cancelled by the run's task group, which is cancelling this call too.
            await anyio.lowlevel.checkpoint()
            raise anyio.get_cancelled_exc_class()()
        if child.error is not None:
            raise child.error
        return child.result

    # -- waiting at the run's end ---------------------------------------------
    #
    # When the model ends its response under a live child, the run has to wait
    # for it. Upstream waits in `after_node_run`, between nodes, where nothing
    # flushes `ctx.emit` events: the child's live activity would freeze on screen
    # until its result arrived. So the wait runs at the end of the ending node's
    # own event stream instead, passing buffered events on as they come.
    # `after_node_run` keeps the same wait as a fallback for an unstreamed node.

    def _ending(self, ctx, result) -> bool:
        """Whether `result` ends the run under a live child with nothing queued."""
        from pydantic_graph import End

        return (
            isinstance(result, End)
            and not isinstance(result.data.output, DeferredToolRequests)
            and bool(self._live)
            and not ctx.pending_messages
        )

    async def _wait_step(self, ctx) -> bool:
        """Wait briefly; True once a child's outcome or steering is enqueued, or none can come.

        Upstream wakes for the next result only. Steering wakes it too, so the
        user is never stuck behind a child the model left running.
        """
        with anyio.move_on_after(POLL_SECONDS):
            self._deliver(ctx, await self._outcomes.receive())
            return True
        if (hub := hub_for(ctx)) is not None and (questions := hub.take_questions()):
            ctx.enqueue(*questions)
            return True
        if steering_pending(ctx) and (messages := await take_steering(ctx)):
            ctx.enqueue(*messages)
            return True
        if not self._live:
            # The last outcome raced the timeout, or a child was cancelled
            # without one; either way nothing more will come.
            for outcome in self._arrived():
                self._deliver(ctx, outcome)
            return True
        return False

    async def wrap_node_run(self, ctx, *, node, handler):
        # The ending node's stream needs its node to know that it ends the run.
        self._node = node
        try:
            return await handler(node)
        finally:
            self._node = None

    async def wrap_run_event_stream(self, ctx, *, stream):
        from pcode.models import runs_own_loop

        node = self._node
        wrapped = super().wrap_run_event_stream(ctx, stream=stream)
        if isinstance(node, ModelRequestNode) and runs_own_loop(getattr(ctx, "model", None)):
            async for event in self._interleave_buffered(ctx, wrapped):
                yield event
        else:
            async for event in wrapped:
                yield event
        # Set by the time the stream is exhausted (private to Pydantic AI; pinned).
        ending = getattr(node, "_next_node", None) if isinstance(node, CallToolsNode) else None
        if self._ending(ctx, ending):
            async for event in dispatch_event_stream(ctx, self._drain_while_waiting(ctx)):
                yield event

    async def _interleave_buffered(self, ctx, stream):
        """Yield `stream` while draining `ctx._event_stream_buffer` live as events arrive.

        During a `ModelRequestNode`, Pydantic AI only drains the run's event
        buffer between pulls of the model stream. An `antigravity:` model runs
        bridged pcode delegations while that pull is in flight, so watching
        the buffer's waiters lets worker start, activity, plan and prose reach
        the panel and `/agents` immediately instead of after the turn ends.
        """
        buffer = ctx._event_stream_buffer
        waiters = getattr(buffer, "waiters", None)
        if buffer is None or waiters is None:
            async for event in stream:
                yield event
            return
        iterator = aiter(stream)
        signal = asyncio.Event()
        waiters.append(signal)
        next_task: asyncio.Task | None = None
        try:
            while True:
                while buffer:
                    async for emitted in dispatch_event_stream(ctx, _take_buffered(buffer)):
                        yield emitted
                signal.clear()
                if next_task is None:
                    next_task = asyncio.create_task(anext(iterator))
                wait_task = asyncio.create_task(signal.wait())
                try:
                    done, _ = await asyncio.wait(
                        {next_task, wait_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    if not wait_task.done():
                        wait_task.cancel()
                        with anyio.CancelScope(shield=True):
                            with contextlib.suppress(asyncio.CancelledError):
                                await wait_task
                if next_task in done:
                    task, next_task = next_task, None
                    try:
                        event = task.result()
                    except StopAsyncIteration:
                        while buffer:
                            async for emitted in dispatch_event_stream(ctx, _take_buffered(buffer)):
                                yield emitted
                        break
                    if isinstance(event, _MODEL_STREAM_EVENTS):
                        while buffer:
                            async for emitted in dispatch_event_stream(ctx, _take_buffered(buffer)):
                                yield emitted
                        yield event
                    else:
                        # A buffered event popped ahead of anything still in `buffer`.
                        yield event
                        while buffer:
                            async for emitted in dispatch_event_stream(ctx, _take_buffered(buffer)):
                                yield emitted
                else:
                    while buffer:
                        async for emitted in dispatch_event_stream(ctx, _take_buffered(buffer)):
                            yield emitted
        finally:
            if signal in waiters:
                waiters.remove(signal)
            with anyio.CancelScope(shield=True):
                try:
                    if next_task is not None and not next_task.done():
                        next_task.cancel()
                        with contextlib.suppress(BaseException):
                            await next_task
                    elif next_task is not None and not next_task.cancelled():
                        with contextlib.suppress(BaseException):
                            next_task.exception()
                finally:
                    await _close_stream(iterator)

    async def _drain_while_waiting(self, ctx):
        buffer = ctx._event_stream_buffer
        await ctx.emit(DelegatesPending(count=self._live))
        while True:
            while buffer:
                yield buffer.pop(0)
            if ctx.pending_messages or await self._wait_step(ctx):
                break
        while buffer:
            yield buffer.pop(0)

    async def after_node_run(self, ctx, *, node, result):
        from pydantic_graph import End

        if isinstance(result, End) and isinstance(result.data.output, DeferredToolRequests):
            # A deferred-tool pause is upstream's to settle: it waits for live
            # tasks and records their results in dispatch order. No pcode agent
            # ends on one today. If one ever does, this wait must wake for worker
            # questions and steering as `_wait_step` does, or a worker blocked
            # on a parent answer holds the pause forever.
            return await super().after_node_run(ctx, node=node, result=result)
        for outcome in self._arrived():
            self._deliver(ctx, outcome)
        if self._ending(ctx, result):
            await ctx.emit(DelegatesPending(count=self._live))
            while not ctx.pending_messages and not await self._wait_step(ctx):
                pass
        return result
