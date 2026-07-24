"""Milestone 1 — the API contract is the source of truth.

These tests assert the OpenAPI document is internally sound and that runtime
validation binds to it correctly (positive and negative payloads). They do not
require a running server.
"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from earshot.api import validation


# --------------------------------------------------------------------------- #
# Document soundness
# --------------------------------------------------------------------------- #

def test_document_loads_and_is_openapi_31():
    doc = validation.openapi_document()
    assert doc["openapi"].startswith("3.1")
    assert doc["paths"]
    assert doc["components"]["schemas"]


def test_every_component_schema_is_valid_draft202012():
    schemas = validation.openapi_document()["components"]["schemas"]
    for name, schema in schemas.items():
        # Raises if the schema itself is not a valid Draft 2020-12 schema.
        Draft202012Validator.check_schema(schema)


def test_every_component_schema_compiles_to_a_validator():
    for name in validation.component_names():
        assert validation.validator_for(name) is not None


def test_every_operation_has_an_operation_id():
    paths = validation.openapi_document()["paths"]
    for path, item in paths.items():
        for method, op in item.items():
            if method == "parameters":
                continue
            assert "operationId" in op, f"{method.upper()} {path} missing operationId"


def test_every_referenced_component_exists():
    """Walk $refs; every #/components/... target must resolve."""
    doc = validation.openapi_document()
    refs: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref" and isinstance(v, str):
                    refs.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    for ref in refs:
        assert ref.startswith("#/"), ref
        node = doc
        for part in ref.lstrip("#/").split("/"):
            assert part in node, f"unresolved $ref {ref} (missing {part!r})"
            node = node[part]


# --------------------------------------------------------------------------- #
# Positive payloads — the exact shapes from rpi/specs/api.md
# --------------------------------------------------------------------------- #

def test_status_example_validates():
    status = {
        "state": "idle",
        "led": {"rgb": [0, 255, 0], "pattern": "solid"},
        "recording": {"session_id": "rec-000043", "elapsed": 128},
        "processing": {
            "session_id": "rec-000042", "kind": "diarize",
            "route": "service", "stage": "diarizing", "progress": 0.42,
        },
        "disk": {"used_percent": 46, "blocked": False},
    }
    assert validation.is_valid(status, "Status")


def test_status_with_nulls_validates():
    status = {
        "state": "booting",
        "led": {"rgb": [255, 255, 255], "pattern": "slow_pulse"},
        "recording": None,
        "processing": None,
        "disk": {"used_percent": 5, "blocked": False},
    }
    assert validation.is_valid(status, "Status")


def test_session_list_example_validates():
    payload = {
        "sessions": [
            {
                "id": "rec-000042", "name": "Weekly sync — pricing",
                "state": "diarized", "created_at": "2026-07-17T14:28:01",
                "duration": 2583.4, "size": 10289152,
                "has_transcript": True, "diarized": True,
            }
        ]
    }
    assert validation.is_valid(payload, "SessionList")


def test_session_unnamed_and_unfinalized_validates():
    payload = {
        "sessions": [
            {
                "id": "rec-000044", "name": None, "state": "recording",
                "created_at": "2026-07-17T14:28:01", "duration": None,
                "size": None, "has_transcript": False, "diarized": False,
            }
        ]
    }
    assert validation.is_valid(payload, "SessionList")


def test_job_example_validates():
    job = {
        "id": 128, "session_id": "rec-000042", "kind": "diarize", "route": "service",
        "state": "running", "stage": "diarizing", "progress": 0.42,
        "attempts": 1, "enqueued_at": "2026-07-17T14:00:00",
        "started_at": "2026-07-17T14:01:00",
    }
    assert validation.is_valid(job, "Job")


def test_queued_job_without_route_validates():
    job = {
        "id": 129, "session_id": "rec-000045", "kind": "transcribe",
        "route": None, "state": "queued", "attempts": 0,
        "enqueued_at": "2026-07-17T14:00:00",
    }
    assert validation.is_valid(job, "Job")


def test_speakers_example_validates():
    payload = {
        "speakers": [
            {"label": "Speaker 1", "name": "Ritchie", "segments": 14},
            {"label": "Speaker 2", "name": None, "segments": 9},
        ]
    }
    assert validation.is_valid(payload, "SpeakerList")


def test_service_example_validates():
    svc = {
        "configured": True, "url": "http://homelab.local:9000",
        "reachable": True, "capabilities": {"transcribe": True, "diarize": True},
    }
    assert validation.is_valid(svc, "Service")


def test_transcript_segments_validate():
    diarized = [{"start": 0.0, "end": 5.8, "text": "hi", "speaker": "Speaker 1"}]
    plain = [{"start": 0.0, "end": 5.8, "text": "hi"}]
    assert validation.is_valid(diarized[0], "TranscriptSegment")
    assert validation.is_valid(plain[0], "TranscriptSegment")


def test_stop_recording_discarded_result_validates():
    assert validation.is_valid(
        {"discarded": True, "reason": "too_short"}, "StopRecordingResult"
    )


@pytest.mark.parametrize("body,schema", [
    ({"name": "Standup"}, "NameUpdate"),
    ({"name": None}, "NameUpdate"),
    ({"kind": "transcribe"}, "JobCreate"),
    ({"kind": "diarize", "target": "pending"}, "BulkJobCreate"),
    ({"url": "http://homelab.local:9000"}, "ServiceUpdate"),
])
def test_request_bodies_validate(body, schema):
    assert validation.is_valid(body, schema)


# --------------------------------------------------------------------------- #
# Negative payloads — unknown fields and bad enums are rejected
# --------------------------------------------------------------------------- #

def test_unknown_request_field_is_rejected():
    # rpi/specs/api.md: unknown request fields are rejected, not ignored.
    assert not validation.is_valid({"name": "x", "extra": 1}, "NameUpdate")


def test_bad_enum_is_rejected():
    assert not validation.is_valid({"kind": "summarize"}, "JobCreate")


def test_bad_session_id_pattern_is_rejected():
    bad = {
        "sessions": [{
            "id": "2026-07-17", "name": None, "state": "pending",
            "created_at": "x", "duration": None, "size": None,
            "has_transcript": False, "diarized": False,
        }]
    }
    assert not validation.is_valid(bad, "SessionList")


def test_led_rgb_out_of_range_is_rejected():
    assert not validation.is_valid(
        {"rgb": [0, 999, 0], "pattern": "solid"}, "Led"
    )


def test_validate_raises_with_messages():
    with pytest.raises(validation.SchemaValidationError) as excinfo:
        validation.validate({"kind": "nope"}, "JobCreate")
    assert excinfo.value.errors
