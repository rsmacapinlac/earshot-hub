"""Chunked WAV writing and tolerant reading (rpi/adr/chunked-recording.md).

Audio is written to sequentially numbered mono 16-bit PCM WAV chunks. A chunk rolls
over when its elapsed audio reaches ``chunk_duration_seconds`` — timer-driven only,
without interrupting capture. Chunks exist for crash resilience (max loss = one
chunk); they are concatenated and encoded at session end, then deleted.
"""

from __future__ import annotations

import wave
from pathlib import Path

from earshot.hal.protocols import CaptureSpec
from earshot.storage.paths import chunk_name

_WAV_HEADER_BYTES = 44


class ChunkWriter:
    """Writes PCM frames into rolling WAV chunks in a session directory."""

    def __init__(self, session_dir: Path, spec: CaptureSpec, chunk_duration_seconds: int):
        self._dir = Path(session_dir)
        self._spec = spec
        self._chunk_frames = max(1, int(chunk_duration_seconds * spec.sample_rate))
        self._index = 0
        self._wav: wave.Wave_write | None = None
        self._frames_in_chunk = 0
        self._total_frames = 0
        self.chunk_paths: list[Path] = []

    @property
    def total_frames(self) -> int:
        return self._total_frames

    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        if self._wav is None or self._frames_in_chunk >= self._chunk_frames:
            self._rollover()
        assert self._wav is not None
        self._wav.writeframes(pcm)
        frames = len(pcm) // self._spec.frame_bytes
        self._frames_in_chunk += frames
        self._total_frames += frames

    def _rollover(self) -> None:
        self._close_current()
        self._index += 1
        path = self._dir / chunk_name(self._index)
        wav = wave.open(str(path), "wb")
        wav.setnchannels(self._spec.channels)
        wav.setsampwidth(self._spec.sample_width)
        wav.setframerate(self._spec.sample_rate)
        self._wav = wav
        self._frames_in_chunk = 0
        self.chunk_paths.append(path)

    def _close_current(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None

    def close(self) -> None:
        self._close_current()


def wav_frame_count(path: Path, spec: CaptureSpec) -> int:
    """Frame count of a WAV, read tolerantly.

    A chunk whose header was never finalised (crash before ``close()``) reports 0
    frames in its header; fall back to deriving the count from the file size
    (rpi/specs/storage.md#crash-recovery).
    """
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            if frames > 0:
                return frames
    except (wave.Error, EOFError):
        pass
    size = path.stat().st_size
    data = max(0, size - _WAV_HEADER_BYTES)
    return data // spec.frame_bytes
