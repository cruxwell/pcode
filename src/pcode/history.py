"""Read-only retrieval over saved conversations, independent of the UI.

Transcripts are parsed into turns here and kept searchable by
`pcode.history_index`; a search or read first refreshes whatever changed.
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pcode.conversation_tree import side_record
from pcode.diagnostics import redact
from pcode.history_index import HistoryIndex, Hit, StoredTurn, signature
from pcode.sessions import SessionInfo, list_sessions
from pcode.worktree import WORKTREES_DIR, project_checkout

Scope = Literal["session", "project", "workspace", "all"]
SCOPES = ("session", "project", "workspace", "all")
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
CHUNK_OVERLAP = 200
# How long one call may spend bringing the index up to date. A first build
# over a few hundred sessions takes seconds; a call that runs out searches
# what is indexed and says how much is left, and the next call carries on.
REFRESH_SECONDS = 30.0
# Ranking is chunk-level, so one long session can otherwise fill the whole limit.
PER_SESSION_LIMIT = 3
# Enough occurrences to find prose without scanning a pathological chunk repeatedly.
MAX_MATCH_POSITIONS = 200
EXCERPT_CHARS = 800
CONCLUSION_CHARS = 240
# A session overview lists this many turns per page, each prompt cut to this.
OVERVIEW_TURNS = 40
OVERVIEW_PROMPT_CHARS = 300
# Shortest session ID prefix accepted: what /resume and tool labels show.
MIN_PREFIX = 8
# Why a failed turn or tool failed, as much as a search needs to find it.
ERROR_CHARS = 500
PHRASE_BONUS = 1.0
_TOKEN = re.compile(r"\w+")


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

    def stored(self) -> StoredTurn:
        return StoredTurn(
            self.id,
            self.parent,
            self.time,
            self.status,
            self.branch,
            self.compacted,
            self.text,
            self.conclusion,
        )


def _error_text(error) -> str:
    """One line from a journaled error: its type, status, and message."""
    if not isinstance(error, dict):
        return ""
    parts = [str(error.get(key)) for key in ("type", "status") if error.get(key)]
    message = error.get("message") or error.get("provider_message") or ""
    text = " ".join(parts) + (f": {message}" if message else "")
    return " ".join(text.split())[:ERROR_CHARS]


@dataclass
class Chunk:
    session: SessionInfo
    turn: StoredTurn
    offset: int
    text: str
    id: int = 0

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
        excerpt = redact(self.text[start : start + EXCERPT_CHARS])
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
        conclusion = redact(self.turn.conclusion[:CONCLUSION_CHARS])
        if conclusion and conclusion not in excerpt:
            result["conclusion"] = conclusion
        return result


@dataclass
class Coverage:
    """How much of a scope the index answered for."""

    sessions_in_scope: int = 0
    sessions_searched: int = 0
    sessions_unreadable: int = 0
    # Not yet indexed, or changed since, when the refresh ran out of time.
    sessions_pending: int = 0
    # Every session in scope was searched as it is now. Warnings added later,
    # such as a semantic provider failing, do not change what was covered.
    complete: bool = True
    warnings: list[str] = field(default_factory=list)


def _turn_of(record: dict, turns: dict[str, "HistoryTurn"], recording: str | None):
    """The turn a record belongs to: the one it names, else the last one started.

    The fallback is for journals written before records carried a run ID.
    """
    identity = record.get("run_id")
    if not isinstance(identity, str) or identity not in turns:
        identity = recording
    return turns.get(identity) if identity is not None else None


def journal_records(info: SessionInfo, root: Path) -> tuple[str, list[dict], bool]:
    """A transcript's recall records, its signature, and whether its last line is torn.

    Raises ``OSError`` for a missing, removed, or symlinked session. Only the
    bytes present at the signature's size are read, so a concurrent append
    shows up as a changed signature next time instead of a half-read line.
    """
    directory = root / info.id
    path = directory / "transcript.jsonl"
    if directory.is_symlink() or path.is_symlink():
        raise OSError("symlinked session")
    if not (directory / "session.json").is_file():
        raise OSError("session removed")
    saved = signature(path)
    size = int(saved.rsplit(":", 2)[1])
    markers = tuple(f'"{kind}"'.encode() for kind in KINDS)
    records, torn, consumed = [], False, 0
    with path.open("rb") as stream:
        while consumed < size and (line := stream.readline(size - consumed)):
            consumed += len(line)
            terminated = line.endswith(b"\n")
            if terminated and not any(marker in line for marker in markers):
                continue
            try:
                record = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                # A complete final object needs no newline, but a torn live
                # write must not be mistaken for complete coverage.
                torn = torn or not terminated
                continue
            if isinstance(record, dict) and record.get("kind") in KINDS:
                records.append(record)
    return saved, records, torn


class TurnReader:
    """Rebuild turns, their ancestry, and the active branch from journal records."""

    def __init__(self):
        self.turns: dict[str, HistoryTurn] = {}
        self.parents: dict[str, str | None] = {}
        self.active: str | None = None
        self.recording: str | None = None

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
                str(record.get("time", "")),
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
                # A failure's error is kept, though: "why did that fail" asks for it.
                line = "Tool: " + " ".join(
                    str(record.get(k) or "") for k in ("name", "detail", "command")
                )
                if record.get("failed"):
                    error = " ".join(str(record.get("error") or "").split())[:ERROR_CHARS]
                    line += f" [failed: {error}]" if error else " [failed]"
                turn.blocks.append(line)
            elif kind == "auto_compacted":
                turn.compacted = True
            elif kind in {"turn_completed", "turn_failed", "turn_cancelled"}:
                turn.status = kind.removeprefix("turn_")
                if kind == "turn_failed" and (error := _error_text(record.get("error"))):
                    turn.blocks.append(f"Turn failed: {error}")

    def finish(self, torn: bool = False):
        for turn in self.turns.values():
            turn.active = None if torn else False
        active, seen = self.active, set()
        while not torn and active in self.parents and active not in seen:
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


def read_turns(info: SessionInfo, root: Path) -> tuple[str, dict[str, HistoryTurn], bool]:
    """Parse one session: its signature, turns in journal order, and a torn-tail flag."""
    saved, records, torn = journal_records(info, root)
    reader = TurnReader()
    for record in records:
        reader.accept(record)
    reader.finish(torn)
    for turn in reader.turns.values():
        if not turn.time:
            turn.time = info.created
    return saved, reader.turns, torn


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


def _tokens(text: str) -> list[str]:
    return list(dict.fromkeys(match.group().casefold() for match in _TOKEN.finditer(text)))


def match_expression(query: str) -> str:
    """Any query word, as FTS5 terms: BM25 then favors rare words and short chunks."""
    return " OR ".join(f'"{token}"' for token in _tokens(query))


def fuse(rankings: list[list[int]]) -> list[int]:
    """Reciprocal-rank fusion: what several rankings agree on rises above any one's top."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    # Ties keep first-seen order: the earlier query's ranking wins.
    order = {key: position for position, key in enumerate(scores)}
    return sorted(scores, key=lambda key: (-scores[key], order[key]))


