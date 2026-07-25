"""Milestone 4 — storage reconciliation + crash recovery (rpi/specs/storage.md).

The fixtures cover every disagreement the reconciler must resolve: a partial last
chunk, chunks present but no m4a, an orphaned directory, a DB row without files,
files without a row, encode-failure isolation, and id-never-reused after a rebuild.
All run off-device on the stub; only ffmpeg/ffprobe are required.
"""

from __future__ import annotations

import json
import struct
import wave
from datetime import datetime
from pathlib import Path

import pytest

from earshot.config import Config
from earshot.hal.protocols import CaptureSpec
from earshot.recording import recover as recover_mod
from earshot.recording.encode import EncodeError, probe_duration
from earshot.storage.db import Database
from earshot.storage.paths import chunk_name, m4a_name, render_session_id
from earshot.storage.reconcile import reconcile
from earshot.storage.store import STATUS_JSON, Store

SPEC = CaptureSpec(sample_rate=16000, channels=1, sample_width=2)


# -- helpers ---------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)
    db = Database(cfg.db_path)
    store = Store(cfg, db)
    yield store
    db.close()


def _write_chunk(path: Path, seconds: float, *, finalized: bool = True) -> None:
    """Write a mono 16-bit silence chunk. When ``finalized`` is False, leave the
    header lengths describing a single frame — the state a crash-before-close
    produces, where the data is on disk but the sizes were never patched."""
    frames = int(SPEC.sample_rate * seconds)
    pcm = b"\x00\x00" * frames
    with wave.open(str(path), "wb") as w:
        w.setnchannels(SPEC.channels)
        w.setsampwidth(SPEC.sample_width)
        w.setframerate(SPEC.sample_rate)
        w.writeframes(pcm)
    if not finalized:
        stale = SPEC.frame_bytes  # header claims one frame; the rest looks "lost"
        with open(path, "r+b") as f:
            f.seek(4)
            f.write(struct.pack("<L", 36 + stale))
            f.seek(40)
            f.write(struct.pack("<L", stale))


def _make_dir(store: Store, session_id: int) -> Path:
    d = store.session_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_status(directory: Path, **fields) -> None:
    payload = {
        "status": "transcribed",
        "device": "test",
        "name": None,
        "speakers": {},
        "created_at": datetime.now().isoformat(),
        "duration": None,
    }
    payload.update(fields)
    (directory / STATUS_JSON).write_text(json.dumps(payload), encoding="utf-8")


def _encode_m4a(directory: Path, seconds: float = 1.0) -> None:
    """Create a real session.m4a so a directory looks already-finalized."""
    from earshot.recording.encode import encode_session

    chunk = directory / chunk_name(1)
    _write_chunk(chunk, seconds)
    encode_session([chunk], directory / m4a_name(), bitrate_kbps=32)
    chunk.unlink()


# -- crash recovery: chunks but no m4a -------------------------------------- #


def test_recover_chunks_no_m4a_updates_existing_row(store):
    sid = store.db.insert_session(datetime.now().isoformat())
    d = _make_dir(store, sid)
    _write_chunk(d / chunk_name(1), 1.0)
    _write_chunk(d / chunk_name(2), 1.0)

    report = reconcile(store, SPEC)

    assert sid in report.recovered
    assert (d / m4a_name()).exists()
    assert not list(d.glob("recording-*.wav"))  # chunks deleted after encode
    row = store.db.get_session(sid)
    assert row["duration"] and row["duration"] > 1.5  # both chunks, ~2s
    assert row["size"] and row["size"] > 0


def test_recover_partial_last_chunk(store):
    """The unfinalized final chunk's audio must be recovered from the file size,
    not the stale header — so the m4a spans both chunks, not just the first."""
    sid = store.db.insert_session(datetime.now().isoformat())
    d = _make_dir(store, sid)
    _write_chunk(d / chunk_name(1), 1.0)
    _write_chunk(d / chunk_name(2), 1.0, finalized=False)  # crash before close

    reconcile(store, SPEC)

    duration = probe_duration(d / m4a_name())
    assert duration > 1.5, f"partial chunk audio was dropped (duration={duration})"


