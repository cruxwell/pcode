"""Read-only, bounded retrieval over saved conversations, independent of the UI."""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pcode.conversation_tree import side_record
from pcode.diagnostics import redact
from pcode.history_cursor import JournalReader, advance
from pcode.sessions import SessionInfo, SessionReadBudget, list_sessions
from pcode.worktree import WORKTREES_DIR, project_checkout

Scope = Literal["session", "project", "workspace", "all"]
KINDS = (
    "turn_started",
    "Message",
    "ToolSummary",
    "turn_completed",
    "turn_failed",
    "turn_cancelled",
    "tree_selected",
    "compaction_checkpoint",
    "auto_compacted",
    "steering",
)
CHUNK_CHARS = 4000
MAX_CHUNKS = 10_000
# Journal bytes read, not searchable text: most of a transcript is streaming
# deltas that the record filter discards. Scanning is ~75 MB/s on a warm cache,
# so this bounds journal I/O per call; `after` resumes without rereading prefixes.
MAX_SCAN_BYTES = 256 * 1024 * 1024
# Ranking is chunk-level, so one long session can otherwise fill the whole limit.
PER_SESSION_LIMIT = 3
# Enough occurrences to find prose without scanning a pathological chunk repeatedly.
MAX_MATCH_POSITIONS = 200
EXCERPT_CHARS = 800
CONCLUSION_CHARS = 240


@dataclass
class HistoryTurn:
    id: str
    parent: str | None
    time: str
    status: str = "incomplete"
    active: bool | None = False
    # Auto-compaction dropped part of this turn from the model's own context,
    # which makes the journal the only copy even while the turn is still running.
    compacted: bool = False
    blocks: list[str] = field(default_factory=list)

    @property
    def branch(self) -> str:
        return "unknown" if self.active is None else "active" if self.active else "inactive"

    @property
    def text(self) -> str:
        return redact("\n\n".join(self.blocks))

    @property
    def conclusion(self) -> str:
        """The last assistant text: what the turn decided, not how it got there."""
        for block in reversed(self.blocks):
            if block.startswith("Assistant: "):
                return redact(block.removeprefix("Assistant: ")).strip()
        return ""


@dataclass
class Chunk:
    session: SessionInfo
    turn: HistoryTurn
    offset: int
    text: str

    def excerpt_start(self, query: str) -> int:
        """Anchor on the densest match in prose: tool lines rarely hold the answer."""
        words = list(dict.fromkeys(query.casefold().split()))
        folded = self.text.casefold()
        positions = sorted(
            {
                match.start()
                for word in words
                for match in list(re.finditer(re.escape(word), folded))[:MAX_MATCH_POSITIONS]
            }
        )
        if not positions:
            return 0
        candidates = [
            position
            for position in positions
            if not self.text.startswith("Tool: ", self.text.rfind("\n", 0, position) + 1)
        ]

        def density(position: int) -> int:
            window = folded[position : position + EXCERPT_CHARS]
            return sum(word in window for word in words)

        # Most distinct terms in the window the excerpt would show; ties go earlier.
        best = max(candidates or positions, key=lambda position: (density(position), -position))
        return max(0, best - 150)

    def result(self, query: str) -> dict:
        start = self.excerpt_start(query)
        excerpt = self.text[start : start + EXCERPT_CHARS]
        result = {
            "turn_id": self.turn.id,
            "time": self.turn.time,
            "status": self.turn.status,
            "branch": self.turn.branch,
            "offset": self.offset + start,
            "excerpt": excerpt,
        }
        # "Did we already do X" is answered by the turn's last word on it, which
        # a match in the middle of a long turn would otherwise bury.
        conclusion = self.turn.conclusion[:CONCLUSION_CHARS]
        if conclusion and conclusion not in excerpt:
            result["conclusion"] = conclusion
        return result


@dataclass
class Scan:
    """Searchable chunks plus the coverage of the scan that produced them."""

    chunks: list[Chunk]
    warnings: list[str]
    sessions_in_scope: int = 0
    sessions_searched: int = 0
    sessions_unreadable: int = 0
    sessions_partial: int = 0
    next_cursor: str | None = None
    scan_complete: bool = False


