"""Startup reconciliation between the SQLite state and the on-disk recordings.

Sessions are born in the database, so the two stores can only disagree through
loss (a rebuilt/deleted ``earshot.db``) or a crash mid-finalize. On boot we walk
the union of DB rows and ``rec-NNNNNN`` directories and apply
rpi/specs/storage.md#reconciliation:

| Situation                              | Action                                    |
|----------------------------------------|-------------------------------------------|
| Row and directory both present         | Normal. Nothing to do.                    |
| Row with no directory                  | Mark the session missing; do not resurrect|
| Directory with no row                  | Adopt it — read status.json, insert a row |
| Directory with chunks but no session.m4a | Finalize (crash recovery), then adopt/keep |

Crash recovery reuses the end-of-session concat-and-encode pass
(:mod:`earshot.recording.recover`). A single session's recovery failure is
logged and never aborts the scan of the others; its chunks are left for the next
boot. After adopting, ``sqlite_sequence`` is raised to the highest id seen so a
rebuilt database can never re-issue an id that already exists on disk.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from earshot.hal.protocols import CaptureSpec
from earshot.recording.encode import EncodeError, probe_duration
from earshot.recording.recover import recover_session_audio
from earshot.storage.paths import m4a_name, render_session_id
from earshot.storage.store import STATUS_JSON, Store

log = logging.getLogger("earshot.storage")


@dataclass
class ReconcileReport:
    """What reconciliation did — one entry per non-trivial session."""

    normal: int = 0
    marked_missing: list[int] = field(default_factory=list)
    adopted: list[int] = field(default_factory=list)
    recovered: list[int] = field(default_factory=list)
    recovery_failed: list[int] = field(default_factory=list)
    orphan_empty: list[int] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"normal={self.normal} "
            f"missing={len(self.marked_missing)} "
            f"adopted={len(self.adopted)} "
            f"recovered={len(self.recovered)} "
            f"recovery_failed={len(self.recovery_failed)} "
            f"orphan_empty={len(self.orphan_empty)}"
        )


def reconcile(store: Store, spec: CaptureSpec) -> ReconcileReport:
    """Reconcile the DB against the recordings directory. Idempotent; safe to
    re-run every boot. Returns a :class:`ReconcileReport`."""
    db = store.db
    bitrate = store.config.recording.encode_bitrate_kbps
    report = ReconcileReport()

    disk = store.iter_session_dirs()
    rows = {int(r["id"]): r for r in db.list_sessions()}

    for sid in sorted(set(disk) | set(rows)):
        _reconcile_one(store, spec, bitrate, sid, disk.get(sid), rows.get(sid), report)

    highest = max([0, *disk, *rows])
    if highest:
        db.set_sqlite_sequence(highest)

    log.info("reconcile: %s", report.summary())
    return report


def _reconcile_one(store, spec, bitrate, sid, directory, row, report) -> None:
    db = store.db
    rendered = render_session_id(sid)

    # Row with no directory: loss or outside removal. Mark it, keep the id.
    if directory is None:
        if not row["missing"]:
            db.update_session(sid, missing=1)
            log.info("%s marked missing (row, no directory)", rendered)
        report.marked_missing.append(sid)
        return

    m4a = directory / m4a_name()
    recovered = None

    # No m4a: an interrupted session — try the crash-recovery encode pass.
    if not m4a.exists():
        try:
            recovered = recover_session_audio(directory, spec, bitrate_kbps=bitrate)
        except EncodeError as exc:
            log.warning("%s crash recovery failed (chunks kept for next boot): %s", rendered, exc)
            report.recovery_failed.append(sid)
            if row is not None and row["missing"]:
                db.update_session(sid, missing=0)
            return

    # An empty directory (no m4a, no usable chunks): nothing to finalize.
    if not m4a.exists():
        if row is None:
            log.info("%s orphan directory with no audio (not adopted)", rendered)
            report.orphan_empty.append(sid)
        elif row["missing"]:
            db.update_session(sid, missing=0)
        return

    # A finalized m4a is present (pre-existing or just recovered).
    if row is None:
        _adopt(store, sid, directory, recovered)
        report.adopted.append(sid)
        if recovered is not None:
            report.recovered.append(sid)
        return

    if row["missing"]:
        db.update_session(sid, missing=0)
    if recovered is not None:
        db.update_session(sid, duration=recovered.duration, size=recovered.size)
        store.write_status_json(sid)
        report.recovered.append(sid)
    else:
        report.normal += 1


def _adopt(store, sid, directory, recovered) -> None:
    """Insert a row for a directory the DB has no record of (status.json + m4a)."""
    db = store.db
    meta = _read_status_json(directory)
    m4a = directory / m4a_name()

    if recovered is not None:
        duration, size = recovered.duration, recovered.size
    else:
        duration = meta.get("duration")
        if duration is None:
            try:
                duration = probe_duration(m4a)
            except EncodeError:
                duration = None
        size = m4a.stat().st_size

    created_at = meta.get("created_at") or datetime.now().isoformat()
    diarized = 1 if meta.get("status") == "diarized" else 0
    db.adopt_session(
        sid, created_at,
        name=meta.get("name"), duration=duration, size=size, diarized=diarized,
    )
    speakers = meta.get("speakers") or {}
    if isinstance(speakers, dict):
        for label, name in speakers.items():
            db.set_speaker_name(sid, label, name)
    store.write_status_json(sid)  # refresh with the now-authoritative row
    log.info("%s adopted from disk", render_session_id(sid))


def _read_status_json(directory: Path) -> dict:
    path = directory / STATUS_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
