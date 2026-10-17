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

Reaches into `BackgroundTools`' private task group, outcome stream and live
count; `tests/test_background_delegation.py` pins them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anyio
import anyio.lowlevel
from pydantic_ai import CallToolsNode, CapabilityEvent
from pydantic_ai._run_context import dispatch_event_stream
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    RunCancelled,
    ToolFailedError,
    ToolRetryError,
)
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai_harness.background_tools import BackgroundTools
from pydantic_ai_harness.background_tools._capability import (
    _deliver,
    _format_background_error,
    _format_background_result,
)

from pcode.steering import run_request, steering_pending, take_steering

DELEGATE = "delegate_task"
# How often a waiting delegation checks for steering; `shell` polls as often.
POLL_SECONDS = 0.2


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


def detached_message(task_id: str) -> str:
    return (
        f"The user sent a follow-up, so this delegation (task {task_id}) moved to the "
        "background; the sub-agent is still working. Do not repeat or poll it: its result "
        "will arrive automatically as a later message. Answer the user, continue independent "
        "work, or end your response to wait for it. A shared-mode sub-agent is still "
        "editing the live files, so leave the files it is working on alone until it reports."
    )


def abandoned_message(task_ids: list[str]) -> str:
    tasks = ", ".join(task_ids)
    return (
        f"The background delegation {tasks} was stopped before it reported, because the "
        "run it belonged to ended (interrupted or failed). Its result will not arrive. "
        "Delegate again if you still need it; a sub-agent may have left partial changes."
    )


class AbandonedDelegations(AbstractCapability):
    """Tell a main turn which detached children died unreported. Per run, main turns only.

    The model was told those results would arrive by themselves. A turn retry
    after a dropped connection, Ctrl+C, or a crash ends the run that owned them,
    so the next request says they won't. `ids` is the branch's own list
    (`TurnContext.abandoned_delegations`), so a retry's new run still sees it.
    """

    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    async def before_model_request(self, ctx, request_context):
        if self.ids:
            note = UserPromptPart(abandoned_message(self.ids))
            run_request(ctx, request_context).parts.append(note)
            self.ids.clear()
        return request_context


def _abandoned(ctx, task_id: str) -> None:
    for capability in ctx.capabilities.values():
        if isinstance(capability, AbandonedDelegations):
            capability.ids.append(task_id)


@dataclass
class _Child:
    done: anyio.Event = field(default_factory=anyio.Event)
    result: Any = None
    error: BaseException | None = None
    cancelled: bool = True
    detached: bool = False


def _failure(task_id: str, error: BaseException) -> tuple[str, ...] | BaseException:
    """What a detached child's error delivers: a message, or the error that ends the run.

    Errors the foreground call would have turned into a failed tool result
    become a message too. Anything else ends the run, as it would have inline:
    Harness's `contain_errors` already turns child crashes into retries.
    """
    if isinstance(error, ToolRetryError | ToolFailedError | ApprovalRequired | CallDeferred):
        detail = _format_background_error(error)
    elif isinstance(error, ModelRetry):
        detail = str(error)
    elif isinstance(error, RunCancelled):
        detail = f"the sub-agent was cancelled. {error}".strip()
    else:
        return error
    return (f"Background tool '{DELEGATE}' (task {task_id}) failed: {detail}",)


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
        child = _Child()
        scope = anyio.CancelScope()

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
                child.done.set()
                if child.detached and child.cancelled:
                    self._live -= 1
                    _abandoned(ctx, task_id)
            if not child.detached or child.cancelled:
                return
            if child.error is not None:
                outcome = _failure(task_id, child.error)
            else:
                outcome = _format_background_result(DELEGATE, task_id, child.result)
            try:
                if not isinstance(outcome, BaseException):
                    # Settles the delegate row; `_live` still holds the run open.
                    await ctx.emit(DelegationDelivered(content="\n".join(map(str, outcome))))
            finally:
                self._send.send_nowait(outcome)
                self._live -= 1

        # Steering already pending still starts the child (steering never skips
        # a requested tool); the first check below detaches it at once.
        self._task_group.start_soon(run, name=f"delegation ({task_id})")
        try:
            while not child.done.is_set():
                # No await between this check and `detached`: the child either
                # finished first and is returned below, or sees the flag.
                if steering_pending(ctx):
                    child.detached = True
                    self._live += 1
                    await ctx.emit(DelegationDetached())
                    return detached_message(task_id)
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
            _deliver(ctx, await self._outcomes.receive())
            return True
        if steering_pending(ctx) and (messages := await take_steering(ctx)):
            ctx.enqueue(*messages)
            return True
        if not self._live:
            # The last outcome raced the timeout, or a child was cancelled
            # without one; either way nothing more will come.
            for outcome in self._arrived():
                _deliver(ctx, outcome)
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
        async for event in super().wrap_run_event_stream(ctx, stream=stream):
            yield event
        node = self._node
        # Set by the time the stream is exhausted (private to Pydantic AI; pinned).
        ending = getattr(node, "_next_node", None) if isinstance(node, CallToolsNode) else None
        if self._ending(ctx, ending):
            async for event in dispatch_event_stream(ctx, self._drain_while_waiting(ctx)):
                yield event

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
            # As upstream: a deferred-tool pause must not become another request.
            return result
        for outcome in self._arrived():
            _deliver(ctx, outcome)
        if self._ending(ctx, result):
            await ctx.emit(DelegatesPending(count=self._live))
            while not ctx.pending_messages and not await self._wait_step(ctx):
                pass
        return result
