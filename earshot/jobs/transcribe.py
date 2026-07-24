"""Local transcription: spawn a cancellable faster-whisper subprocess.

The worker calls :meth:`LocalTranscriber.run`, which blocks in the child until it
finishes; :meth:`cancel` (from another thread) terminates the child so a new
recording preempts inference immediately — cancellation is a signal, not a
cooperative flag (rpi/adr/job-execution.md, rpi/specs/processing.md#preemption).

Tests inject a fake transcriber with the same ``run``/``cancel`` shape, so the
queue/route/retry/preemption logic is exercised without the model.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path

from earshot.jobs.transcript import Segment, segments_from_raw

log = logging.getLogger("earshot.jobs")

DEFAULT_MODEL_DIR = "~/.local/share/earshot/models"


class TranscribeError(RuntimeError):
    """The child exited non-zero (model load or inference failure)."""


class Cancelled(RuntimeError):
    """The child was terminated (preemption or an explicit cancel)."""


class LocalTranscriber:
    """Runs one transcription in a child process. Single-use per job."""

    def __init__(self, *, model: str, threads: int, download_root: str | None = None,
                 python: str | None = None):
        self._model = model
        self._threads = threads
        self._download_root = str(Path(download_root or DEFAULT_MODEL_DIR).expanduser())
        self._python = python or sys.executable
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self._lock = threading.Lock()

    def run(self, m4a_path: Path) -> list[Segment]:
        cmd = [
            self._python, "-m", "earshot.jobs._whisper_child",
            str(m4a_path), self._model, str(self._threads), self._download_root,
        ]
        with self._lock:
            if self._cancelled:
                raise Cancelled()
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = self._proc.communicate()  # blocks; cancel() terminates the child
        rc = self._proc.returncode
        if self._cancelled:
            raise Cancelled()
        if rc != 0:
            raise TranscribeError(err.decode("utf-8", "replace").strip() or f"exit {rc}")
        try:
            return segments_from_raw(json.loads(out.decode("utf-8"))["segments"])
        except (ValueError, KeyError) as exc:
            raise TranscribeError(f"unparseable child output: {exc}") from exc

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
