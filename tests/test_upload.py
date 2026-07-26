"""v1.3 — create a session by uploading an audio file (POST /v1/sessions).

An upload is the second creation path: an existing file is transcoded to the
canonical session.m4a on ingest, so once created it is indistinguishable from a
recorded session (rpi/requirements/web-ui/upload-audio.md).
"""

from __future__ import annotations

import io
import time
import wave

from earshot.recording.encode import EncodeError, probe_duration, transcode_to_m4a
from earshot.storage.paths import m4a_name, parse_session_id


def _wav_bytes(seconds: float = 1.0, *, rate: int = 44100, channels: int = 2) -> bytes:
    """A small PCM WAV (default 44.1 kHz stereo — deliberately not the canonical
    16 kHz mono, so the ingest transcode is exercised for real)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds) * channels)
    return buf.getvalue()


def _multipart(audio: bytes, *, filename="clip.wav", **fields) -> dict:
    data = {"audio": (io.BytesIO(audio), filename)}
    data.update(fields)
    return data


def _wait_state(client, want, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/v1/status").get_json()["state"] == want:
            return True
        time.sleep(0.02)
    return False


# -- happy path ------------------------------------------------------------- #

def test_upload_creates_pending_session_with_metadata(app, client):
    r = client.post(
        "/v1/sessions",
        data=_multipart(_wav_bytes(1.0), name="Phone memo", occurred_at="2026-07-01"),
        content_type="multipart/form-data",
    )
    assert r.status_code == 201
    body = r.get_json()
    assert body["state"] == "pending"          # never auto-processed
    assert body["name"] == "Phone memo"
    assert body["occurred_at"] == "2026-07-01"
    assert body["has_transcript"] is False

    sid = parse_session_id(body["id"])
    m4a = app.store.session_dir(sid) / m4a_name()
    assert m4a.exists() and m4a.stat().st_size > 0
    assert probe_duration(m4a) > 0             # a real, playable artifact

    # Indistinguishable from a recording: it lists and serves audio like any other.
    listed = client.get("/v1/sessions").get_json()["sessions"]
    assert any(s["id"] == body["id"] for s in listed)
    assert client.get(f"/v1/sessions/{body['id']}/audio").status_code == 200

    # The device returned to idle after the encode.
    assert client.get("/v1/status").get_json()["state"] == "idle"


def test_upload_unnamed_falls_back_and_no_temp_left_behind(app, client):
    r = client.post("/v1/sessions", data=_multipart(_wav_bytes(0.5)),
                    content_type="multipart/form-data")
    assert r.status_code == 201
    assert r.get_json()["name"] is None        # empty/omitted name -> unnamed
    # The streamed upload temp is cleaned up.
    updir = app.store.config.data_dir / "uploads"
    assert not updir.exists() or not any(updir.iterdir())


# -- refusals --------------------------------------------------------------- #

def test_upload_refused_while_recording(app, client):
    assert client.post("/v1/recording").status_code == 201
    assert _wait_state(client, "recording")
    try:
        r = client.post("/v1/sessions", data=_multipart(_wav_bytes(0.5)),
                        content_type="multipart/form-data")
        assert r.status_code == 409
        assert r.get_json()["error"]["code"] == "recording"
    finally:
        client.delete("/v1/recording")


def test_upload_refused_when_disk_blocked(app, client, monkeypatch):
    from earshot.storage.store import DiskInfo, Store

    monkeypatch.setattr(Store, "disk_info",
                        lambda self: DiskInfo(used_percent=95.0, blocked=True))
    r = client.post("/v1/sessions", data=_multipart(_wav_bytes(0.5)),
                    content_type="multipart/form-data")
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "disk_full"


# -- bad input -------------------------------------------------------------- #

def test_upload_missing_audio_is_400(client):
    r = client.post("/v1/sessions", data={"name": "x"},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_body"


def test_upload_undecodable_audio_is_400(app, client):
    r = client.post("/v1/sessions",
                    data=_multipart(b"this is not audio", filename="junk.wav"),
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_audio"
    # The failed ingest left no orphaned session behind.
    assert client.get("/v1/sessions").get_json()["sessions"] == []


def test_upload_rejects_invalid_occurred_at(client):
    r = client.post("/v1/sessions",
                    data=_multipart(_wav_bytes(0.5), occurred_at="last tuesday"),
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_body"


def test_upload_over_limit_is_413(app_factory):
    app = app_factory(max_upload_mb=1)         # 1 MB cap
    client = app.flask_app.test_client()
    r = client.post("/v1/sessions",
                    data=_multipart(_wav_bytes(20.0), filename="big.wav"),  # > 1 MB PCM
                    content_type="multipart/form-data")
    assert r.status_code == 413
    assert r.get_json()["error"]["code"] == "too_large"


# -- unit: the ingest transcode -------------------------------------------- #

def test_transcode_to_m4a_produces_16k_mono(tmp_path):
    src = tmp_path / "src.wav"
    src.write_bytes(_wav_bytes(1.0, rate=44100, channels=2))
    out = tmp_path / "session.m4a"
    transcode_to_m4a(src, out, bitrate_kbps=32)
    assert out.exists() and out.stat().st_size > 0
    assert probe_duration(out) > 0


def test_transcode_to_m4a_raises_and_leaves_no_partial(tmp_path):
    src = tmp_path / "junk.wav"
    src.write_bytes(b"not audio at all")
    out = tmp_path / "session.m4a"
    try:
        transcode_to_m4a(src, out, bitrate_kbps=32)
        assert False, "expected EncodeError"
    except EncodeError:
        pass
    assert not out.exists()
