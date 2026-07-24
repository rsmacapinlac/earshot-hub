"""SQLite state store (rpi/adr/state-storage.md).

WAL mode; one writer, concurrent readers, crash-safe commits. `sqlite3` is in the
standard library — no daemon, no dependency. A single connection is shared across
threads (``check_same_thread=False``) and every access is serialised by a lock,
which is ample for the device's handful of operations while keeping correctness
obvious.

Session identity is ``INTEGER PRIMARY KEY AUTOINCREMENT`` — the ``AUTOINCREMENT``
keyword is required so ids are monotonic and never reused
(rpi/adr/session-identity.md).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    created_at  TEXT NOT NULL,
    duration    REAL,
    size        INTEGER,
    diarized    INTEGER NOT NULL DEFAULT 0,
    missing     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS speakers (
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    name        TEXT,
    PRIMARY KEY (session_id, label)
);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    route          TEXT,
    state          TEXT NOT NULL,
    stage          TEXT,
    progress       REAL,
    remote_job_id  TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    enqueued_at    TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- generic helpers --------------------------------------------------- #

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def _query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # -- sessions ---------------------------------------------------------- #

    def insert_session(self, created_at: str, name: str | None = None) -> int:
        """Insert a new session row and return its allocated id.

        This is on the capture path: the id exists before any audio is written.
        """
        cur = self._execute(
            "INSERT INTO sessions (name, created_at) VALUES (?, ?)",
            (name, created_at),
        )
        return int(cur.lastrowid)

    def adopt_session(self, session_id: int, created_at: str, **fields: Any) -> None:
        """Insert a row with an explicit id (reconciliation adopting a directory)."""
        cols = ["id", "created_at"] + list(fields)
        vals = [session_id, created_at] + list(fields.values())
        placeholders = ", ".join("?" for _ in cols)
        self._execute(
            f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )

    def get_session(self, session_id: int) -> sqlite3.Row | None:
        return self._query_one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def list_sessions(self) -> list[sqlite3.Row]:
        """All sessions, newest id first (ordering is by id, never by clock)."""
        return self._query("SELECT * FROM sessions ORDER BY id DESC")

    def update_session(self, session_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k}=?" for k in fields)
        self._execute(
            f"UPDATE sessions SET {assignments} WHERE id=?",
            list(fields.values()) + [session_id],
        )

    def delete_session(self, session_id: int) -> None:
        self._execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def max_session_id(self) -> int:
        row = self._query_one("SELECT MAX(id) AS m FROM sessions")
        return int(row["m"]) if row and row["m"] is not None else 0

    def set_sqlite_sequence(self, value: int) -> None:
        """Bump AUTOINCREMENT's high-water mark so ids never regress after a rebuild."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('sessions', ?) "
                "ON CONFLICT(name) DO UPDATE SET seq=MAX(seq, excluded.seq)",
                (value,),
            )
            self._conn.commit()

    # -- speakers ---------------------------------------------------------- #

    def replace_speakers(self, session_id: int, labels: Iterable[str]) -> None:
        """Ensure a row exists for each label, preserving any assigned name."""
        with self._lock:
            for label in labels:
                self._conn.execute(
                    "INSERT OR IGNORE INTO speakers (session_id, label, name) "
                    "VALUES (?, ?, NULL)",
                    (session_id, label),
                )
            self._conn.commit()

    def clear_speakers(self, session_id: int) -> None:
        self._execute("DELETE FROM speakers WHERE session_id=?", (session_id,))

    def set_speaker_name(self, session_id: int, label: str, name: str | None) -> None:
        self._execute(
            "INSERT INTO speakers (session_id, label, name) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id, label) DO UPDATE SET name=excluded.name",
            (session_id, label, name),
        )

    def get_speakers(self, session_id: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT label, name FROM speakers WHERE session_id=? ORDER BY label",
            (session_id,),
        )

    # -- jobs -------------------------------------------------------------- #

    def insert_job(self, session_id: int, kind: str, enqueued_at: str) -> int:
        cur = self._execute(
            "INSERT INTO jobs (session_id, kind, state, enqueued_at) "
            "VALUES (?, ?, 'queued', ?)",
            (session_id, kind, enqueued_at),
        )
        return int(cur.lastrowid)

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self._query_one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def list_jobs(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM jobs ORDER BY id ASC")

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k}=?" for k in fields)
        self._execute(
            f"UPDATE jobs SET {assignments} WHERE id=?",
            list(fields.values()) + [job_id],
        )

    def latest_job_for_session(self, session_id: int) -> sqlite3.Row | None:
        return self._query_one(
            "SELECT * FROM jobs WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )

    def active_job_for_session(self, session_id: int) -> sqlite3.Row | None:
        """The queued or running job for a session, if any (at most one)."""
        return self._query_one(
            "SELECT * FROM jobs WHERE session_id=? AND state IN ('queued','running') "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
