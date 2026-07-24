"""Per-session recording: own the chunk writer, then finalize to session.m4a.

The control loop feeds PCM blocks in (it stays single-threaded, checking for a stop
between blocks — rpi/specs/state-machine.md). The recorder handles chunk rollover
and the end-of-session encode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from earshot.hal.protocols import CaptureSpec
from earshot.recording.encode import encode_session, probe_duration
from earshot.recording.wav import ChunkWriter
from earshot.storage.paths import m4a_name

log = logging.getLogger("earshot.recording")


@dataclass
class FinalizeResult:
    m4a_path: Path
    duration: float
    size: int


class Recorder:
    def __init__(
        self,
        session_dir: Path,
        spec: CaptureSpec,
        *,
        chunk_duration_seconds: int,
        encode_bitrate_kbps: int,
    ):
        self._dir = Path(session_dir)
        self._spec = spec
        self._bitrate = encode_bitrate_kbps
        self._writer = ChunkWriter(self._dir, spec, chunk_duration_seconds)

    def open(self) -> None:
        # ChunkWriter opens its first chunk lazily on the first write.
        pass

    def write(self, pcm: bytes) -> None:
        self._writer.write(pcm)

    @property
    def captured_seconds(self) -> float:
        return self._writer.total_frames / self._spec.sample_rate

    def discard(self) -> None:
        """Abandon a too-short session: close and delete its chunks."""
        self._writer.close()
        for p in self._writer.chunk_paths:
            Path(p).unlink(missing_ok=True)

    def finalize(self) -> FinalizeResult:
        """Concatenate + encode to session.m4a, then delete the chunks.

        Raises :class:`earshot.recording.encode.EncodeError` on failure; callers
        retain the chunks for next-boot recovery (FR-6a).
        """
        self._writer.close()
        out = self._dir / m4a_name()
        encode_session(self._writer.chunk_paths, out, bitrate_kbps=self._bitrate)
        duration = probe_duration(out)
        size = out.stat().st_size
        for p in self._writer.chunk_paths:
            Path(p).unlink(missing_ok=True)
        log.info("finalized %s: %.2fs, %d bytes", out.name, duration, size)
        return FinalizeResult(m4a_path=out, duration=duration, size=size)
