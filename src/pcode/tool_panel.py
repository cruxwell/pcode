"""Live tool activity: running calls only, since settled ones reach scrollback."""

import re
from dataclasses import dataclass, field
from time import monotonic

from rich.text import Text

from pcode.runtime import EditCompleted, ToolStarted, ToolSummary
from pcode.tool_display import JOB_HANDLE_TOOLS, PLAN_TOOLS, command_preview, label, plain

# Delegates outlive their own chatter, so they keep the panel's first rows.
DELEGATE = "delegate_task"
# Marks a row as a sub-agent rather than a tool. One terminal cell wide in
# common fonts, unlike emoji, so the panel's width math still holds.
AGENT_ICON = "»"
# The built-in sub-agent. Its rows lead with the purpose alone; any other
# agent's name is shown, since it can mean different tools and permissions.
DEFAULT_AGENT = "worker"
# A delegate's purpose leads its row, so it is cut shorter than the 60 cells
# a purpose may have: the clock and phase after it must still fit 80 columns.
PURPOSE_CELLS = 40
# On the status row, a command that finishes in milliseconds appears and
# vanishes before it can be read, and a burst of them strobes. A finished call
# keeps the row, marked done, for this long after it settles, unless real work
# starts first. The tally on the same row is the lasting record, so this only
# has to stop the flicker.
STATUS_DWELL = 0.6
# A sub-agent's plan is a window around its active task, like the parent's, but
# shorter: several delegates share the panel with the parent's own tasks.
CHILD_PLAN_ROWS = 3
# Rows for delegates beside the parent's tasks, before any child plans.
DELEGATE_ROWS = 3
# The parent's task window at the default height. With `tasks_max_height`
# set, the tasks fill whatever budget the tools leave instead.
TASK_ROWS = 5
ACTIVE_TASK_ICON = "↺"
PLAN_ICONS = {
    "pending": "○",
    "completed": "✓",
    "cancelled": "–",
    "blocked": "!",
}
# Slots in the palette's sub-agent hue ring (`Palette.agents`).
AGENT_HUES = 3
# Leading tree guides, then the icon, of a row `panel_fragments` colours in
# parts. Only task and sub-agent rows qualify: other rows pass through whole.
_TREE_GUIDE = re.compile(r"[│├└─ ]*")
_PART_STYLED = re.compile(rf"plan\.({'|'.join(['agent', 'in_progress', *PLAN_ICONS])})\b")


@dataclass
class _PanelNode:
    row: tuple[str, str]
    children: list["_PanelNode"] = field(default_factory=list)


def _tree_rows(nodes: list[_PanelNode], prefix: str | None = None) -> list[tuple[str, str]]:
    """Draw guides for the visible tree, leaving unparented roots undecorated."""
    rows = []
    for index, node in enumerate(nodes):
        last = index == len(nodes) - 1
        style, text = node.row
        branch = "" if prefix is None else prefix + ("└── " if last else "├── ")
        rows.append((style, branch + text))
        stem = "" if prefix is None else prefix + ("    " if last else "│   ")
        rows.extend(_tree_rows(node.children, stem))
    return rows