def test_recovery_failure_keeps_chunks_and_isolates(store, monkeypatch):
    """A failed recovery leaves that session's chunks for the next boot and never
    aborts the scan — the healthy session is still recovered."""
    good = store.db.insert_session(datetime.now().isoformat())
    bad = store.db.insert_session(datetime.now().isoformat())
    dg, db_ = _make_dir(store, good), _make_dir(store, bad)
    _write_chunk(dg / chunk_name(1), 1.0)
    _write_chunk(db_ / chunk_name(1), 1.0)

    real = recover_mod.recover_session_audio

    def flaky(directory, spec, *, bitrate_kbps):
        if Path(directory).name == render_session_id(bad):
            raise EncodeError("simulated disk-full")
        return real(directory, spec, bitrate_kbps=bitrate_kbps)

    monkeypatch.setattr("earshot.storage.reconcile.recover_session_audio", flaky)

    report = reconcile(store, SPEC)

    assert good in report.recovered
    assert bad in report.recovery_failed
    assert (dg / m4a_name()).exists()               # healthy one finalized
    assert not (db_ / m4a_name()).exists()          # failed one left as chunks
    assert list(db_.glob("recording-*.wav"))        # chunks kept for retry


# -- adoption: directory with no row ---------------------------------------- #


def test_adopt_orphan_directory(store):
    """A finalized directory the DB has lost is adopted: row + speakers restored."""
    sid = 3
    d = _make_dir(store, sid)
    _encode_m4a(d, 1.0)
    _write_status(
        d, status="diarized", name="Weekly sync", occurred_at="2026-07-20T14:00",
        speakers={"Speaker 1": "Ritchie", "Speaker 2": "Sarah"},
    )

    report = reconcile(store, SPEC)

    assert sid in report.adopted
    row = store.db.get_session(sid)
    assert row is not None
    assert row["name"] == "Weekly sync"
    assert row["occurred_at"] == "2026-07-20T14:00"
    assert row["duration"] and row["duration"] > 0
    assert row["size"] and row["size"] > 0
    assert row["diarized"] == 1
    speakers = {s["label"]: s["name"] for s in store.db.get_speakers(sid)}
    assert speakers == {"Speaker 1": "Ritchie", "Speaker 2": "Sarah"}


def test_adopt_sets_sequence_so_ids_never_reused(store):
    """After adopting rec-000005 into a rebuilt DB, the next allocation is 6."""
    d = _make_dir(store, 5)
    _encode_m4a(d, 0.5)
    _write_status(d, name=None)

    reconcile(store, SPEC)

    assert store.db.get_session(5) is not None
    next_id = store.db.insert_session(datetime.now().isoformat())
    assert next_id == 6  # never 1, never a reused 5


# -- loss: row with no directory -------------------------------------------- #


def test_row_without_directory_marked_missing(store):
    sid = store.db.insert_session(datetime.now().isoformat())
    # no directory created

    report = reconcile(store, SPEC)

    assert sid in report.marked_missing
    row = store.db.get_session(sid)
    assert row is not None            # not resurrected away — id is preserved
    assert row["missing"] == 1
    # hidden from the API listing
    listing = store.list_sessions_api(active_id=None)
    assert all(s["id"] != render_session_id(sid) for s in listing["sessions"])


def test_reappearing_directory_clears_missing(store):
    """If a directory returns on a later boot, its missing flag is cleared."""
    sid = store.db.insert_session(datetime.now().isoformat())
    store.db.update_session(sid, missing=1)
    d = _make_dir(store, sid)
    _encode_m4a(d, 0.5)

    reconcile(store, SPEC)

    assert store.db.get_session(sid)["missing"] == 0


# -- idempotency & integration ---------------------------------------------- #


def test_reconcile_is_idempotent(store):
    sid = store.db.insert_session(datetime.now().isoformat())
    d = _make_dir(store, sid)
    _write_chunk(d / chunk_name(1), 1.0)

    first = reconcile(store, SPEC)
    assert sid in first.recovered
    second = reconcile(store, SPEC)  # nothing left to do
    assert second.recovered == []
    assert second.recovery_failed == []
    assert second.normal == 1


def test_reconcile_runs_on_application_boot(tmp_path, monkeypatch):
    """build_application reconciles before the control loop starts, so a session
    that crashed mid-record shows up finalized on the next boot."""
    from earshot.app import build_application

    for var in ("EARSHOT_HAL", "EARSHOT_CONFIG", "EARSHOT_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)

    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)

    # Seed a crashed session (row + chunks, no m4a) before the app boots.
    db = Database(cfg.db_path)
    seed = Store(cfg, db)
    sid = db.insert_session(datetime.now().isoformat())
    d = _make_dir(seed, sid)
    _write_chunk(d / chunk_name(1), 1.0)
    _write_chunk(d / chunk_name(2), 1.0, finalized=False)
    db.close()

    app = build_application(config=cfg, hal_override="stub", realtime=False)
    app.start()
    try:
        assert app.store.m4a_path(sid).exists()
        row = app.store.db.get_session(sid)
        assert row["duration"] and row["duration"] > 1.5
        listing = app.flask_app.test_client().get("/v1/sessions").get_json()
        assert any(s["id"] == render_session_id(sid) for s in listing["sessions"])
    finally:
        app.stop()
