"""Forward child activity; child prose goes to the worker viewer, never the transcript.

Harness 0.31 supplies delegation lifecycle events and a child stream handler, but
not the parent's call identity in that handler. A context-local execution wrapper
pairs concurrent children with their own parent tool call. No global event queue
or mutable per-agent handler is needed, and cancellation resets the binding.
"""

import asyncio
import logging
import random
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from time import monotonic

from pydantic_ai import CapabilityEvent, RunContext, capture_run_messages

# Private: no public way to reach the enclosing capture (Harness's, in `_child_run`).
from pydantic_ai._agent_graph import get_captured_run_messages
from pydantic_ai.agent import WrapperAgent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelOverloadedError,
    ModelQuotaExceededError,
    ModelRateLimitError,
)
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    repair_messages,
)

from pcode import retries
from pcode.antigravity_tools import BridgedToolFinished, BridgedToolStarted
from pcode.background_delegation import DelegationDelivered, DelegationDetached
from pcode.cache_warnings import CacheBustEvent
from pcode.diagnostics import quota_exhausted, transient
from pcode.error_report import error_message
from pcode.filesystem import FileChangeEvent
from pcode.inspection import capture
from pcode.jobs import registry
from pcode.planning import PlanSnapshot
from pcode.preferences import SETTINGS, load_preferences
from pcode.retries import RequestCheckpoint
from pcode.runtime import ToolStarted, ToolSummary
from pcode.tool_display import (
    COMMAND_TOOLS,
    assignment,
    command_error,
    command_exit_code,
    execution_mode,
    invocation,
    native_result_detail,
    native_result_projection,
    result_detail,
    stated_purpose,
    stored_source,
    subject,
    target,
    tool_outcome,
)
from pcode.worker_channel import WorkerUpdate, current_worker, notify

_parent: ContextVar[RunContext | None] = ContextVar("delegation_parent", default=None)
logger = logging.getLogger(__name__)

# A worker refused for a rate limit or overload waits and resends, a turn does
# not: parallel workers cause their own limits, and a parent re-delegating them
# all at once meets the same limit again. Seconds per try, jittered.
RATE_LIMIT_BACKOFF = (15.0, 30.0, 60.0)
# A longer `retry-after` (a plan's usage window) fails at once; it is resumable.
RATE_LIMIT_LONGEST = 60.0


def _model_error(error: BaseException) -> ModelAPIError | None:
    """The provider error behind `error`, following a short cause chain."""
    current: BaseException | None = error
    for _ in range(8):
        if current is None or isinstance(current, ModelAPIError):
            return current
        current = current.__cause__ or current.__context__
    return None


def rate_limit_wait(error: BaseException, waits: int) -> float | None:
    """Seconds a worker waits before resending after a rate limit; None to fail now."""
    cause = _model_error(error)
    if cause is None or waits >= len(RATE_LIMIT_BACKOFF):
        return None
    limited = isinstance(cause, ModelRateLimitError | ModelOverloadedError) or getattr(
        cause, "status_code", None
    ) in (429, 529)
    if not limited or isinstance(cause, ModelQuotaExceededError) or quota_exhausted(error):
        return None
    if (asked := getattr(cause, "retry_after", None)) is not None:
        return None if asked > RATE_LIMIT_LONGEST else max(asked, 1.0)
    return RATE_LIMIT_BACKOFF[waits] * random.uniform(0.8, 1.2)


@dataclass(kw_only=True)
class ChildActivity(CapabilityEvent, namespace="pcode_delegation", name="activity"):
    activity: str
    child: ToolStarted | ToolSummary | None = None
    # The child's whole plan, sent only when it changed.
    plan: list[dict] | None = None
    # Explicit delegate call id when forwarded from a nested sub-agent.
    delegate_call_id: str = ""


@dataclass(kw_only=True)
class ChildOutput(CapabilityEvent, namespace="pcode_delegation", name="output"):
    """A slice of the child's prose or reasoning; `start` opens a new part."""

    text: str
    thinking: bool = False
    start: bool = False
    # Explicit delegate call id when forwarded from a nested sub-agent.
    delegate_call_id: str = ""