def _turn_of(record: dict, turns: dict[str, "HistoryTurn"], recording: str | None):
    """The turn a record belongs to: the one it names, else the last one started.

    The fallback is for journals written before records carried a run ID, and
    for a scan window that began after the turn's own start record.
    """
    identity = record.get("run_id")
    if not isinstance(identity, str) or identity not in turns:
        identity = recording
    return turns.get(identity) if identity is not None else None


class TurnReader:
    """Keep attribution and ancestry intact across journal byte pages."""

    def __init__(self, info: SessionInfo, root: Path):
        self.journal = JournalReader(info, root, KINDS)
        self.turns: dict[str, HistoryTurn] = {}
        self.parents: dict[str, str | None] = {}
        self.active: str | None = None
        self.recording: str | None = None

    def read(self, budget: SessionReadBudget):
        for record in self.journal.records(budget):
            self.accept(record)
        if self.journal.done:
            self.finish()

    def accept(self, record: dict):
        kind = record["kind"]
        if kind == "turn_started" and isinstance(record.get("prompt"), str):
            identity = record.get("run_id") or f"turn-{len(self.turns) + 1}"
            parent = record.get("parent_id", self.active)
            if not isinstance(identity, str) or not (parent is None or isinstance(parent, str)):
                return
            self.parents[identity] = parent
            self.turns[identity] = HistoryTurn(
                identity,
                parent,
                str(record.get("time", self.journal.info.created)),
                active=None,
                blocks=[
                    ("User (side question): " if record.get("side") else "User: ")
                    + record["prompt"]
                ],
            )
            # A side question is a branch beside the conversation, never its head.
            if not side_record(record):
                self.active = self.recording = identity
        elif kind == "compaction_checkpoint":
            identity, parent = record.get("node_id"), record.get("parent_id")
            if isinstance(identity, str) and (parent is None or isinstance(parent, str)):
                self.parents[identity] = parent
                self.active, self.recording = identity, None
        elif kind == "tree_selected":
            identity = record.get("node_id")
            if identity is None or isinstance(identity, str):
                self.active = identity
        elif (turn := _turn_of(record, self.turns, self.recording)) is not None:
            if kind == "steering" and isinstance(record.get("prompt"), str):
                turn.blocks.append("User (steering): " + record["prompt"])
            elif kind == "Message" and isinstance(record.get("markdown"), str):
                turn.blocks.append("Assistant: " + record["markdown"])
            elif kind == "ToolSummary":
                # No full results: they are noisy, often huge, and can hold private data.
                turn.blocks.append(
                    "Tool: "
                    + " ".join(str(record.get(k) or "") for k in ("name", "detail", "command"))
                    + (" [failed]" if record.get("failed") else "")
                )
            elif kind == "auto_compacted":
                turn.compacted = True
            elif kind in {"turn_completed", "turn_failed", "turn_cancelled"}:
                turn.status = kind.removeprefix("turn_")

    def finish(self):
        for turn in self.turns.values():
            turn.active = None if self.journal.incomplete_tail else False
        active, seen = self.active, set()
        while not self.journal.incomplete_tail and active in self.parents and active not in seen:
            seen.add(active)
            if active in self.turns:
                self.turns[active].active = True
            active = self.parents[active]
        # Resolve compaction nodes to the nearest actual turn for context retrieval.
        for turn in self.turns.values():
            seen = set()
            while (
                turn.parent in self.parents
                and turn.parent not in self.turns
                and turn.parent not in seen
            ):
                seen.add(turn.parent)
                turn.parent = self.parents[turn.parent]


def read_turns(
    info: SessionInfo, root: Path, *, budget: SessionReadBudget | None = None
) -> dict[str, HistoryTurn]:
    """Read a bounded prefix; unfinished readers leave branch labels unknown."""
    reader = TurnReader(info, root)
    reader.read(budget or SessionReadBudget(MAX_SCAN_BYTES))
    return reader.turns