class History:
    def __init__(
        self,
        workspace: Path,
        root: Path,
        session_id: str | None = None,
        refresh_seconds: float | None = None,
    ):
        self.workspace = workspace.resolve()
        self.root = root
        self.session_id = session_id
        self.index = HistoryIndex(root)
        self.refresh_seconds = REFRESH_SECONDS if refresh_seconds is None else refresh_seconds

    def sessions(self, scope: Scope) -> list[SessionInfo]:
        if scope not in SCOPES:
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

    def _refresh(self, db, sessions: list[SessionInfo], deadline: float | None) -> Coverage:
        """Reindex changed sessions in scope and forget deleted ones."""
        coverage = Coverage(sessions_in_scope=len(sessions))
        saved = self.index.signatures(db)
        listed = {info.id for info in list_sessions(self.root)}
        gone = [identity for identity in saved if identity not in listed]
        for info in sessions:
            known = saved.get(info.id)
            try:
                current = signature(self.root / info.id / "transcript.jsonl")
                if known is not None and current == known[0]:
                    pass
                elif deadline is not None and time.monotonic() > deadline:
                    # Out of time: an outdated copy is still searched, a missing one not.
                    coverage.sessions_pending += 1
                    if known is None:
                        continue
                elif (fresh := self.index.state(db, info.id)) and fresh[0] == current:
                    known = fresh  # Another process indexed it meanwhile.
                else:
                    current, turns, torn = read_turns(info, self.root)
                    self.index.store(
                        db,
                        info.id,
                        current,
                        torn,
                        [turn.stored() for turn in turns.values()],
                        CHUNK_CHARS,
                        CHUNK_OVERLAP,
                    )
                    known = (current, torn)
            except OSError:
                coverage.sessions_unreadable += 1
                coverage.complete = False
                coverage.warnings.append(f"Skipped unreadable session {info.id}.")
                if known:
                    gone.append(info.id)
                continue
            if known[1]:
                coverage.complete = False
                coverage.warnings.append(
                    f"Unfinished final record in session {info.id}; search again later."
                )
            if known[0] == current:
                coverage.sessions_searched += 1
        self.index.forget(db, gone)
        if coverage.sessions_pending:
            coverage.complete = False
            coverage.warnings.append(
                f"The search index is still being built: {coverage.sessions_pending} sessions "
                "were not indexed or are out of date. Search again to cover them."
            )
        return coverage

    def _scope(self, db, scope: Scope, deadline: float | None):
        sessions = self.sessions(scope)
        coverage = self._refresh(db, sessions, deadline)
        return {info.id: info for info in sessions}, coverage

    def _deadline(self) -> float:
        return time.monotonic() + self.refresh_seconds

    def _exclude(self, exclude_turn: str | None) -> tuple[str, str] | None:
        if exclude_turn and self.session_id:
            return self.session_id, exclude_turn
        return None

    @staticmethod
    def _chunk(hit: Hit, infos: dict[str, SessionInfo]) -> Chunk:
        return Chunk(infos[hit.session], hit.turn, hit.start, hit.text, hit.chunk)

    def search(
        self, scope: Scope, queries: list[str], *, exclude_turn: str | None = None
    ) -> tuple[list[Chunk], Coverage]:
        """Chunks matching any query, fused across queries, best first."""
        with self.index.connect() as db:
            infos, coverage = self._scope(db, scope, self._deadline())
            exclude = self._exclude(exclude_turn)
            with self.index.snapshot(db):
                ranked = [self._rank(db, query, infos, exclude) for query in queries]
        chunks = {chunk.id: chunk for ranking in ranked for chunk in ranking}
        rankings = [[chunk.id for chunk in ranking] for ranking in ranked]
        ranking = rankings[0] if len(rankings) == 1 else fuse(rankings)
        return [chunks[key] for key in ranking], coverage

    def _rank(self, db, query: str, infos, exclude) -> list[Chunk]:
        """BM25 order, plus a bonus for chunks holding the query as an exact phrase."""
        expression = match_expression(query)
        if not expression or not infos:
            return []
        phrase = " ".join(query.casefold().split())
        scored = []
        for hit in self.index.match(db, expression, list(infos), exclude):
            chunk = self._chunk(hit, infos)
            bonus = PHRASE_BONUS if phrase in " ".join(chunk.text.casefold().split()) else 0
            scored.append((hit.score + bonus, len(scored), chunk))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [chunk for _, _, chunk in scored]

    def chunks(
        self, scope: Scope, *, exclude_turn: str | None = None, refresh: bool = True
    ) -> tuple[list[Chunk], Coverage]:
        """Every indexed chunk in scope, for semantic ranking.

        `refresh=False` reads the index as the preceding search left it.
        """
        with self.index.connect() as db:
            infos, coverage = self._scope(db, scope, self._deadline() if refresh else 0.0)
            hits = self.index.chunks(db, list(infos), self._exclude(exclude_turn))
        return [self._chunk(hit, infos) for hit in hits], coverage

    def resolve(self, session_id: str, scope: Scope) -> SessionInfo:
        """A session by its full ID or a unique prefix, as people paste them."""
        session_id = session_id.strip()
        sessions = self.sessions(scope)
        exact = [s for s in sessions if s.id == session_id]
        if exact:
            return exact[0]
        matches = (
            [s for s in sessions if s.id.startswith(session_id)]
            if len(session_id) >= MIN_PREFIX
            else []
        )
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ValueError(
                f"{len(matches)} sessions in scope start with {session_id!r}; give more of the ID."
            )
        raise ValueError(
            "Session not found in scope. Use a session_id from search_sessions or the user, "
            f"at least {MIN_PREFIX} characters, or widen the scope."
        )

    def read(
        self,
        session_id: str,
        turn_id: str = "",
        scope: Scope = "project",
        context_turns: int = 1,
        offset: int = 0,
        max_chars: int = 8000,
    ) -> dict:
        if not 0 <= context_turns <= 3 or offset < 0 or not 1 <= max_chars <= 16_000:
            raise ValueError("Use context_turns 0..3, offset >= 0, and max_chars 1..16000")
        info = self.resolve(session_id, scope)
        with self.index.connect() as db:
            # One session, however long it takes: a read must see the whole turn.
            coverage = self._refresh(db, [info], None)
            if coverage.sessions_unreadable:
                raise ValueError("Session transcript is unreadable.")
            turns = self.index.turns(db, info.id)
        if not turn_id:
            return self._overview(info, turns, offset, coverage)
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
                {
                    "turn_id": parent,
                    "status": previous.status,
                    "text": redact(previous.text[:1000]),
                }
            )
            parent = previous.parent
        end = min(offset + max_chars, len(text))
        return {
            "session_id": info.id,
            "turn_id": turn.id,
            "time": turn.time,
            "workspace": redact(info.workspace),
            "status": turn.status,
            "branch": turn.branch,
            "warnings": coverage.warnings,
            "scan_complete": coverage.complete,
            "context": context[::-1],
            "text": redact(text[offset:end]),
            "offset": offset,
            "next_offset": end if end < len(text) else None,
        }

    @staticmethod
    def _overview(
        info: SessionInfo, turns: dict[str, StoredTurn], offset: int, coverage: Coverage
    ) -> dict:
        """What a session was about, turn by turn: the map to pick turns to read from."""
        listed = list(turns.values())
        if offset > len(listed):
            raise ValueError("offset exceeds the session's turn count")
        page = listed[offset : offset + OVERVIEW_TURNS]
        entries = []
        for turn in page:
            # The first block is the prompt; a long one is cut, not lost: read the turn.
            prompt = turn.text.split("\n\n", 1)[0]
            entry = {
                "turn_id": turn.id,
                "time": turn.time,
                "status": turn.status,
                "branch": turn.branch,
                "chars": len(turn.text),
                "prompt": redact(prompt[:OVERVIEW_PROMPT_CHARS]),
            }
            if turn.conclusion:
                entry["conclusion"] = redact(turn.conclusion[:CONCLUSION_CHARS])
            failure = next(
                (line for line in turn.text.splitlines() if line.startswith("Turn failed: ")), ""
            )
            if failure:
                entry["failure"] = redact(failure.removeprefix("Turn failed: "))
            entries.append(entry)
        end = offset + len(page)
        return {
            "session_id": info.id,
            "title": info.name or info.title or "",
            "workspace": redact(info.workspace),
            "created": info.created,
            "updated": info.updated,
            "turn_count": len(listed),
            "warnings": coverage.warnings,
            "scan_complete": coverage.complete,
            "turns": entries,
            "offset": offset,
            "next_offset": end if end < len(listed) else None,
        }


