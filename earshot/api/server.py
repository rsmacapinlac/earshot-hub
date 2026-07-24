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
from typing import Any

from flask import Flask, Response, jsonify, request

from earshot.api.errors import ApiError
from earshot.api import validation
from earshot.storage.paths import parse_session_id

log = logging.getLogger("earshot.api")


def create_app(controller, store, config) -> Flask:
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
    # Jobs (list only in the skeleton; enqueue/cancel land with the worker)
    # ------------------------------------------------------------------ #

    @app.get("/v1/jobs")
    def list_jobs():
        from earshot.jobs.serialize import job_api

        jobs = [job_api(r) for r in store.db.list_jobs()]
        return respond({"jobs": jobs}, "JobList")

    return app


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