@dataclass
class ToolCall:
    event: ToolStarted
    started: float = field(default_factory=monotonic)
    settled: float | None = None
    failed: bool = False
    # A delegate's slot in the hue ring, kept for its whole run.
    hue: int = 0
    # What an edit changed (`+3 −1`), once its `EditCompleted` arrives: two
    # edits to one file are otherwise the same row.
    change: str = ""

    @property
    def elapsed(self) -> float:
        """Seconds so far; a settled call keeps the duration it finished with."""
        return (self.settled if self.settled is not None else monotonic()) - self.started

    def line(self, *, timed: bool = True) -> str:
        """The call without a status icon; each surface supplies its own.

        `timed=False` leaves the duration out, for the status row, which
        keeps its clock in a fixed column of its own.
        """
        event = self.event
        elapsed = self.elapsed if timed else None
        if event.name == DELEGATE:
            return self._delegate_line(elapsed)
        # A stated purpose is what this row is for: the widget is the one place
        # that shows a job while it runs, when the command has not paid off yet.
        command = (
            f"{event.purpose} · {command_preview(event.command)}"
            if event.command and event.purpose
            else command_preview(event.command)
            if event.command
            else ""
        )
        detail = plain(event.detail, limit=None)
        if event.name in JOB_HANDLE_TOOLS:
            # The id matches the job's own row and notices; the command says
            # what it runs. Neither is enough alone.
            detail = " · ".join(part for part in (detail, command) if part)
        else:
            detail = command or detail
        state = plain(event.activity) if event.activity else ""
        clock = "" if elapsed is None else f"{elapsed:.1f}s"
        return " · ".join(part for part in (label(event.name), state, clock, detail) if part)

    def _delegate_line(self, elapsed: float | None) -> str:
        """`» reviewing the fix · 5.5s · Thinking`: what the delegate is doing.

        The parent's stated purpose leads, since it is what tells delegates
        apart; the agent's name joins it only when it is not the default
        worker (`» Reviewer: checking the fix · ...`). Either way the head runs
        to the first ` · `, which `_row_parts` sets bold, and is kept short so
        the clock and state survive a narrow pane. With no purpose the task
        text stands in, and goes last: it can run long.

        A settled delegate's last phase is stale (nearly always "Responding"),
        so it says how it ended instead.
        """
        event = self.event
        if self.settled is not None:
            state = "Failed" if self.failed else "Done"
        else:
            state = plain(event.activity) or "Starting"
        name = event.agent[:1].upper() + event.agent[1:] or label(event.name)
        clock = "" if elapsed is None else f"{elapsed:.1f}s"
        if event.purpose:
            # Sessions saved before delegates recorded their agent had only workers.
            default = event.agent in ("", DEFAULT_AGENT)
            # Its own ` · ` would end the bold head partway through.
            purpose = plain(event.purpose.replace(" · ", " – "), PURPOSE_CELLS)
            head = purpose if default else f"{name}: {purpose}"
            parts = (f"{AGENT_ICON} {head}", clock, state)
        else:
            task = event.task or plain(event.detail, limit=None)
            parts = (f"{AGENT_ICON} {name}", clock, state, task)
        return " · ".join(part for part in parts if part)