def _output(event) -> ChildOutput | None:
    """The prose or reasoning a streamed part carries, if any."""
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart | ThinkingPart):
        return ChildOutput(
            text=event.part.content, thinking=isinstance(event.part, ThinkingPart), start=True
        )
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return ChildOutput(text=event.delta.content_delta)
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, ThinkingPartDelta):
        if event.delta.content_delta:
            return ChildOutput(text=event.delta.content_delta, thinking=True)
    return None


class DelegationReporting(AbstractCapability):
    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        if call.tool_name != "delegate_task":
            return await handler(args)
        token = _parent.set(ctx)
        try:
            return await handler(args)
        finally:
            _parent.reset(token)


class ResumableAgent(WrapperAgent):
    """Resend a sub-agent's request after a dropped connection, as a turn does.

    Without this, one child's ECONNRESET fails its delegation outright. The
    resend reuses `AgentRuntime.stream`'s rule: only a request that never got an
    answer goes again, from the exact messages it carried, so no tool the child
    already ran is run twice. A drop anywhere else still ends the child.
    """

    def __init__(self, wrapped, *, model=None):
        super().__init__(wrapped)
        self.override_model = model

    async def run(self, user_prompt=None, *, message_history=None, capabilities=None, **kwargs):
        if self.override_model is not None:
            kwargs["model"] = self.override_model
        attempts = int(load_preferences().get("retry_attempts", SETTINGS["retry_attempts"].default))
        attempt = waits = 0
        handle = current_worker()
        earlier = handle.tokens if handle is not None else 0
        if message_history is None and handle is not None and handle.resume_from:
            # `continue_worker`: the follow-up joins the worker's own history.
            # Repaired as Harness repairs its own resumes: a run that ended on
            # deferred tool calls cannot otherwise take a new prompt.
            message_history = repair_messages(handle.resume_from)
        # Harness's own capture around this call, which ours below would hide.
        try:
            outer = get_captured_run_messages()
        except LookupError:
            outer = None
        # Responses already counted, by run: earlier runs and earlier attempts.
        # By run id rather than position, which retries shift. (Responses a
        # run's own compaction drops before it ends still go uncounted.)
        seen = {m.run_id for m in message_history or () if isinstance(m, ModelResponse)}
        spent = 0
        while True:
            checkpoint = RequestCheckpoint()
            with capture_run_messages() as captured:
                try:
                    result = await self.wrapped.run(
                        user_prompt,
                        message_history=message_history,
                        capabilities=[*(capabilities or ()), checkpoint],
                        **kwargs,
                    )
                except BaseException as error:
                    failure = error
                else:
                    failure = None
                finally:
                    # Exact, from the responses themselves: the live count misses
                    # the final response and cannot read a shared usage counter.
                    added = [
                        m for m in captured if isinstance(m, ModelResponse) and m.run_id not in seen
                    ]
                    spent += sum(m.usage.total_tokens for m in added)
                    seen.update(m.run_id for m in added)
                    if handle is not None:
                        handle.tokens = earlier + spent
                    if outer is not None:
                        # For Harness's `keep_partial_output` on a timeout or stop.
                        outer.messages[:] = captured
            if failure is None:
                if handle is not None:
                    handle.messages = result.all_messages()
                return result
            if any(m.state not in ("incomplete", "interrupted") for m in added):
                # Progress was made before this failure: give subsequent transient
                # errors fresh retry and rate-limit backoff budgets.
                attempt = waits = 0
            error = failure
            delay = None
            if isinstance(error, Exception) and checkpoint.messages is not None:
                if attempt < attempts and transient(error):
                    attempt += 1
                    delay = retries.RETRY_DELAY
                    activity = f"Reconnecting {attempt}/{attempts}"
                elif (wait := rate_limit_wait(error, waits)) is not None:
                    waits += 1
                    delay = wait
                    activity = f"Rate limited; retrying in {round(wait)}s"
            if delay is None:
                if handle is not None:
                    # Kept so `continue_worker` picks up here rather than from
                    # nothing: the request as sent if one was refused, otherwise
                    # all the run got to (a stop mid-tool), else its last history.
                    kept = checkpoint.messages or repair_messages(captured)
                    if kept:
                        handle.messages = kept
                raise error
            user_prompt, message_history = None, checkpoint.messages
            if handle is not None:
                # A stop or timeout during the wait below must not lose this run.
                handle.messages = message_history
            # The panel shows the retry; a warning would print over it.
            logger.debug("Sub-agent %r: %s %s", self.name, error_message(error), activity)
            if handle is not None:
                handle.activity = activity
                handle.running_tools = []  # The resent request starts over.
                handle.mark_progress()
            if (parent := _parent.get()) is not None:
                await parent.emit(ChildActivity(activity=activity))
            await asyncio.sleep(delay)


