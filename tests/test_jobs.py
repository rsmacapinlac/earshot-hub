"""Milestone 6 — the job engine and local transcription.

A fake transcriber stands in for faster-whisper so the queue, routing, retry,
preemption, and crash-recovery logic are exercised off-device (rpi/specs/
processing.md, rpi/adr/job-execution.md). Every job runs through the real worker
thread, control loop, store, and /v1 API.
"""

from __future__ import annotations

import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import pytest

from earshot.app import build_application
from earshot.config import Config
from earshot.jobs.transcribe import Cancelled, TranscribeError
from earshot.jobs.transcript import Segment
from earshot.recording.encode import encode_session, probe_duration
from earshot.storage.paths import chunk_name, m4a_name, render_session_id

SPEC_RATE = 16000


# -- fakes & helpers -------------------------------------------------------- #


class FakeTranscriber:
    """Stands in for LocalTranscriber. Optionally blocks (a long job) so tests can
    observe `processing` and preempt/cancel it."""

    def __init__(self, *, segments=None, error=None, block: threading.Event | None = None):
        self._segments = segments if segments is not None else [Segment(0.0, 1.0, "hello world")]
        self._error = error
        self._block = block
        self._cancel = threading.Event()

    def run(self, m4a_path):
        if self._block is not None:
            while not self._cancel.is_set() and not self._block.wait(0.02):
                pass
        if self._cancel.is_set():
            raise Cancelled()
        if self._error is not None:
            raise TranscribeError(self._error)
        return self._segments

    def cancel(self):
        self._cancel.set()


def _write_silence(path: Path, seconds: float) -> None:
    frames = int(SPEC_RATE * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SPEC_RATE)
        w.writeframes(b"\x00\x00" * frames)


