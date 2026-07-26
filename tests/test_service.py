"""Milestone 7 — processing-service integration, diarization, speakers.

A fake service client (injected) drives the worker's service route, diarization,
and the /v1/service + speaker endpoints off-device; one test exercises the real
urllib client against a throwaway HTTP server so the transport is covered too.
"""

from __future__ import annotations

import json
import threading
import time
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from earshot.app import build_application
from earshot.config import Config
from earshot.jobs.service import ServiceClient, ServiceUnreachable
from earshot.jobs.transcribe import Cancelled, TranscribeError
from earshot.jobs.transcript import Segment
from earshot.recording.encode import encode_session, probe_duration
from earshot.storage.paths import chunk_name, m4a_name, render_session_id


# -- fakes ------------------------------------------------------------------ #


class FakeState:
    """Shared, test-mutable service behaviour (the manager keeps one client)."""

    def __init__(self, **kw):
        self.reachable = kw.get("reachable", True)
        self.caps = kw.get("caps", {"transcribe": True, "diarize": True})
        self.segments = kw.get("segments", [
            Segment(0.0, 5.0, "morning all", "SPEAKER_01"),
            Segment(5.0, 9.0, "analytics look fine", "SPEAKER_00"),
        ])
        self.reachable_gate: threading.Event | None = kw.get("reachable_gate")
        self.reachable_entered: threading.Event | None = kw.get("reachable_entered")
        self.poll_gate: threading.Event | None = kw.get("poll_gate")
        self.fail = kw.get("fail", False)
        self.submitted: list = []
        self.cancelled: list = []


class FakeServiceClient:
    def __init__(self, url, state: FakeState):
        self.url = url.rstrip("/")
        self.s = state

    def openapi(self):
        if not self.s.reachable:
            raise ServiceUnreachable("down")
        params = [{"name": "diarize"}] if self.s.caps.get("diarize") else []
        return {"paths": {"/asr": {"post": {"parameters": params}}}}

    def reachable(self):
        if self.s.reachable_entered is not None:
            self.s.reachable_entered.set()
        if self.s.reachable_gate is not None:
            self.s.reachable_gate.wait(timeout=2.0)
        return self.s.reachable

    def capabilities(self):
        return dict(self.s.caps) if self.s.reachable else None

    def process(self, m4a_path, kind, *, num_speakers=None):
        if not self.s.reachable:
            raise ServiceUnreachable("down")
        self.s.submitted.append((str(m4a_path), kind, num_speakers))
        if self.s.fail:
            from earshot.jobs.service import ServiceJobFailed
            raise ServiceJobFailed("svc boom")
        if self.s.poll_gate is not None:
            while not self.s.poll_gate.wait(0.02):
                pass
        if not self.s.reachable:
            from earshot.jobs.service import ServiceJobFailed
            raise ServiceJobFailed("down")
        return list(self.s.segments)

    def cancel(self, remote=None):
        self.s.cancelled.append(remote)


class FakeLocalTranscriber:
    def __init__(self, segments):
        self._segments = segments
        self._cancel = threading.Event()

    def run(self, m4a_path):
        if self._cancel.is_set():
            raise Cancelled()
        return self._segments

    def cancel(self):
        self._cancel.set()


# -- helpers ---------------------------------------------------------------- #