def project_path(workspace: Path) -> Path:
    workspace = workspace.resolve()
    if workspace.exists():
        project = project_checkout(workspace)
        if project:
            return project
    # Older metadata did not save project identity. Recover pcode's conventional
    # worktree layout even after deletion; arbitrary deleted worktrees stay separate.
    for parent in workspace.parents:
        if parent.name == WORKTREES_DIR:
            candidate = parent.parent
            if (candidate / ".git").exists():
                return candidate
    return workspace


class History:
    def __init__(self, workspace: Path, root: Path, session_id: str | None = None):
        self.workspace = workspace.resolve()
        self.root = root
        self.session_id = session_id

    def sessions(self, scope: Scope) -> list[SessionInfo]:
        if scope not in ("session", "project", "workspace", "all"):
            raise ValueError("scope must be session, project, workspace, or all")
        records = list_sessions(self.root)
        if scope == "session":
            return [s for s in records if s.id == self.session_id]
        if scope == "all":
            return records
        if scope == "workspace":
            return [s for s in records if Path(s.workspace).resolve() == self.workspace]
        project = project_path(self.workspace)
        projects: dict[tuple[str | None, str], Path] = {}
        result = []
        for info in records:
            key = (info.project, info.workspace)
            if key not in projects:
                projects[key] = (
                    Path(info.project).resolve()
                    if info.project
                    else project_path(Path(info.workspace))
                )
            if projects[key] == project:
                result.append(info)
        return result

    def chunks(
        self, scope: Scope, *, exclude_turn: str | None = None, after: str | None = None
    ) -> "Scan":
        """Return a bounded page; cursors retain both journal and chunk positions."""
        binding = self._binding(scope, "search", exclude_turn)
        scan, cursor = advance(binding, after, lambda: self._chunk_pages(scope, exclude_turn))
        scan.next_cursor = cursor
        return scan

    def _binding(self, scope: Scope, *request) -> tuple:
        # Validate even when resuming, before looking up a process-local cursor.
        if scope not in ("session", "project", "workspace", "all"):
            raise ValueError("scope must be session, project, workspace, or all")
        return (str(self.root.resolve()), str(self.workspace), self.session_id, scope, *request)

    def _chunk_pages(self, scope: Scope, exclude_turn: str | None):
        # Freeze traversal order: live metadata updates must not reorder later pages.
        sessions = self.sessions(scope)
        eligible = {info.id for info in sessions}
        searched = unreadable = 0
        warnings = []

        def page():
            return Scan(
                [],
                warnings.copy(),
                sessions_in_scope=len(sessions),
                sessions_searched=searched,
                sessions_unreadable=unreadable,
            )

        scan = page()
        budget = SessionReadBudget(MAX_SCAN_BYTES)
        for info in sessions:
            try:
                if budget.remaining == 0:
                    scan.warnings.append("Journal byte budget reached; continue with next_cursor.")
                    yield scan, True
                    eligible = {current.id for current in self.sessions(scope)}
                    scan, budget = page(), SessionReadBudget(MAX_SCAN_BYTES)
                if info.id not in eligible:
                    raise ValueError("Session is no longer in scope; restart without after.")
                reader = TurnReader(info, self.root)
                while True:
                    reader.read(budget)
                    if reader.journal.done:
                        break
                    scan.sessions_partial = 1
                    scan.warnings.append(
                        f"Journal byte budget reached inside session {info.id}; continue with "
                        "next_cursor. Its hits are deferred until its snapshot is fully read."
                    )
                    yield scan, True
                    eligible = self._validate_resume(info, scope, reader)
                    scan, budget = page(), SessionReadBudget(MAX_SCAN_BYTES)
                if reader.journal.incomplete_tail:
                    warnings.append(
                        f"Unfinished final record in session {info.id}; search again later."
                    )
                    scan.warnings = warnings.copy()
                for turn in reversed(list(reader.turns.values())):
                    if (
                        turn.id == exclude_turn
                        and info.id == self.session_id
                        and not turn.compacted
                    ):
                        continue
                    # Redact once, before splitting; never redact arbitrary byte pages.
                    text = turn.text
                    for offset in range(0, len(text), CHUNK_CHARS - 200):
                        if len(scan.chunks) == MAX_CHUNKS:
                            scan.sessions_partial = 1
                            scan.warnings.append(
                                f"Search limited to {MAX_CHUNKS} chunks; continue with next_cursor."
                            )
                            yield scan, True
                            eligible = self._validate_resume(info, scope, reader)
                            scan, budget = page(), SessionReadBudget(MAX_SCAN_BYTES)
                        scan.chunks.append(
                            Chunk(info, turn, offset, text[offset : offset + CHUNK_CHARS])
                        )
                searched += 1
                scan.sessions_searched = searched
                scan.sessions_partial = 0
            except OSError:
                unreadable += 1
                warnings.append(f"Skipped unreadable session {info.id}.")
                scan.sessions_unreadable = unreadable
                scan.warnings = warnings.copy()
        scan.scan_complete = not warnings
        yield scan, False

    def _validate_resume(self, info: SessionInfo, scope: Scope, reader: TurnReader):
        eligible = {current.id for current in self.sessions(scope)}
        if info.id not in eligible:
            raise ValueError("Session is no longer in scope; restart without after.")
        reader.journal.validate()
        return eligible

    def read(
        self,
        session_id: str,
        turn_id: str,
        scope: Scope = "project",
        context_turns: int = 1,
        offset: int = 0,
        max_chars: int = 8000,
        after: str | None = None,
    ) -> dict:
        if not 0 <= context_turns <= 3 or offset < 0 or not 1 <= max_chars <= 16_000:
            raise ValueError("Use context_turns 0..3, offset >= 0, and max_chars 1..16000")
        binding = self._binding(
            scope, "read", session_id, turn_id, context_turns, offset, max_chars
        )
        result, cursor = advance(
            binding,
            after,
            lambda: self._read_pages(session_id, turn_id, scope, context_turns, offset, max_chars),
        )
        result["next_cursor"] = cursor
        return result

    def _read_pages(self, session_id, turn_id, scope, context_turns, offset, max_chars):
        info = next((s for s in self.sessions(scope) if s.id == session_id), None)
        if info is None:
            raise ValueError(
                "Session not found in scope. Use an exact session_id from search_sessions."
            )
        try:
            reader = TurnReader(info, self.root)
            while True:
                reader.read(SessionReadBudget(MAX_SCAN_BYTES))
                if reader.journal.done:
                    break
                yield (
                    {
                        "session_id": info.id,
                        "turn_id": turn_id,
                        "scan_complete": False,
                        "next_cursor": None,
                        "text": "",
                        "context": [],
                        "offset": offset,
                        "next_offset": None,
                        "warnings": [
                            "Journal byte budget reached; the requested turn is not yet resolved. "
                            "Continue with next_cursor as after, keeping the other "
                            "arguments unchanged."
                        ],
                    },
                    True,
                )
                self._validate_resume(info, scope, reader)
        except OSError:
            raise ValueError("Session transcript is unreadable.") from None
        turns = reader.turns
        if turn_id not in turns:
            raise ValueError("Turn not found. Use a turn_id from search_sessions.")
        turn = turns[turn_id]
        text = turn.text
        if offset > len(text):
            raise ValueError("offset exceeds the turn's text length")
        context = []
        parent, seen = turn.parent, {turn.id}
        while parent in turns and parent not in seen and len(context) < context_turns:
            previous = turns[parent]
            seen.add(parent)
            context.append(
                {"turn_id": parent, "status": previous.status, "text": previous.text[:1000]}
            )
            parent = previous.parent
        end = min(offset + max_chars, len(text))
        yield (
            {
                "session_id": info.id,
                "turn_id": turn.id,
                "time": turn.time,
                "workspace": redact(info.workspace),
                "status": turn.status,
                "branch": turn.branch,
                "warnings": ["Unfinished final journal record; read again later."]
                if reader.journal.incomplete_tail
                else [],
                "scan_complete": not reader.journal.incomplete_tail,
                "next_cursor": None,
                "context": context[::-1],
                "text": text[offset:end],
                "offset": offset,
                "next_offset": end if end < len(text) else None,
            },
            False,
        )


