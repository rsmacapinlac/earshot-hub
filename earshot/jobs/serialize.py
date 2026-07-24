"""Serialise a ``jobs`` row to the API Job shape (rpi/specs/api.md)."""

from __future__ import annotations

import sqlite3
from typing import Any

from earshot.storage.paths import render_session_id


def job_api(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_id": render_session_id(int(row["session_id"])),
        "kind": row["kind"],
        "route": row["route"],
        "state": row["state"],
        "stage": row["stage"],
        "progress": row["progress"],
        "attempts": int(row["attempts"]),
        "last_error": row["last_error"],
        "enqueued_at": row["enqueued_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