@dataclass
class ToolHistory:
    """Calls still in flight, oldest first. A result removes its call from
    `calls`; `turn_calls` keeps the turn's top-level calls for the tool rows.

    The two surfaces split the work: the status row reports tool calls, and
    the task panel lists running delegates with their plans. A delegate
    leaves when it settles, taking its plan and its own calls with it, and
    scrollback keeps the record.
    """

    calls: list[ToolCall] = field(default_factory=list)
    # The last tool call (never a delegate) to leave, kept only so the status
    # row can hold it.
    recent: ToolCall | None = None
    # Each running delegate's plan, keyed by its call id.
    plans: dict[str, list[dict]] = field(default_factory=dict)
    # Every top-level call since the tool rows were last emptied, in start
    # order and kept once settled, so finished calls hold their rows. Recorded
    # here rather than when a frame draws: a call that started and finished
    # between two frames would otherwise never get a row.
    turn_calls: list[ToolCall] = field(default_factory=list)

    def record_edit(self, change: EditCompleted) -> None:
        """Note an edit's line counts on its call, running or just settled.

        A change with no diff (binary, too large) has no counts to
        give: `+0 −0` would say it changed nothing.
        """
        if change.omitted or change.operation == "unchanged":
            return
        for call in (*self.calls, self.recent):
            if call is not None and change.call_id and call.event.call_id == change.call_id:
                call.change = f"+{change.added} −{change.removed}"
                return

    def record_plan(self, call_id: str, items: list[dict]) -> None:
        if any(c.event.call_id == call_id for c in self.calls):
            self.plans[call_id] = items

    def record(self, event: ToolStarted | ToolSummary) -> None:
        # Planning operations have their own panel. Settled calls leave the
        # live view regardless of whether they need a scrollback entry.
        if event.name in PLAN_TOOLS:
            return
        existing = next(
            (c for c in self.calls if event.call_id and c.event.call_id == event.call_id), None
        )
        if isinstance(event, ToolSummary):
            if existing is None:
                return
            existing.failed = event.failed
            existing.settled = monotonic()
            if existing.event.name != DELEGATE:
                self.recent = existing
            # A sub-agent's calls leave the way the parent's do: scrollback
            # keeps them under their delegate. A finished delegate takes any
            # still listed, and its plan, with it.
            call_id = existing.event.call_id
            self.calls = [
                c for c in self.calls if c is not existing and c.event.parent_call_id != call_id
            ]
            self.plans.pop(call_id, None)
        elif existing is not None:
            # A restated start carries fresh progress, not a new invocation.
            existing.event = event
        else:
            call = ToolCall(event)
            if event.name == DELEGATE:
                # The first free slot, not the delegate's position: a slot that
                # followed position would recolour a worker when one before it
                # finished.
                used = [c.hue for c in self.delegates]
                call.hue = min(range(AGENT_HUES), key=lambda hue: (used.count(hue), hue))
            # Delegates have panel rows, and a sub-agent's calls are its own.
            elif not event.parent_call_id:
                self.turn_calls.append(call)
            self.calls.append(call)

    def clear(self) -> None:
        self.calls.clear()
        self.plans.clear()
        self.turn_calls.clear()
        self.recent = None

    @property
    def animating(self) -> bool:
        """Every listed call is live, so a delegate's panel row or the status row ticks."""
        return bool(self.calls)

    @property
    def active(self) -> ToolCall | None:
        """What the status row above the tasks reports.

        The newest running tool call, or else the one that just finished, for
        as long as its dwell lasts. Anything that starts meanwhile wins the
        row: holding a stale line over live work would be the worse lie.
        Delegates never take it; they have rows of their own on the panel.
        """
        running = next(
            (c for c in reversed(self.calls) if c.settled is None and c.event.name != DELEGATE),
            None,
        )
        if running is not None:
            return running
        held = self.recent
        if held is not None and monotonic() - (held.settled or held.started) < STATUS_DWELL:
            return held
        self.recent = None
        return None

    @property
    def running(self) -> int:
        """This agent's tool calls in flight, for the status row's `Running N tools`.

        Delegates are counted by `delegates` instead, and the calls a
        sub-agent makes are its own.
        """
        return sum(
            c.settled is None and not c.event.parent_call_id and c.event.name != DELEGATE
            for c in self.calls
        )

    @property
    def delegates(self) -> list[ToolCall]:
        """Every running delegate, oldest first: each keeps its panel row for its whole run."""
        return [c for c in self.calls if c.event.name == DELEGATE and c.settled is None]

    def plan_rows(self) -> int:
        """Rows the running delegates' plans would fill, before any budget."""
        return sum(
            min(CHILD_PLAN_ROWS, len(self.plans.get(c.event.call_id, []))) for c in self.delegates
        )

    def wanted(self) -> int:
        """Rows the delegates and their plans would fill, before any budget."""
        return len(self.delegates) + self.plan_rows()

    def rows(self, count: int, *, nested: bool = False, icon: str = ACTIVE_TASK_ICON):
        return _tree_rows(self._nodes(count, icon), "" if nested else None)

    def _nodes(self, count: int, icon: str) -> list[_PanelNode]:
        """Running sub-agents, each with a window of its plan beneath it.

        Only delegates get rows here. Ordinary calls, the parent's or a
        sub-agent's, mostly settle in milliseconds, so listing the ones running
        beside the status row made rows strobe in and out under the tasks. The
        status row and its `Running N tools` tally already cover them.
        """
        if count <= 0:
            return []
        delegates = self.delegates[:count]
        remaining = count - len(delegates)
        nodes = []
        for parent in delegates:
            # `»` stands in for a task icon: with a task's icon in front, a
            # sub-agent would read as one of the parent's tasks. Its hue,
            # shared with its plan rows, tells parallel sub-agents apart.
            node = _PanelNode((f"class:plan.agent,agent.hue.{parent.hue}", parent.line()))
            nodes.append(node)
            plan = self.plans.get(parent.event.call_id, [])
            steps, _ = plan_window(plan, min(CHILD_PLAN_ROWS, len(plan), remaining))
            remaining -= len(steps)
            node.children = [_PanelNode(plan_row(plan[index], icon, parent.hue)) for index in steps]
        return nodes


