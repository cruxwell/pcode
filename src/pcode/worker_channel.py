"""A two-way channel between the parent agent and the workers it delegates to.

Delegation used to be fire-and-wait: a brief went down, one result came back.
This adds what both sides need in between, for every delegating session:

- Short worker ids (`w1`, `w2`, ...) the parent can name in later calls.
- `ask_parent` for a worker that would otherwise guess, answered with the
  parent's `manage_worker(action='answer')`; `action='message'` steers a
  running worker.
- The worker's final report and message history, kept so a later brief can
  include the report and a follow-up can resume the same worker.

Workers can delegate up to three levels deep (main -> worker -> sub-worker ->
leaf), so each delegating level owns a `WorkerHub`: the top-level session's
hub issues `w1`, `w2`, ... and saves finished workers to
`<session>/workers/<id>.json`, while a nested worker gets a per-run hub issuing
dotted ids (`w1.1`, `w1.1.1`, ...). `current_worker()` is the handle of the
delegation the caller runs inside, set by `delegate_task` for the child's whole
run. Once the top-level hub is attached to a saved session, each finished
worker is written to `<session>/workers/<id>.json` and read back on demand, so
a follow-up or a report still works after a restart or resume.

How a question reaches a parent that is blocked inside a foreground
`delegate_task` is `BackgroundDelegation`'s job; see dev/manager-mode.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

import anyio
from pydantic_ai import CapabilityEvent, ModelRetry
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessagesTypeAdapter, UserPromptPart
from pydantic_ai.toolsets import FunctionToolset

from pcode.steering import run_request

logger = logging.getLogger(__name__)

# Finished workers kept in memory; running ones are never dropped, and a saved
# session reloads a dropped one from disk when it is named again.
HISTORY = 20
# What a saved worker keeps besides its messages.
SAVED_FIELDS = (
    "id",
    "call_id",
    "agent",
    "task",
    "purpose",
    "plan_step",
    "workspace_mode",
    "worktree_task_id",
    "model",
    "status",
    "report",
    "review",
    "review_note",
    "tokens",
)
WORKER_ID = re.compile(r"w(\d+)")
# A report included in another brief is cut here; the worker's full output stays
# in its delegate result.
REPORT_LIMIT = 8000
ASK_WAIT_SECONDS = 600
ASK_WAIT_BOUNDS = (30, 3600)
# A shielded notice gives up after this, as Harness bounds its own shielded emits.
NOTIFY_SECONDS = 5
# Quiet this long before `manage_worker` list calls a running worker possibly
# stuck. Long: one thinking-heavy model call or test run can go many minutes.
STALL_SECONDS = 900
# `WorkerHandle.activity` before the worker's own stream starts.
QUEUED = "Queued for a worker slot"
SETTING_UP = "Setting up its worktree"

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
    "The parent reads only your final message; end it with a report under these headings "
    "('None' if empty): "
    + ", ".join(REPORT_HEADINGS)
    + ". Verified: only what you ran or checked. Decisions: choices other workers must "
    "follow. If ambiguity would change the "
    "result, call `ask_parent` rather than guess."
)

PARENT_INSTRUCTIONS = (
    "\nResults name a worker id (w1, ...). Answer a worker's question with `manage_worker`, "
    "asking the user if only they can decide; relay the user's 'Answer for w3: ...' or "
    "'Message for w3: ...' with action answer or message. A running worker can go "
    "minutes without visible progress inside one model call, and its token count stays "
    "0 until a call ends: that is not a stall. Judge it by `liveness` in manage_worker "
    "list, and stop or redo a running worker only when the user asks or it is clearly "
    "off track."
)


@dataclass(kw_only=True)
class WorkerUpdate(CapabilityEvent, namespace="pcode_workers", name="update"):
    """A change to one worker's dashboard state, keyed by its delegate call.

    `call_id` is explicit because the event is often emitted from another
    tool's call (`manage_worker`, a review) or from inside the child.
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
    # "isolated" or "shared", once delegation has decided.
    workspace: str | None = None


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
    worktree_task_id: str = ""
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
    # Order of the latest claim (new or continued), so "newest" survives resumes.
    claimed: int = 0
    # What a continued worker was before its claim, for `WorkerHub.release`.
    previous: tuple | None = None
    # Whether `delegate_task` got as far as running it since the last claim.
    started: bool = False
    # `<session>/workers` it belongs to, fixed at claim so a worker still running
    # across `/new` or `/resume` is saved with the session that started it.
    directory: Path | None = None
    # Dedicated `AntigravityModel(worker=True)` for this worker, reused across `continue_worker`.
    worker_model: Any = None
    # Liveness for `manage_worker(action='list')`, as `monotonic()` times. Without
    # it a running worker read as just "running, 0 tokens" (tokens only move when a
    # model call ends, and an `antigravity:` worker is one call), and parents
    # stopped live workers as stalled.
    started_at: float | None = None
    progressed_at: float | None = None
    activity: str = ""
    running_tools: list[str] = field(default_factory=list)
    tool_calls: int = 0

    @property
    def running(self) -> bool:
        return self.status == "running"

    def mark_started(self, activity: str = "Starting") -> None:
        self.started_at = self.progressed_at = monotonic()
        self.activity = activity
        self.running_tools = []
        self.tool_calls = 0

    def mark_progress(self) -> None:
        self.progressed_at = monotonic()

    def summary(self) -> dict[str, Any]:
        info: dict[str, Any] = {
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
        if self.worktree_task_id:
            info["worktree_task_id"] = self.worktree_task_id
        if self.running and self.started_at is not None:
            now = monotonic()
            quiet = round(now - (self.progressed_at or self.started_at))
            if not self.tokens:
                # Not counted yet, rather than none used.
                info["tokens"] = None
            info.update(
                elapsed_seconds=round(now - self.started_at),
                seconds_since_progress=quiet,
                activity=self.activity or "Starting",
                running_tools=list(self.running_tools),
                tool_calls=self.tool_calls,
                liveness=self.liveness(quiet),
            )
        return info

    def liveness(self, quiet: int) -> str:
        """What a running worker is doing, worded so a quiet one does not read as stuck."""
        if self.question is not None:
            return (
                "Waiting on your answer; reply with "
                f"manage_worker(action='answer', worker_id='{self.id}')."
            )
        if self.activity == QUEUED:
            # Waiting on other workers, never stuck on its own account.
            return f"{QUEUED}; it starts when another worker finishes."
        if self.running_tools:
            text = f"Running {', '.join(self.running_tools)}; last progress {quiet}s ago."
        elif self.activity == SETTING_UP:
            text = f"{SETTING_UP}; last progress {quiet}s ago."
        else:
            text = (
                f"In a model call; last progress {quiet}s ago. One model call can run "
                "for minutes with no visible progress, and tokens update only when it "
                "ends, so quiet or zero tokens is normal."
            )
        if quiet >= STALL_SECONDS:
            return (
                f"{text} No progress for over {STALL_SECONDS // 60} minutes, so it may be "
                "stuck; tell the user before stopping it."
            )
        return f"{text} Live: keep waiting; its result arrives on its own."


class WorkerHub:
    """The parent's workers by id and by delegate call. One per delegating run."""

    def __init__(self, *, prefix: str = "w") -> None:
        self._prefix = prefix
        self._handles: dict[str, WorkerHandle] = {}
        # By delegate call, including running workers detached by a session switch.
        self._by_call: dict[str, WorkerHandle] = {}
        self._count = 0
        self._claims = 0
        self._trimmed_tokens = 0
        # `<session>/workers` once attached to a saved session; memory only before.
        self._directory: Path | None = None

    def attach(self, session_directory: Path | None) -> None:
        """Save finished workers under this session, and resume them from it.

        Called each turn, since `/new` and `/resume` change the session. A
        switch forgets the old session's workers: finished ones are saved
        there, and running ones stay reachable only through their delegate
        call, so their ids cannot shadow this session's and they save to
        their own session when they settle. Token spend restarts with it.
        """
        directory = session_directory / "workers" if session_directory is not None else None
        if directory == self._directory:
            return
        if self._directory is not None:
            # Workers detached by an earlier switch that have settled since.
            for handle in {*self._by_call.values()} - {*self._handles.values()}:
                if not handle.running:
                    self._forget_detached(handle)
            for handle in list(self._handles.values()):
                self._forget(handle, detach=handle.running)
            self._count = self._trimmed_tokens = 0
        self._directory = directory
        self._count = max(self._count, *(_number(i)[0] for i in self._saved_ids()), 0)

    def handles(self) -> list[WorkerHandle]:
        return list(self._handles.values())

    def get(self, worker_id: str) -> WorkerHandle | None:
        key = worker_id.strip()
        if key.isdigit():
            key = f"{self._prefix}{key}"
        return self._handles.get(key) or self._load(key)

    def require(self, worker_id: str) -> WorkerHandle:
        handle = self.get(worker_id)
        if handle is None:
            ids = {*self._handles, *self._saved_ids()}
            known = ", ".join(sorted(ids, key=_number)) or "none yet"
            raise ModelRetry(f"Unknown worker {worker_id!r}. Known workers: {known}.")
        return handle

    def save(self, handle: WorkerHandle) -> None:
        """Write a settled worker to its session; a failure only logs."""
        if (directory := handle.directory) is None or handle.running:
            return
        data: dict[str, Any] = {name: getattr(handle, name) for name in SAVED_FIELDS}
        # An isolated worker cannot be resumed, so only its report is worth keeping.
        messages = handle.messages if handle.workspace_mode != "isolated" else []
        path = directory / f"{handle.id}.json"
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            data["messages"] = ModelMessagesTypeAdapter.dump_python(messages, mode="json")
            # Not `parents`: a session directory that is gone stays gone.
            directory.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        except Exception:
            # Losing a resumable history must not fail the delegation that made it.
            logger.warning("Could not save worker %s to %s", handle.id, path, exc_info=True)
            temporary.unlink(missing_ok=True)

    def _read_saved(self, worker_id: str, *, include_messages: bool = True) -> WorkerHandle | None:
        if self._directory is None or not WORKER_ID.fullmatch(worker_id):
            return None
        path = self._directory / f"{worker_id}.json"
        if path.is_symlink():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            messages = (
                ModelMessagesTypeAdapter.validate_python(data.get("messages") or [])
                if include_messages
                else []
            )
            handle = WorkerHandle(**{name: data[name] for name in SAVED_FIELDS if name in data})
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning("Could not load worker %s from %s", worker_id, path, exc_info=True)
            return None
        if handle.id != worker_id or handle.running or not isinstance(handle.tokens, int):
            return None
        handle.messages = list(messages)
        handle.directory = self._directory
        return handle

    def _load(self, worker_id: str) -> WorkerHandle | None:
        """A worker this session saved, back in memory; None if there is none."""
        handle = self._read_saved(worker_id, include_messages=True)
        if handle is None:
            return None
        # Its earlier tokens were spent before this process; only new ones count.
        # Evened out again when it is trimmed or forgotten.
        self._trimmed_tokens -= handle.tokens
        self._handles[handle.id] = handle
        self._trim(keep=handle)
        return handle

    def saved_summary(self, worker_id: str) -> dict[str, Any] | None:
        """Summary of a saved-on-disk worker without loading its messages into memory."""
        handle = self._read_saved(worker_id, include_messages=False)
        return handle.summary() if handle is not None else None

    def _saved_ids(self) -> list[str]:
        if self._directory is None:
            return []
        try:
            names = [path.stem for path in self._directory.glob("w*.json")]
        except OSError:
            return []
        return [name for name in names if WORKER_ID.fullmatch(name)]

    def for_call(self, call_id: str) -> WorkerHandle | None:
        return self._by_call.get(call_id)

    def claim(self, call_id: str, args: dict[str, Any]) -> WorkerHandle:
        """The handle for this delegate call, created or resumed on its first claim.

        Idempotent per call, so `BackgroundDelegation` (which needs the id for
        its receipt) and `delegate_task` (which runs it) agree on one handle.
        """
        if (handle := self.for_call(call_id)) is not None:
            return handle
        # Everything that can refuse the call is checked before any state changes.
        for worker_id in args.get("include_reports") or ():
            self.require(str(worker_id))
        agent = str(args.get("agent_name") or "worker")
        self._claims += 1
        if prior_id := args.get("continue_worker"):
            handle = self.require(str(prior_id))
            if args.get("workspace_mode") == "isolated":
                raise ModelRetry(
                    "continue_worker resumes a worker in the shared workspace it worked in; "
                    "drop workspace_mode='isolated'."
                )
            if handle.running:
                raise ModelRetry(
                    f"Worker {handle.id} is still running; steer it with "
                    "manage_worker(action='message')."
                )
            if handle.workspace_mode == "isolated":
                raise ModelRetry(
                    f"Worker {handle.id} ran in an isolated worktree, which cannot be resumed. "
                    "Integrate its branch and delegate a new task instead."
                )
            if not handle.messages:
                raise ModelRetry(
                    f"Worker {handle.id} ({handle.status}) left no history to continue from; "
                    "delegate a new task."
                )
            if handle.agent != agent:
                raise ModelRetry(f"Worker {handle.id} is a {handle.agent!r}; use that agent_name.")
            # The previous call no longer names it, so its row stops updating.
            self._unmap(handle)
            handle.previous = (handle.status, handle.review, handle.review_note, handle.call_id)
            handle.resume_from = handle.messages
            handle.status = "running"
            handle.review = handle.review_note = ""
            handle.stop_requested = False
            handle.cancel = None
        else:
            self._count += 1
            handle = WorkerHandle(
                id=f"{self._prefix}{self._count}",
                call_id=call_id,
                agent=agent,
                directory=self._directory,
            )
            self._handles[handle.id] = handle
            self._trim()
        handle.claimed = self._claims
        handle.started = False
        handle.call_id = call_id
        handle.task = str(args.get("task") or "")
        handle.purpose = str(args.get("purpose") or "")
        handle.plan_step = str(args.get("plan_step") or "")
        handle.model = str(args.get("model") or "")
        self._by_call[call_id] = handle
        return handle

    def release(self, handle: WorkerHandle) -> None:
        """Undo a claim whose call was refused before the worker started.

        A new worker is forgotten; a continued one gets back the state it had.
        """
        self._unmap(handle)
        handle.resume_from = None
        if handle.previous is None:
            self._handles.pop(handle.id, None)
            return
        handle.status, handle.review, handle.review_note, handle.call_id = handle.previous
        handle.previous = None
        self._by_call[handle.call_id] = handle

    def _trim(self, keep: WorkerHandle | None = None) -> None:
        finished = [h for h in self._handles.values() if not h.running and h is not keep]
        for handle in finished[: max(0, len(self._handles) - HISTORY)]:
            # Forgotten workers still count against a token budget.
            self._trimmed_tokens += handle.tokens
            self._forget(handle)

    def _forget(self, handle: WorkerHandle, *, detach: bool = False) -> None:
        """Drop a worker by id; `detach` keeps a running one reachable by its call."""
        del self._handles[handle.id]
        if not detach:
            self._forget_detached(handle)

    def _forget_detached(self, handle: WorkerHandle) -> None:
        if handle.worker_model is not None:
            handle.worker_model.retire()
            handle.worker_model = None
        self._unmap(handle)

    def retire_all(self) -> None:
        """Close every live worker model owned by this hub (e.g. when a nested run ends)."""
        for handle in {*self._handles.values(), *self._by_call.values()}:
            if handle.worker_model is not None:
                handle.worker_model.retire()
                handle.worker_model = None

    def _unmap(self, handle: WorkerHandle) -> None:
        self._by_call = {c: h for c, h in self._by_call.items() if h is not handle}

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
        """Every worker's tokens this session, including workers trimmed from history."""
        return self._trimmed_tokens + sum(handle.tokens for handle in self._handles.values())


def question_message(handle: WorkerHandle, question: Question) -> str:
    about = handle.purpose or " ".join(handle.task.split())[:80]
    fallback = (
        f" If it gets no answer it will proceed with: {question.fallback}"
        if question.fallback
        else ""
    )
    return (
        f"[pcode] Worker {handle.id} ({about}) asks: {question.text}\n{fallback}\n"
        f"Answer with manage_worker(action='answer', worker_id='{handle.id}', text=...). "
        "If only the user can decide, ask them in visible text and relay their reply; "
        "the worker waits meanwhile."
    ).replace("\n\n", "\n")


DOTTED_WORKER_ID = re.compile(r"w(\d+(?:\.\d+)*)")


def _number(worker_id: str) -> tuple[int, ...]:
    match = DOTTED_WORKER_ID.fullmatch(worker_id)
    return tuple(int(part) for part in match.group(1).split(".")) if match else (0,)


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


def resume_hint(handle: WorkerHandle) -> str:
    """Guidance added to a failed or stopped worker's result when it can be resumed or inspected.

    Only promised when true: an isolated worker is its branch, and an Antigravity
    worker's session ends with the failure (its tools ran inside one model call,
    so pcode's history misses what they did).
    """
    if handle.workspace_mode == "isolated":
        if handle.worktree_task_id:
            return (
                f" Worker {handle.id} ran in an isolated worktree (task_id="
                f"'{handle.worktree_task_id}', branch 'task-{handle.worktree_task_id}'); "
                "its partial work is preserved there. Inspect it with "
                "task_worktree(action='list')."
            )
        return ""
    if not handle.messages or handle.worker_model is not None:
        return ""
    return (
        f" Worker {handle.id} kept its history up to where it stopped: resume it with "
        f"delegate_task(continue_worker='{handle.id}', task=...) instead of starting a new "
        "worker."
    )


def interrupted_note(handle: WorkerHandle) -> str:
    """A line for a resumed worker whose last run failed or was stopped, else ''."""
    if handle.resume_from is None or handle.previous is None:
        return ""
    if handle.previous[0] not in ("failed", "stopped"):
        return ""
    return (
        f"[pcode] Your last run was cut off ({handle.previous[0]}). A tool it was running "
        "may have partly taken effect, so check the workspace before redoing an "
        "interrupted step.\n\n"
    )


def labelled(handle: WorkerHandle, result: Any) -> Any:
    """A delegate result that names its worker, so later calls can refer to it."""
    if isinstance(result, dict):
        return {"worker_id": handle.id, **result}
    return f"[worker {handle.id}]\n{result}"


async def ask_parent(
    question: str, fallback: str = "", wait_seconds: int = ASK_WAIT_SECONDS
) -> str:
    """Ask the parent a question and wait for the answer; do independent work first.

    Args:
        question: The question and the options you see.
        fallback: What you will do if no answer comes in time.
        wait_seconds: How long to wait (30 to 3600).
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
        with anyio.move_on_after(NOTIFY_SECONDS, shield=True):
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
