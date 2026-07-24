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


def _labels_in_order(segments) -> list[str]:
    """Distinct speaker labels in first-appearance order (Speaker 1, 2, …)."""
    seen: list[str] = []
    for s in segments:
        if s.speaker and s.speaker not in seen:
            seen.append(s.speaker)
    return seen


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

    def transcript_raw_path(self, session_id: int) -> Path:
        return self.session_dir(session_id) / TRANSCRIPT_RAW

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

    # -- transcripts (job results) ---------------------------------------- #

    def write_transcript_result(self, session_id: int, segments, *, diarized: bool = False) -> None:
        """Persist a completed job's segments and render ``transcript.md``.

        A plain transcribe **reverts** any prior diarization (removes the diarized
        raw and clears speaker labels), which is how a diarized session is reverted
        even locally (rpi/specs/processing.md#diarization). Diarize registers the
        detected labels. There is only ever one ``transcript.md`` per session.
        """
        import json

        sdir = self.session_dir(session_id)
        raw_name = TRANSCRIPT_DIARIZED_RAW if diarized else TRANSCRIPT_RAW
        payload = [s.api() for s in segments]
        self._atomic_write(sdir / raw_name, json.dumps(payload, indent=2))

        if diarized:
            self.db.update_session(session_id, diarized=1)
            labels = _labels_in_order(segments)
            self.db.replace_speakers(session_id, labels)
        else:
            self.diarized_raw_path(session_id).unlink(missing_ok=True)
            self.db.clear_speakers(session_id)
            self.db.update_session(session_id, diarized=0)

        self._render_transcript(session_id, list(segments))
        self.write_status_json(session_id)

    def assign_speaker(self, session_id: int, label: str, name: str | None) -> None:
        """Assign/clear a speaker name and substitute it throughout ``transcript.md``.

        Local relabelling only — nothing is sent anywhere
        (rpi/requirements/web-ui/name-speakers.md)."""
        self.db.set_speaker_name(session_id, label, name)
        if self.is_diarized(session_id):
            self._render_transcript(session_id, self.read_current_segments(session_id))
        self.write_status_json(session_id)

    def _render_transcript(self, session_id: int, segments: list) -> None:
        from earshot.jobs.transcript import render

        row = self.db.get_session(session_id)
        names = {s["label"]: s["name"] for s in self.db.get_speakers(session_id) if s["name"]}
        md = render(
            header=row["name"] or render_session_id(session_id),
            session_dirname=render_session_id(session_id),
            duration=row["duration"] or 0.0,
            segments=segments,
            speaker_names=names,
        )
        self._atomic_write(self.session_dir(session_id) / TRANSCRIPT_MD, md)

    def read_current_segments(self, session_id: int) -> list:
        """The current transcript's segments (diarized raw if diarized, else raw)."""
        import json

        from earshot.jobs.transcript import segments_from_raw

        path = self.diarized_raw_path(session_id) if self.is_diarized(session_id) \
            else self.transcript_raw_path(session_id)
        if not path.exists():
            return []
        return segments_from_raw(json.loads(path.read_text(encoding="utf-8")))

    def transcript_markdown(self, session_id: int) -> str | None:
        path = self.transcript_path(session_id)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def pending_session_ids(self) -> list[int]:
        """Sessions eligible for a bulk transcribe: finalized, no transcript, no
        active job, and no unresolved failure — oldest id first."""
        out: list[int] = []
        for row in self.db.list_sessions():
            sid = int(row["id"])
            if row["missing"] or not self.m4a_path(sid).exists():
                continue
            if self.has_transcript(sid) or self.db.active_job_for_session(sid) is not None:
                continue
            latest = self.db.latest_job_for_session(sid)
            if latest is not None and latest["state"] == "failed":
                continue
            out.append(sid)
        return sorted(out)

    def _atomic_write(self, path: Path, text: str) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

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
        job_row = self.db.active_job_for_session(session_id) or self.db.latest_job_for_session(session_id)
        base["speakers"] = self.speakers_api(session_id)["speakers"]
        base["job"] = job_api(job_row) if job_row is not None else None
        return base

    def speakers_api(self, session_id: int) -> dict[str, Any]:
        return {
            "speakers": [
                {"label": s["label"], "name": s["name"],
                 "segments": self._segment_count(session_id, s["label"])}
                for s in self.db.get_speakers(session_id)
            ]
        }

    def _segment_count(self, session_id: int, label: str) -> int:
        return sum(1 for s in self.read_current_segments(session_id) if s.speaker == label)

    def speaker_sample(self, session_id: int, label: str, *, max_seconds: float = 6.0) -> bytes:
        """A short audio sample of *label*'s voice, cut from ``session.m4a`` for the
        user to listen to before naming (rpi/specs/api.md). Returns m4a bytes, or
        raises KeyError if the label has no segments."""
        segs = [s for s in self.read_current_segments(session_id) if s.speaker == label]
        if not segs:
            raise KeyError(label)
        seg = max(segs, key=lambda s: s.end - s.start)  # the clearest (longest) turn
        start = max(0.0, seg.start)
        duration = min(max_seconds, max(1.0, seg.end - seg.start))
        from earshot.recording.encode import cut_sample

        return cut_sample(self.m4a_path(session_id), start=start, duration=duration)

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
