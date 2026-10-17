"""Private session metadata/journal around Harness's native step persistence."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zlib
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from filelock import FileLock, Timeout
from pydantic import BaseModel
from pydantic_ai_harness.step_persistence import (
    ContinuableSnapshot,
    SqliteStepStore,
    StepEvent,
    ToolEffectRecord,
)

from pcode.conversation_tree import ConversationTree, side_record
from pcode.diagnostics import error_report, redact, versions


class SessionError(ValueError):
    pass


class SessionBusy(SessionError):
    """Another process holds the session's lock."""


class SessionInfo(BaseModel):
    version: Literal[1] = 1
    id: str
    model: str
    workspace: str
    # Preserve project scope even after a linked worktree has been removed.
    project: str | None = None
    # HEAD when the session began, so /diffs still shows work once it is committed.
    start_commit: str | None = None
    created: str
    updated: str
    status: str = "new"
    # Set with /rename; /resume lists and searches it.
    name: str | None = None
    # Asked of the model beside the first turn (`session_naming`); `name` wins.
    title: str | None = None
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Defaulted, so a session written before cache accounting still loads.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    packages: dict[str, str]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_root() -> Path:
    if value := os.environ.get("PCODE_SESSION_DIR"):
        return Path(value).expanduser()
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state / "pcode" / "sessions"


