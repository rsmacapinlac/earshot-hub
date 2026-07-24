"""The device state machine as an explicit, testable transition table.

Every legal ``(state, trigger) -> transition`` the control loop can make lives
here as data (rpi/specs/state-machine.md). The :class:`~earshot.statemachine.
machine.Controller` consults this table instead of scattering the rules across
its tick methods, and the table is unit-tested exhaustively in isolation.

A ``(state, trigger)`` pair **absent** from the table means the trigger is
ignored in that state — which is exactly how the spec words most of its
prohibitions ("button holds during recording or processing are ignored";
"presses ... are ignored during post-recording processing").

Design notes tying the table to the spec:

- **``processing`` means a *local* job.** Per DECISIONS.md, the device is only
  ``processing`` while local CPU-bound work runs; a job on the processing service
  leaves the device ``idle``/``recording``. So the preemption rule (FR-2) falls
  straight out of the table: ``PROCESSING + START -> RECORDING`` (cancel the local
  job, requeue it to the front) via :data:`Action.PREEMPT_RECORD`, while a service
  job — not a state — simply never blocks the ordinary ``IDLE + START`` path.
- **Disk gating is a guard, not duplicated logic.** ``START``/``PRESS`` from either
  ``IDLE`` or ``DISK_FULL`` route through :data:`Guard.DISK_OK`; a blocked disk
  fails the guard (a ``press`` is ignored, a web ``start`` returns ``disk_full``).
- ``finalizing`` is a real, observable state: the encode runs synchronously on the
  control thread, so a concurrent ``GET /v1/status`` can catch it before the
  follow-up ``FINALIZED``/``TOO_SHORT`` trigger returns the device to ``idle``.

The job triggers (``JOB_STARTED``/``JOB_FINISHED``) and ``PREEMPT_RECORD`` are
encoded and tested here now; the Controller begins emitting them when the job
worker lands (M6).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class State(str, Enum):
    """Device states. The six persistent ones match api.md ``DeviceState``;
    ``shutting_down`` is terminal (the device powers off)."""

    BOOTING = "booting"
    IDLE = "idle"
    DISK_FULL = "disk_full"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    PROCESSING = "processing"
    SHUTTING_DOWN = "shutting_down"


# Reported to GET /v1/status (api.md DeviceState). shutting_down is not reported —
# the device is on its way off.
DEVICE_STATES: frozenset[str] = frozenset(
    s.value for s in State if s is not State.SHUTTING_DOWN
)


class Trigger(str, Enum):
    """What can move the machine. External gestures/commands and internal signals."""

    READY = "ready"                # boot complete
    PRESS = "press"                # button press (context toggles record/stop)
    START = "start"                # web start command
    STOP = "stop"                  # web stop command
    HOLD = "hold"                  # button hold (>= shutdown_hold_seconds)
    DISK_BLOCKED = "disk_blocked"  # usage crossed the threshold
    DISK_CLEARED = "disk_cleared"  # usage dropped back below it
    TOO_SHORT = "too_short"        # captured < min duration at stop
    FINALIZED = "finalized"        # encode finished (success or logged failure)
    JOB_STARTED = "job_started"    # a local job was dequeued (M6)
    JOB_FINISHED = "job_finished"  # the local job ended (done/failed/cancelled) (M6)


class Guard(str, Enum):
    DISK_OK = "disk_ok"  # the disk threshold is not reached


class Action(str, Enum):
    """A side effect the Controller runs when a transition fires."""

    BEGIN = "begin_recording"
    END = "end_recording"
    PREEMPT_RECORD = "preempt_and_record"  # cancel + requeue the local job, then record
    SHUTDOWN = "safe_shutdown"


@dataclass(frozen=True)
class Transition:
    target: State
    guard: Guard | None = None
    action: Action | None = None


# The transition table. Absent (state, trigger) pairs are ignored in that state.
TABLE: dict[tuple[State, Trigger], Transition] = {
    # -- FR-1: boot -> idle, with disk gating already possible at startup ----- #
    (State.BOOTING, Trigger.READY): Transition(State.IDLE),
    (State.BOOTING, Trigger.DISK_BLOCKED): Transition(State.DISK_FULL),

    # -- FR-2: start recording (guarded by disk); FR-4: hold -> shutdown ------ #
    (State.IDLE, Trigger.PRESS): Transition(State.RECORDING, Guard.DISK_OK, Action.BEGIN),
    (State.IDLE, Trigger.START): Transition(State.RECORDING, Guard.DISK_OK, Action.BEGIN),
    (State.IDLE, Trigger.HOLD): Transition(State.SHUTTING_DOWN, action=Action.SHUTDOWN),
    (State.IDLE, Trigger.DISK_BLOCKED): Transition(State.DISK_FULL),
    (State.IDLE, Trigger.JOB_STARTED): Transition(State.PROCESSING),

    # A blocked disk still routes start attempts through the guard (press ignored,
    # web start -> disk_full error); holds still shut down.
    (State.DISK_FULL, Trigger.PRESS): Transition(State.RECORDING, Guard.DISK_OK, Action.BEGIN),
    (State.DISK_FULL, Trigger.START): Transition(State.RECORDING, Guard.DISK_OK, Action.BEGIN),
    (State.DISK_FULL, Trigger.DISK_CLEARED): Transition(State.IDLE),
    (State.DISK_FULL, Trigger.HOLD): Transition(State.SHUTTING_DOWN, action=Action.SHUTDOWN),
    (State.DISK_FULL, Trigger.JOB_STARTED): Transition(State.PROCESSING),

    # -- FR-3: stop recording -> finalize (encode); disk-full mid-session stops. #
    (State.RECORDING, Trigger.PRESS): Transition(State.FINALIZING, action=Action.END),
    (State.RECORDING, Trigger.STOP): Transition(State.FINALIZING, action=Action.END),
    (State.RECORDING, Trigger.DISK_BLOCKED): Transition(State.FINALIZING, action=Action.END),
    # HOLD and START are absent here: ignored while recording.

    # Finalizing is transient; the END action emits one of these to settle to idle.
    (State.FINALIZING, Trigger.FINALIZED): Transition(State.IDLE),
    (State.FINALIZING, Trigger.TOO_SHORT): Transition(State.IDLE),

    # -- Processing (local job): coexist with nothing; recording preempts it. -- #
    (State.PROCESSING, Trigger.JOB_FINISHED): Transition(State.IDLE),
    (State.PROCESSING, Trigger.PRESS): Transition(State.RECORDING, Guard.DISK_OK, Action.PREEMPT_RECORD),
    (State.PROCESSING, Trigger.START): Transition(State.RECORDING, Guard.DISK_OK, Action.PREEMPT_RECORD),
    (State.PROCESSING, Trigger.DISK_BLOCKED): Transition(State.DISK_FULL),
    # HOLD absent: shutdown only while idle/disk_full.
}


def lookup(state: State, trigger: Trigger) -> Transition | None:
    """The transition for ``(state, trigger)``, or ``None`` if the trigger is ignored."""
    return TABLE.get((state, trigger))


def legal_triggers(state: State) -> frozenset[Trigger]:
    """Triggers that cause a transition from *state* (everything else is ignored)."""
    return frozenset(t for (s, t) in TABLE if s == state)


def validate_table() -> None:
    """Sanity-check the table's shape. Called once at import so a malformed edit
    fails loudly rather than at 2 a.m. on the device."""
    from earshot.hal.led_states import led_state_for

    for (state, trigger), tr in TABLE.items():
        assert isinstance(state, State) and isinstance(trigger, Trigger)
        assert isinstance(tr.target, State)
        if tr.target is not State.SHUTTING_DOWN:
            led_state_for(tr.target.value)  # raises KeyError if a target has no LED
    # Every persistent state except the terminal shutdown must be reachable.
    targets = {tr.target for tr in TABLE.values()} | {State.BOOTING}
    for s in State:
        if s is State.SHUTTING_DOWN:
            continue
        assert s in targets, f"state {s} is unreachable"


validate_table()
