"""A two-way channel between the parent agent and the workers it delegates to.

Delegation used to be fire-and-wait: a brief went down, one result came back.
This adds what both sides need in between, for every delegating session:

- Short worker ids (`w1`, `w2`, ...) the parent can name in later calls.
- `ask_parent` for a worker that would otherwise guess, answered with the
  parent's `answer_worker`; `message_worker` to steer a running worker.
- The worker's final report and message history, kept so a later brief can
  include the report and a follow-up can resume the same worker.

Workers are one level deep (a worker cannot delegate), so a worker's parent is
always the top-level session. The `WorkerHub` belongs to the parent's
`WorkspaceSubAgents`; `current_worker()` is the handle of the delegation the
caller runs inside, set by `delegate_task` for the child's whole run. Memory
only: a reload or restart starts with no workers.

How a question reaches a parent that is blocked inside a foreground
`delegate_task` is `BackgroundDelegation`'s job; see dev/manager-mode.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import anyio
from pydantic_ai import CapabilityEvent, ModelRetry
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.toolsets import FunctionToolset

from pcode.steering import run_request

# Finished workers kept for reports and follow-ups; running ones are never dropped.
HISTORY = 20
# A report included in another brief is cut here; the worker's full output stays
# in its delegate result.
REPORT_LIMIT = 8000
ASK_WAIT_SECONDS = 600
ASK_WAIT_BOUNDS = (30, 3600)

_current: ContextVar[WorkerHandle | None] = ContextVar("current_worker", default=None)

REPORT_HEADINGS = (
    "Summary",
    "Changes",
    "Commands run",
    "Verified",
    "Not verified",
    "Open questions",
    "Decisions",
)

CHILD_INSTRUCTIONS = (
    "You are a delegated worker. Your parent agent reads only your final message, so end "
    "it with a report under these Markdown headings, writing 'None' where one is empty: "
    + ", ".join(f"`## {heading}`" for heading in REPORT_HEADINGS)
    + ". Under Verified list only what you actually ran or checked; anything you believe "
    "but did not check goes under Not verified. Under Decisions list choices other workers "
    "must stay consistent with. If the brief is ambiguous in a way that changes the result, "
    "call `ask_parent` instead of guessing, and give the fallback you will use if no answer "
    "comes. Messages from the parent can arrive while you work; follow them."
)

PARENT_INSTRUCTIONS = (
    "\nEach delegation gets a worker id (w1, w2, ...) named in its result. Write a brief "
    "the worker can act on alone: put the goal in `task`, and use `context` (files, earlier "
    "decisions), `boundaries` (what not to touch) and `done_when` (how it knows it is "
    "finished) when they apply. `include_reports` hands earlier workers' reports to a "
    "dependent step; `plan_step` links the worker to one of your plan step ids; "
    "`run_in_background` returns at once so you can dispatch independent workers and keep "
    "talking to the user; `continue_worker` resumes a finished shared-mode worker with its "
    "own context for a follow-up. A worker may ask you a question: answer it with "
    "`answer_worker`, asking the user first when only they can decide. Steer a running "
    "worker with `message_worker` and stop one with `stop_worker`. When the user writes "
    "'Answer for w3: ...' or 'Message for w3: ...', relay it with the matching tool."
)


@dataclass(kw_only=True)
class WorkerUpdate(CapabilityEvent, namespace="pcode_workers", name="update"):
    """A change to one worker's dashboard state, keyed by its delegate call.

    `call_id` is explicit because the event is often emitted from another
    tool's call (`answer_worker`, a review) or from inside the child.
    Fields left at None did not change.
    """

    call_id: str
    worker_id: str
    question: str | None = None
    fallback: str | None = None
    answered: bool | None = None
    tokens: int | None = None
    review: str | None = None
    status: str | None = None


@dataclass(eq=False)
class Question:
    text: str
    fallback: str = ""
    delivered: bool = False
    answer: str | None = None
    answered: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(eq=False)
class WorkerHandle:
    id: str
    call_id: str
    agent: str = ""
    task: str = ""
    purpose: str = ""
    plan_step: str = ""
    workspace_mode: str = "shared"
    model: str = ""
    # running, done, failed or stopped.
    status: str = "running"
    report: str = ""
    # The child's whole history once it settled, for `continue_worker`.
    messages: list = field(default_factory=list)
    # A finished worker's history to resume from, taken by its next run.
    resume_from: list | None = None
    inbox: list[str] = field(default_factory=list)
    question: Question | None = None
    review: str = ""
    review_note: str = ""
    tokens: int = 0
    # Set by `BackgroundDelegation` while the call is detached.
    cancel: Callable[[], None] | None = None
    stop_requested: bool = False

    @property
    def running(self) -> bool:
        return self.status == "running"

    def summary(self) -> dict[str, Any]:
        return {
            "worker_id": self.id,
            "agent": self.agent,
            "purpose": self.purpose or " ".join(self.task.split())[:80],
            "status": self.status,
            "plan_step": self.plan_step,
            "workspace_mode": self.workspace_mode,
            "model": self.model,
            "tokens": self.tokens,
            "review": self.review,
            "waiting_on_answer": self.question is not None,
        }


class WorkerHub:
    """The parent's workers by id and by delegate call. One per parent agent."""

    def __init__(self) -> None:
        self._handles: dict[str, WorkerHandle] = {}
        self._by_call: dict[str, str] = {}
        self._count = 0

    def handles(self) -> list[WorkerHandle]:
        return list(self._handles.values())

    def get(self, worker_id: str) -> WorkerHandle | None:
        key = worker_id.strip()
        if key.isdigit():
            key = f"w{key}"
        return self._handles.get(key)

    def require(self, worker_id: str) -> WorkerHandle:
        handle = self.get(worker_id)
        if handle is None:
            known = ", ".join(self._handles) or "none yet"
            raise ModelRetry(f"Unknown worker {worker_id!r}. Known workers: {known}.")
        return handle

    def for_call(self, call_id: str) -> WorkerHandle | None:
        worker_id = self._by_call.get(call_id)
        return self._handles.get(worker_id) if worker_id else None

    def claim(self, call_id: str, args: dict[str, Any]) -> WorkerHandle:
        """The handle for this delegate call, created or resumed on its first claim.

        Idempotent per call, so `BackgroundDelegation` (which needs the id for
        its receipt) and `delegate_task` (which runs it) agree on one handle.
        """
        if (handle := self.for_call(call_id)) is not None:
            return handle
        agent = str(args.get("agent_name") or "worker")
        if prior_id := args.get("continue_worker"):
            handle = self.require(str(prior_id))
            if handle.running:
                raise ModelRetry(
                    f"Worker {handle.id} is still running; use message_worker to steer it."
                )
            if handle.workspace_mode == "isolated":
                raise ModelRetry(
                    f"Worker {handle.id} ran in an isolated worktree, which cannot be resumed. "
                    "Integrate its branch and delegate a new task instead."
                )
            if not handle.messages:
                raise ModelRetry(f"Worker {handle.id} left no history to continue from.")
            if handle.agent != agent:
                raise ModelRetry(f"Worker {handle.id} is a {handle.agent!r}; use that agent_name.")
            # The previous call no longer names it, so its row stops updating.
            self._by_call = {c: w for c, w in self._by_call.items() if w != handle.id}
            handle.resume_from = handle.messages
            handle.status = "running"
            handle.review = handle.review_note = ""
        else:
            self._count += 1
            handle = WorkerHandle(id=f"w{self._count}", call_id=call_id, agent=agent)
            self._handles[handle.id] = handle
            self._trim()
        handle.call_id = call_id
        handle.task = str(args.get("task") or "")
        handle.purpose = str(args.get("purpose") or "")
        handle.plan_step = str(args.get("plan_step") or "")
        handle.model = str(args.get("model") or "")
        self._by_call[call_id] = handle.id
        return handle

    def _trim(self) -> None:
        finished = [h for h in self._handles.values() if not h.running]
        for handle in finished[: max(0, len(self._handles) - HISTORY)]:
            del self._handles[handle.id]
            self._by_call = {c: w for c, w in self._by_call.items() if w != handle.id}

    def has_undelivered(self) -> bool:
        return any(
            h.question is not None and not h.question.delivered for h in self._handles.values()
        )

    def take_questions(self) -> list[str]:
        """Undelivered questions as messages for the parent, marked delivered."""
        messages = []
        for handle in self._handles.values():
            question = handle.question
            if question is None or question.delivered:
                continue
            question.delivered = True
            messages.append(question_message(handle, question))
        return messages

    def total_tokens(self) -> int:
        return sum(handle.tokens for handle in self._handles.values())


