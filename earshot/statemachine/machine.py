"""Control loop + Controller.

The loop thread owns every state transition. The button is read inside the loop;
the API hands work in as commands on a queue and blocks on the result, so the two
surfaces cannot act concurrently. Capture is read block-by-block inside the loop so
a stop (button or web) is honoured between blocks — this is the "single-threaded
control loop" the spec describes, not a separate capture thread.

Skeleton scope (M3): idle <-> recording <-> finalizing, disk gating, min-duration
discard, and safe-shutdown dispatch. Local-vs-service job preemption and the
Processing state are layered on in later milestones.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from earshot.api.errors import ApiError
from earshot.config import Config
from earshot.hal.bundle import Hal
from earshot.hal.led_states import DISCARDED, READY, RECORDING, SHUTTING_DOWN, led_state_for
from earshot.recording.encode import EncodeError
from earshot.recording.recorder import Recorder
from earshot.storage.paths import render_session_id
from earshot.storage.store import Store

log = logging.getLogger("earshot.statemachine")

_IDLE_POLL_SECONDS = 0.05
_COMMAND_TIMEOUT_SECONDS = 300.0  # generous: a stop waits for the encode to finish


@dataclass
class _Command:
    kind: str  # "start" | "stop"
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: ApiError | None = None

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
        self._state = "booting"
        self._session_id: int | None = None
        self._started_monotonic: float | None = None
        self._recorder: Recorder | None = None
        self._ready = threading.Event()

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        self.hal.led.set(led_state_for("booting"))
        self.hal.button.start()
        self._thread.start()

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

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

    def _submit(self, kind: str) -> dict:
        cmd = _Command(kind)
        self._commands.put(cmd)
        if not cmd.done.wait(_COMMAND_TIMEOUT_SECONDS):
            raise ApiError(503, "busy", "the device did not respond in time")
        if cmd.error is not None:
            raise cmd.error
        return cmd.result

    @property
    def active_session_id(self) -> int | None:
        """The id of the in-progress recording, or None. Read by the API layer."""
        with self._state_lock:
            return self._session_id if self._state == "recording" else None

    def status(self) -> dict:
        with self._state_lock:
            state = self._state
            session_id = self._session_id
            started = self._started_monotonic
        recording = None
        if state == "recording" and session_id is not None and started is not None:
            recording = {
                "session_id": render_session_id(session_id),
                "elapsed": round(time.monotonic() - started, 3),
            }
        disk = self.store.disk_info()
        led = led_state_for(state)
        return {
            "state": state,
            "led": {"rgb": list(led.rgb), "pattern": led.pattern.value},
            "recording": recording,
            "processing": None,  # populated once the job worker lands
            "disk": {"used_percent": disk.used_percent, "blocked": disk.blocked},
        }

    # -- internal state helpers ------------------------------------------- #

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self._state = state
        self.hal.led.set(led_state_for(state))

    def _current_state(self) -> str:
        with self._state_lock:
            return self._state

    # -- control loop ------------------------------------------------------ #

    def _run(self) -> None:
        self._set_state("idle")
        self._ready.set()
        while not self._stop.is_set():
            state = self._current_state()
            if state in ("idle", "disk_full"):
                self._idle_tick()
            elif state == "recording":
                self._recording_tick()
            else:
                time.sleep(_IDLE_POLL_SECONDS)

    def _idle_tick(self) -> None:
        # Disk gating (rpi/specs/storage.md#disk-space-management).
        blocked = self.store.disk_info().blocked
        state = self._current_state()
        if blocked and state != "disk_full":
            self._set_state("disk_full")
        elif not blocked and state == "disk_full":
            self._set_state("idle")

        # A queued command takes priority over a fresh button press.
        cmd = self._take_command()
        if cmd is not None:
            self._handle_idle_command(cmd)
            return

        event = self.hal.button.poll_event(timeout=_IDLE_POLL_SECONDS)
        if event is None:
            return
        if event.value == "press":
            try:
                self._begin_recording()
            except ApiError as exc:
                log.info("button start ignored: %s", exc.message)
        elif event.value == "hold":
            self._safe_shutdown()

    def _handle_idle_command(self, cmd: _Command) -> None:
        if cmd.kind == "start":
            try:
                cmd.resolve(self._begin_recording())
            except ApiError as exc:
                cmd.fail(exc)
        elif cmd.kind == "stop":
            cmd.fail(ApiError(409, "not_recording", "nothing is recording"))

    def _recording_tick(self) -> None:
        pcm = self.hal.capture.read()
        if pcm:
            self._recorder.write(pcm)  # type: ignore[union-attr]

        # Stop from the button (a HOLD is ignored while recording).
        event = self.hal.button.poll_event(timeout=0)
        if event is not None and event.value == "press":
            self._end_recording()
            return

        cmd = self._take_command()
        if cmd is not None:
            if cmd.kind == "stop":
                cmd.resolve(self._end_recording())
            elif cmd.kind == "start":
                cmd.fail(ApiError(409, "already_recording", "a recording is already active"))
            return

        # Disk threshold reached mid-session (rpi/specs/recording.md).
        if self.store.disk_info().blocked:
            log.warning("disk threshold reached mid-session; stopping recording")
            self._end_recording()

    def _take_command(self) -> _Command | None:
        try:
            return self._commands.get_nowait()
        except queue.Empty:
            return None

    # -- transitions ------------------------------------------------------- #

    def _begin_recording(self) -> dict:
        if self.store.disk_info().blocked:
            raise ApiError(409, "disk_full", "disk threshold reached; recording blocked")

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
        self._set_state("recording")
        self.hal.led.set(RECORDING)
        log.info("recording started: %s", render_session_id(session_id))

        row = self.store.db.get_session(session_id)
        return self.store.session_detail_api(row, active_id=session_id)

    def _end_recording(self) -> dict:
        with self._state_lock:
            session_id = self._session_id
            recorder = self._recorder
        assert session_id is not None and recorder is not None

        self._set_state("finalizing")
        self.hal.capture.stop()

        min_seconds = self.config.recording.min_duration_seconds
        if recorder.captured_seconds < min_seconds:
            log.info(
                "recording %s too short (%.2fs < %ds); discarding",
                render_session_id(session_id), recorder.captured_seconds, min_seconds,
            )
            recorder.discard()
            self.store.delete_session(session_id)
            self.hal.led.set(DISCARDED)
            self._reset_recording_state()
            self._reject_pending("device busy finalizing")
            self._set_state("idle")
            return {"discarded": True, "reason": "too_short"}

        try:
            result = recorder.finalize()
            self.store.finalize_session(session_id, result.duration, result.size)
        except EncodeError as exc:
            # FR-6a: retain chunks, remove any partial m4a, return to idle green.
            log.error("finalization failed for %s: %s", render_session_id(session_id), exc)
        row = self.store.db.get_session(session_id)
        detail = self.store.session_detail_api(row, active_id=None)

        self.hal.led.set(READY)
        self._reset_recording_state()
        self._reject_pending("device busy finalizing")
        self._set_state("idle")
        log.info("recording finalized: %s", render_session_id(session_id))
        return detail

    def _reset_recording_state(self) -> None:
        with self._state_lock:
            self._session_id = None
            self._recorder = None
            self._started_monotonic = None

    def _reject_pending(self, message: str) -> None:
        """Reject any start/stop that arrived during the post-recording window."""
        while True:
            cmd = self._take_command()
            if cmd is None:
                return
            cmd.fail(ApiError(409, "busy", message))

    def _safe_shutdown(self) -> None:
        log.info("safe shutdown requested (button hold while idle)")
        self.hal.led.set(SHUTTING_DOWN)
        try:
            self._shutdown_fn()
        except Exception:  # pragma: no cover - shutdown is best-effort
            log.exception("shutdown failed")
