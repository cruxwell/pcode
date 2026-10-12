"""Forward child activity; child prose goes to the worker viewer, never the transcript.

Harness 0.31 supplies delegation lifecycle events and a child stream handler, but
not the parent's call identity in that handler. A context-local execution wrapper
pairs concurrent children with their own parent tool call. No global event queue
or mutable per-agent handler is needed, and cancellation resets the binding.
"""

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from time import monotonic

from pydantic_ai import CapabilityEvent, RunContext
from pydantic_ai.agent import WrapperAgent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)

from pcode import retries
from pcode.cache_warnings import CacheBustEvent
from pcode.diagnostics import transient
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
    execution_mode,
    invocation,
    result_detail,
    stated_purpose,
    stored_source,
    subject,
    target,
)

_parent: ContextVar[RunContext | None] = ContextVar("delegation_parent", default=None)
logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class ChildActivity(CapabilityEvent, namespace="pcode_delegation", name="activity"):
    activity: str
    child: ToolStarted | ToolSummary | None = None
    # The child's whole plan, sent only when it changed.
    plan: list[dict] | None = None


@dataclass(kw_only=True)
class ChildOutput(CapabilityEvent, namespace="pcode_delegation", name="output"):
    """A slice of the child's prose or reasoning; `start` opens a new part."""

    text: str
    thinking: bool = False
    start: bool = False


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

    async def run(self, user_prompt=None, *, message_history=None, capabilities=None, **kwargs):
        attempts = int(load_preferences().get("retry_attempts", SETTINGS["retry_attempts"].default))
        attempt = 0
        while True:
            checkpoint = RequestCheckpoint()
            try:
                return await self.wrapped.run(
                    user_prompt,
                    message_history=message_history,
                    capabilities=[*(capabilities or ()), checkpoint],
                    **kwargs,
                )
            except Exception as error:
                if attempt >= attempts or checkpoint.messages is None or not transient(error):
                    raise
                attempt += 1
                user_prompt, message_history = None, checkpoint.messages
                # The panel shows the reconnect; a warning would print over it.
                logger.debug(
                    "Sub-agent %r: %s Retrying %d/%d",
                    self.name,
                    error_message(error),
                    attempt,
                    attempts,
                )
                if (parent := _parent.get()) is not None:
                    await parent.emit(ChildActivity(activity=f"Reconnecting {attempt}/{attempts}"))
                await asyncio.sleep(retries.RETRY_DELAY)


async def stream_child_activity(_ctx, events):
    """Consume child streams: tool boundaries, phase changes, plans and prose."""
    parent = _parent.get()
    plan_items: list[dict] = []
    tools = {}
    # The child's calls by id, so its `read_tool_result` rows name what spilled.
    called: dict[str, str] = {}
    phase = ""
    async for event in events:
        if parent is None:
            continue
        if isinstance(event, FileChangeEvent):
            await parent.emit(
                FileChangeEvent(
                    change=replace(
                        event.change,
                        call_id=f"{parent.tool_call_id or ''}:{event.change.call_id}",
                    )
                )
            )
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
            outcome = "retry" if isinstance(event.part, RetryPromptPart) else event.part.outcome
            detail, failed = result_detail(
                name, args, event.part.content, outcome, stored_source(args, called)
            )
            child = ToolSummary(
                name,
                detail,
                failed=failed,
                call_id=f"{parent_id}:{event.tool_call_id}",
                elapsed_seconds=max(0, monotonic() - started),
                command=invocation(name, args),
                purpose=stated_purpose(args),
                error=command_error(event.part.content) if failed and name in COMMAND_TOOLS else "",
                result=capture(event.part.content),
                run_id=parent.run_id or "",
                outcome=outcome,
                parent_call_id=parent_id,
            )
            activity = "Working" if tools else "Waiting for model"
        elif isinstance(event, PartStartEvent):
            if isinstance(event.part, ThinkingPart):
                activity = "Thinking"
            elif isinstance(event.part, TextPart):
                activity = "Responding"
        if child is not None or activity != phase:
            await parent.emit(ChildActivity(activity=activity, child=child))
            phase = activity