def question_message(handle: WorkerHandle, question: Question) -> str:
    about = handle.purpose or " ".join(handle.task.split())[:80]
    fallback = (
        f" If it gets no answer it will proceed with: {question.fallback}"
        if question.fallback
        else ""
    )
    return (
        f"[pcode] Worker {handle.id} ({about}) asks: {question.text}\n{fallback}\n"
        f"Answer with answer_worker('{handle.id}', ...). If only the user can decide, ask them "
        "in visible text and relay their reply; the worker waits meanwhile."
    ).replace("\n\n", "\n")


def current_worker() -> WorkerHandle | None:
    return _current.get()


def bind_worker(handle: WorkerHandle | None):
    """Make `handle` the current worker; returns the token for `unbind_worker`."""
    return _current.set(handle)


def unbind_worker(token) -> None:
    _current.reset(token)


def hub_for(ctx) -> WorkerHub | None:
    """The parent's hub, from the run's capabilities; None in a worker or without delegation."""
    for capability in (getattr(ctx, "capabilities", None) or {}).values():
        hub = getattr(capability, "hub", None)
        if isinstance(hub, WorkerHub):
            return hub
    return None


async def notify(handle: WorkerHandle, ctx=None, **changes) -> None:
    """Tell the parent's display about a change; from the parent (`ctx`) or the child."""
    if ctx is None:
        from pcode.delegation import _parent

        ctx = _parent.get()
    if ctx is None:
        return
    await ctx.emit(WorkerUpdate(call_id=handle.call_id, worker_id=handle.id, **changes))


