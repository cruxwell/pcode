"""A persistent full-text index over saved conversations, refreshed on demand.

Recall used to reparse every transcript in scope on every search. Transcripts
are mostly streaming deltas (hundreds of MB across a few hundred sessions), so
calls were slow and a byte budget cut most broad searches short. This keeps
each turn's searchable text, already redacted, plus FTS5 postings in
`<session-dir>/.history-index.sqlite3` (mode 0600, beside the transcripts that
hold the raw text anyway). A session is reread only when its transcript's
identity, size or mtime changed: transcripts are append-only and at most tens
of MB, so a changed session is reread whole rather than resumed. Rows for a
session go as soon as a refresh no longer lists it.

Several pcode processes share the file. Parsing happens outside any write
lock, and a session's rows are replaced in one transaction, so a reader sees
either the old turns or the new ones.
"""

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from pcode.sessions import private_file

INDEX_NAME = ".history-index.sqlite3"
# Bump whenever the schema or the text a turn is indexed as changes; an index
# written by another version is dropped and rebuilt.
VERSION = 1
# Candidates fetched per query before the phrase bonus reorders them.
CANDIDATES = 400

SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    incomplete INTEGER NOT NULL
);
CREATE TABLE turns (
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
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    turn INTEGER NOT NULL,
    start INTEGER NOT NULL,
    length INTEGER NOT NULL
);
CREATE INDEX chunks_by_turn ON chunks (turn);
-- Contentless: the text lives once, in turns.text. Underscores stay inside
-- tokens so identifiers like search_sessions match as one word.
CREATE VIRTUAL TABLE chunk_text USING fts5(
    text, content='', contentless_delete=1, tokenize="unicode61 tokenchars '_'"
);
"""


@dataclass
class StoredTurn:
    """A turn as indexed: redacted text and labels resolved at index time."""

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
    length: int
    score: float


def signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"


def _turn(row) -> StoredTurn:
    turn, parent, time, status, branch, compacted, text, conclusion = row
    return StoredTurn(turn, parent, time, status, branch, bool(compacted), text, conclusion)


TURN_COLUMNS = "t.turn, t.parent, t.time, t.status, t.branch, t.compacted, t.text, t.conclusion"


class HistoryIndex:
    def __init__(self, root: Path):
        self.path = root / INDEX_NAME

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_file(self.path)
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            # Deleted pages are zeroed, so a forgotten session's text leaves the file.
            db.execute("PRAGMA secure_delete=ON")
            if db.execute("PRAGMA user_version").fetchone()[0] != VERSION:
                self._rebuild(db)
            yield db

    def _rebuild(self, db: sqlite3.Connection) -> None:
        db.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have rebuilt it while this one waited.
            if db.execute("PRAGMA user_version").fetchone()[0] != VERSION:
                for (name,) in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'chunk_text_%'"
                ).fetchall():
                    db.execute(f'DROP TABLE IF EXISTS "{name}"')
                # Statement by statement: executescript would commit first.
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(f"PRAGMA user_version = {VERSION}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def signatures(self, db) -> dict[str, tuple[str, bool]]:
        return {
            identity: (saved, bool(incomplete))
            for identity, saved, incomplete in db.execute(
                "SELECT id, signature, incomplete FROM sessions"
            )
        }

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
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

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
            row = db.execute("SELECT signature FROM sessions WHERE id = ?", (session,)).fetchone()
            if row and row[0] == saved_signature:
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
        self,
        db,
        expression: str,
        sessions: list[str],
        exclude: tuple[str, str] | None,
    ) -> list[Hit]:
        """FTS5 BM25 candidates in scope, best first; ties go to the newer turn."""
        session, turn = exclude or ("", "")
        rows = db.execute(
            f"SELECT t.session, {TURN_COLUMNS}, c.id, c.start, c.length, bm25(chunk_text) "
            "FROM chunk_text JOIN chunks c ON c.id = chunk_text.rowid "
            "JOIN turns t ON t.id = c.turn "
            "WHERE chunk_text MATCH ? AND t.session IN (SELECT value FROM json_each(?)) "
            "AND NOT (t.session = ? AND t.turn = ? AND t.compacted = 0) "
            "ORDER BY bm25(chunk_text), t.time DESC, t.position DESC, c.start LIMIT ?",
            (expression, json.dumps(sessions), session, turn, CANDIDATES),
        ).fetchall()
        return [Hit(row[0], _turn(row[1:9]), row[9], row[10], row[11], -row[12]) for row in rows]

    def chunks(self, db, sessions: list[str], exclude: tuple[str, str] | None) -> list[Hit]:
        """Every chunk in scope, newest turn first, for semantic ranking."""
        session, turn = exclude or ("", "")
        rows = db.execute(
            f"SELECT t.session, {TURN_COLUMNS}, c.id, c.start, c.length "
            "FROM chunks c JOIN turns t ON t.id = c.turn "
            "WHERE t.session IN (SELECT value FROM json_each(?)) "
            "AND NOT (t.session = ? AND t.turn = ? AND t.compacted = 0) "
            "ORDER BY t.session, t.position DESC, c.start",
            (json.dumps(sessions), session, turn),
        ).fetchall()
        return [Hit(row[0], _turn(row[1:9]), row[9], row[10], row[11], 0.0) for row in rows]

    def turns(self, db, session: str) -> dict[str, StoredTurn]:
        rows = db.execute(
            f"SELECT {TURN_COLUMNS} FROM turns t WHERE t.session = ? ORDER BY t.position",
            (session,),
        ).fetchall()
        return {row[0]: _turn(row) for row in rows}
