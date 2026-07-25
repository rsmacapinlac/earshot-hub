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

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    occurred_at TEXT,
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
    num_speakers   INTEGER,
    route          TEXT,
    state          TEXT NOT NULL,
    stage          TEXT,
    progress       REAL,
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
            self._ensure_columns()
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()

    def _ensure_columns(self) -> None:
        """Add columns introduced after v1 without rebuilding operator data."""
        session_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(sessions)")}
        if "occurred_at" not in session_cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN occurred_at TEXT")

        job_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        if "num_speakers" not in job_cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN num_speakers INTEGER")

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
        """Raise AUTOINCREMENT's high-water mark so ids never regress after a rebuild.

        ``sqlite_sequence`` is an internal table with no UNIQUE constraint, so an
        upsert is impossible; read the current seq and write the max explicitly.
        The row for ``sessions`` exists once any row has been inserted into the
        AUTOINCREMENT table (including an explicit-id adopt).
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='sessions'"
            )
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES ('sessions', ?)",
                    (value,),
                )
            elif value > row[0]:
                self._conn.execute(
                    "UPDATE sqlite_sequence SET seq=? WHERE name='sessions'",
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

    def insert_job(self, session_id: int, kind: str, enqueued_at: str,
                   num_speakers: int | None = None) -> int:
        cur = self._execute(
            "INSERT INTO jobs (session_id, kind, num_speakers, state, enqueued_at) "
            "VALUES (?, ?, ?, 'queued', ?)",
            (session_id, kind, num_speakers, enqueued_at),
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

    # -- job queue (the worker drives these) ------------------------------- #

    def peek_next_job(self) -> sqlite3.Row | None:
        """The oldest queued job (enqueue order = job id), or None. Not claimed."""
        return self._query_one(
            "SELECT * FROM jobs WHERE state='queued' ORDER BY id ASC LIMIT 1"
        )

    def mark_job_running(self, job_id: int, route: str, started_at: str) -> bool:
        """Claim a queued job for *route*. False if it is no longer queued (e.g.
        cancelled underneath us) — the caller re-peeks. The route is fixed here, at
        dequeue, not at enqueue (rpi/specs/processing.md#the-queue)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET state='running', route=?, started_at=? "
                "WHERE id=? AND state='queued'",
                (route, started_at, job_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def mark_job_done(self, job_id: int, finished_at: str) -> None:
        self._execute(
            "UPDATE jobs SET state='done', stage=NULL, progress=NULL, "
            "last_error=NULL, finished_at=? WHERE id=?",
            (finished_at, job_id),
        )

    def mark_job_failed(self, job_id: int, attempts: int, last_error: str, finished_at: str) -> None:
        """Terminal failure: retries exhausted (rpi/specs/processing.md#failure)."""
        self._execute(
            "UPDATE jobs SET state='failed', attempts=?, last_error=?, "
            "stage=NULL, progress=NULL, finished_at=? WHERE id=?",
            (attempts, last_error, finished_at, job_id),
        )

    def requeue_job(self, job_id: int, *, attempts: int | None = None,
                    last_error: str | None = None, keep_remote: bool = False) -> None:
        """Return a job to the queue, keeping its id (so it stays in order/front).

        Used for a retry (pass the bumped *attempts*/*last_error*) and for
        preemption/crash recovery (pass neither — not a failure). ``keep_remote`` is
        accepted only for compatibility with older callers; synchronous service jobs
        have no remote state to preserve (rpi/specs/processing.md#crash-resilience)."""
        fields: dict[str, Any] = {
            "state": "queued", "route": None, "started_at": None,
            "finished_at": None, "stage": None, "progress": None,
        }
        if attempts is not None:
            fields["attempts"] = attempts
        if last_error is not None:
            fields["last_error"] = last_error
        self.update_job(job_id, **fields)

    def reset_running_jobs(self) -> int:
        """Return every ``running`` job to ``queued`` on startup (crash resilience).

        Service and local jobs both simply re-run; the synchronous service has no
        remote state to resume (rpi/specs/processing.md#crash-resilience)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET state='queued', route=NULL, started_at=NULL, "
                "stage=NULL, progress=NULL WHERE state='running'"
            )
            self._conn.commit()
            return cur.rowcount

    def cancel_queued_job(self, job_id: int, finished_at: str) -> bool:
        """Drop a queued job. False if it is not (still) queued."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET state='cancelled', finished_at=? "
                "WHERE id=? AND state='queued'",
                (finished_at, job_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def set_job_cancelled(self, job_id: int, finished_at: str) -> None:
        """Mark a job cancelled unconditionally (a running job the worker killed)."""
        self._execute(
            "UPDATE jobs SET state='cancelled', stage=NULL, progress=NULL, "
            "finished_at=? WHERE id=?",
            (finished_at, job_id),
        )
