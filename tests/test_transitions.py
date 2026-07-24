"""Milestone 5 — the explicit state-machine transition table.

Two independent encodings of rpi/specs/state-machine.md must agree: the shipped
``TABLE`` and the ``EXPECTED`` map transcribed here from the spec. Drift in either
direction fails. Behavioural tests then drive the Controller through the paths the
walking-skeleton suite doesn't reach: safe shutdown (and its prohibitions) and a
mid-session disk-threshold stop.
"""

from __future__ import annotations

import time

import pytest

from earshot.config import Config
from earshot.hal.led_states import LedPattern
from earshot.statemachine.transitions import (
    DEVICE_STATES,
    TABLE,
    Action,
    Guard,
    State,
    Trigger,
    legal_triggers,
    lookup,
)

# --- the table, transcribed independently from state-machine.md ------------ #
# value = (target, guard, action) for a firing transition; absent = ignored.
_S, _T, _G, _A = State, Trigger, Guard, Action
EXPECTED: dict[tuple[State, Trigger], tuple] = {
    (_S.BOOTING, _T.READY): (_S.IDLE, None, None),
    (_S.BOOTING, _T.DISK_BLOCKED): (_S.DISK_FULL, None, None),

    (_S.IDLE, _T.PRESS): (_S.RECORDING, _G.DISK_OK, _A.BEGIN),
    (_S.IDLE, _T.START): (_S.RECORDING, _G.DISK_OK, _A.BEGIN),
    (_S.IDLE, _T.HOLD): (_S.SHUTTING_DOWN, None, _A.SHUTDOWN),
    (_S.IDLE, _T.DISK_BLOCKED): (_S.DISK_FULL, None, None),
    (_S.IDLE, _T.JOB_STARTED): (_S.PROCESSING, None, None),

    (_S.DISK_FULL, _T.PRESS): (_S.RECORDING, _G.DISK_OK, _A.BEGIN),
    (_S.DISK_FULL, _T.START): (_S.RECORDING, _G.DISK_OK, _A.BEGIN),
    (_S.DISK_FULL, _T.DISK_CLEARED): (_S.IDLE, None, None),
    (_S.DISK_FULL, _T.HOLD): (_S.SHUTTING_DOWN, None, _A.SHUTDOWN),
    (_S.DISK_FULL, _T.JOB_STARTED): (_S.PROCESSING, None, None),

    (_S.RECORDING, _T.PRESS): (_S.FINALIZING, None, _A.END),
    (_S.RECORDING, _T.STOP): (_S.FINALIZING, None, _A.END),
    (_S.RECORDING, _T.DISK_BLOCKED): (_S.FINALIZING, None, _A.END),

    (_S.FINALIZING, _T.FINALIZED): (_S.IDLE, None, None),
    (_S.FINALIZING, _T.TOO_SHORT): (_S.IDLE, None, None),

    (_S.PROCESSING, _T.JOB_FINISHED): (_S.IDLE, None, None),
    (_S.PROCESSING, _T.PRESS): (_S.RECORDING, _G.DISK_OK, _A.PREEMPT_RECORD),
    (_S.PROCESSING, _T.START): (_S.RECORDING, _G.DISK_OK, _A.PREEMPT_RECORD),
    (_S.PROCESSING, _T.DISK_BLOCKED): (_S.DISK_FULL, None, None),
}


# --- table structure ------------------------------------------------------- #


def test_table_matches_spec_transcription():
    """Every firing transition, and nothing more, matches the spec transcription."""
    shipped = {k: (t.target, t.guard, t.action) for k, t in TABLE.items()}
    assert shipped == EXPECTED


@pytest.mark.parametrize("state", list(State))
@pytest.mark.parametrize("trigger", list(Trigger))
def test_every_state_trigger_pair_is_defined_or_ignored(state, trigger):
    """No accidental entries: each pair is exactly what the spec transcription says
    (a firing transition or an ignored no-op)."""
    tr = lookup(state, trigger)
    expected = EXPECTED.get((state, trigger))
    if expected is None:
        assert tr is None, f"{state}+{trigger} should be ignored"
    else:
        assert (tr.target, tr.guard, tr.action) == expected