def plan_window(items: list[dict], count: int) -> tuple[range, int | None]:
    """The `count` steps worth showing, centred on the active one, and its index."""
    active = next((i for i, item in enumerate(items) if item["status"] == "in_progress"), None)
    anchor = active if active is not None else 0
    start = min(max(0, anchor - count // 2), max(0, len(items) - count))
    return range(start, start + count), active


def plan_row(item: dict, active_icon: str, hue: int | None = None) -> tuple[str, str]:
    """A task row, styled by its status, and by its sub-agent's hue when `hue` is set."""
    status = item["status"] if item["status"] in (*PLAN_ICONS, "in_progress") else "pending"
    style = f"class:plan.{status}" + ("" if hue is None else f",agent.hue.{hue}")
    icon = active_icon if status == "in_progress" else PLAN_ICONS[status]
    return style, f"{icon} {plain(item['content'], limit=None)}"


def task_panel_rows(
    items: list[dict],
    tools: ToolHistory,
    budget: int,
    active_icon: str,
    max_tasks: int = TASK_ROWS,
):
    """A bounded task viewport, with running delegates below the active task.

    Each delegate brings a window of its own plan. Tool calls stay on the
    status row. Keep at least one task visible, even on short panes, and
    never add headers or empty rows.
    """
    if budget <= 0:
        return []
    room = max(0, budget - bool(items))
    agent_rows = min(DELEGATE_ROWS + tools.plan_rows(), tools.wanted(), room)
    steps, active = plan_window(items, min(max_tasks, len(items), budget - agent_rows))
    nodes = []
    agent_nodes = tools._nodes(agent_rows, active_icon)
    for index in steps:
        node = _PanelNode(plan_row(items[index], active_icon))
        nodes.append(node)
        if index == active:
            node.children = agent_nodes
    if active is None or active not in steps:
        nodes.extend(agent_nodes)
    return _tree_rows(nodes)


def panel_fragments(lines: list, width: int):
    """Clip by terminal cells before prompt_toolkit renders non-wrapping rows.

    A row is one `(style, text)` pair, or a list of fragments already styled
    and fitted to the width (delta's edit preview, the barred thinking rows),
    which passes through.
    """
    fragments = []
    for index, row in enumerate(lines):
        if isinstance(row, list):
            fragments.append(("", "\n" if index else ""))
            fragments.extend(row)
            continue
        style, line = row
        text = Text(plain(line, limit=None))
        text.truncate(max(1, width), overflow="ellipsis")
        if index:
            fragments.append(("", "\n"))
        fragments.extend(_row_parts(style, text.plain))
    return fragments


def _row_parts(style: str, line: str) -> list[tuple[str, str]]:
    """Split a task or sub-agent row into guides, icon and text, each styled apart.

    The guides stay muted whatever the row's status, so a dimmed row keeps
    its place in the tree. A task's icon takes its status's colour even under
    a sub-agent's hue; a sub-agent's `»` and head (its purpose, or name) go bold in that hue.
    """
    kind = _PART_STYLED.search(style)
    if kind is None:
        return [(style, line)]
    guides = _TREE_GUIDE.match(line).end()
    icon_end = line.find(" ", guides)
    icon_end = len(line) if icon_end < 0 else icon_end
    if kind[1] == "agent":
        name_end = line.find(" · ", icon_end)
        icon_end, icon_style = (len(line) if name_end < 0 else name_end), f"{style} bold"
    else:
        icon_style = f"class:plan.icon.{kind[1]}"
    parts = [
        ("class:plan.tree", line[:guides]),
        (icon_style, line[guides:icon_end]),
        (style, line[icon_end:]),
    ]
    return [part for part in parts if part[1]]
