"""The Flask app serving the /v1 API (rpi/specs/api.md).

Request bodies and responses are validated against the OpenAPI component schemas
(:mod:`earshot.api.validation`), so the wire format cannot drift from the contract.
Skeleton scope (M3): status, events, sessions (list/detail), recording control,
and an (empty) jobs list. The remaining endpoints land in later milestones.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory

from earshot.api.errors import ApiError
from earshot.api import validation
from earshot.jobs.serialize import job_api
from earshot.storage.paths import parse_session_id, render_session_id

log = logging.getLogger("earshot.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(controller, store, config, worker=None, service=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.url_map.strict_slashes = False

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def respond(obj: Any, schema_name: str, status: int = 200) -> Response:
        # Validate the response against the contract; drift becomes a 500 that
        # tests catch rather than a silently malformed body.
        errors = validation.error_messages(obj, schema_name)
        if errors:
            log.error("response for %s violates schema: %s", schema_name, errors)
            raise ApiError(500, "internal", f"response violates {schema_name}")
        return app.response_class(
            json.dumps(obj), status=status, mimetype="application/json"
        )

    def request_json(schema_name: str) -> dict:
        if not request.is_json:
            raise ApiError(400, "invalid_body", "expected a JSON body")
        try:
            body = request.get_json()
        except Exception:
            raise ApiError(400, "invalid_body", "malformed JSON")
        errors = validation.error_messages(body, schema_name)
        if errors:
            raise ApiError(400, "invalid_body", "; ".join(errors))
        return body

    def session_row_or_404(id_str: str):
        session_id = parse_session_id(id_str)
        if session_id is None:
            raise ApiError(404, "not_found", f"no session {id_str}")
        row = store.db.get_session(session_id)
        if row is None or row["missing"]:
            raise ApiError(404, "not_found", f"no session {id_str}")
        return session_id, row

    def diarize_available() -> bool:
        # Diarization needs a processing service that reports the capability.
        return service is not None and service.diarize_available()

    def enqueue(session_id: int, kind: str, num_speakers: int | None = None) -> dict:
        if kind == "diarize" and not diarize_available():
            raise ApiError(409, "diarize_unavailable",
                           "no processing service provides diarization")
        job_id = store.db.insert_job(session_id, kind, datetime.now().isoformat(), num_speakers)
        if worker is not None:
            worker.wake()
        return job_api(store.db.get_job(job_id))

    # ------------------------------------------------------------------ #
    # Error handling
    # ------------------------------------------------------------------ #

    @app.errorhandler(ApiError)
    def _handle_api_error(err: ApiError):
        return jsonify(err.to_dict()), err.status

    # ------------------------------------------------------------------ #
    # Device status
    # ------------------------------------------------------------------ #

    @app.get("/v1/status")
    def get_status():
        return respond(controller.status(), "Status")

    @app.get("/v1/events")
    def get_events():
        def _fingerprint(obj) -> str:
            return json.dumps(obj, sort_keys=True)

        def stream():
            # On connect: one state event with the current status.
            snapshot = controller.status()
            yield _sse("state", snapshot)
            last_state = _fingerprint(snapshot)
            last_sessions = _fingerprint(store.list_sessions_api(controller.active_session_id))
            last_jobs = _fingerprint([job_api(r) for r in store.db.list_jobs()])
            while True:
                time.sleep(1.0)
                snapshot = controller.status()
                cur_state = _fingerprint(snapshot)
                if cur_state != last_state:
                    yield _sse("state", snapshot)
                    last_state = cur_state
                # Change hints: the client refetches the collection (rpi/specs/api.md).
                cur_sessions = _fingerprint(store.list_sessions_api(controller.active_session_id))
                if cur_sessions != last_sessions:
                    yield _sse("sessions-changed", {})
                    last_sessions = cur_sessions
                cur_jobs = _fingerprint([job_api(r) for r in store.db.list_jobs()])
                if cur_jobs != last_jobs:
                    yield _sse("jobs-changed", {})
                    last_jobs = cur_jobs
                yield ": keep-alive\n\n"

        return Response(stream(), mimetype="text/event-stream")

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #

    @app.get("/v1/sessions")
    def list_sessions():
        return respond(store.list_sessions_api(controller.active_session_id), "SessionList")

    @app.get("/v1/sessions/<id_str>")
    def get_session(id_str: str):
        _, row = session_row_or_404(id_str)
        return respond(
            store.session_detail_api(row, controller.active_session_id), "SessionDetail"
        )

    @app.patch("/v1/sessions/<id_str>")
    def patch_session(id_str: str):
        session_id, _ = session_row_or_404(id_str)
        body = request_json("SessionPatch")
        fields: dict[str, Any] = {}
        if "name" in body:
            fields["name"] = body["name"]
        if "occurred_at" in body:
            try:
                fields["occurred_at"] = store.normalize_occurred_at(body["occurred_at"])
            except ValueError as exc:
                raise ApiError(400, "invalid_body", str(exc))
        store.set_session_fields(session_id, **fields)  # rewrites transcript.md header if present
        updated = store.db.get_session(session_id)
        return respond(store.session_detail_api(updated, controller.active_session_id), "SessionDetail")

    @app.delete("/v1/sessions/<id_str>")
    def delete_session(id_str: str):
        session_id, _ = session_row_or_404(id_str)
        if controller.active_session_id == session_id:
            raise ApiError(409, "recording", "cannot delete a session while it is recording")
        active = store.db.active_job_for_session(session_id)
        if active is not None and worker is not None:
            worker.cancel_running(int(active["id"]))  # in-flight job discarded
        store.delete_session(session_id)
        return ("", 204)

    @app.get("/v1/sessions/<id_str>/audio")
    def get_audio(id_str: str):
        session_id, row = session_row_or_404(id_str)
        m4a = store.m4a_path(session_id)
        if not m4a.exists():
            raise ApiError(404, "not_finalized", "session has no audio yet")
        download = "download" in request.args
        name = row["name"] or render_session_id(session_id)
        # conditional=True → ETag + Range support (206 on a ranged request), for seeking.
        return send_from_directory(
            m4a.parent, m4a.name, mimetype="audio/mp4", conditional=True,
            as_attachment=download, download_name=f"{name}.m4a",
        )

    @app.get("/v1/sessions/<id_str>/transcript")
    def get_transcript(id_str: str):
        session_id, _ = session_row_or_404(id_str)
        if not store.has_transcript(session_id):
            raise ApiError(404, "no_transcript", "session has no transcript yet")
        # Content-negotiated: JSON segments (for the UI) or rendered markdown (export).
        if "application/json" in request.headers.get("Accept", ""):
            segments = [s.api() for s in store.read_current_segments(session_id)]
            return app.response_class(json.dumps(segments), mimetype="application/json")
        md = store.transcript_markdown(session_id) or ""
        return app.response_class(md, mimetype="text/markdown")

    # ------------------------------------------------------------------ #
    # Speakers (diarized sessions — rpi/requirements/web-ui/name-speakers.md)
    # ------------------------------------------------------------------ #

    @app.get("/v1/sessions/<id_str>/speakers")
    def list_speakers(id_str: str):
        session_id, _ = session_row_or_404(id_str)
        return respond(store.speakers_api(session_id), "SpeakerList")

    @app.put("/v1/sessions/<id_str>/speakers/<label>")
    def name_speaker(id_str: str, label: str):
        session_id, _ = session_row_or_404(id_str)
        if not _has_speaker(session_id, label):
            raise ApiError(404, "not_found", f"no speaker {label!r} in this session")
        body = request_json("NameUpdate")
        store.assign_speaker(session_id, label, body["name"])
        return respond(store.speakers_api(session_id), "SpeakerList")

    @app.get("/v1/sessions/<id_str>/speakers/<label>/sample")
    def speaker_sample(id_str: str, label: str):
        session_id, _ = session_row_or_404(id_str)
        try:
            audio = store.speaker_sample(session_id, label)
        except KeyError:
            raise ApiError(404, "not_found", f"no speaker {label!r} in this session")
        return app.response_class(audio, mimetype="audio/mp4")

    def _has_speaker(session_id: int, label: str) -> bool:
        return any(s["label"] == label for s in store.db.get_speakers(session_id))

    # ------------------------------------------------------------------ #
    # Recording control
    # ------------------------------------------------------------------ #

    @app.post("/v1/recording")
    def start_recording():
        detail = controller.start_recording()
        return respond(detail, "SessionDetail", status=201)

    @app.delete("/v1/recording")
    def stop_recording():
        result = controller.stop_recording()
        return respond(result, "StopRecordingResult")

    # ------------------------------------------------------------------ #
    # Jobs (rpi/specs/processing.md#the-queue)
    # ------------------------------------------------------------------ #

    @app.get("/v1/jobs")
    def list_jobs():
        jobs = [job_api(r) for r in store.db.list_jobs()]
        return respond({"jobs": jobs}, "JobList")

    @app.post("/v1/sessions/<id_str>/jobs")
    def enqueue_job(id_str: str):
        session_id, _ = session_row_or_404(id_str)
        body = request_json("JobCreate")
        if body.get("num_speakers") is not None and body["kind"] != "diarize":
            raise ApiError(400, "invalid_body", "num_speakers is only valid for diarize jobs")
        if not store.m4a_path(session_id).exists():
            raise ApiError(409, "not_finalized", "session has no audio to process")
        if store.db.active_job_for_session(session_id) is not None:
            raise ApiError(409, "job_exists", "a job is already queued or running for this session")
        return respond(enqueue(session_id, body["kind"], body.get("num_speakers")), "Job", status=202)

    @app.post("/v1/jobs")
    def bulk_enqueue():
        body = request_json("BulkJobCreate")
        kind = body["kind"]
        if kind == "diarize" and not diarize_available():
            raise ApiError(409, "diarize_unavailable",
                           "no processing service provides diarization")
        jobs = [enqueue(sid, kind) for sid in store.pending_session_ids()]
        return respond({"jobs": jobs}, "JobList", status=202)

    @app.get("/v1/jobs/<int:job_id>")
    def get_job(job_id: int):
        row = store.db.get_job(job_id)
        if row is None:
            raise ApiError(404, "not_found", f"no job {job_id}")
        return respond(job_api(row), "Job")

    @app.delete("/v1/jobs/<int:job_id>")
    def cancel_job(job_id: int):
        row = store.db.get_job(job_id)
        if row is None:
            raise ApiError(404, "not_found", f"no job {job_id}")
        state = row["state"]
        if state == "queued":
            store.db.cancel_queued_job(job_id, datetime.now().isoformat())
        elif state == "running" and worker is not None:
            # A running local job is terminated; a finished-underneath race is a no-op.
            worker.cancel_running(job_id)
        # done/failed/cancelled: already terminal — cancellation is idempotent.
        return ("", 204)

    # ------------------------------------------------------------------ #
    # Processing service (the one operational connection — rpi/specs/api.md)
    # ------------------------------------------------------------------ #

    @app.get("/v1/service")
    def get_service():
        return respond(_service_status(), "Service")

    @app.put("/v1/service")
    def put_service():
        body = request_json("ServiceUpdate")
        if service is None:
            raise ApiError(503, "unavailable", "service configuration unavailable")
        return respond(service.set_url(body["url"]), "Service")

    @app.delete("/v1/service")
    def delete_service():
        if service is not None:
            service.clear()
        return ("", 204)

    def _service_status() -> dict:
        if service is None:
            return {"configured": False, "url": None, "reachable": False, "capabilities": None}
        return service.status()

    # ------------------------------------------------------------------ #
    # Web UI — vanilla assets served statically (rpi/requirements/web-ui)
    # ------------------------------------------------------------------ #

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/app.js")
    def app_js():
        return send_from_directory(WEB_DIR, "app.js", mimetype="text/javascript")

    return app


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
