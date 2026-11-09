"""Each delegated worker's own stream, kept for the read-only viewer; no terminal imports.

The conversation's transcript shows a worker only as its delegate row and the
calls beneath it. This keeps what the worker itself said and did, in order, so
`/agents` can show it while it runs and after it settles. Memory only: nothing
here is journaled, so a resumed session starts with no workers.
"""

import json
from dataclasses import dataclass, field
from time import monotonic

from pcode.runtime import ChildPlan, ChildText, ToolStarted, ToolSummary, WorkerStatus
from pcode.tool_display import PLAN_TOOLS

DELEGATE = "delegate_task"
# Workers accumulate over a long session; keep the recent ones.
WORKER_HISTORY = 20
# WorkerStatus.status values that settle a worker, mapped to how it ended.
SETTLED = {"done": "", "failed": "", "stopped": "Stopped"}


def delegate_options(arguments: str | None) -> dict:
    """The delegate call's own arguments, or {} when they were not captured as a JSON object."""
    try:
        value = json.loads(arguments or "")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def option(options: dict, key: str) -> str:
    value = options.get(key)
    return str(value).strip() if value not in (None, "") else ""


@dataclass
class WorkerEntry:
    """One step of a worker's stream: a run of prose or reasoning, or one tool call."""

    kind: str  # "text", "thinking" or "tool"
    text: str = ""
    tool: ToolStarted | ToolSummary | None = None


@dataclass
class Worker:
    call_id: str
    agent: str
    task: str
    started: float = field(default_factory=monotonic)
    settled: float | None = None
    failed: bool = False
    # Why it stopped when that was not a normal finish, e.g. "Interrupted".
    ending: str = ""
    activity: str = ""
    plan: list[dict] = field(default_factory=list)
    entries: list[WorkerEntry] = field(default_factory=list)
    # Short id ("w1") the parent uses to answer, message or stop it; "" until known.
    worker_id: str = ""
    plan_step: str = ""
    model: str = ""
    workspace_mode: str = ""
    tokens: int = 0
    # A pending ask_parent question and what the worker will do if unanswered.
    question: str = ""
    fallback: str = ""
    review: str = ""  # "accepted", "rejected" or ""
    # Bumped on every change, so a viewer can skip unchanged repaints.
    version: int = 0

    @property
    def running(self) -> bool:
        return self.settled is None

    def elapsed(self) -> float:
        return (self.settled if self.settled is not None else monotonic()) - self.started

    def state(self) -> str:
        if self.running:
            if self.question:
                return "? asks parent"
            return self.activity or "Starting"
        if self.ending:
            return self.ending
        if self.failed:
            return "Failed"
        if self.review == "accepted":
            return "Done · reviewed"
        if self.review == "rejected":
            return "Done · rejected"
        return "Done"

    def apply(self, event: WorkerStatus) -> None:
        """Fold a status change; None fields leave their value alone."""
        if event.worker_id:
            self.worker_id = event.worker_id
        if event.question is not None:
            self.question = event.question
            self.fallback = event.fallback or ""
        if event.answered is not None:
            self.question = self.fallback = ""
        if event.tokens is not None:
            self.tokens = event.tokens
        if event.review is not None:
            self.review = event.review
        if event.workspace:
            self.workspace_mode = event.workspace
        if event.status == "running" and not self.running:
            # Continued with continue_worker: the same worker runs again.
            self.settled, self.failed, self.ending, self.review = None, False, "", ""
        elif event.status in SETTLED and self.running:
            self.settled = monotonic()
            self.failed = event.status != "done"
            self.ending = SETTLED[event.status]
        if not self.running:
            self.question = self.fallback = ""

    def add_text(self, event: ChildText) -> None:
        kind = "thinking" if event.thinking else "text"
        last = self.entries[-1] if self.entries else None
        if event.start or last is None or last.kind != kind:
            self.entries.append(WorkerEntry(kind, event.text))
        else:
            last.text += event.text

    def add_tool(self, event: ToolStarted | ToolSummary) -> None:
        """A start adds a row; its restatement or result replaces that row."""
        for entry in reversed(self.entries):
            if entry.tool is not None and entry.tool.call_id == event.call_id:
                entry.tool = event
                return
        self.entries.append(WorkerEntry("tool", tool=event))


@dataclass
class Workers:
    items: list[Worker] = field(default_factory=list)

    def get(self, call_id: str) -> Worker | None:
        return next((w for w in self.items if w.call_id == call_id), None)

    def record(self, event) -> None:
        """Fold one runtime event into the worker it belongs to, if any."""
        if isinstance(event, ToolStarted | ToolSummary) and event.name == DELEGATE:
            self._delegate(event)
            return
        if isinstance(event, WorkerStatus):
            if worker := self.get(event.call_id):
                worker.apply(event)
                worker.version += 1
            return
        call_id = (
            event.call_id
            if isinstance(event, ChildText | ChildPlan)
            else event.parent_call_id
            if isinstance(event, ToolStarted | ToolSummary)
            else ""
        )
        worker = self.get(call_id) if call_id else None
        if worker is None:
            return
        if isinstance(event, ChildText):
            worker.add_text(event)
        elif isinstance(event, ChildPlan):
            worker.plan = event.items
        elif event.name not in PLAN_TOOLS:
            # The plan has its own section; its tool calls would only repeat it.
            worker.add_tool(event)
        worker.version += 1

    def _delegate(self, event: ToolStarted | ToolSummary) -> None:
        worker = self.get(event.call_id)
        if isinstance(event, ToolStarted):
            if worker is None:
                options = delegate_options(event.arguments)
                worker = Worker(
                    event.call_id,
                    event.agent or "worker",
                    event.task,
                    plan_step=option(options, "plan_step"),
                    model=option(options, "model"),
                    workspace_mode=option(options, "workspace_mode"),
                )
                self.items.append(worker)
                del self.items[:-WORKER_HISTORY]
            # A restated start carries the worker's current phase.
            worker.activity = event.activity
        elif worker is not None and worker.running:
            worker.settled = monotonic()
            worker.failed = event.failed
        else:
            return
        worker.version += 1

    def end_turn(self) -> None:
        """A turn that ends under a running worker stopped it, e.g. by cancellation."""
        for worker in self.items:
            if worker.running:
                worker.settled = monotonic()
                worker.failed = True
                worker.ending = "Interrupted"
                worker.version += 1

    def running(self) -> int:
        return sum(worker.running for worker in self.items)

    def waiting(self) -> int:
        """Workers blocked on an ask_parent question."""
        return sum(bool(worker.question) for worker in self.items)

    def tokens(self) -> int:
        return sum(worker.tokens for worker in self.items)

    def latest(self) -> Worker | None:
        """The newest running worker, else the newest one."""
        running = [worker for worker in self.items if worker.running]
        return (running or self.items or [None])[-1]