def render_brief(
    task: str,
    *,
    context: str = "",
    boundaries: str = "",
    done_when: str = "",
    reports: list[WorkerHandle] = (),  # type: ignore[assignment]
) -> str:
    """The task with its optional sections, as the worker reads it."""
    parts = [task.strip()]
    for heading, body in (
        ("Context", context),
        ("Boundaries", boundaries),
        ("Done when", done_when),
    ):
        if body.strip():
            parts.append(f"## {heading}\n{body.strip()}")
    if reports:
        parts.append("## Reports from earlier workers")
        for handle in reports:
            about = handle.purpose or " ".join(handle.task.split())[:80]
            report = handle.report.strip() or "(no report)"
            if len(report) > REPORT_LIMIT:
                report = report[:REPORT_LIMIT] + "\n[report truncated]"
            parts.append(f"### {handle.id}: {about}\n{report}")
    return "\n\n".join(parts)


def report_text(result: Any) -> str:
    """The worker's report from a delegate result: its output, or an isolated record's summary."""
    if isinstance(result, dict):
        return str(result.get("summary") or "")
    return str(result or "")


def labelled(handle: WorkerHandle, result: Any) -> Any:
    """A delegate result that names its worker, so later calls can refer to it."""
    if isinstance(result, dict):
        return {"worker_id": handle.id, **result}
    return f"[worker {handle.id}]\n{result}"


async def ask_parent(
    question: str, fallback: str = "", wait_seconds: int = ASK_WAIT_SECONDS
) -> str:
    """Ask the parent agent a question and wait for its answer.

    Use it when the brief is ambiguous in a way that changes the result, rather
    than guessing. The parent may need to ask the user, so keep working on
    anything that does not depend on the answer before asking.

    Args:
        question: The question, with the options you see and what each implies.
        fallback: What you will do if no answer arrives in time.
        wait_seconds: How long to wait for an answer (30 to 3600).
    """
    handle = current_worker()
    if handle is None:
        raise ModelRetry("There is no parent to ask here; decide from the evidence.")
    if handle.question is not None:
        raise ModelRetry("A question is already waiting on the parent.")
    pending = Question(question.strip(), fallback.strip())
    handle.question = pending
    await notify(handle, question=pending.text, fallback=pending.fallback)
    low, high = ASK_WAIT_BOUNDS
    try:
        with anyio.move_on_after(min(max(int(wait_seconds), low), high)):
            await pending.answered.wait()
    finally:
        handle.question = None
        with anyio.CancelScope(shield=True):
            await notify(handle, answered=pending.answer is not None)
    if pending.answer is None:
        tail = f" Proceed with your fallback: {pending.fallback}" if pending.fallback else ""
        return f"No answer from the parent in time.{tail} Note the assumption in your report."
    return f"The parent answered: {pending.answer}"


class ChildChannel(AbstractCapability):
    """`ask_parent`, parent messages and the report format, for every delegate.

    Shared by all delegates (Harness's `shared_capabilities`), so all per-child
    state comes from `current_worker()`, never from this instance.
    """

    def __init__(self) -> None:
        self._toolset = FunctionToolset([ask_parent])

    def get_instructions(self):
        return CHILD_INSTRUCTIONS

    def get_toolset(self):
        return self._toolset

    async def before_model_request(self, ctx, request_context):
        handle = current_worker()
        if handle is not None and handle.inbox:
            messages, handle.inbox = handle.inbox, []
            request = run_request(ctx, request_context)
            for message in messages:
                request.parts.append(UserPromptPart(f"[Message from your parent agent] {message}"))
        return request_context