def _seed(store, *, seconds: float = 10.0, name: str | None = None) -> int:
    sid = store.db.insert_session(datetime.now().isoformat(), name=name)
    d = store.session_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    chunk = d / chunk_name(1)
    with wave.open(str(chunk), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
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

    def _make(*, state=None, url="http://svc:9000", request_timeout=0,
              transcriber_factory=None, max_failures=3):
        cfg = Config()
        cfg.storage.data_dir = str(tmp_path)
        cfg.processing.service_url = url
        cfg.processing.request_timeout_seconds = request_timeout
        cfg.processing.max_failures = max_failures
        factory = (lambda u, **_: FakeServiceClient(u, state)) if state is not None else None
        app = build_application(
            config=cfg, hal_override="stub", realtime=True,
            service_client_factory=factory, transcriber_factory=transcriber_factory,
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
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return fn()


def _sid(x):
    return render_session_id(x)


# -- /v1/service ------------------------------------------------------------ #


def test_service_config_endpoints(make_app):
    state = FakeState(caps={"transcribe": True, "diarize": True})
    app = make_app(state=state, url="")  # start unconfigured
    client = app.flask_app.test_client()

    assert client.get("/v1/service").get_json() == {
        "configured": False, "url": None, "reachable": False, "capabilities": None}

    put = client.put("/v1/service", json={"url": "http://homelab.local:9000"}).get_json()
    assert put["configured"] and put["reachable"] and put["capabilities"]["diarize"] is True
    # Persisted to config.toml so it survives a restart.
    assert 'service_url = "http://homelab.local:9000"' in (Path(app.config.data_dir) / "config.toml").read_text()

    assert client.delete("/v1/service").status_code == 204
    assert client.get("/v1/service").get_json()["configured"] is False


# -- diarization via the service ------------------------------------------- #


def test_diarize_via_service_writes_speakers(make_app):
    state = FakeState()
    app = make_app(state=state)
    client = app.flask_app.test_client()
    sid = _seed(app.store, name="Weekly sync")

    job = client.post(f"/v1/sessions/{_sid(sid)}/jobs", json={"kind": "diarize", "num_speakers": 2}).get_json()
    assert _poll(lambda: client.get(f"/v1/jobs/{job['id']}").get_json()["state"] == "done"), \
        client.get(f"/v1/jobs/{job['id']}").get_json()
    assert client.get(f"/v1/jobs/{job['id']}").get_json()["route"] == "service"
    assert len(state.submitted) == 1  # submitted once, not resubmitted
    assert state.submitted[0][2] == 2

    sess = client.get(f"/v1/sessions/{_sid(sid)}").get_json()
    assert sess["state"] == "diarized" and sess["diarized"] is True

    md = client.get(f"/v1/sessions/{_sid(sid)}/transcript",
                    headers={"Accept": "text/markdown"}).get_data(as_text=True)
    assert "Speaker 1: morning all" in md and "Speaker 2: analytics look fine" in md

    speakers = client.get(f"/v1/sessions/{_sid(sid)}/speakers").get_json()["speakers"]
    assert [s["label"] for s in speakers] == ["Speaker 1", "Speaker 2"]
    assert all(s["segments"] == 1 for s in speakers)


def test_cancel_during_service_reachability_check_is_not_resurrected(make_app):
    entered = threading.Event()
    release = threading.Event()
    state = FakeState(reachable=False, reachable_entered=entered, reachable_gate=release)
    app = make_app(state=state)
    client = app.flask_app.test_client()
    sid = _seed(app.store)

    job_id = app.db.insert_job(sid, "diarize", datetime.now().isoformat())
    app.worker.wake()
    assert entered.wait(timeout=3.0)
    assert client.delete(f"/v1/jobs/{job_id}").status_code == 204
    release.set()
    assert _poll(lambda: client.get(f"/v1/jobs/{job_id}").get_json()["state"] == "cancelled"), \
        client.get(f"/v1/jobs/{job_id}").get_json()
    time.sleep(0.2)
    assert client.get(f"/v1/jobs/{job_id}").get_json()["state"] == "cancelled"


def test_bulk_diarize_targets_all_undiarized_sessions(make_app):
    state = FakeState()
    app = make_app(state=state)
    client = app.flask_app.test_client()
    audio_only = _seed(app.store)
    plain = _seed(app.store)
    done = _seed(app.store)
    app.store.write_transcript_result(plain, [Segment(0.0, 1.0, "plain")])
    app.store.write_transcript_result(done, [Segment(0.0, 1.0, "labelled", "Speaker 1")], diarized=True)

    resp = client.post("/v1/jobs", json={"kind": "diarize", "target": "undiarized"})
    assert resp.status_code == 202
    jobs = resp.get_json()["jobs"]
    assert [j["session_id"] for j in jobs] == [_sid(audio_only), _sid(plain)]
    assert all(j["kind"] == "diarize" for j in jobs)


def test_transcribe_routes_to_reachable_service(make_app):
    state = FakeState(caps={"transcribe": True, "diarize": False},
                      segments=[Segment(0.0, 2.0, "just words")])
    app = make_app(state=state)
    client = app.flask_app.test_client()
    sid = _seed(app.store)

    job = client.post(f"/v1/sessions/{_sid(sid)}/jobs", json={"kind": "transcribe"}).get_json()
    assert _poll(lambda: client.get(f"/v1/jobs/{job['id']}").get_json()["state"] == "done")
    assert client.get(f"/v1/jobs/{job['id']}").get_json()["route"] == "service"
    assert client.get(f"/v1/sessions/{_sid(sid)}").get_json()["state"] == "transcribed"


def test_service_job_keeps_device_idle(make_app):
    gate = threading.Event()
    state = FakeState(poll_gate=gate)
    app = make_app(state=state)
    client = app.flask_app.test_client()
    sid = _seed(app.store)
    client.post(f"/v1/sessions/{_sid(sid)}/jobs", json={"kind": "diarize"})

    # A service job runs on another machine: device stays idle (LED green),
    # while status.processing surfaces the remote work.
    assert _poll(lambda: client.get("/v1/status").get_json()["processing"] is not None)
    status = client.get("/v1/status").get_json()
    assert status["state"] == "idle"
    assert status["led"]["rgb"] == [0, 255, 0]
    assert status["processing"]["route"] == "service"
    assert "stage" not in status["processing"] and "progress" not in status["processing"]

    gate.set()
    assert _poll(lambda: client.get("/v1/status").get_json()["processing"] is None)


def test_service_drop_during_request_is_a_failure(make_app):
    # Enqueue while reachable (diarize needs the capability), then drop the service
    # mid-request: the synchronous attempt fails, while audio remains on device.
    gate = threading.Event()
    state = FakeState(poll_gate=gate)
    app = make_app(state=state, max_failures=1)
    client = app.flask_app.test_client()
    sid = _seed(app.store)
    job = client.post(f"/v1/sessions/{_sid(sid)}/jobs", json={"kind": "diarize"}).get_json()
    assert _poll(lambda: client.get("/v1/status").get_json()["processing"] is not None)

    state.reachable = False  # LAN outage while the synchronous request is in flight
    gate.set()
    assert _poll(lambda: client.get(f"/v1/jobs/{job['id']}").get_json()["state"] == "failed")
    row = client.get(f"/v1/jobs/{job['id']}").get_json()
    assert row["attempts"] == 1 and "down" in row["last_error"]
    assert app.store.m4a_path(sid).exists()


def test_service_job_failure_is_terminal(make_app):
    state = FakeState(fail=True)
    app = make_app(state=state, max_failures=1)
    client = app.flask_app.test_client()
    sid = _seed(app.store)
    job = client.post(f"/v1/sessions/{_sid(sid)}/jobs", json={"kind": "diarize"}).get_json()

    assert _poll(lambda: client.get(f"/v1/jobs/{job['id']}").get_json()["state"] == "failed")
    assert "svc boom" in client.get(f"/v1/jobs/{job['id']}").get_json()["last_error"]


def test_diarize_blocked_without_capability(make_app):
    state = FakeState(caps={"transcribe": True, "diarize": False})
    app = make_app(state=state)
    client = app.flask_app.test_client()
    sid = _seed(app.store)
    resp = client.post(f"/v1/sessions/{_sid(sid)}/jobs", json={"kind": "diarize"})
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "diarize_unavailable"


# -- speakers --------------------------------------------------------------- #


def test_name_speaker_substitutes_and_reverts(make_app):
    state = FakeState()
    app = make_app(state=state)
    client = app.flask_app.test_client()
    sid = _seed(app.store)
    job = client.post(f"/v1/sessions/{_sid(sid)}/jobs", json={"kind": "diarize"}).get_json()
    assert _poll(lambda: client.get(f"/v1/jobs/{job['id']}").get_json()["state"] == "done")

    put = client.put(f"/v1/sessions/{_sid(sid)}/speakers/Speaker%201", json={"name": "Ritchie"})
    assert put.status_code == 200
    assert {s["label"]: s["name"] for s in put.get_json()["speakers"]}["Speaker 1"] == "Ritchie"
    md = client.get(f"/v1/sessions/{_sid(sid)}/transcript",
                    headers={"Accept": "text/markdown"}).get_data(as_text=True)
    assert "Ritchie: morning all" in md and "Speaker 1:" not in md

    # Clearing the name reverts the label.
    client.put(f"/v1/sessions/{_sid(sid)}/speakers/Speaker%201", json={"name": None})
    md2 = client.get(f"/v1/sessions/{_sid(sid)}/transcript",
                     headers={"Accept": "text/markdown"}).get_data(as_text=True)
    assert "Speaker 1: morning all" in md2

    assert client.put(f"/v1/sessions/{_sid(sid)}/speakers/Nobody", json={"name": "x"}).status_code == 404


def test_speaker_sample_returns_audio(make_app):
    state = FakeState()
    app = make_app(state=state)
    client = app.flask_app.test_client()
    sid = _seed(app.store)
    job = client.post(f"/v1/sessions/{_sid(sid)}/jobs", json={"kind": "diarize"}).get_json()
    assert _poll(lambda: client.get(f"/v1/jobs/{job['id']}").get_json()["state"] == "done")

    resp = client.get(f"/v1/sessions/{_sid(sid)}/speakers/Speaker%201/sample")
    assert resp.status_code == 200 and resp.mimetype == "audio/mp4"
    assert len(resp.get_data()) > 0
    assert client.get(f"/v1/sessions/{_sid(sid)}/speakers/Nobody/sample").status_code == 404


def test_local_retranscribe_reverts_diarization(make_app):
    state = FakeState()
    app = make_app(state=state,
                   transcriber_factory=lambda: FakeLocalTranscriber([Segment(0.0, 2.0, "plain text")]))
    client = app.flask_app.test_client()
    sid = _seed(app.store)
    dj = client.post(f"/v1/sessions/{_sid(sid)}/jobs", json={"kind": "diarize"}).get_json()
    assert _poll(lambda: client.get(f"/v1/jobs/{dj['id']}").get_json()["state"] == "done")
    assert client.get(f"/v1/sessions/{_sid(sid)}").get_json()["diarized"] is True

    # Service is gone; a local re-transcribe removes the speaker labels.
    state.reachable = False
    tj = client.post(f"/v1/sessions/{_sid(sid)}/jobs", json={"kind": "transcribe"}).get_json()
    assert _poll(lambda: client.get(f"/v1/jobs/{tj['id']}").get_json()["state"] == "done")
    assert client.get(f"/v1/jobs/{tj['id']}").get_json()["route"] == "local"

    sess = client.get(f"/v1/sessions/{_sid(sid)}").get_json()
    assert sess["state"] == "transcribed" and sess["diarized"] is False
    assert client.get(f"/v1/sessions/{_sid(sid)}/speakers").get_json()["speakers"] == []


# -- crash resume ----------------------------------------------------------- #


def test_running_service_job_reruns_after_crash(tmp_path, monkeypatch):
    for var in ("EARSHOT_HAL", "EARSHOT_CONFIG", "EARSHOT_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)
    cfg.processing.service_url = "http://svc:9000"
    cfg.processing.request_timeout_seconds = 0

    from earshot.storage.db import Database
    from earshot.storage.store import Store

    db = Database(cfg.db_path)
    seed = Store(cfg, db)
    sid = _seed(seed)
    job_id = db.insert_job(sid, "diarize", datetime.now().isoformat())
    db.mark_job_running(job_id, "service", datetime.now().isoformat())
    db.close()

    state = FakeState()
    app = build_application(
        config=cfg, hal_override="stub", realtime=True,
        service_client_factory=lambda u, **_: FakeServiceClient(u, state),
    )
    app.start()
    try:
        client = app.flask_app.test_client()
        assert _poll(lambda: client.get(f"/v1/jobs/{job_id}").get_json()["state"] == "done")
        assert len(state.submitted) == 1  # re-run against stateless synchronous service
        assert app.store.is_diarized(sid)
    finally:
        app.stop()


# -- real HTTP transport ---------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/openapi.json":
            self._send(200, {"paths": {"/asr": {"post": {"parameters": [{"name": "diarize"}]}}}})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        assert self.path.startswith("/asr?") and "diarize=true" in self.path
        assert "min_speakers=2" in self.path and "max_speakers=2" in self.path
        assert ctype.startswith("multipart/form-data"), ctype
        assert b'name="audio_file"' in body
        self._send(200, {"segments": [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_01"}]})


def test_real_service_client_over_http(tmp_path):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        client = ServiceClient(url)
        assert client.reachable()
        assert client.capabilities() == {"transcribe": True, "diarize": True}

        m4a = tmp_path / "x.m4a"
        m4a.write_bytes(b"\x00\x01\x02\x03")
        segs = client.process(m4a, "diarize", num_speakers=2)
        assert segs[0].speaker == "Speaker 1" and segs[0].text == "hi"
    finally:
        server.shutdown()
