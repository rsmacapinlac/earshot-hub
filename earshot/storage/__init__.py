"""Storage — SQLite for state, files for artifacts (rpi/adr/state-storage.md).

Session identity is a DB-allocated ``INTEGER PRIMARY KEY AUTOINCREMENT``
(rpi/adr/session-identity.md), rendered as ``rec-NNNNNN``. Artifacts live under
``recordings/rec-NNNNNN/``. Startup reconciliation and crash recovery land in the
storage milestone.
"""

from earshot.storage.paths import (
    SESSION_ID_RE,
    chunk_name,
    parse_session_id,
    render_session_id,
    session_dirname,
)
from earshot.storage.db import Database
from earshot.storage.store import Store

__all__ = [
    "SESSION_ID_RE",
    "chunk_name",
    "parse_session_id",
    "render_session_id",
    "session_dirname",
    "Database",
    "Store",
]