def group_results(
    chunks: list[Chunk],
    query: str,
    limit: int,
    current_session: str | None = None,
    current_turn: str | None = None,
) -> list[dict]:
    """Group ranked hits by session, spreading a small limit across sessions first.

    A long session produces many chunks and can otherwise take every slot, so
    the first pass caps each session and a second pass spends whatever is left.
    """
    ordered: list[Chunk] = []
    seen: set[tuple[str, str]] = set()
    for chunk in chunks:
        key = (chunk.session.id, chunk.turn.id)
        if key not in seen:
            seen.add(key)
            ordered.append(chunk)
    picked: set[int] = set()
    counts: dict[str, int] = {}
    for cap in (PER_SESSION_LIMIT, len(ordered)):
        for position, chunk in enumerate(ordered):
            if len(picked) == limit:
                break
            if position in picked or counts.get(chunk.session.id, 0) >= cap:
                continue
            picked.add(position)
            counts[chunk.session.id] = counts.get(chunk.session.id, 0) + 1
    groups: dict[str, dict] = {}
    for position in sorted(picked):
        chunk = ordered[position]
        group = groups.setdefault(
            chunk.session.id,
            {
                "session_id": chunk.session.id,
                "workspace": redact(chunk.session.workspace),
                "title": chunk.session.title or "",
                "current": chunk.session.id == current_session,
                "turns": [],
            },
        )
        result = chunk.result(query)
        if chunk.turn.id == current_turn and chunk.session.id == current_session:
            # Only survives the search when auto-compaction took it out of context:
            # this is the turn asking, recovering its own dropped history.
            result["current_turn"] = True
        group["turns"].append(result)
    return list(groups.values())
