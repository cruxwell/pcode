"""A persistent full-text index over saved conversations, refreshed on demand.

Recall used to reparse every transcript in scope on every search. Transcripts
are mostly streaming deltas (hundreds of MB across a few hundred sessions), so
calls were slow and a byte budget cut most broad searches short. This keeps
each turn's searchable text, already redacted, plus FTS5 postings in an index
file in the session directory (mode 0600, beside the transcripts that hold the
raw text anyway). A session is reread only when its transcript's identity,
size or mtime changed: transcripts are append-only and at most tens of MB, so
a changed session is reread whole rather than resumed. Rows for a session go
as soon as a refresh no longer lists it.

Several pcode processes share the file, possibly running different pcode
versions (a session keeps the source it started with). The file name carries
the format version and a hash of the redaction rules, so versions never fight
over one file; a file nobody has touched for a week is removed. Parsing
happens outside any write lock, and a session's rows are replaced in one
transaction, so a reader sees either the old turns or the new ones.
"""

import hashlib
import inspect
import json
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from pcode import diagnostics
from pcode.sessions import private_file

# Bump whenever the schema or the text a turn is indexed as changes.
VERSION = 1
INDEX_GLOB = ".history-index-*.sqlite3"
# Another version's index that has gone untouched this long is not in use.
STALE_SECONDS = 7 * 24 * 3600
# Candidates fetched per query before the phrase bonus reorders them.
CANDIDATES = 400

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    incomplete INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    session TEXT NOT NULL,
    turn TEXT NOT NULL,
    parent TEXT,
    time TEXT NOT NULL,
    status TEXT NOT NULL,
    branch TEXT NOT NULL,
    compacted INTEGER NOT NULL,
    position INTEGER NOT NULL,
    text TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    UNIQUE (session, turn)
);
-- AUTOINCREMENT: a chunk ID is never reused, so IDs from two reads can be
-- compared even when a session was reindexed in between.
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn INTEGER NOT NULL,
    start INTEGER NOT NULL,
    length INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_by_turn ON chunks (turn);