def private_file(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def read_info(path: Path) -> SessionInfo:
    try:
        info = SessionInfo.model_validate_json((path / "session.json").read_text())
    except OSError, ValueError:
        raise SessionError(
            "Session metadata is unreadable or uses an unsupported format."
        ) from None
    if info.id != path.name:
        raise SessionError("Session ID does not match its directory.")
    return info


def list_sessions(root: Path | None = None) -> list[SessionInfo]:
    root = root or session_root()
    result = []
    if root.is_dir():
        for path in root.iterdir():
            if path.is_dir() and not path.is_symlink():
                try:
                    result.append(read_info(path))
                except SessionError:
                    continue
    return sorted(result, key=lambda info: info.updated, reverse=True)


def is_open(directory: Path) -> bool:
    """Whether a process holds this session, probed without keeping the lock."""
    lock = FileLock(directory / ".lock", mode=0o600)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return True
    except OSError:
        return False  # Deleted since it was listed; nothing can hold it now.
    lock.release()
    return False


def sharing(
    workspace: Path, root: Path | None = None, exclude: str | None = None
) -> list[tuple[str, bool]]:
    """Other sessions still using `workspace`, as (ID, open) pairs, excluding `exclude`.

    A copied session (`--fork`, or one whose original was busy) shares that
    original's directory, so tidying a worktree on the way out has to ask this
    first. One counts while it is open or has a finished turn to resume.
    """
    root = root or session_root()
    home = str(workspace.resolve())
    found = []
    for info in list_sessions(root):
        if info.workspace != home or info.id == exclude:
            continue
        running = is_open(root / info.id)
        if running or info.turns:
            found.append((info.id, running))
    return found


def delete_session(identity: str, root: Path | None = None) -> None:
    """Remove a saved session's directory; refuses one open in any process."""
    root = root or session_root()
    directory = root / identity
    if directory.is_symlink() or not directory.is_dir() or directory.parent != root:
        raise SessionError("No such session.")
    lock = FileLock(directory / ".lock", mode=0o600)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise SessionError("This session is open in another process.") from None
    try:
        try:
            info = read_info(directory)
        except SessionError:
            info = None
        shutil.rmtree(directory)
    finally:
        lock.release()
    if info is not None:
        # By the directory just removed: its refs are keyed by that id.
        forget_refs(info.model_copy(update={"id": identity}))


def forget_refs(info: SessionInfo) -> None:
    """Drop the git refs a session kept (its /diffs review checkpoint).

    Best effort: the repository may be gone, and a leftover ref only holds
    objects git would otherwise collect.
    """
    from pcode.git_diff import GitDiffError, forget_session
    from pcode.worktree import session_scope

    try:
        scope = session_scope(info)
        if scope.is_dir():
            forget_session(scope, info.id)
    except GitDiffError, OSError:
        pass


@dataclass
class ToolCall:
    """One settled tool call, summarized the way scrollback summarized it.

    The transcript's own `result` is deliberately left behind: it is most of a
    transcript's bulk (hundreds of KB where the summaries are tens), and the
    tool inspector is where a full result belongs.
    """

    name: str
    detail: str = ""
    failed: bool = False
    elapsed_seconds: float | None = None
    command: str = ""


@dataclass
class Turn:
    """One prompt and the final response it produced, for browsing and search."""

    prompt: str
    response: str = ""
    status: str = "running"  # "complete", "cancelled", "failed", or still "running".
    # Text blocks and settled tool calls in transcript order, so a browsed turn
    # can be replayed in the order it happened. `response` stays the last text
    # block: the session list and the conversation tree want that one alone.
    blocks: list["str | ToolCall"] = field(default_factory=list)
    # A /btw side question journaled beside the conversation (`side_record`).
    side: bool = False


_TURN_KINDS = (
    "turn_started",
    "Message",
    "ToolSummary",
    "turn_completed",
    "turn_cancelled",
    "turn_failed",
)


@dataclass
class SessionReadBudget:
    """A shared byte budget for readers that can report partial coverage."""

    remaining: int
    exhausted: bool = False


def session_records(
    info: SessionInfo,
    root: Path | None = None,
    *,
    kinds: tuple[str, ...] = _TURN_KINDS,
    budget: SessionReadBudget | None = None,
):
    """Yield turn-level records without opening or locking the session.

    Raises ``OSError`` when the transcript cannot be read. Transcripts are
    mostly streaming deltas, so only candidate lines are parsed.
    """
    directory = (root or session_root()) / info.id
    path = directory / "transcript.jsonl"
    if directory.is_symlink() or path.is_symlink():
        raise OSError("symlinked session")
    markers = tuple(f'"{kind}"'.encode() for kind in kinds)
    with path.open("rb") as stream:
        while line := stream.readline(budget.remaining + 1 if budget else -1):
            if budget:
                if len(line) > budget.remaining:
                    budget.exhausted = True
                    return
                budget.remaining -= len(line)
            if not any(marker in line for marker in markers):
                continue
            try:
                record = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("kind") in kinds:
                yield record


def session_turns(info: SessionInfo, root: Path | None = None) -> list[Turn] | None:
    """Read every turn in file order; ``None`` when the transcript is unreadable.

    Forked branches are included: reconstructing the active path needs the
    session lock, which the live session holds, and abandoned prompts are
    still worth finding.
    """
    turns: list[Turn] = []
    # By run id: a side question's records land in the middle of a running
    # turn's. Records without one belong to the last ordinary turn started.
    by_run: dict[str, Turn] = {}
    current: Turn | None = None
    try:
        for record in session_records(info, root):
            kind = record["kind"]
            run_id = record.get("run_id")
            if kind == "turn_started":
                prompt = record.get("prompt")
                if not isinstance(prompt, str):
                    continue
                # A /resend retry reuses the prompt; its outcome belongs to the same turn.
                if record.get("continuation") and current is not None and current.prompt == prompt:
                    current.status = "running"
                    if isinstance(run_id, str):
                        by_run[run_id] = current
                    continue
                turn = Turn(prompt, side=side_record(record))
                turns.append(turn)
                if isinstance(run_id, str):
                    by_run[run_id] = turn
                if not side_record(record):
                    current = turn
                continue
            turn = by_run.get(run_id) if isinstance(run_id, str) else None
            turn = turn or current
            if turn is None:
                continue
            if kind == "Message":
                markdown = record.get("markdown")
                if isinstance(markdown, str):
                    turn.response = markdown
                    turn.blocks.append(markdown)
            elif kind == "ToolSummary":
                elapsed = record.get("elapsed_seconds")
                turn.blocks.append(
                    ToolCall(
                        str(record.get("name") or ""),
                        str(record.get("detail") or ""),
                        bool(record.get("failed")),
                        elapsed if isinstance(elapsed, (int, float)) else None,
                        str(record.get("command") or ""),
                    )
                )
            elif kind == "turn_completed":
                turn.status = "complete"
            elif kind == "turn_cancelled":
                turn.status = "cancelled"
            elif kind == "turn_failed":
                turn.status = "failed"
    except OSError:
        return None
    return turns


def first_prompt(info: SessionInfo, root: Path | None = None) -> str:
    """The first submitted prompt, stopping at the first record so listing stays cheap."""
    try:
        for record in session_records(info, root):
            # A side question is not what the conversation was about.
            if (
                record["kind"] == "turn_started"
                and isinstance(record.get("prompt"), str)
                and not side_record(record)
            ):
                return record["prompt"]
    except OSError:
        return "(Prompt unavailable)"
    return "(No prompt yet)"


SNAPSHOTS_PER_RUN = 2
"""How many step checkpoints a turn keeps.

Harness saves the whole message history again at every settled step, so a turn
with 300 tool calls stores 300 copies of itself and a session reaches gigabytes.
Nothing here reads them: `history_at` and `recover` both take the newest
snapshot of a run, and a compaction node restores from the conversation tree
instead. The bound keeps the newest two, and Harness widens that retain set to
cover the newest `complete` one when the newest is interrupted, so a crash mid
turn still continues from its last settled step.

What it gives up is rewinding *inside* a turn, which no command offers, and
pre-compaction history for a run, which `history_at` takes from the tree.
"""


def _store_bytes(database: Path) -> int:
    """The store's size including its write-ahead log, where recent writes still live."""
    names = (database.name, database.name + "-wal", database.name + "-shm")
    return sum(
        (database.parent / name).stat().st_size
        for name in names
        if (database.parent / name).is_file()
    )


def compact_snapshots(directory: Path, keep: int = SNAPSHOTS_PER_RUN) -> tuple[int, int]:
    """Shrink one existing session's store, returning its size before and after.

    Sessions written before the retain bound existed keep every step
    checkpoint, and ones written before `MESSAGE_REFS` hold a whole history in
    every row, so this applies the bound and then stores each message once.
    Deleting rows alone frees pages for reuse without returning them to the
    filesystem, so it vacuums as well. A gigabyte session takes seconds.

    A session open elsewhere is skipped (its size reported unchanged) rather
    than compacted underneath the process still writing to it.
    """
    database = directory / "steps.sqlite3"
    if not database.is_file():
        return 0, 0
    before = _store_bytes(database)
    lock = FileLock(directory / ".lock", mode=0o600)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return before, before
    try:
        connection = sqlite3.connect(database)
        try:
            # Mirrors Harness's retain set: the newest `keep` of each run, plus
            # each run's newest settled snapshot, which is the one every read
            # here takes. A row predating the state column counts as complete.
            connection.execute(
                "DELETE FROM snapshots WHERE seq NOT IN ("
                "  SELECT seq FROM ("
                "    SELECT seq, row_number() OVER ("
                "      PARTITION BY run_id ORDER BY seq DESC) AS rank FROM snapshots"
                "  ) WHERE rank <= ?"
                "  UNION SELECT max(seq) FROM snapshots"
                "  WHERE coalesce(state, 'complete') = 'complete' GROUP BY run_id)",
                (keep,),
            )
            _dedupe_stored_snapshots(connection)
            connection.commit()
            connection.execute("VACUUM")
            connection.commit()
            # A vacuum in WAL mode rewrites the store into the log; the file on
            # disk only shrinks once that log is folded back and truncated.
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
    except sqlite3.Error, ValueError:
        # A session whose store cannot be rewritten is left exactly as it was.
        return before, _store_bytes(database)
    finally:
        lock.release()
    return before, _store_bytes(database)


def is_session_selector(value: str) -> bool:
    """Tell an ID/prefix apart from prompt text after an optional-argument flag."""
    return value == "latest" or bool(re.fullmatch(r"[a-f0-9-]{8,36}", value))


def resolve_session(selector: str, root: Path | None = None, workspace: Path | None = None) -> Path:
    """Resolve an ID/prefix, or the newest session, optionally scoped to a workspace."""
    root = root or session_root()
    if not is_session_selector(selector):
        raise SessionError("Use a session ID from --sessions, or omit it for the latest.")
    sessions = list_sessions(root)
    if selector != "latest":
        matches = [s for s in sessions if s.id.startswith(selector)]
    elif workspace is None:
        matches = sessions[:1]
    else:
        # "Latest" means this checkout's newest session, never another repo's.
        home = str(workspace.resolve())
        matches = [s for s in sessions if s.workspace == home][:1]
        if not matches:
            raise SessionError(f"No saved session for {home}; use --sessions to pick one.")
    if len(matches) != 1:
        raise SessionError("Session not found or prefix is ambiguous; use --sessions.")
    return root / matches[0].id


MESSAGE_REFS = "pcode_message_refs"
"""Key of a snapshot row that lists its messages by hash instead of holding them.

Harness stores a snapshot's whole history at every checkpoint, and each turn
keeps its newest ones, so a session of N turns holds N copies of a history that
only ever grew at the end: one long session reached a gigabyte, of which 50 MB
was distinct messages. Each message is stored once, compressed, in
`pcode_messages`, and a snapshot row keeps only the ordered hashes.
"""

_MESSAGE_TABLE = (
    "CREATE TABLE IF NOT EXISTS pcode_messages (hash TEXT PRIMARY KEY, body BLOB NOT NULL)"
    " WITHOUT ROWID"
)
# Hashes per lookup, under SQLite's lowest default host-parameter limit.
_LOOKUP_CHUNK = 500


def _message_refs(text: str) -> list[str] | None:
    """The hashes a snapshot row lists, or None when the row holds its messages itself."""
    # Harness only ever writes a list, so a row that is not an object is legacy.
    if not text.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or MESSAGE_REFS not in data:
        return None
    refs = data[MESSAGE_REFS]
    if not isinstance(refs, list) or not all(isinstance(key, str) for key in refs):
        raise ValueError("snapshot row has malformed message references")
    return refs


def _store_messages(
    connection: sqlite3.Connection, messages: list, stored: set[str] | frozenset = frozenset()
) -> dict:
    """Store each message not already `stored` and return what the snapshot row holds instead."""
    texts = [json.dumps(message, separators=(",", ":"), sort_keys=True) for message in messages]
    hashes = [hashlib.blake2b(text.encode(), digest_size=16).hexdigest() for text in texts]
    fresh = {key: text for key, text in zip(hashes, texts) if key not in stored}
    connection.executemany(
        "INSERT OR IGNORE INTO pcode_messages (hash, body) VALUES (?, ?)",
        ((key, zlib.compress(text.encode())) for key, text in fresh.items()),
    )
    return {MESSAGE_REFS: hashes}


def snapshot_messages_json(connection: sqlite3.Connection, text: str) -> str:
    """A snapshot row's messages as the JSON list Harness wrote, however pcode stored them.

    Anything reading `snapshots.messages` directly needs this; the store's own
    reads already apply it. Raises ValueError for a row referencing a message
    the store does not hold.
    """
    hashes = _message_refs(text)
    if hashes is None:
        return text
    distinct = list(set(hashes))
    bodies: dict[str, bytes] = {}
    for start in range(0, len(distinct), _LOOKUP_CHUNK):
        chunk = distinct[start : start + _LOOKUP_CHUNK]
        marks = ",".join("?" * len(chunk))
        bodies.update(
            connection.execute(
                f"SELECT hash, body FROM pcode_messages WHERE hash IN ({marks})", chunk
            ).fetchall()
        )
    missing = len(distinct) - len(bodies)
    if missing:
        raise ValueError(f"snapshot references {missing} messages the store does not hold")
    try:
        return "[" + ",".join(zlib.decompress(bodies[key]).decode() for key in hashes) + "]"
    except zlib.error as error:
        raise ValueError(f"stored message is corrupt: {error}") from None


def _dedupe_stored_snapshots(connection: sqlite3.Connection) -> None:
    """Move rows written with whole histories to hashes, and drop unreferenced messages.

    Only safe while no store has the session open: a writer commits its new
    messages a moment before the row that references them, and this would
    take those for unreferenced. Raises ValueError, before deleting anything,
    on a row whose references it cannot read.
    """
    connection.execute(_MESSAGE_TABLE)
    referenced: set[str] = set()
    for (seq,) in connection.execute("SELECT seq FROM snapshots").fetchall():
        # One row at a time: a single legacy row can hold tens of megabytes.
        (text,) = connection.execute(
            "SELECT messages FROM snapshots WHERE seq = ?", (seq,)
        ).fetchone()
        if not isinstance(text, str):
            continue
        refs = _message_refs(text)
        if refs is not None:
            referenced.update(refs)
            continue
        try:
            messages = json.loads(text)
        except ValueError:
            continue
        if not isinstance(messages, list):
            continue  # Not a shape Harness writes; left for its reader to judge.
        row = _store_messages(connection, messages, referenced)
        referenced.update(row[MESSAGE_REFS])
        connection.execute(
            "UPDATE snapshots SET messages = ? WHERE seq = ?", (json.dumps(row), seq)
        )
    stored = [key for (key,) in connection.execute("SELECT hash FROM pcode_messages")]
    connection.executemany(
        "DELETE FROM pcode_messages WHERE hash = ?",
        ((key,) for key in stored if key not in referenced),
    )


class PrivateStepStore(SqliteStepStore):
    """Use the public store hooks to redact error strings, not replay-critical history.

    It also stores snapshot messages once each (see `MESSAGE_REFS`) by wrapping
    the SQLite store's row reads and writes, below the (de)serialization and
    media handling Harness does around them. Assumes the `database=` form, which
    opens a fresh autocommit connection per call.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Hashes this store has written, so a step only compresses and inserts
        # what is new. Only compaction deletes messages, and never while open.
        self._stored: set[str] = set()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        # Before Harness's schema, whose success marks the whole schema ready.
        connection.execute(_MESSAGE_TABLE)
        super()._ensure_schema(connection)

    def _sync_save_snapshot(self, snapshot: ContinuableSnapshot, messages_json: object) -> None:
        if isinstance(messages_json, list):
            connection = self._open()
            try:
                self._ensure_schema(connection)
                # Committed before the row that references them, so a crash
                # between the two leaves spare messages, never a broken row.
                with connection:
                    connection.execute("BEGIN")
                    messages_json = _store_messages(connection, messages_json, self._stored)
                self._stored.update(messages_json[MESSAGE_REFS])
            finally:
                self._maybe_close(connection)
        super()._sync_save_snapshot(snapshot, messages_json)

    def _sync_load_latest_snapshot(self, run_id: str, include_interrupted: bool):
        row = super()._sync_load_latest_snapshot(run_id, include_interrupted)
        if row is None:
            return None
        connection = self._open()
        try:
            return (*row[:6], snapshot_messages_json(connection, row[6]), row[7])
        finally:
            self._maybe_close(connection)

    def _sync_load_snapshot_rows(self, run_id: str, include_interrupted: bool):
        rows = super()._sync_load_snapshot_rows(run_id, include_interrupted)
        if not rows:
            return rows
        connection = self._open()
        try:
            expanded = []
            for row in rows:
                try:
                    text = snapshot_messages_json(connection, row[6])
                except ValueError, TypeError, AttributeError:
                    # Left as stored: the caller skips a row it cannot parse.
                    text = row[6]
                expanded.append((*row[:6], text, row[7]))
            return expanded
        finally:
            self._maybe_close(connection)

    async def append_event(self, event: StepEvent) -> None:
        if event.error:
            event = replace(event, error=redact(event.error))
        await super().append_event(event)

    async def record_tool_effect(self, record: ToolEffectRecord) -> None:
        if record.effect_summary:
            record = replace(record, effect_summary=redact(record.effect_summary))
        await super().record_tool_effect(record)


def _parse(line: bytes) -> dict | None:
    """One journal line as a record; anything else (torn, foreign) is skipped."""
    try:
        record = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


class SessionJournal:
    """A session's conversation, readable without owning it.

    `read` never locks or writes, so a terminal can display a session that a
    host process holds open and keeps appending to; `refresh` follows it.
    """

    def __init__(self, directory: Path, info: SessionInfo) -> None:
        if directory.is_symlink():
            raise SessionError("Refusing a symlinked session directory.")
        self.directory = directory
        self.info = info
        # The session this one was copied from because that one was open.
        self.forked_from: str | None = None
        # True when the copy was asked for (`--fork`) rather than forced by a lock.
        self.fork_requested = False
        self.tree = ConversationTree()
        # End of the last complete journal line fed to `tree`, and which file it
        # was in, so a replaced journal is rebuilt rather than read mid-record.
        self._offset = 0
        self._journal_id: tuple[int, int] | None = None

    @classmethod
    def read(cls, directory: Path) -> "SessionJournal":
        """Open a session read-only, even while another process holds and writes it."""
        if directory.is_symlink():
            raise SessionError("Refusing a symlinked session directory.")
        journal = cls(directory, read_info(directory))
        journal._follow()
        return journal

    def refresh(self) -> bool:
        """Catch up with what the owner has written since; whether anything changed."""
        changed = self._follow()
        info = read_info(self.directory)
        if info != self.info:
            self.info = info
            changed = True
        return changed

    def _follow(self) -> bool:
        """Feed `tree` the complete lines appended since the last call.

        A final line without its newline may still be being written, so it is
        left for the next call to read whole.
        """
        path = self.directory / "transcript.jsonl"
        try:
            file = path.open("rb")
        except FileNotFoundError:
            return False
        with file:
            stat = os.fstat(file.fileno())
            identity = (stat.st_dev, stat.st_ino)
            changed = False
            if identity != self._journal_id or stat.st_size < self._offset:
                changed = self._offset > 0 or self._journal_id is not None
                self.tree = ConversationTree()
                self._offset = 0
                self._journal_id = identity
            file.seek(self._offset)
            for line in file:
                if not line.endswith(b"\n"):
                    break
                self._offset += len(line)
                changed = True
                if (record := _parse(line)) is not None:
                    self.tree.consume(record)
        return changed

    def journal_size(self) -> int:
        """Where the next record will start; `end` for records written before now."""
        return (self.directory / "transcript.jsonl").stat().st_size

    def records(self, end: int | None = None):
        """Read intact records; a hard kill may leave a torn final append.

        `end` stops at a `journal_size()` taken earlier, so a turn still being
        written can be left out of what is read.
        """
        with (self.directory / "transcript.jsonl").open("rb") as file:
            consumed = 0
            for line in file:
                consumed += len(line)
                if end is not None and consumed > end:
                    return
                if (record := _parse(line)) is not None:
                    yield record

    def active_records(self, end: int | None = None):
        """Records on the selected branch, by the turn each one names.

        `recording` is the pre-run-id fallback: in an older journal a record
        belongs to the last turn started before it, which is only true because
        those sessions could never have two turns open at once.
        """
        selected = set(self.tree.path(self.tree.active))
        recording = None
        for record in self.records(end):
            if record.get("kind") == "turn_started" and not side_record(record):
                recording = record.get("run_id")
            if not self.tree.nodes or (record.get("run_id") or recording) in selected:
                yield record

    def latest_plan(self) -> list[dict]:
        """Recover UI/tool state even when the last update predates replay's limit."""
        if self.tree.nodes:
            return deepcopy(self.tree.nodes[self.tree.active].plan) if self.tree.active else []
        items = []
        for record in self.active_records():
            if record.get("kind") == "PlanUpdated":
                items = record["items"]
        return items

    def tool_events(self):
        """Stream tool lifecycle records independently of the transcript replay limit."""
        for record in self.active_records():
            if record.get("kind") in {
                "ToolStarted",
                "ToolSummary",
                "turn_started",
                "turn_completed",
                "turn_cancelled",
                "turn_failed",
            }:
                yield record

    def transcript_records(self, end: int | None = None):
        """Stream active-path display records; Transcript owns the retention budget.

        Only unfinished text is buffered. Flush thinking before interleaved display
        records, as live output does, and don't repeat it at the completion marker.
        """
        partial = ""
        thinking = ""
        thinking_streamed = False
        for record in self.active_records(end):
            kind = record.get("kind")
            if kind == "ThinkingDelta":
                thinking += record["text"]
                thinking_streamed = True
                continue
            if kind not in {
                "Thinking",
                "TextDelta",
                "Message",
                "ToolSummary",
                "JobFinished",
                "EditCompleted",
                "CacheBust",
                "steering",
                "turn_started",
                "turn_completed",
                "turn_failed",
                "turn_cancelled",
            }:
                continue
            if thinking:
                yield {"kind": "thinking_partial", "text": thinking}
                thinking = ""
            if kind == "Thinking":
                if not thinking_streamed:
                    yield record
                thinking_streamed = False
            elif kind == "TextDelta":
                thinking_streamed = False
                partial += record["text"]
            elif kind == "Message":
                thinking_streamed = False
                partial = ""
                yield record
            elif kind.startswith("turn_"):
                thinking_streamed = False
                if partial:
                    yield {"kind": "partial", "markdown": partial}
                    partial = ""
                yield record
            else:
                yield record
        if thinking:
            yield {"kind": "thinking_partial", "text": thinking}
        if partial:
            yield {"kind": "partial", "markdown": partial}


class SavedSession(SessionJournal):
    """The one process that owns a session: holds its lock and writes it."""

    def __init__(self, directory: Path, info: SessionInfo) -> None:
        super().__init__(directory, info)
        directory.chmod(0o700)
        self.lock = FileLock(directory / ".lock", mode=0o600)
        try:
            self.lock.acquire(timeout=0)
        except Timeout:
            raise SessionBusy("This session is already open in another process.") from None
        try:
            for name in ("steps.sqlite3", "transcript.jsonl", "session.json"):
                private_file(directory / name)
            self.store = PrivateStepStore(
                database=directory / "steps.sqlite3",
                max_snapshots_per_run=SNAPSHOTS_PER_RUN,
            )
            for record in self.records():
                self.tree.consume(record)
        except BaseException:
            self.lock.release()
            raise

    @classmethod
    def create(
        cls, model: str, workspace: Path, root: Path | None = None, identity: str | None = None
    ):
        """Create a session; `identity` lets a worktree named before the session share its ID."""
        root = root or session_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity = identity or str(uuid4())
        directory = root / identity
        directory.mkdir(mode=0o700)
        from pcode.worktree import head_commit, project_checkout

        project = project_checkout(workspace)
        info = SessionInfo(
            id=identity,
            project=str(project) if project else None,
            start_commit=head_commit(workspace) if project else None,
            model=model,
            workspace=str(workspace.resolve()),
            created=now(),
            updated=now(),
            packages=versions(),
        )
        session = cls(directory, info)
        try:
            session.save_info()
        except BaseException:
            session.close()
            raise
        return session

    @classmethod
    def open(
        cls,
        selector: str,
        root: Path | None = None,
        workspace: Path | None = None,
        *,
        fork_if_open: bool = False,
        fork: bool = False,
    ):
        """Open a session; `fork_if_open` continues a copy of one open elsewhere.

        `fork` always continues a copy, leaving the original as it was.
        """
        path = resolve_session(selector, root, workspace)
        if fork:
            session = cls.fork(path)
            session.fork_requested = True
            return session
        try:
            return cls(path, read_info(path))
        except SessionBusy:
            if not fork_if_open:
                raise
        return cls.fork(path)

    @classmethod
    def fork(cls, source: Path):
        """Copy a session, which another process may be writing, to continue it separately.

        Nothing here takes the source's lock or writes to its directory. The
        journal is copied before the step store, so every turn the copied
        journal calls finished already has its checkpoint in the copied store,
        and the store may be a step ahead of the journal but never behind it. A
        torn final journal line is skipped on read, as after a crash.

        A turn still running in the source is copied the way a crash leaves
        one: recovery continues from its last settled step, or from the turn
        before it when it has settled none, and `/resend` carries it on.
        """
        for name in ("session.json", "transcript.jsonl", "steps.sqlite3"):
            if (source / name).is_symlink() or not (source / name).is_file():
                raise SessionError("The session to copy is missing or symlinked.")
        info = read_info(source)
        identity = str(uuid4())
        directory = source.parent / identity
        directory.mkdir(mode=0o700)
        try:
            for name in ("transcript.jsonl", "steps.sqlite3"):
                private_file(directory / name)
            shutil.copyfile(source / "transcript.jsonl", directory / "transcript.jsonl")
            # The backup API reads one consistent snapshot of a WAL store that
            # its owner keeps writing; a file copy could tear it.
            origin = sqlite3.connect(source / "steps.sqlite3")
            try:
                copy = sqlite3.connect(directory / "steps.sqlite3")
                try:
                    origin.backup(copy)
                finally:
                    copy.close()
            finally:
                origin.close()
            stamp = now()
            info = info.model_copy(
                update={"id": identity, "created": stamp, "updated": stamp, "packages": versions()}
            )
            session = cls(directory, info)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        session.forked_from = source.name
        try:
            # Written last: until the manifest exists, listings skip the copy.
            session.save_info()
        except BaseException:
            session.abandon()
            raise
        return session

    def abandon(self) -> None:
        """Close after a resume that did not go ahead; a copy made for it is removed."""
        try:
            if self.forked_from:
                shutil.rmtree(self.directory, ignore_errors=True)
        finally:
            self.close()

    def save_info(self, *, touch: bool = True) -> None:
        """Write the manifest; `touch=False` keeps `updated`, the /resume sort and date."""
        if touch:
            self.info.updated = now()
        # Atomic replacement: interruption cannot leave half a manifest.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as file:
                temporary = Path(file.name)
                file.write(self.info.model_dump_json(indent=2))
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.directory / "session.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def append(self, kind: str, *, sync: bool = False, **data) -> None:
        record = {"version": 1, "time": now(), "kind": kind, **data}
        # Conversation content is intentionally retained losslessly. The directory
        # is private; credentials/transport headers are never passed to this API.
        with (self.directory / "transcript.jsonl").open("ab+") as file:
            if file.tell():
                file.seek(-1, os.SEEK_END)
                if file.read(1) != b"\n":
                    # Preserve a torn final record for debugging, but don't glue
                    # the next valid event onto it after a restart.
                    file.write(b"\n")
            file.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
            file.flush()
            if sync:
                os.fsync(file.fileno())

        self.tree.consume(record)

    def record_error(
        self,
        error: BaseException,
        *,
        run_id: str,
        provider_context: dict[str, str] | None = None,
        detail: str = "",
    ) -> Path | None:
        """Append the traceback the transcript's bounded `error` summary cannot carry.

        A type and a message name the symptom; only frames name the line. This
        is a separate append-only file rather than a transcript record so a
        crash loop cannot push conversation history out of a replay window, and
        so nothing that renders the transcript has to filter it out.

        Diagnostics must never replace the failure being diagnosed: a directory
        that has gone away or read-only is silently accepted.
        """
        path = self.directory / "errors.log"
        try:
            private_file(path)
            with path.open("a", encoding="utf-8") as file:
                file.write(f"--- {now()} run {run_id} ---\n")
                if detail:
                    file.write(redact(detail) + "\n")
                if provider_context:
                    file.write(
                        "Configured provider: " + redact(json.dumps(provider_context)) + "\n"
                    )
                file.write(f"{error_report(error)}\n")
        except OSError:
            return None
        return path

    def event(self, event, *, run_id: str = "") -> None:
        """Record a display event, named with the turn that produced it.

        Turn membership used to be inferred from file order, which only holds
        while one turn runs at a time. An event that names its own run (tool
        events already do) keeps that name; everything else takes the caller's.
        """
        data = asdict(event)
        data["run_id"] = data.get("run_id") or run_id
        self.append(type(event).__name__, **data)

    def refresh(self) -> bool:
        """The owner's tree and manifest are already current, and hold unsaved state
        (navigation, unsaved turns' history) that re-reading the files would drop."""
        return False

    async def history_at(self, identity: str | None):
        path = self.tree.path(identity)
        for run_id in reversed(path):
            node = self.tree.nodes[run_id]
            if node.kind == "compaction":
                return deepcopy(node.history)
            # Never opt into interrupted snapshots with pending tool calls.
            snapshot = await self.store.latest_snapshot(run_id=run_id)
            if snapshot is not None:
                return snapshot.messages
            if self.tree.nodes[run_id].status == "completed":
                raise SessionError("The selected turn's checkpoint is missing; branch unchanged.")
        return []

    async def recover(self):
        """Restore settled history without replaying tools or resolving their effects.

        Interrupted tools may have changed the workspace. Keep their ledger entries
        intact for diagnostics, but do not require review to continue the session.
        """
        if self.tree.nodes:
            return await self.history_at(self.tree.active)
        # Compatibility with sessions whose journal predates run IDs. Delegated
        # runs are stored too, and a sub-agent's history is not this conversation.
        runs = [
            run
            for run in await self.store.list_runs(conversation_id=self.info.id)
            if run.parent_run_id is None
        ]
        for run in reversed(runs):
            snapshot = await self.store.latest_snapshot(run_id=run.run_id)
            if snapshot is not None:
                return snapshot.messages
        return []

    def close(self) -> None:
        self.lock.release()
