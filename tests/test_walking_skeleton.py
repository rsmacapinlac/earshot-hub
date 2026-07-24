"""Milestone 3 — the end-to-end thin slice on the stub HAL.

boots -> API up -> POST /v1/recording -> chunked WAV + session row ->
GET /v1/sessions -> DELETE /v1/recording finalizes to session.m4a ->
/v1/status reflects each transition. Both the web control and the hardware
button are exercised. No transcription yet.
"""

from __future__ import annotations

import time

from earshot.api import validation
from earshot.storage.paths import parse_session_id


def _wait_state(client, target, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get("/v1/status").get_json()["state"]
        if state == target:
            return state
        time.sleep(0.02)
    return client.get("/v1/status").get_json()["state"]


def test_boots_to_idle(client):
    resp = client.get("/v1/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["state"] == "idle"
    assert body["led"] == {"rgb": [0, 255, 0], "pattern": "solid"}
    assert body["recording"] is None
    assert validation.is_valid(body, "Status")


def test_empty_session_list(client):
    body = client.get("/v1/sessions").get_json()
    assert body == {"sessions": []}


def test_record_via_api_end_to_end(app, client):
    # Start
    resp = client.post("/v1/recording")
    assert resp.status_code == 201
    detail = resp.get_json()
    assert validation.is_valid(detail, "SessionDetail")
    session_id_str = detail["id"]
    session_id = parse_session_id(session_id_str)
    assert session_id is not None
    assert detail["state"] == "recording"

    # A DB row and a session directory exist immediately.
    assert app.store.db.get_session(session_id) is not None
    assert app.store.session_dir(session_id).is_dir()

    # Status reflects recording, with a rendered id + elapsed.
    assert _wait_state(client, "recording") == "recording"
    status = client.get("/v1/status").get_json()
    assert status["recording"]["session_id"] == session_id_str
    assert status["led"]["rgb"] == [255, 0, 0]

    # It appears in the list as recording.
    listing = client.get("/v1/sessions").get_json()
    assert any(s["id"] == session_id_str and s["state"] == "recording"
               for s in listing["sessions"])

    # Let some audio accumulate, then a chunk WAV should be on disk.
    time.sleep(0.3)
    chunks = list(app.store.session_dir(session_id).glob("recording-*.wav"))
    assert chunks, "expected at least one chunk WAV during recording"

    # Stop -> finalize
    resp = client.delete("/v1/recording")
    assert resp.status_code == 200
    finalized = resp.get_json()
    assert validation.is_valid(finalized, "StopRecordingResult")
    assert finalized["state"] == "pending"
    assert finalized["duration"] and finalized["duration"] > 0
    assert finalized["size"] and finalized["size"] > 0

    # session.m4a exists; the transient chunks are gone.
    m4a = app.store.m4a_path(session_id)
    assert m4a.exists()
    assert not list(app.store.session_dir(session_id).glob("recording-*.wav"))

    # Back to idle.
    assert _wait_state(client, "idle") == "idle"


def test_double_start_conflicts(client):
    assert client.post("/v1/recording").status_code == 201
    _wait_state(client, "recording")
    resp = client.post("/v1/recording")
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "already_recording"
    client.delete("/v1/recording")


def test_stop_when_idle_conflicts(client):
    resp = client.delete("/v1/recording")
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "not_recording"


def test_too_short_recording_is_discarded(app_factory):
    app = app_factory(min_duration=3)  # default minimum
    client = app.flask_app.test_client()
    resp = client.post("/v1/recording")
    assert resp.status_code == 201
    session_id = parse_session_id(resp.get_json()["id"])

    time.sleep(0.1)  # well under 3s of audio
    resp = client.delete("/v1/recording")
    assert resp.status_code == 200
    assert resp.get_json() == {"discarded": True, "reason": "too_short"}

    # The session and its directory are gone.
    assert app.store.db.get_session(session_id) is None
    assert not app.store.session_dir(session_id).exists()
    assert client.get("/v1/sessions").get_json()["sessions"] == []


def test_button_press_drives_recording(app):
    button = app.hal.button  # StubButton
    client = app.flask_app.test_client()

    button.press()  # start
    assert _wait_state(client, "recording") == "recording"
    time.sleep(0.2)
    button.press()  # stop
    assert _wait_state(client, "idle") == "idle"

    sessions = client.get("/v1/sessions").get_json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["state"] == "pending"


def test_disk_threshold_blocks_recording(app_factory, monkeypatch):
    from earshot.storage.store import DiskInfo, Store

    app = app_factory()
    monkeypatch.setattr(Store, "disk_info", lambda self: DiskInfo(used_percent=95.0, blocked=True))
    client = app.flask_app.test_client()

    assert _wait_state(client, "disk_full") == "disk_full"
    status = client.get("/v1/status").get_json()
    assert status["led"]["rgb"] == [255, 128, 0]

    resp = client.post("/v1/recording")
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "disk_full"
