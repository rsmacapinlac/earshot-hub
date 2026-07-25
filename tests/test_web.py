"""Milestone 8 — web endpoints the UI binds to (audio/Range, rename, delete, static)."""

from __future__ import annotations

import wave
from datetime import datetime

import pytest

from earshot.jobs.transcript import Segment
from earshot.recording.encode import encode_session, probe_duration
from earshot.storage.paths import chunk_name, m4a_name, render_session_id


def _seed(store, *, seconds=1.0, name=None):
    sid = store.db.insert_session(datetime.now().isoformat(), name=name)
    d = store.session_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    chunk = d / chunk_name(1)
    with wave.open(str(chunk), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    encode_session([chunk], d / m4a_name(), bitrate_kbps=32)
    chunk.unlink()
    m4a = d / m4a_name()
    store.finalize_session(sid, probe_duration(m4a), m4a.stat().st_size)
    return sid


def test_index_and_app_js_served(client):
    idx = client.get("/")
    assert idx.status_code == 200
    body = idx.get_data(as_text=True)
    assert "<title>earshot hub</title>" in body and "/app.js" in body
    js = client.get("/app.js")
    assert js.status_code == 200 and "text/javascript" in js.headers["Content-Type"]
    assert "EventSource" in js.get_data(as_text=True)


def test_audio_full_and_range(app, client):
    sid = _seed(app.store)
    r = client.get(f"/v1/sessions/{render_session_id(sid)}/audio")
    assert r.status_code == 200 and r.mimetype == "audio/mp4"
    assert r.headers.get("Accept-Ranges") == "bytes"
    full = r.get_data()
    assert len(full) > 0

    ranged = client.get(f"/v1/sessions/{render_session_id(sid)}/audio", headers={"Range": "bytes=0-10"})
    assert ranged.status_code == 206
    assert ranged.headers["Content-Range"].startswith("bytes 0-10/")
    assert len(ranged.get_data()) == 11


def test_audio_download_disposition(app, client):
    sid = _seed(app.store, name="Weekly sync")
    r = client.get(f"/v1/sessions/{render_session_id(sid)}/audio?download")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("Content-Disposition", "")
    assert "Weekly sync.m4a" in r.headers.get("Content-Disposition", "")


def test_audio_404_before_finalize(app, client):
    sid = app.store.db.insert_session(datetime.now().isoformat())
    app.store.session_dir(sid).mkdir(parents=True, exist_ok=True)
    assert client.get(f"/v1/sessions/{render_session_id(sid)}/audio").status_code == 404


def test_rename_updates_session_and_transcript_header(app, client):
    sid = _seed(app.store)
    app.store.write_transcript_result(sid, [Segment(0.0, 1.0, "hi there")])

    r = client.patch(f"/v1/sessions/{render_session_id(sid)}", json={"name": "Renamed"})
    assert r.status_code == 200 and r.get_json()["name"] == "Renamed"
    # The transcript.md header is rewritten in place.
    md = client.get(f"/v1/sessions/{render_session_id(sid)}/transcript",
                    headers={"Accept": "text/markdown"}).get_data(as_text=True)
    assert md.startswith("# Renamed")

    cleared = client.patch(f"/v1/sessions/{render_session_id(sid)}", json={"name": None})
    assert cleared.get_json()["name"] is None


def test_delete_session(app, client):
    sid = _seed(app.store)
    assert client.delete(f"/v1/sessions/{render_session_id(sid)}").status_code == 204
    assert client.get(f"/v1/sessions/{render_session_id(sid)}").status_code == 404
    assert not app.store.session_dir(sid).exists()


def test_delete_while_recording_conflicts(app, client):
    import time
    assert client.post("/v1/recording").status_code == 201
    deadline = time.time() + 3
    while time.time() < deadline and client.get("/v1/status").get_json()["state"] != "recording":
        time.sleep(0.02)
    sid = client.get("/v1/status").get_json()["recording"]["session_id"]
    r = client.delete(f"/v1/sessions/{sid}")
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "recording"
    client.delete("/v1/recording")
