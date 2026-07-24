"""The single in-process job worker (rpi/adr/job-execution.md).

One thread drains the ``jobs`` table oldest-first. For each job it decides the
route **at dequeue** (local vs. service), then runs it. M6 implements the local
route — a cancellable faster-whisper subprocess — with the service route deferred
to M7; the routing decision and its seam are here now.

Coordination with the state machine (all device-state changes stay on the control
loop thread):

- Before running a **local** job the worker asks the controller to enter
  ``processing`` (:meth:`Controller.begin_processing`). The request is granted only
  when the device is idle, so a local job never starts during a recording.
- A recording that starts *while* a local job runs preempts it: the control loop
  calls :meth:`preempt`, which terminates the child; the job returns to ``queued``
  and is re-run after the recording ends. Cancellation is a signal, so "without
  delay" is a contract (rpi/specs/processing.md#preemption).

Retry: on failure ``attempts`` is bumped and ``last_error`` recorded; the job is
re-queued until ``processing.max_failures`` is reached (``0`` = forever).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable, Protocol

from earshot.jobs.transcribe import Cancelled, LocalTranscriber, TranscribeError
from earshot.jobs.transcript import Segment
from earshot.storage.paths import render_session_id

log = logging.getLogger("earshot.jobs")


class Transcriber(Protocol):
    def run(self, m4a_path) -> list[Segment]: ...
    def cancel(self) -> None: ...


def decide_route(service: Any, kind: str) -> str:
    """Route decided at job start (rpi/specs/processing.md). Diarize has no local
    path; transcribe goes to a reachable service if configured, else local. An
    unreachable service is not a failure — it just falls back to local."""
    if kind == "diarize":
        return "service"
    if service is not None and service.reachable():
        return "service"
    return "local"


def _now() -> str:
    return datetime.now().isoformat()


class JobWorker:
    def __init__(
        self,
        config,
        store,
        controller,
        *,
        service: Any = None,
        transcriber_factory: Callable[[], Transcriber] | None = None,
    ):
        self.config = config
        self.store = store
        self.db = store.db
        self._controller = controller
        self._service = service
        self._transcriber_factory = transcriber_factory

        self._thread = threading.Thread(target=self._run, name="earshot-job", daemon=True)
        self._stop = threading.Event()
        self._wake = threading.Event()

        # Guards the currently running local job so preempt/cancel can reach it.
        self._active_lock = threading.Lock()
        self._active: Transcriber | None = None
        self._active_job_id: int | None = None
        self._reason: str | None = None       # "preempt" | "cancel"
        self._pending_cancel = False          # cancel arrived before the child started

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        # Terminate any running child so shutdown is prompt.
        with self._active_lock:
            if self._active is not None:
                self._active.cancel()
        self._thread.join(timeout=5)

    def wake(self) -> None:
        """Nudge the worker after an enqueue so it doesn't wait out its poll."""
        self._wake.set()

    # -- preemption / cancellation (called from the control loop / API) ---- #

    def preempt(self) -> None:
        """Recording is starting: terminate the running local job (loop thread)."""
        with self._active_lock:
            self._reason = "preempt"
            if self._active is not None:
                self._active.cancel()
            else:
                self._pending_cancel = True

    def cancel_running(self, job_id: int) -> bool:
        """Cancel a specific running local job (API DELETE). False if it isn't running."""
        with self._active_lock:
            if self._active_job_id != job_id:
                return False
            self._reason = "cancel"
            if self._active is not None:
                self._active.cancel()
            else:
                self._pending_cancel = True
            return True

    # -- the loop ---------------------------------------------------------- #

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self.db.peek_next_job()
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            route = decide_route(self._service, job["kind"])
            if route == "local":
                self._process_local(job)
            else:
                self._process_service(job)

    def _process_service(self, job) -> None:  # pragma: no cover - M7
        # No service client in M6; a diarize/service job cannot run yet.
        self.db.mark_job_failed(
            int(job["id"]), int(job["attempts"]) + 1,
            "processing service route not available", _now(),
        )
        log.error("job %d needs a processing service (M7)", int(job["id"]))

    def _process_local(self, job) -> None:
        job_id = int(job["id"])
        session_id = int(job["session_id"])
        snapshot = {
            "session_id": render_session_id(session_id),
            "kind": job["kind"], "route": "local", "stage": "transcribing",
        }

        if not self._controller.begin_processing(snapshot):
            # Device is busy (recording/finalizing): retry shortly.
            self._stop.wait(0.1)
            return
        if not self.db.mark_job_running(job_id, "local", _now()):
            self._controller.end_processing()  # cancelled from queued underneath us
            return

        transcriber = self._new_transcriber()
        with self._active_lock:
            self._active_job_id = job_id
            self._active = transcriber
            if self._pending_cancel:
                self._pending_cancel = False
                transcriber.cancel()  # cancel/preempt arrived during setup

        segments: list[Segment] | None = None
        error: str | None = None
        try:
            segments = transcriber.run(self.store.m4a_path(session_id))
        except Cancelled:
            reason = self._reason or "preempt"
        except TranscribeError as exc:
            error = str(exc)
            reason = None
        else:
            reason = None
        finally:
            with self._active_lock:
                self._active = None
                self._active_job_id = None
                self._reason = None
                self._pending_cancel = False

        self._resolve(job, segments, error, reason)

    def _resolve(self, job, segments, error, reason) -> None:
        job_id = int(job["id"])
        session_id = int(job["session_id"])
        now = _now()

        if reason == "preempt":
            # Not a failure: back to the queue, re-run after recording. The control
            # loop is already moving to `recording`, so we do not touch device state.
            self.db.requeue_job(job_id)
            log.info("job %d preempted by recording; requeued", job_id)
            return
        if reason == "cancel":
            self.db.set_job_cancelled(job_id, now)
            self._controller.end_processing()
            log.info("job %d cancelled", job_id)
            return
        if error is not None:
            self._handle_failure(job, error, now)
            self._controller.end_processing()
            return

        # Success.
        self.store.write_transcript_result(session_id, segments or [])
        self.db.mark_job_done(job_id, now)
        self._controller.end_processing()
        log.info("job %d done: %s (%d segments)",
                 job_id, render_session_id(session_id), len(segments or []))

    def _handle_failure(self, job, error: str, now: str) -> None:
        job_id = int(job["id"])
        attempts = int(job["attempts"]) + 1
        max_failures = self.config.processing.max_failures
        if max_failures > 0 and attempts >= max_failures:
            self.db.mark_job_failed(job_id, attempts, error, now)
            log.error("job %d failed permanently after %d attempt(s): %s", job_id, attempts, error)
        else:
            self.db.requeue_job(job_id, attempts=attempts, last_error=error)
            log.warning("job %d failed (attempt %d); requeued: %s", job_id, attempts, error)

    def _new_transcriber(self) -> Transcriber:
        if self._transcriber_factory is not None:
            return self._transcriber_factory()
        return LocalTranscriber(
            model=self.config.transcription.model,
            threads=self.config.transcription.threads,
        )