-- Contentless: the text lives once, in turns.text. Underscores stay inside
-- tokens so identifiers like search_sessions match as one word.
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_text USING fts5(
    text, content='', contentless_delete=1, tokenize="unicode61 tokenchars '_'"
)
"""


def _rules() -> str:
    """Changes whenever redaction does: text indexed under old rules is not reused."""
    try:
        source = inspect.getsource(diagnostics.redact)
    except OSError, TypeError:
        source = ""
    patterns = diagnostics._TOKEN.pattern + diagnostics._ASSIGNMENT.pattern
    return hashlib.sha256((source + patterns).encode()).hexdigest()[:12]


def index_name() -> str:
    return f".history-index-v{VERSION}-{_rules()}.sqlite3"


@dataclass
class StoredTurn:
    """A turn as indexed: redacted text and labels resolved at index time.

    Search hits leave `text` empty and carry their chunk's slice instead: a
    long turn can match hundreds of times.
    """

    id: str
    parent: str | None
    time: str
    status: str
    branch: str
    compacted: bool
    text: str
    conclusion: str


@dataclass
class Hit:
    session: str
    turn: StoredTurn
    chunk: int
    start: int
    text: str
    score: float


def signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"


def _turn(row, text: str) -> StoredTurn:
    turn, parent, time_, status, branch, compacted, conclusion = row
    return StoredTurn(turn, parent, time_, status, branch, bool(compacted), text, conclusion)


TURN_COLUMNS = "t.turn, t.parent, t.time, t.status, t.branch, t.compacted, t.conclusion"
CHUNK_TEXT = "substr(t.text, c.start + 1, c.length)"
IN_SCOPE = (
    "t.session IN (SELECT value FROM json_each(?)) "
    "AND NOT (t.session = ? AND t.turn = ? AND t.compacted = 0)"
)


class HistoryIndex:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / index_name()

    def _remove(self, path: Path) -> None:
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)

    def _sweep(self) -> None:
        now = time.time()
        for path in self.root.glob(INDEX_GLOB):
            try:
                if path != self.path and now - path.stat().st_mtime > STALE_SECONDS:
                    self._remove(path)
            except OSError:
                pass

    def _open(self) -> sqlite3.Connection:
        private_file(self.path)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            # Deleted pages are zeroed, so a forgotten session's text leaves the file.
            db.execute("PRAGMA secure_delete=ON")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunk_text'").fetchone():
                db.execute("BEGIN IMMEDIATE")
                try:
                    # Statement by statement: executescript would commit first.
                    for statement in SCHEMA.split(";"):
                        if statement.strip():
                            db.execute(statement)
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._sweep()
        try:
            db = self._open()
        except sqlite3.DatabaseError as error:
            if isinstance(error, sqlite3.OperationalError):
                raise  # Locked or unwritable: the file is fine, try again later.
            # Not a database, or corrupt. It is only a cache of the transcripts.
            self._remove(self.path)
            db = self._open()
        with closing(db):
            yield db

    @contextmanager
    def snapshot(self, db):
        """One consistent read across several queries."""
        db.execute("BEGIN")
        try:
            yield
        finally:
            db.execute("COMMIT")

    def signatures(self, db) -> dict[str, tuple[str, bool]]:
        return {
            identity: (saved, bool(incomplete))
            for identity, saved, incomplete in db.execute(
                "SELECT id, signature, incomplete FROM sessions"
            )
        }

    def state(self, db, session: str) -> tuple[str, bool] | None:
        """A session's stored signature and torn-tail flag, if it is indexed."""
        row = db.execute(
            "SELECT signature, incomplete FROM sessions WHERE id = ?", (session,)
        ).fetchone()
        return (row[0], bool(row[1])) if row else None

    def _delete(self, db, session: str) -> None:
        chunks = [
            identity
            for (identity,) in db.execute(
                "SELECT c.id FROM chunks c JOIN turns t ON t.id = c.turn WHERE t.session = ?",
                (session,),
            )
        ]
        db.executemany("DELETE FROM chunk_text WHERE rowid = ?", [(c,) for c in chunks])
        db.execute(
            "DELETE FROM chunks WHERE turn IN (SELECT id FROM turns WHERE session = ?)", (session,)
        )
        db.execute("DELETE FROM turns WHERE session = ?", (session,))
        db.execute("DELETE FROM sessions WHERE id = ?", (session,))

    def forget(self, db, sessions: list[str]) -> None:
        if not sessions:
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            for session in sessions:
                self._delete(db, session)
            # FTS5 only tombstones deleted rows until segments merge; merge now
            # so the words go too. Sessions are deleted rarely, so this is cheap.
            db.execute("INSERT INTO chunk_text (chunk_text) VALUES ('optimize')")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        # Passive: never waits on other connections. Whatever it cannot copy
        # back now goes at a later checkpoint.
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def store(
        self,
        db,
        session: str,
        saved_signature: str,
        incomplete: bool,
        turns: list[StoredTurn],
        chunk_chars: int,
        overlap: int,
    ) -> None:
        """Replace a session's rows, unless another process already stored this snapshot."""
        db.execute("BEGIN IMMEDIATE")
        try:
            if (state := self.state(db, session)) and state[0] == saved_signature:
                db.execute("COMMIT")
                return
            self._delete(db, session)
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)", (session, saved_signature, int(incomplete))
            )
            for position, turn in enumerate(turns):
                cursor = db.execute(
                    "INSERT INTO turns (session, turn, parent, time, status, branch, compacted, "
                    "position, text, conclusion) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session,
                        turn.id,
                        turn.parent,
                        turn.time,
                        turn.status,
                        turn.branch,
                        int(turn.compacted),
                        position,
                        turn.text,
                        turn.conclusion,
                    ),
                )
                turn_row = cursor.lastrowid
                for start in range(0, len(turn.text), chunk_chars - overlap):
                    text = turn.text[start : start + chunk_chars]
                    chunk = db.execute(
                        "INSERT INTO chunks (turn, start, length) VALUES (?, ?, ?)",
                        (turn_row, start, len(text)),
                    ).lastrowid
                    db.execute("INSERT INTO chunk_text (rowid, text) VALUES (?, ?)", (chunk, text))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def match(
        self, db, expression: str, sessions: list[str], exclude: tuple[str, str] | None
    ) -> list[Hit]:
        """FTS5 BM25 candidates in scope, best first; ties go to the newer turn."""
        session, turn = exclude or ("", "")
        rows = db.execute(
            f"SELECT t.session, {TURN_COLUMNS}, c.id, c.start, {CHUNK_TEXT}, bm25(chunk_text) "
            "FROM chunk_text JOIN chunks c ON c.id = chunk_text.rowid "
            f"JOIN turns t ON t.id = c.turn WHERE chunk_text MATCH ? AND {IN_SCOPE} "
            "ORDER BY bm25(chunk_text), t.time DESC, t.position DESC, c.start LIMIT ?",
            (expression, json.dumps(sessions), session, turn, CANDIDATES),
        ).fetchall()
        return [Hit(r[0], _turn(r[1:8], ""), r[8], r[9], r[10], -r[11]) for r in rows]

    def chunks(self, db, sessions: list[str], exclude: tuple[str, str] | None) -> list[Hit]:
        """Every chunk in scope, newest turn first, for semantic ranking."""
        session, turn = exclude or ("", "")
        rows = db.execute(
            f"SELECT t.session, {TURN_COLUMNS}, c.id, c.start, {CHUNK_TEXT} "
            f"FROM chunks c JOIN turns t ON t.id = c.turn WHERE {IN_SCOPE} "
            "ORDER BY t.session, t.position DESC, c.start",
            (json.dumps(sessions), session, turn),
        ).fetchall()
        return [Hit(r[0], _turn(r[1:8], ""), r[8], r[9], r[10], 0.0) for r in rows]

    def turns(self, db, session: str) -> dict[str, StoredTurn]:
        rows = db.execute(
            f"SELECT {TURN_COLUMNS}, t.text FROM turns t WHERE t.session = ? ORDER BY t.position",
            (session,),
        ).fetchall()
        return {row[0]: _turn(row[:7], row[7]) for row in rows}
