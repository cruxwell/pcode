"""Conversation paths, independent of model-message and tool-effect storage."""

from copy import deepcopy
from dataclasses import dataclass, field

from pcode.links import Link, extract_links, remember_link


def excerpt(text: str) -> str:
    """One safe line of ``text`` for a list row."""
    from pcode.diagnostics import strip_controls
    from pcode.ui import plain

    return plain(strip_controls(" ".join(text.split())), 100)


def side_record(record: dict) -> bool:
    """Whether `record` starts a /btw side question's node.

    A side question runs beside the conversation, often while a turn is in
    flight, and is journaled when it settles. Its node is a branch off where it
    was asked: it never becomes the active node, and it is never the turn that
    later records without a `run_id` belong to. Every reader that tracks "the
    turn started last" must skip it, and attribute records by `run_id`.
    """
    return record.get("kind") == "turn_started" and bool(record.get("side"))


@dataclass
class TurnNode:
    id: str
    parent: str | None
    prompt: str
    status: str = "interrupted"
    continuation: bool = False
    resend_blocked: bool = False
    response: str = ""
    plan: list[dict] = field(default_factory=list)
    # "turn", "compaction", or "aside": a /btw side question or a side
    # thread's summary, recorded like a turn that ran no tools.
    kind: str = "turn"
    # A side question's own details (see `side_record`): its thread, model,
    # label, effort, provider conversation id, outcome and how it was kept.
    # Empty for every other node, including a thread's summary.
    side: dict = field(default_factory=dict)
    # Unsaved turns and durable compaction checkpoints carry message history.
    history: list | None = None
    links: dict[str, Link] = field(default_factory=dict)
    message_links: dict[str, Link] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.add_links(self.prompt, "user")
        self.add_links(self.response, "assistant")

    def add_links(self, text: str, source: str, *, tool: bool = False) -> None:
        # Keep URL recency in event order, including assistant text before tools.
        for link in extract_links(text, source):
            remember_link(self.links, link)
            if not tool:
                remember_link(self.message_links, link)


class ConversationTree:
    def __init__(self) -> None:
        self.nodes: dict[str, TurnNode] = {}
        self.active: str | None = None
        # Which turn a record belongs to when it does not say. Older journals
        # rely on this entirely; they could never have two turns open at once.
        self.recording: str | None = None

    def path(self, identity: str | None) -> list[str]:
        path = []
        while identity is not None:
            if identity in path or identity not in self.nodes:
                raise ValueError("Invalid conversation tree path.")
            path.append(identity)
            identity = self.nodes[identity].parent
        return path[::-1]

    def consume(self, record: dict) -> None:
        kind = record.get("kind")
        if kind == "turn_started" and record.get("run_id"):
            identity = record["run_id"]
            parent = record.get("parent_id", self.active)  # Old sessions form a linear tree.
            self.path(parent)
            side = record.get("side")
            self.nodes[identity] = TurnNode(
                identity,
                parent,
                record["prompt"],
                continuation=record.get("continuation", False),
                plan=deepcopy(self.nodes[parent].plan) if parent else [],
                kind="aside" if record.get("aside") else "turn",
                side=dict(side) if isinstance(side, dict) else {},
            )
            if not side_record(record):
                self.active = self.recording = identity
        elif kind == "compaction_checkpoint":
            from pydantic_ai.messages import ModelMessagesTypeAdapter

            identity, parent = record["node_id"], record["parent_id"]
            self.path(parent)
            history = ModelMessagesTypeAdapter.validate_python(record["messages"])
            self.nodes[identity] = TurnNode(
                identity,
                parent,
                record.get("focus", ""),
                status="completed",
                response=f"Context compacted: ~{record['before']} → ~{record['after']} tokens",
                plan=deepcopy(record["plan"]),
                history=history,
                kind="compaction",
            )
            self.active = identity
            self.recording = None
        elif kind == "tree_selected":
            self.path(record["node_id"])
            self.active = record["node_id"]
        # Attribute by the record's own run_id, not its position, so a record from
        # another branch's turn is never absorbed into whichever turn started last.
        # Journals from before run_id fall back to `recording` (see
        # `SessionJournal.active_records`).
        elif (node := self.nodes.get(record.get("run_id") or self.recording)) is not None:
            if kind == "Message":
                node.response = record["markdown"]
                node.add_links(node.response, "assistant")
            elif kind in {"ToolStarted", "ToolSummary"}:
                # Keep only URLs, not another copy of potentially large payloads.
                # The same journal events rebuild these on session resume.
                source = record.get("name") or "tool"
                for key in ("arguments", "result", "command", "detail", "error"):
                    text = record.get(key)
                    if isinstance(text, str):
                        node.add_links(text, source, tool=True)
            elif kind == "PlanUpdated":
                node.plan = deepcopy(record["items"])
            elif kind in {"turn_completed", "turn_failed", "turn_cancelled"}:
                node.status = kind.removeprefix("turn_")
                node.resend_blocked = record.get("resend_blocked", False)
                if node.side:
                    # The side question's own words for it: "timed out" is a failure.
                    node.side["status"] = record.get("side_status", "")
                    node.side["error"] = record.get("side_error", "")
            elif kind == "side_kept" and node.side:
                node.side["kept"] = record.get("how", "")

    def rows(self) -> list[tuple[tuple[str | None, bool], str]]:
        """Depth-first user/edit and assistant/continue rows, with stable IDs."""
        rows = [
            ((None, False), "Conversation start" + (" ← active" if self.active is None else ""))
        ]
        children: dict[str | None, list[TurnNode]] = {}
        for node in self.nodes.values():
            children.setdefault(node.parent, []).append(node)
        # Iterative traversal also handles very long sessions. Only a fork adds
        # a level: user/assistant pairs and single-child continuations stay aligned.
        roots = children.get(None, [])

        def forks(nodes: list[TurnNode], parent: TurnNode | None = None) -> bool:
            # A side question is drawn as a branch even as the only child: in
            # line, it would read as the conversation's next turn. Its own
            # follow-ups continue its thread, so they stay in line under it.
            if len(nodes) > 1:
                return True
            return (parent is None or not parent.side) and any(node.side for node in nodes)

        pending = [
            (node, "", forks(roots), i == len(roots) - 1)
            for i, node in reversed(list(enumerate(roots)))
        ]
        while pending:
            node, prefix, fork, last = pending.pop()
            # Keep labels visible even with many nested forks.
            if len(prefix) > 24:
                prefix = "… " + prefix[-22:]
            connector = ("└─ " if last else "├─ ") if fork else ""
            if node.kind != "compaction":
                who = "btw: " if node.kind == "aside" else "user: "
                rows.append(((node.id, True), prefix + connector + who + excerpt(node.prompt)))
            continuation = prefix + (("   " if last else "│  ") if fork else "")
            rows.append(
                (
                    (node.id, False),
                    continuation
                    + ("compaction: " if node.kind == "compaction" else "assistant: ")
                    + excerpt(node.response or f"[{node.status}; last safe checkpoint]")
                    + (f" [{node.status}]" if node.response and node.status != "completed" else "")
                    + (" ← active" if node.id == self.active else ""),
                )
            )
            descendants = children.get(node.id, [])
            pending.extend(
                (child, continuation, forks(descendants, node), i == len(descendants) - 1)
                for i, child in reversed(list(enumerate(descendants)))
            )
        return rows