_TOKEN = re.compile(r"\w+")
# Harness ConversationSearch's defaults: k1 above Lucene's 1.2 favors repeated terms.
BM25_K1 = 1.5
BM25_B = 0.75
PHRASE_BONUS = 1.0


def _tokenize(text: str) -> list[str]:
    return [match.group().casefold() for match in _TOKEN.finditer(text)]


def keyword_ranking(chunks: list[Chunk], query: str) -> list[int]:
    """BM25 over chunks, ported from Harness ConversationSearch, plus an exact-phrase bonus.

    Rare terms outrank common ones and long chunks are length-normalized, so
    a query like "editor flicker" finds the turn about it rather than the
    turn that merely mentions "editor" the most times.
    """
    query_tokens = list(dict.fromkeys(_tokenize(query)))
    if not query_tokens or not chunks:
        return []
    documents = [_tokenize(chunk.text) for chunk in chunks]
    avgdl = sum(len(tokens) for tokens in documents) / len(documents)
    if not avgdl:
        return []
    frequencies = [Counter(tokens) for tokens in documents]
    total = len(documents)
    idf = {}
    for term in query_tokens:
        df = sum(1 for counts in frequencies if term in counts)
        idf[term] = math.log((total - df + 0.5) / (df + 0.5) + 1.0) if df else 0.0
    phrase = " ".join(query.casefold().split())
    scored = []
    for index, (counts, chunk) in enumerate(zip(frequencies, chunks)):
        dl = sum(counts.values())
        score = 0.0
        for term in query_tokens:
            tf = counts.get(term, 0)
            if tf:
                score += (
                    idf[term]
                    * tf
                    * (BM25_K1 + 1.0)
                    / (tf + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avgdl))
                )
        if score > 0 and phrase in " ".join(chunk.text.casefold().split()):
            score += PHRASE_BONUS
        if score > 0:
            scored.append((score, index))
    return [index for _, index in sorted(scored, key=lambda item: (-item[0], item[1]))]


