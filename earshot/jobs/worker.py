"""The single in-process job worker (rpi/adr/job-execution.md).

One thread drains the ``jobs`` table oldest-first. For each job it decides the
route **at dequeue** (rpi/specs/processing.md#the-queue) and runs it:

- **local** — a cancellable faster-whisper subprocess (rpi/specs/processing.md#fr-15).
  The device enters ``processing`` (loop-serialised) so a local job never runs during
  a recording; a recording preempts it via :meth:`preempt`, requeuing it with no
  attempt bump.
- **service** — submit ``session.m4a`` to the synchronous off-the-shelf service and
  render the returned segments (rpi/specs/processing.md#fr-15b). A service job runs on
  another machine, so it does **not** change device state and is **not** preempted by
  recording; it only populates ``status.processing``. An unreachable service is a
  connection problem, not a failure — the job is requeued without a bump, and
  transcription falls back to local next time.

Crash resilience: a job left ``running`` is reset to ``queued`` on boot and re-run;
there is no remote job state to resume (rpi/specs/processing.md#crash-resilience).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable, Protocol

from earshot.jobs.service import ServiceJobFailed, ServiceUnreachable
from earshot.jobs.transcribe import Cancelled, LocalTranscriber, TranscribeError
from earshot.jobs.transcript import Segment, normalize_speaker_labels
from earshot.storage.paths import render_session_id

log = logging.getLogger("earshot.jobs")


class Transcriber(Protocol):
    def run(self, m4a_path) -> list[Segment]: ...
    def cancel(self) -> None: ...


class _Interrupted(RuntimeError):
    """A service poll loop was interrupted (user cancel or shutdown)."""


def decide_route(service: Any, job) -> str:
    """Route decided at job start (rpi/specs/processing.md).

    Diarize has no local path. Transcribe goes to a reachable service if configured,
    else local — an unreachable service just falls back to local, never a failure."""
    if job["kind"] == "diarize":
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

        # Guards the currently running job so preempt/cancel can reach it.
        self._active_lock = threading.Lock()
        self._active: Transcriber | None = None          # local child, if any
        self._active_cancel: threading.Event | None = None  # service poll interrupt
        self._active_job_id: int | None = None
        self._reason: str | None = None                  # "preempt" | "cancel"
        self._pending_cancel = False                     # arrived before the job started

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._active_lock:
            if self._active is not None:
                self._active.cancel()
            if self._active_cancel is not None:
                self._active_cancel.set()
        self._thread.join(timeout=5)

    def wake(self) -> None:
        """Nudge the worker after an enqueue so it doesn't wait out its poll."""
        self._wake.set()

    # -- preemption / cancellation ----------------------------------------- #

    def preempt(self) -> None:
        """Recording is starting: terminate the running local job (loop thread).

        Only ever called while the device is ``processing``, which only a local job
        causes — a service job leaves the device idle and is not preempted."""
        with self._active_lock:
            self._reason = "preempt"
            if self._active is not None:
                self._active.cancel()
            else:
                self._pending_cancel = True

    def cancel_running(self, job_id: int) -> bool:
        """Cancel a specific running job (API DELETE). False if it isn't running."""
        with self._active_lock:
            if self._active_job_id != job_id:
                return False
            self._reason = "cancel"
            if self._active is not None:
                self._active.cancel()
            if self._active_cancel is not None:
                self._active_cancel.set()
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
            if decide_route(self._service, job) == "local":
                self._process_local(job)
            else:
                self._process_service(job)

    def _claim(self, job_id: int, route: str, cancel: threading.Event | None,
               transcriber: Transcriber | None) -> bool:
        """Register the active job and claim it ``running``. False if it was
        cancelled from the queue underneath us."""
        with self._active_lock:
            self._active_job_id = job_id
            self._active = transcriber
            self._active_cancel = cancel
            self._reason = None
            if self._pending_cancel:
                self._pending_cancel = False
                if transcriber is not None:
                    transcriber.cancel()
                if cancel is not None:
                    cancel.set()
        return self.db.mark_job_running(job_id, route, _now())

    def _release(self) -> str | None:
        with self._active_lock:
            reason = self._reason
            self._active = None
            self._active_cancel = None
            self._active_job_id = None
            self._reason = None
            self._pending_cancel = False
        return reason

    # -- local route ------------------------------------------------------- #

    def _process_local(self, job) -> None:
        job_id = int(job["id"])
        session_id = int(job["session_id"])
        snapshot = {
            "session_id": render_session_id(session_id),
            "kind": job["kind"], "route": "local", "stage": "transcribing",
        }
        if not self._controller.begin_processing(snapshot):
            self._stop.wait(0.1)  # device busy (recording/finalizing): retry shortly
            return
        transcriber = self._new_transcriber()
        if not self._claim(job_id, "local", None, transcriber):
            self._release()
            self._controller.end_processing()  # cancelled from queued underneath us
            return

        segments: list[Segment] | None = None
        error: str | None = None
        try:
            segments = transcriber.run(self.store.m4a_path(session_id))
        except Cancelled:
            segments = None
        except TranscribeError as exc:
            error = str(exc)
        reason = self._release()

        if reason == "preempt":
            self.db.requeue_job(job_id)  # not a failure; loop already moving to recording
            log.info("job %d preempted by recording; requeued", job_id)
            return
        if reason == "cancel":
            self.db.set_job_cancelled(job_id, _now())
            self._controller.end_processing()
            log.info("job %d cancelled", job_id)
            return
        if error is not None:
            self._handle_failure(job, error, _now())
            self._controller.end_processing()
            return
        self.store.write_transcript_result(session_id, segments or [])
        self.db.mark_job_done(job_id, _now())
        self._controller.end_processing()
        log.info("job %d done: %s (%d segments)",
                 job_id, render_session_id(session_id), len(segments or []))

    # -- service route ----------------------------------------------------- #

    def _process_service(self, job) -> None:
        job_id = int(job["id"])
        session_id = int(job["session_id"])
        kind = job["kind"]
        client = self._service.client() if self._service is not None else None
        if client is None:
            self.db.mark_job_failed(job_id, int(job["attempts"]) + 1,
                                    "no processing service configured", _now())
            log.error("job %d needs a processing service", job_id)
            return
        if not client.reachable():
            self.db.requeue_job(job_id)
            log.warning("processing service unreachable for job %d; requeued, no attempt bump", job_id)
            self._stop.wait(1.0)
            return

        cancel = threading.Event()
        snapshot = {"session_id": render_session_id(session_id), "kind": kind,
                    "route": "service"}
        self._controller.set_processing(snapshot)
        if not self._claim(job_id, "service", cancel, None):
            self._release()
            self._controller.set_processing(None)  # cancelled from queued underneath us
            return

        try:
            segments = self._call_service(client, job, cancel)
        except _Interrupted:
            self._service_interrupted(job_id)
            return
        except (ServiceJobFailed, ServiceUnreachable) as exc:
            self._handle_failure(job, str(exc), _now())
            self._after_service()
            return

        if kind == "diarize":
            segments = normalize_speaker_labels(segments)
        self.store.write_transcript_result(session_id, segments, diarized=(kind == "diarize"))
        self.db.mark_job_done(job_id, _now())
        self._after_service()
        log.info("service job %d done: %s (%d segments)",
                 job_id, render_session_id(session_id), len(segments))

    def _call_service(self, client, job, cancel) -> list[Segment]:
        result: dict[str, Any] = {}

        def target() -> None:
            try:
                result["segments"] = client.process(
                    self.store.m4a_path(int(job["session_id"])),
                    job["kind"],
                    num_speakers=job["num_speakers"],
                )
            except BaseException as exc:  # passed back to the worker thread
                result["error"] = exc

        thread = threading.Thread(target=target, name="earshot-service-request", daemon=True)
        thread.start()
        while thread.is_alive():
            if cancel.wait(0.1):
                raise _Interrupted()
        if "error" in result:
            raise result["error"]
        return result["segments"]

    def _service_interrupted(self, job_id: int) -> None:
        reason = self._release()
        if self._stop.is_set() and reason != "cancel":
            self.db.requeue_job(job_id)
        else:
            # The stateless synchronous service cannot be cancelled; the request thread
            # may finish later and its result is discarded.
            self.db.set_job_cancelled(job_id, _now())
            log.info("service job %d cancelled", job_id)
        self._controller.set_processing(None)

    def _after_service(self) -> None:
        self._release()
        self._controller.set_processing(None)

    # -- shared ------------------------------------------------------------ #

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
