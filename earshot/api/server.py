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
from typing import Any

from flask import Flask, Response, jsonify, request

from earshot.api.errors import ApiError
from earshot.api import validation
from earshot.jobs.serialize import job_api
from earshot.storage.paths import parse_session_id

log = logging.getLogger("earshot.api")


def create_app(controller, store, config, worker=None) -> Flask:
    app = Flask(__name__)
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
        # Diarization needs a processing service reporting the capability; there is
        # no service client until M7, so it is unavailable on the standalone device.
        return False

    def enqueue(session_id: int, kind: str) -> dict:
        if kind == "diarize" and not diarize_available():
            raise ApiError(409, "diarize_unavailable",
                           "no processing service provides diarization")
        job_id = store.db.insert_job(session_id, kind, datetime.now().isoformat())
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
        def stream():
            last = None
            # On connect: one state event with the current status.
            snapshot = controller.status()
            yield _sse("state", snapshot)
            last = json.dumps(snapshot, sort_keys=True)
            while True:
                time.sleep(1.0)
                snapshot = controller.status()
                current = json.dumps(snapshot, sort_keys=True)
                if current != last:
                    yield _sse("state", snapshot)
                    last = current
                else:
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
        if not store.m4a_path(session_id).exists():
            raise ApiError(409, "not_finalized", "session has no audio to process")
        if store.db.active_job_for_session(session_id) is not None:
            raise ApiError(409, "job_exists", "a job is already queued or running for this session")
        return respond(enqueue(session_id, body["kind"]), "Job", status=202)

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

    return app


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
