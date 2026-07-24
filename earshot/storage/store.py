"""Higher-level session store: DB + on-disk artifacts, as the API sees them.

Combines the SQLite state (:class:`earshot.storage.db.Database`) with the file
layout (rpi/specs/storage.md) and derives the API-facing session shape
(rpi/specs/api.md). Session **state** is derived, per
rpi/requirements/web-ui/list-sessions.md and processing.md.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from earshot.config import Config
from earshot.storage.db import Database
from earshot.storage.paths import m4a_name, render_session_id, session_dir

TRANSCRIPT_MD = "transcript.md"
TRANSCRIPT_RAW = "transcript_raw.json"
TRANSCRIPT_DIARIZED_RAW = "transcript_diarized_raw.json"
STATUS_JSON = "status.json"


@dataclass
class DiskInfo:
    used_percent: float
    blocked: bool


class Store:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.recordings_dir = config.recordings_dir
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

    # -- paths ------------------------------------------------------------- #

    def session_dir(self, session_id: int) -> Path:
        return session_dir(self.recordings_dir, session_id)

    def iter_session_dirs(self) -> dict[int, Path]:
        """On-disk ``rec-NNNNNN`` session directories, keyed by parsed id.

        Used by startup reconciliation to compare the filesystem against the DB
        (rpi/specs/storage.md#reconciliation). Non-matching entries are ignored.
        """
        from earshot.storage.paths import parse_session_id

        found: dict[int, Path] = {}
        if not self.recordings_dir.exists():
            return found
        for child in self.recordings_dir.iterdir():
            if not child.is_dir():
                continue
            sid = parse_session_id(child.name)
            if sid is not None:
                found[sid] = child
        return found

    def m4a_path(self, session_id: int) -> Path:
        return self.session_dir(session_id) / m4a_name()

    def transcript_path(self, session_id: int) -> Path:
        return self.session_dir(session_id) / TRANSCRIPT_MD

    def diarized_raw_path(self, session_id: int) -> Path:
        return self.session_dir(session_id) / TRANSCRIPT_DIARIZED_RAW

    # -- lifecycle --------------------------------------------------------- #

    def allocate_session(self) -> int:
        """Insert a session row (created_at = now) and create its directory.

        The id is allocated by the database before any audio exists
        (rpi/adr/session-identity.md).
        """
        created_at = datetime.now().isoformat()
        session_id = self.db.insert_session(created_at)
        self.session_dir(session_id).mkdir(parents=True, exist_ok=True)
        return session_id

    def finalize_session(self, session_id: int, duration: float, size: int) -> None:
        self.db.update_session(session_id, duration=duration, size=size)
        self.write_status_json(session_id)

    def delete_session(self, session_id: int) -> None:
        """Remove the session directory and its row (rpi/specs/api.md DELETE)."""
        d = self.session_dir(session_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        self.db.delete_session(session_id)

    def set_name(self, session_id: int, name: str | None) -> None:
        self.db.update_session(session_id, name=name)
        if self.m4a_path(session_id).exists():
            self.write_status_json(session_id)

    # -- disk -------------------------------------------------------------- #

    def disk_info(self) -> DiskInfo:
        usage = shutil.disk_usage(self.config.data_dir)
        used_percent = usage.used / usage.total * 100.0
        blocked = used_percent >= self.config.storage.disk_threshold_percent
        return DiskInfo(used_percent=round(used_percent, 1), blocked=blocked)

    # -- derivation -------------------------------------------------------- #

    def has_transcript(self, session_id: int) -> bool:
        return self.transcript_path(session_id).exists()

    def is_diarized(self, session_id: int) -> bool:
        return self.diarized_raw_path(session_id).exists()

    def derive_state(self, row: sqlite3.Row, active_id: int | None) -> str:
        if active_id is not None and row["id"] == active_id:
            return "recording"
        session_id = int(row["id"])
        if self.has_transcript(session_id):
            return "diarized" if self.is_diarized(session_id) else "transcribed"
        latest = self.db.latest_job_for_session(session_id)
        if latest is not None and latest["state"] == "failed":
            return "failed"
        return "pending"

    # -- API serialisation ------------------------------------------------- #

    def session_api(self, row: sqlite3.Row, active_id: int | None) -> dict[str, Any]:
        session_id = int(row["id"])
        return {
            "id": render_session_id(session_id),
            "name": row["name"],
            "state": self.derive_state(row, active_id),
            "created_at": row["created_at"],
            "duration": row["duration"],
            "size": row["size"],
            "has_transcript": self.has_transcript(session_id),
            "diarized": bool(row["diarized"]) and self.has_transcript(session_id),
        }

    def session_detail_api(self, row: sqlite3.Row, active_id: int | None) -> dict[str, Any]:
        from earshot.jobs.serialize import job_api  # local import avoids a cycle

        session_id = int(row["id"])
        base = self.session_api(row, active_id)
        speakers = [
            {"label": s["label"], "name": s["name"],
             "segments": self._segment_count(session_id, s["label"])}
            for s in self.db.get_speakers(session_id)
        ]
        job_row = self.db.active_job_for_session(session_id) or self.db.latest_job_for_session(session_id)
        base["speakers"] = speakers
        base["job"] = job_api(job_row) if job_row is not None else None
        return base

    def _segment_count(self, session_id: int, label: str) -> int:
        # Segment counts come from the diarized raw JSON; 0 until that lands.
        return 0

    def list_sessions_api(self, active_id: int | None) -> dict[str, Any]:
        rows = [r for r in self.db.list_sessions() if not r["missing"]]
        return {"sessions": [self.session_api(r, active_id) for r in rows]}

    # -- status.json (DB rebuild path) ------------------------------------ #

    def write_status_json(self, session_id: int) -> None:
        import json
        import socket

        row = self.db.get_session(session_id)
        if row is None:
            return
        if self.is_diarized(session_id):
            status = "diarized"
        elif self.has_transcript(session_id):
            status = "transcribed"
        else:
            status = "encoded"
        speakers = {s["label"]: s["name"] for s in self.db.get_speakers(session_id) if s["name"]}
        payload = {
            "status": status,
            "device": socket.gethostname(),
            "name": row["name"],
            "speakers": speakers,
            "created_at": row["created_at"],
            "duration": row["duration"],
        }
        path = self.session_dir(session_id) / STATUS_JSON
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