async def stream_child_activity(_ctx, events):
    """Consume child streams: tool boundaries, phase changes, plans and prose."""
    parent = _parent.get()
    plan_items: list[dict] = []
    tools = {}
    worker = current_worker()
    # The child's usage can be the parent's counter; count only what it adds.
    usage = getattr(_ctx, "usage", None)
    if parent is not None and usage is parent.usage:
        # Forwarded usage is the parent's counter: siblings and the parent
        # would count too. The exact total arrives when the worker settles.
        usage = None
    baseline = usage.total_tokens if usage is not None else 0
    # A resumed worker's count continues from its earlier runs.
    earlier = reported = worker.tokens if worker is not None else 0
    # The child's calls by id, so its spill-reading rows name what spilled.
    called: dict[str, str] = {}
    # Nested `delegate_task` calls that detached into the background; settled by
    # `DelegationDelivered` rather than the immediate receipt `FunctionToolResultEvent`.
    detached: dict[str, tuple[str, dict, float]] = {}
    settled_detached: set[str] = set()
    phase = ""
    async for event in events:
        if parent is None:
            continue
        if worker is not None:
            # Any child event, streamed text and thinking included, shows it is alive.
            worker.mark_progress()
        if isinstance(event, FileChangeEvent):
            parent_id = parent.tool_call_id or ""
            child_parent = (
                f"{parent_id}:{event.change.parent_call_id}"
                if event.change.parent_call_id
                else parent_id
            )
            await parent.emit(
                FileChangeEvent(
                    change=replace(
                        event.change,
                        call_id=f"{parent_id}:{event.change.call_id}",
                        parent_call_id=child_parent,
                    )
                )
            )
            continue
        if isinstance(event, DelegationDetached):
            if (entry := tools.get(event.tool_call_id)) is not None:
                detached[event.tool_call_id] = entry
            continue
        if isinstance(event, DelegationDelivered):
            entry = detached.pop(event.tool_call_id, None)
            if entry is not None:
                name, args, started = entry
                if event.tool_call_id in tools:
                    settled_detached.add(event.tool_call_id)
                parent_id = parent.tool_call_id or ""
                detail, failed = result_detail(name, args, event.content, "success")
                delivered = ToolSummary(
                    name,
                    detail,
                    failed=failed,
                    call_id=f"{parent_id}:{event.tool_call_id}",
                    elapsed_seconds=max(0, monotonic() - started),
                    command=invocation(name, args),
                    purpose=stated_purpose(args),
                    result=capture(event.content),
                    run_id=parent.run_id or "",
                    outcome="error" if failed else "success",
                    parent_call_id=parent_id,
                    execution="background",
                )
                await parent.emit(ChildActivity(activity=phase, child=delivered))
            continue
        if isinstance(event, BridgedToolStarted):
            parent_id = parent.tool_call_id or ""
            tools[event.call_id] = (event.tool_name, event.args, monotonic())
            called[event.call_id] = event.tool_name
            agent, task = assignment(event.tool_name, event.args)
            command, purpose = subject(event.tool_name, event.args, registry())
            bridged_start = ToolStarted(
                event.tool_name,
                target(event.tool_name, event.args, stored_source(event.args, called)),
                f"{parent_id}:{event.call_id}",
                arguments=capture(event.args),
                run_id=parent.run_id or "",
                started_at=datetime.now(timezone.utc).isoformat(),
                parent_call_id=parent_id,
                command=command,
                purpose=purpose,
                execution=execution_mode(event.tool_name, event.args),
                agent=agent,
                task=task,
            )
            await parent.emit(ChildActivity(activity="Working", child=bridged_start))
            phase = "Working"
            continue
        if isinstance(event, BridgedToolFinished):
            name, args, started = tools.pop(event.call_id, (event.tool_name, {}, monotonic()))
            if event.call_id in detached or event.call_id in settled_detached:
                settled_detached.discard(event.call_id)
                continue
            parent_id = parent.tool_call_id or ""
            outcome = "error" if event.failed else "success"
            detail, failed = result_detail(
                name, args, event.result, outcome, stored_source(args, called)
            )
            bridged_end = ToolSummary(
                name,
                detail,
                failed=event.failed or failed,
                call_id=f"{parent_id}:{event.call_id}",
                elapsed_seconds=max(0, monotonic() - started),
                command=invocation(name, args),
                purpose=stated_purpose(args),
                result=capture(event.result),
                run_id=parent.run_id or "",
                outcome=outcome,
                parent_call_id=parent_id,
            )
            activity = "Working" if tools else "Waiting for model"
            await parent.emit(ChildActivity(activity=activity, child=bridged_end))
            phase = activity
            continue
        if isinstance(event, CacheBustEvent):
            await parent.emit(CacheBustEvent(text=f"Sub-agent: {event.text}"))
            continue
        if isinstance(event, PlanSnapshot):
            # Its own planning announces the plan; the store stays private to it.
            if event.items != plan_items:
                plan_items = event.items
                await parent.emit(ChildActivity(activity=phase, plan=event.items))
            continue
        if isinstance(event, WorkerUpdate):
            parent_id = parent.tool_call_id or ""
            await parent.emit(replace(event, call_id=f"{parent_id}:{event.call_id}"))
            continue
        if isinstance(event, ChildActivity):
            parent_id = parent.tool_call_id or ""
            source_id = event.delegate_call_id or event.tool_call_id or ""
            nested_id = f"{parent_id}:{source_id}" if source_id else parent_id
            nested_child = (
                replace(
                    event.child,
                    call_id=f"{parent_id}:{event.child.call_id}",
                    parent_call_id=(
                        f"{parent_id}:{event.child.parent_call_id}"
                        if event.child.parent_call_id
                        else parent_id
                    ),
                )
                if event.child is not None
                else None
            )
            await parent.emit(
                ChildActivity(
                    activity=event.activity,
                    child=nested_child,
                    plan=event.plan,
                    delegate_call_id=nested_id,
                )
            )
            continue
        if isinstance(event, ChildOutput):
            parent_id = parent.tool_call_id or ""
            source_id = event.delegate_call_id or event.tool_call_id or ""
            nested_id = f"{parent_id}:{source_id}" if source_id else parent_id
            await parent.emit(replace(event, delegate_call_id=nested_id))
            continue
        if (output := _output(event)) is not None:
            await parent.emit(output)
        child = None
        activity = phase
        parent_id = parent.tool_call_id or ""
        if isinstance(event, FunctionToolCallEvent):
            part = event.part
            try:
                args = part.args_as_dict()
            except TypeError, ValueError:
                args = {}
            tools[part.tool_call_id] = (part.tool_name, args, monotonic())
            called[part.tool_call_id] = part.tool_name
            agent, task = assignment(part.tool_name, args)
            # Resolved here, in the child's context, so an isolated worker's
            # job ids are looked up among its own jobs, not the parent's.
            command, purpose = subject(part.tool_name, args, registry())
            child = ToolStarted(
                part.tool_name,
                target(part.tool_name, args, stored_source(args, called)),
                f"{parent_id}:{part.tool_call_id}",
                arguments=capture(args),
                run_id=parent.run_id or "",
                started_at=datetime.now(timezone.utc).isoformat(),
                parent_call_id=parent_id,
                command=command,
                purpose=purpose,
                execution=execution_mode(part.tool_name, args),
                agent=agent,
                task=task,
            )
            activity = "Working"
        elif isinstance(event, FunctionToolResultEvent):
            name, args, started = tools.pop(
                event.tool_call_id, (event.part.tool_name or "tool", {}, monotonic())
            )
            if event.tool_call_id in detached or event.tool_call_id in settled_detached:
                # `DelegationDelivered` settles this detached child once it finishes.
                settled_detached.discard(event.tool_call_id)
                activity = "Working" if tools else "Waiting for model"
                if activity != phase:
                    await parent.emit(ChildActivity(activity=activity))
                    phase = activity
                continue
            outcome = tool_outcome(event.part)
            detail, failed = result_detail(
                name, args, event.part.content, outcome, stored_source(args, called)
            )
            exit_code = (
                command_exit_code(name, event.part.content) if outcome == "success" else None
            )
            child = ToolSummary(
                name,
                detail,
                failed=failed,
                call_id=f"{parent_id}:{event.tool_call_id}",
                elapsed_seconds=max(0, monotonic() - started),
                command=invocation(name, args),
                purpose=stated_purpose(args),
                error=(
                    command_error(event.part.content)
                    if (failed or exit_code) and name in COMMAND_TOOLS
                    else ""
                ),
                exit_code=exit_code,
                result=capture(event.part.content),
                run_id=parent.run_id or "",
                outcome=outcome,
                parent_call_id=parent_id,
            )
            activity = "Working" if tools else "Waiting for model"
        elif isinstance(event, PartEndEvent) and isinstance(event.part, NativeToolCallPart):
            part = event.part
            if part.provider_name == "antigravity" and part.tool_name == "mcp_pcode_delegate_task":
                # `BridgedToolStarted` carries the real `delegate_task` call.
                continue
            try:
                args = part.args_as_dict()
            except TypeError, ValueError:
                args = {}
            tools[part.tool_call_id] = (part.tool_name, args, monotonic())
            agent, task = assignment(part.tool_name, args)
            child = ToolStarted(
                part.tool_name,
                target(part.tool_name, args),
                f"{parent_id}:{part.tool_call_id}",
                arguments=capture(args if args else part.args),
                run_id=parent.run_id or "",
                started_at=datetime.now(timezone.utc).isoformat(),
                parent_call_id=parent_id,
                command=invocation(part.tool_name, args),
                purpose=stated_purpose(args),
                execution=execution_mode(part.tool_name, args),
                agent=agent,
                task=task,
            )
            activity = "Working"
        elif isinstance(event, PartStartEvent) and isinstance(event.part, NativeToolReturnPart):
            part = event.part
            if part.provider_name == "antigravity" and part.tool_name == "mcp_pcode_delegate_task":
                continue
            name, args, started = tools.pop(part.tool_call_id, (part.tool_name, {}, monotonic()))
            provider = part.provider_name or ""
            detail, failed = native_result_detail(name, args, part.content, part.outcome, provider)
            projected = native_result_projection(part.content, provider)
            child = ToolSummary(
                name,
                detail,
                failed=failed,
                call_id=f"{parent_id}:{part.tool_call_id}",
                elapsed_seconds=max(0, monotonic() - started),
                command=invocation(name, args),
                purpose=stated_purpose(args),
                error=command_error(projected) if failed and name in COMMAND_TOOLS else "",
                result=capture(projected),
                run_id=parent.run_id or "",
                outcome=part.outcome if not failed else "error",
                parent_call_id=parent_id,
            )
            activity = "Working" if tools else "Waiting for model"
        elif isinstance(event, PartStartEvent):
            if isinstance(event.part, ThinkingPart):
                activity = "Thinking"
            elif isinstance(event.part, TextPart):
                activity = "Responding"
            elif isinstance(event.part, NativeToolCallPart):
                activity = "Working"
        if child is not None or activity != phase:
            if worker is not None:
                worker.activity = activity
                worker.running_tools = [name for name, _args, _started in tools.values()]
                if isinstance(child, ToolStarted):
                    worker.tool_calls += 1
            await parent.emit(ChildActivity(activity=activity, child=child))
            phase = activity
            if worker is not None and usage is not None:
                tokens = earlier + max(0, usage.total_tokens - baseline)
                if tokens != reported:
                    reported = worker.tokens = tokens
                    await notify(worker, parent, tokens=tokens)