def seed_session(store, *, seconds: float = 1.0, name: str | None = None) -> int:
    """Create a finalized session (row + session.m4a), ready to process."""
    sid = store.db.insert_session(datetime.now().isoformat(), name=name)
    d = store.session_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    chunk = d / chunk_name(1)
    _write_silence(chunk, seconds)
    encode_session([chunk], d / m4a_name(), bitrate_kbps=32)
    chunk.unlink()
    m4a = d / m4a_name()
    store.finalize_session(sid, probe_duration(m4a), m4a.stat().st_size)
    return sid


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    for var in ("EARSHOT_HAL", "EARSHOT_CONFIG", "EARSHOT_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    apps = []

    def _make(*, factory=None, max_failures: int = 3, min_duration: int = 0):
        cfg = Config()
        cfg.storage.data_dir = str(tmp_path)
        cfg.processing.max_failures = max_failures
        cfg.recording.min_duration_seconds = min_duration
        if factory is None:
            factory = lambda: FakeTranscriber()  # noqa: E731
        app = build_application(
            config=cfg, hal_override="stub", realtime=True, transcriber_factory=factory,
        )
        app.start()
        apps.append(app)
        return app

    yield _make
    for app in apps:
        app.stop()


def _poll(fn, timeout=4.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = fn()
        if val:
            return val
        time.sleep(interval)
    return fn()


def _job(client, job_id):
    return client.get(f"/v1/jobs/{job_id}").get_json()


# -- success path ----------------------------------------------------------- #


def test_transcribe_success_writes_and_renders(make_app):
    app = make_app(factory=lambda: FakeTranscriber(segments=[Segment(0.0, 2.0, "hello world")]))
    client = app.flask_app.test_client()
    sid = seed_session(app.store, name="Weekly sync")

    resp = client.post(f"/v1/sessions/{render_session_id(sid)}/jobs", json={"kind": "transcribe"})
    assert resp.status_code == 202
    job = resp.get_json()
    assert job["state"] == "queued" and job["route"] is None and job["attempts"] == 0

    done = _poll(lambda: _job(client, job["id"])["state"] == "done")
    assert done, _job(client, job["id"])
    finished = _job(client, job["id"])
    assert finished["route"] == "local"
    assert finished["finished_at"] is not None

    # Session now transcribed; transcript readable both ways.
    sess = client.get(f"/v1/sessions/{render_session_id(sid)}").get_json()
    assert sess["state"] == "transcribed" and sess["has_transcript"] is True

    md = client.get(f"/v1/sessions/{render_session_id(sid)}/transcript",
                    headers={"Accept": "text/markdown"}).get_data(as_text=True)
    assert "# Weekly sync" in md and "hello world" in md and f"**Session:** {render_session_id(sid)}" in md

    segs = client.get(f"/v1/sessions/{render_session_id(sid)}/transcript",
                      headers={"Accept": "application/json"}).get_json()
    assert segs == [{"start": 0.0, "end": 2.0, "text": "hello world"}]


def test_transcribe_shows_processing_state(make_app):
    block = threading.Event()
    app = make_app(factory=lambda: FakeTranscriber(block=block))
    client = app.flask_app.test_client()
    sid = seed_session(app.store)
    client.post(f"/v1/sessions/{render_session_id(sid)}/jobs", json={"kind": "transcribe"})

    assert _poll(lambda: client.get("/v1/status").get_json()["state"] == "processing")
    status = client.get("/v1/status").get_json()
    assert status["processing"]["session_id"] == render_session_id(sid)
    assert status["processing"]["kind"] == "transcribe" and status["processing"]["route"] == "local"
    assert status["led"] == {"rgb": [255, 179, 0], "pattern": "very_slow_pulse"}

    block.set()
    assert _poll(lambda: client.get("/v1/status").get_json()["state"] == "idle")
    assert client.get("/v1/status").get_json()["processing"] is None


# -- preemption ------------------------------------------------------------- #


def test_recording_preempts_local_job(make_app):
    block = threading.Event()
    app = make_app(factory=lambda: FakeTranscriber(block=block))
    client = app.flask_app.test_client()
    sid = seed_session(app.store)
    job = client.post(f"/v1/sessions/{render_session_id(sid)}/jobs",
                      json={"kind": "transcribe"}).get_json()
    assert _poll(lambda: client.get("/v1/status").get_json()["state"] == "processing")

    # Start recording — the local job must yield immediately and requeue.
    assert client.post("/v1/recording").status_code == 201
    assert _poll(lambda: client.get("/v1/status").get_json()["state"] == "recording")
    assert _poll(lambda: _job(client, job["id"])["state"] == "queued")
    assert _job(client, job["id"])["attempts"] == 0  # preemption is not a failure

    # Stop recording; the requeued job runs again and completes.
    time.sleep(0.15)
    client.delete("/v1/recording")
    block.set()
    assert _poll(lambda: _job(client, job["id"])["state"] == "done"), _job(client, job["id"])


# -- retry / failure -------------------------------------------------------- #


def test_failure_retries_then_marks_failed(make_app):
    app = make_app(factory=lambda: FakeTranscriber(error="boom"), max_failures=2)
    client = app.flask_app.test_client()
    sid = seed_session(app.store)
    job = client.post(f"/v1/sessions/{render_session_id(sid)}/jobs",
                      json={"kind": "transcribe"}).get_json()

    assert _poll(lambda: _job(client, job["id"])["state"] == "failed"), _job(client, job["id"])
    failed = _job(client, job["id"])
    assert failed["attempts"] == 2 and "boom" in failed["last_error"]
    # A failed job makes the session `failed`, not pending.
    assert client.get(f"/v1/sessions/{render_session_id(sid)}").get_json()["state"] == "failed"
    assert not app.store.has_transcript(sid)


def test_max_failures_zero_requeues_indefinitely(make_app):
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        # Fail the first two attempts, then succeed — never a terminal `failed`.
        return FakeTranscriber(error="flaky") if calls["n"] <= 2 else FakeTranscriber()

    app = make_app(factory=factory, max_failures=0)
    client = app.flask_app.test_client()
    sid = seed_session(app.store)
    job = client.post(f"/v1/sessions/{render_session_id(sid)}/jobs",
                      json={"kind": "transcribe"}).get_json()

    assert _poll(lambda: _job(client, job["id"])["state"] == "done", timeout=6.0), _job(client, job["id"])
    assert _job(client, job["id"])["attempts"] >= 2  # retried, never marked failed


# -- cancellation ----------------------------------------------------------- #


def test_cancel_running_job(make_app):
    block = threading.Event()
    app = make_app(factory=lambda: FakeTranscriber(block=block))
    client = app.flask_app.test_client()
    sid = seed_session(app.store)
    job = client.post(f"/v1/sessions/{render_session_id(sid)}/jobs",
                      json={"kind": "transcribe"}).get_json()
    assert _poll(lambda: client.get("/v1/status").get_json()["state"] == "processing")

    assert client.delete(f"/v1/jobs/{job['id']}").status_code == 204
    assert _poll(lambda: _job(client, job["id"])["state"] == "cancelled"), _job(client, job["id"])
    assert _poll(lambda: client.get("/v1/status").get_json()["state"] == "idle")
    assert not app.store.has_transcript(sid)


def test_cancel_queued_job(make_app):
    block = threading.Event()
    app = make_app(factory=lambda: FakeTranscriber(block=block))
    client = app.flask_app.test_client()
    busy = seed_session(app.store)
    waiting = seed_session(app.store)

    client.post(f"/v1/sessions/{render_session_id(busy)}/jobs", json={"kind": "transcribe"})
    assert _poll(lambda: client.get("/v1/status").get_json()["state"] == "processing")
    job_b = client.post(f"/v1/sessions/{render_session_id(waiting)}/jobs",
                        json={"kind": "transcribe"}).get_json()
    assert _job(client, job_b["id"])["state"] == "queued"

    assert client.delete(f"/v1/jobs/{job_b['id']}").status_code == 204
    assert _job(client, job_b["id"])["state"] == "cancelled"
    block.set()


# -- enqueue rules ---------------------------------------------------------- #


def test_enqueue_dedup_conflicts(make_app):
    block = threading.Event()
    app = make_app(factory=lambda: FakeTranscriber(block=block))
    client = app.flask_app.test_client()
    sid = seed_session(app.store)
    client.post(f"/v1/sessions/{render_session_id(sid)}/jobs", json={"kind": "transcribe"})
    assert _poll(lambda: client.get("/v1/status").get_json()["state"] == "processing")

    resp = client.post(f"/v1/sessions/{render_session_id(sid)}/jobs", json={"kind": "transcribe"})
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "job_exists"
    block.set()


def test_enqueue_before_finalize_conflicts(make_app):
    app = make_app()
    client = app.flask_app.test_client()
    # A session row with no m4a (allocated but never finalized).
    sid = app.store.db.insert_session(datetime.now().isoformat())
    app.store.session_dir(sid).mkdir(parents=True, exist_ok=True)
    resp = client.post(f"/v1/sessions/{render_session_id(sid)}/jobs", json={"kind": "transcribe"})
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "not_finalized"


def test_diarize_without_service_conflicts(make_app):
    app = make_app()
    client = app.flask_app.test_client()
    sid = seed_session(app.store)
    resp = client.post(f"/v1/sessions/{render_session_id(sid)}/jobs", json={"kind": "diarize"})
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "diarize_unavailable"
    bulk = client.post("/v1/jobs", json={"kind": "diarize", "target": "pending"})
    assert bulk.status_code == 409


def test_bulk_transcribe_pending(make_app):
    app = make_app()
    client = app.flask_app.test_client()
    ids = [seed_session(app.store) for _ in range(3)]

    resp = client.post("/v1/jobs", json={"kind": "transcribe", "target": "pending"})
    assert resp.status_code == 202
    jobs = resp.get_json()["jobs"]
    assert len(jobs) == 3
    # Oldest session first (enqueue order = job id order = session id order).
    assert [j["session_id"] for j in jobs] == [render_session_id(i) for i in ids]

    for sid in ids:
        assert _poll(lambda s=sid: app.store.has_transcript(s), timeout=6.0)


# -- crash resilience ------------------------------------------------------- #


def test_running_job_reset_and_rerun_on_boot(tmp_path, monkeypatch):
    for var in ("EARSHOT_HAL", "EARSHOT_CONFIG", "EARSHOT_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)

    # Seed a session with a job left `running` by a crash.
    from earshot.storage.db import Database
    from earshot.storage.store import Store

    db = Database(cfg.db_path)
    seed = Store(cfg, db)
    sid = seed_session(seed)
    job_id = db.insert_job(sid, "transcribe", datetime.now().isoformat())
    db.mark_job_running(job_id, "local", datetime.now().isoformat())
    db.close()

    app = build_application(config=cfg, hal_override="stub", realtime=True,
                            transcriber_factory=lambda: FakeTranscriber())
    app.start()
    try:
        client = app.flask_app.test_client()
        # Reset to queued on boot, then re-run to completion.
        assert _poll(lambda: _job(client, job_id)["state"] == "done"), _job(client, job_id)
        assert app.store.has_transcript(sid)
    finally:
        app.stop()