def group_results(
    chunks: list[Chunk],
    ranking: list[int],
    query: str,
    limit: int,
    current_session: str | None = None,
    current_turn: str | None = None,
) -> list[dict]:
    """Group hits by session, spreading a small limit across sessions first.

    A long session produces many chunks and can otherwise take every slot, so
    the first pass caps each session and a second pass spends whatever is left.
    """
    ordered: list[Chunk] = []
    seen: set[tuple[str, str]] = set()
    for index in ranking:
        chunk = chunks[index]
        key = (chunk.session.id, chunk.turn.id)
        if key not in seen:
            seen.add(key)
            ordered.append(chunk)
    picked: set[int] = set()
    counts: Counter[str] = Counter()
    for cap in (PER_SESSION_LIMIT, len(ordered)):
        for position, chunk in enumerate(ordered):
            if len(picked) == limit:
                break
            if position in picked or counts[chunk.session.id] >= cap:
                continue
            picked.add(position)
            counts[chunk.session.id] += 1
    groups: dict[str, dict] = {}
    for position in sorted(picked):
        chunk = ordered[position]
        group = groups.setdefault(
            chunk.session.id,
            {
                "session_id": chunk.session.id,
                "workspace": redact(chunk.session.workspace),
                "current": chunk.session.id == current_session,
                "turns": [],
            },
        )
        result = chunk.result(query)
        if chunk.turn.id == current_turn and chunk.session.id == current_session:
            # Only survives the scan when auto-compaction took it out of context:
            # this is the turn asking, recovering its own dropped history.
            result["current_turn"] = True
        group["turns"].append(result)
    return list(groups.values())


def merge_rankings(keyword: list[int], semantic: list[int]) -> list[int]:
    """Reciprocal-rank fusion keeps exact matches useful alongside semantic recall."""
    scores: dict[int, float] = {}
    for ranking in (keyword, semantic):
        for rank, index in enumerate(ranking):
            scores[index] = scores.get(index, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda index: (-scores[index], index))