def test_shutting_down_is_terminal():
    assert legal_triggers(State.SHUTTING_DOWN) == frozenset()


def test_device_states_match_api_contract():
    """The reported states equal the OpenAPI DeviceState enum (minus terminal shutdown)."""
    import yaml

    spec = yaml.safe_load(open("earshot/api/openapi.yaml"))
    enum = set(spec["components"]["schemas"]["DeviceState"]["enum"])
    assert DEVICE_STATES == frozenset(enum)
    assert "shutting_down" not in enum  # terminal; never reported


def test_recording_is_only_reached_through_a_disk_guard():
    """You can never enter RECORDING without passing the disk guard."""
    for (state, trigger), tr in TABLE.items():
        if tr.target is State.RECORDING:
            assert tr.guard is Guard.DISK_OK, f"{state}+{trigger} enters recording unguarded"


def test_led_pattern_for_processing_is_slower_than_finalizing():
    """Amber processing must be visually distinct (slower pulse) from amber finalizing."""
    from earshot.hal.led_states import FINALIZING, PROCESSING

    assert FINALIZING.pattern is LedPattern.SLOW_PULSE
    assert PROCESSING.pattern is LedPattern.VERY_SLOW_PULSE


# --- behavioural: safe shutdown & mid-session disk stop -------------------- #


@pytest.fixture
def app_with_shutdown(tmp_path, monkeypatch):
    """Build a stub app whose shutdown_fn is a spy, with a fast min duration."""
    from earshot.app import build_application

    for var in ("EARSHOT_HAL", "EARSHOT_CONFIG", "EARSHOT_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)

    calls: list[int] = []
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)
    cfg.recording.min_duration_seconds = 0

    app = build_application(
        config=cfg, hal_override="stub", realtime=True,
        shutdown_fn=lambda: calls.append(1),
    )
    app.start()
    app.shutdown_calls = calls  # type: ignore[attr-defined]
    yield app
    app.stop()


def _wait_state(client, target, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/v1/status").get_json()["state"] == target:
            return True
        time.sleep(0.02)
    return client.get("/v1/status").get_json()["state"] == target


def test_hold_while_idle_shuts_down(app_with_shutdown):
    app = app_with_shutdown
    client = app.flask_app.test_client()
    assert _wait_state(client, "idle")

    app.hal.button.hold()
    time.sleep(0.2)
    assert app.shutdown_calls == [1]
    # Not powered off (stub), so it stays a usable idle device with the green LED.
    assert _wait_state(client, "idle")


def test_hold_while_recording_is_ignored(app_with_shutdown):
    app = app_with_shutdown
    client = app.flask_app.test_client()
    app.hal.button.press()
    assert _wait_state(client, "recording")

    app.hal.button.hold()
    time.sleep(0.2)
    assert app.shutdown_calls == []          # shutdown ignored mid-recording
    assert _wait_state(client, "recording")  # still recording
    app.hal.button.press()                   # clean stop


def test_hold_while_disk_full_shuts_down(app_with_shutdown, monkeypatch):
    from earshot.storage.store import DiskInfo, Store

    app = app_with_shutdown
    client = app.flask_app.test_client()
    monkeypatch.setattr(Store, "disk_info", lambda self: DiskInfo(used_percent=95.0, blocked=True))
    assert _wait_state(client, "disk_full")

    app.hal.button.hold()
    time.sleep(0.2)
    assert app.shutdown_calls == [1]


def test_disk_threshold_mid_recording_stops_and_finalizes(app_with_shutdown, monkeypatch):
    from earshot.storage.store import DiskInfo, Store

    app = app_with_shutdown
    client = app.flask_app.test_client()
    app.hal.button.press()
    assert _wait_state(client, "recording")
    time.sleep(0.2)  # accumulate some audio

    # Disk crosses the threshold mid-session -> the recording is stopped.
    monkeypatch.setattr(Store, "disk_info", lambda self: DiskInfo(used_percent=95.0, blocked=True))
    assert _wait_state(client, "disk_full")  # ended, then blocked at the threshold

    # The interrupted session was finalized (encoded), not lost.
    sessions = client.get("/v1/sessions").get_json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["state"] == "pending"
    assert sessions[0]["duration"] and sessions[0]["duration"] > 0
