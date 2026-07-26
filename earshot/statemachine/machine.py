"""Control loop + Controller, driven by the explicit transition table.

The loop thread owns every state transition. It turns inputs — a button gesture,
a queued web command, a sensed disk-threshold crossing, an internal completion —
into a :class:`~earshot.statemachine.transitions.Trigger`, and routes it through
:data:`~earshot.statemachine.transitions.TABLE`. A ``(state, trigger)`` pair not
in the table is ignored, which is how the spec words most of its prohibitions.

The button is read inside the loop; the API hands work in as commands on a queue
and blocks on the result, so the two surfaces cannot act concurrently — the
"single-threaded control loop" the spec describes, not a separate capture thread.

Live in M5: idle ↔ recording ↔ finalizing, disk gating, min-duration discard,
safe-shutdown. The table also encodes the ``processing`` state and the local-job
preemption rule (FR-2); the Controller starts emitting the job triggers when the
worker lands (M6).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from earshot.api.errors import ApiError
from earshot.config import Config
from earshot.hal.bundle import Hal
from earshot.hal.led_states import DISCARDED, SHUTTING_DOWN, led_state_for
from earshot.recording.encode import EncodeError, probe_duration, transcode_to_m4a
from earshot.recording.recorder import Recorder
from earshot.statemachine.transitions import (
    Action,
    Guard,
    State,
    Transition,
    Trigger,
    lookup,
)
from earshot.storage.paths import render_session_id
from earshot.storage.store import Store

log = logging.getLogger("earshot.statemachine")

_IDLE_POLL_SECONDS = 0.05
_COMMAND_TIMEOUT_SECONDS = 300.0  # generous: a stop waits for the encode to finish

_COMMAND_TRIGGERS = {"start": Trigger.START, "stop": Trigger.STOP}


@dataclass
class _Command:
    kind: str  # "start" | "stop" | "proc_begin" | "proc_end"
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: ApiError | None = None
    payload: Any = None

    def resolve(self, result: Any) -> None:
        self.result = result
        self.done.set()

    def fail(self, error: ApiError) -> None:
        self.error = error
        self.done.set()


class Controller:
    def __init__(
        self,
        config: Config,
        hal: Hal,
        store: Store,
        *,
        shutdown_fn: Callable[[], None] | None = None,
    ):
        self.config = config
        self.hal = hal
        self.store = store
        self._shutdown_fn = shutdown_fn or (lambda: log.warning("shutdown requested (no-op on %s)", hal.name))

        self._commands: "queue.Queue[_Command]" = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="earshot-control", daemon=True)

        self._state_lock = threading.Lock()
        self._state: State = State.BOOTING
        self._session_id: int | None = None
        self._started_monotonic: float | None = None
        self._recorder: Recorder | None = None
        self._ready = threading.Event()
        self._processing: dict | None = None  # running local-job snapshot (status)
        self._worker = None  # set by attach_worker; the job worker to preempt

        # Action name -> handler. The table names an Action; the Controller binds it.
        self._actions: dict[Action, Callable[[Transition], Any]] = {
            Action.BEGIN: self._do_begin,
            Action.END: self._do_end,
            Action.PREEMPT_RECORD: self._do_preempt_record,
            Action.SHUTDOWN: self._do_shutdown,
        }

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        self.hal.led.start()  # pi backend opens SPI + its animator; stub is a no-op
        self.hal.led.set(led_state_for(State.BOOTING.value))
        self.hal.button.start()
        self._thread.start()

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def attach_worker(self, worker) -> None:
        """Wire the job worker so recording can preempt a running local job."""
        self._worker = worker

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        try:
            self.hal.capture.stop()
        finally:
            self.hal.led.stop()
            self.hal.button.stop()

    # -- public API (thread-safe) ----------------------------------------- #

    def start_recording(self) -> dict:
        return self._submit("start")

    def stop_recording(self) -> dict:
        return self._submit("stop")

    def _submit(self, kind: str, payload: Any = None) -> dict:
        cmd = _Command(kind, payload=payload)
        self._commands.put(cmd)
        if not cmd.done.wait(_COMMAND_TIMEOUT_SECONDS):
            raise ApiError(503, "busy", "the device did not respond in time")
        if cmd.error is not None:
            raise cmd.error
        return cmd.result

    def ingest_upload(self, src: Path, name: str | None, occurred_at: str | None) -> dict:
        """Create a session from an uploaded audio file (rpi/requirements/web-ui/
        upload-audio.md). Serialised on the control loop so it is refused while a
        recording is active and surfaces the ``finalizing`` state during the encode.

        Returns the finished ``SessionDetail``; raises ApiError (409/400) otherwise.
        """
        return self._submit(
            "ingest", payload={"src": src, "name": name, "occurred_at": occurred_at}
        )

    @property
    def active_session_id(self) -> int | None:
        """The id of the in-progress recording, or None. Read by the API layer."""
        with self._state_lock:
            return self._session_id if self._state is State.RECORDING else None

    def is_recording(self) -> bool:
        with self._state_lock:
            return self._state is State.RECORDING

    # -- worker coordination (called from the job worker thread) ----------- #

    def begin_processing(self, snapshot: dict) -> bool:
        """Ask to enter ``processing`` for a local job. Granted only when idle, so a
        local job never runs during a recording. Serialised on the control loop."""
        cmd = _Command("proc_begin", payload=snapshot)
        self._commands.put(cmd)
        if not cmd.done.wait(_COMMAND_TIMEOUT_SECONDS):
            return False
        return bool(cmd.result)

    def end_processing(self) -> None:
        """Leave ``processing`` when a local job finishes. Serialised on the loop."""
        cmd = _Command("proc_end")
        self._commands.put(cmd)
        cmd.done.wait(_COMMAND_TIMEOUT_SECONDS)

    def set_processing(self, snapshot: dict | None) -> None:
        """Populate ``status.processing`` for a **service** job without changing the
        device state — a service job runs on another machine, so the device stays
        idle/recording (LED unchanged) while the UI surfaces the job (DECISIONS.md)."""
        with self._state_lock:
            self._processing = snapshot

    def status(self) -> dict:
        with self._state_lock:
            state = self._state
            session_id = self._session_id
            started = self._started_monotonic
            processing = self._processing
        recording = None
        if state is State.RECORDING and session_id is not None and started is not None:
            recording = {
                "session_id": render_session_id(session_id),
                "elapsed": round(time.monotonic() - started, 3),
            }
        disk = self.store.disk_info()
        led = led_state_for(state.value)
        return {
            "state": state.value,
            "led": {"rgb": list(led.rgb), "pattern": led.pattern.value},
            "recording": recording,
            "processing": processing,  # a running local job; also set for service jobs (M7)
            "disk": {"used_percent": disk.used_percent, "blocked": disk.blocked},
        }

    # -- transition plumbing ---------------------------------------------- #

    def _enter(self, state: State) -> None:
        """Move to *state* and show its LED. The single place a resting state changes."""
        with self._state_lock:
            self._state = state
        self.hal.led.set(led_state_for(state.value))

    def _advance(self, trigger: Trigger) -> None:
        """Apply an internal, unconditional transition (boot/disk/completion).

        The transition must exist and be actionless — a pure state move; anything
        with an action goes through :meth:`_fire`."""
        tr = lookup(self._state, trigger)
        assert tr is not None and tr.action is None, f"no pure transition {self._state}+{trigger}"
        self._enter(tr.target)

    def _fire(self, trigger: Trigger, command: _Command | None = None) -> None:
        """Route an external trigger (button gesture or web command) through the table."""
        tr = lookup(self._state, trigger)
        if tr is None:
            if command is not None:
                command.fail(self._illegal_command_error(self._state, trigger))
            return
        if tr.guard is Guard.DISK_OK and self.store.disk_info().blocked:
            if command is not None:
                command.fail(ApiError(409, "disk_full", "disk threshold reached; recording blocked"))
            else:
                log.info("start ignored: disk threshold reached")
            return
        if tr.action is None:
            self._enter(tr.target)
            if command is not None:
                command.resolve(None)
            return
        # Action-bearing transitions manage their own (possibly multi-step) entries.
        result = self._actions[tr.action](tr)
        if command is not None:
            command.resolve(result)

    def _illegal_command_error(self, state: State, trigger: Trigger) -> ApiError:
        if trigger is Trigger.START and state is State.RECORDING:
            return ApiError(409, "already_recording", "a recording is already active")
        if trigger is Trigger.STOP:
            return ApiError(409, "not_recording", "nothing is recording")
        return ApiError(409, "busy", "device is busy")

    # -- control loop ------------------------------------------------------ #

    def _run(self) -> None:
        self._advance(Trigger.READY)  # BOOTING -> IDLE
        self._ready.set()
        while not self._stop.is_set():
            self._tick()

    def _tick(self) -> None:
        state = self._state

        # Pump capture while recording so a stop is honoured between blocks.
        if state is State.RECORDING and self._recorder is not None:
            pcm = self.hal.capture.read()
            if pcm:
                self._recorder.write(pcm)

        # Sense the disk threshold (may itself end a recording).
        self._sense_disk()
        if self._state is not state:
            return  # disk sensing changed state; re-evaluate next tick

        # A queued command takes priority over a fresh button press.
        cmd = self._take_command()
        if cmd is not None:
            self._handle_command(cmd)
            return

        timeout = 0.0 if self._state is State.RECORDING else _IDLE_POLL_SECONDS
        event = self.hal.button.poll_event(timeout=timeout)
        if event is None:
            return
        if event.value == "press":
            self._fire(Trigger.PRESS)
        elif event.value == "hold":
            self._fire(Trigger.HOLD)
        self._reject_pending("device busy")

    def _sense_disk(self) -> None:
        state = self._state
        blocked = self.store.disk_info().blocked
        if not blocked:
            if state is State.DISK_FULL:
                self._advance(Trigger.DISK_CLEARED)  # DISK_FULL -> IDLE
            return
        if state is State.IDLE:
            self._advance(Trigger.DISK_BLOCKED)      # IDLE -> DISK_FULL
        elif state is State.RECORDING:
            log.warning("disk threshold reached mid-session; stopping recording")
            self._fire(Trigger.DISK_BLOCKED)         # RECORDING -> FINALIZING (END)

    def _handle_command(self, cmd: _Command) -> None:
        if cmd.kind in _COMMAND_TRIGGERS:
            self._fire(_COMMAND_TRIGGERS[cmd.kind], command=cmd)
            self._reject_pending("device busy")
        elif cmd.kind == "ingest":
            self._ingest(cmd)
        elif cmd.kind == "proc_begin":
            cmd.resolve(self._grant_processing(cmd.payload))
        elif cmd.kind == "proc_end":
            self._end_processing_internal()
            cmd.resolve(None)

    def _grant_processing(self, snapshot: dict | None) -> bool:
        """Enter ``processing`` for a local job if the device is idle (table:
        IDLE/DISK_FULL + JOB_STARTED). Denied while recording/finalizing/processing."""
        tr = lookup(self._state, Trigger.JOB_STARTED)
        if tr is None:
            return False
        self._processing = snapshot
        self._enter(tr.target)  # -> PROCESSING (amber, very slow pulse)
        return True

    def _end_processing_internal(self) -> None:
        self._processing = None
        if self._state is State.PROCESSING:
            self._advance(Trigger.JOB_FINISHED)  # PROCESSING -> IDLE

    def _ingest(self, cmd: _Command) -> None:
        """Ingest an uploaded file into a new session (upload-audio.md).

        Only from idle: a recording is sacred and the encode holds Pi CPU, so an
        upload is refused while recording/finalizing and while a local job runs
        (processing). Self-contained — it resolves/fails the command itself so no
        exception escapes onto the control-loop thread. The encode blocks the loop
        exactly as a recording finalize does; the ``finalizing`` LED/state is shown
        while it runs.
        """
        if self._state in (State.RECORDING, State.FINALIZING):
            cmd.fail(ApiError(409, "recording", "upload is disabled while recording"))
            return
        if self._state not in (State.IDLE, State.DISK_FULL):
            cmd.fail(ApiError(409, "busy", "device is busy"))  # processing / booting
            return
        if self.store.disk_info().blocked:
            cmd.fail(ApiError(409, "disk_full", "disk threshold reached; upload blocked"))
            return

        payload = cmd.payload
        src = Path(payload["src"])
        self._enter(State.FINALIZING)  # amber; observable by a concurrent GET /v1/status
        session_id = self.store.allocate_session()
        try:
            out = self.store.m4a_path(session_id)
            transcode_to_m4a(
                src, out, bitrate_kbps=self.config.recording.encode_bitrate_kbps
            )
            duration = probe_duration(out)
            size = out.stat().st_size
            self.store.finalize_session(session_id, duration, size)
            if payload["name"] is not None or payload["occurred_at"] is not None:
                self.store.set_session_fields(
                    session_id, name=payload["name"], occurred_at=payload["occurred_at"]
                )
            row = self.store.db.get_session(session_id)
            log.info("uploaded session ingested: %s", render_session_id(session_id))
            cmd.resolve(self.store.session_detail_api(row, active_id=None))
        except EncodeError as exc:
            log.warning(
                "upload ingest failed for %s: %s", render_session_id(session_id), exc
            )
            self.store.delete_session(session_id)  # drop the empty allocated session
            cmd.fail(ApiError(400, "invalid_audio", "uploaded audio could not be decoded"))
        finally:
            src.unlink(missing_ok=True)
            self._advance(Trigger.FINALIZED)  # FINALIZING -> IDLE

    def _take_command(self) -> _Command | None:
        try:
            return self._commands.get_nowait()
        except queue.Empty:
            return None

    def _reject_pending(self, message: str) -> None:
        """Fail any start/stop that queued during a synchronous action (finalize).

        Actions block the loop while they run, so start/stop that arrive mid-encode
        are "during post-recording processing" and are rejected, per FR-3. Worker
        (``proc_*``) commands are left queued and handled normally next tick."""
        keep: list[_Command] = []
        while True:
            cmd = self._take_command()
            if cmd is None:
                break
            if cmd.kind in _COMMAND_TRIGGERS:
                cmd.fail(ApiError(409, "busy", message))
            else:
                keep.append(cmd)
        for cmd in keep:
            self._commands.put(cmd)

    # -- actions ----------------------------------------------------------- #

    def _do_begin(self, tr: Transition) -> dict:
        session_id = self.store.allocate_session()
        recorder = Recorder(
            self.store.session_dir(session_id),
            self.hal.capture.spec,
            chunk_duration_seconds=self.config.recording.chunk_duration_seconds,
            encode_bitrate_kbps=self.config.recording.encode_bitrate_kbps,
        )
        recorder.open()
        self.hal.capture.start()
        with self._state_lock:
            self._session_id = session_id
            self._recorder = recorder
            self._started_monotonic = time.monotonic()
        self._enter(tr.target)  # RECORDING (red)
        log.info("recording started: %s", render_session_id(session_id))
        row = self.store.db.get_session(session_id)
        return self.store.session_detail_api(row, active_id=session_id)

    def _do_preempt_record(self, tr: Transition) -> dict:
        # FR-2: a local job yields to recording — cancel it and requeue to the front.
        self._preempt_local_job()
        self._processing = None
        return self._do_begin(tr)

    def _preempt_local_job(self) -> None:
        """Terminate a running local job so it returns to the queue; recording then
        begins without delay (rpi/specs/processing.md#preemption)."""
        if self._worker is not None:
            self._worker.preempt()

    def _do_end(self, tr: Transition) -> dict:
        self._enter(tr.target)  # FINALIZING (amber)
        with self._state_lock:
            session_id = self._session_id
            recorder = self._recorder
        assert session_id is not None and recorder is not None
        self.hal.capture.stop()

        min_seconds = self.config.recording.min_duration_seconds
        if recorder.captured_seconds < min_seconds:
            log.info(
                "recording %s too short (%.2fs < %ds); discarding",
                render_session_id(session_id), recorder.captured_seconds, min_seconds,
            )
            recorder.discard()
            self.store.delete_session(session_id)
            self._reset_recording_state()
            self.hal.led.set(DISCARDED)          # transient double-flash
            self._advance(Trigger.TOO_SHORT)     # FINALIZING -> IDLE
            return {"discarded": True, "reason": "too_short"}

        try:
            result = recorder.finalize()
            self.store.finalize_session(session_id, result.duration, result.size)
        except EncodeError as exc:
            # FR-6a: retain chunks, remove any partial m4a, return to idle green.
            log.error("finalization failed for %s: %s", render_session_id(session_id), exc)
        row = self.store.db.get_session(session_id)
        detail = self.store.session_detail_api(row, active_id=None)

        self._reset_recording_state()
        self._advance(Trigger.FINALIZED)         # FINALIZING -> IDLE (green)
        log.info("recording finalized: %s", render_session_id(session_id))
        return detail

    def _reset_recording_state(self) -> None:
        with self._state_lock:
            self._session_id = None
            self._recorder = None
            self._started_monotonic = None

    def _do_shutdown(self, tr: Transition) -> None:
        # Terminal on real hardware: the process is powered off and never returns.
        # We do not durably report `shutting_down` (not an api.md DeviceState); the
        # LED shows it, and if shutdown_fn returns (stub/no-op or failure) we restore
        # the resting LED and stay put.
        log.info("safe shutdown requested (button hold while idle)")
        self.hal.led.set(SHUTTING_DOWN)
        try:
            self._shutdown_fn()
        except Exception:  # pragma: no cover - shutdown is best-effort
            log.exception("shutdown failed")
        self.hal.led.set(led_state_for(self._state.value))  # not powered off: restore
        return None
