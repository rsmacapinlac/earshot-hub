"""Chunked WAV writing and tolerant reading (rpi/adr/chunked-recording.md).

Audio is written to sequentially numbered mono 16-bit PCM WAV chunks. A chunk rolls
over when its elapsed audio reaches ``chunk_duration_seconds`` — timer-driven only,
without interrupting capture. Chunks exist for crash resilience (max loss = one
chunk); they are concatenated and encoded at session end, then deleted.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

from earshot.hal.protocols import CaptureSpec
from earshot.storage.paths import chunk_name

_WAV_HEADER_BYTES = 44
# Byte offsets into the canonical 44-byte PCM WAV header the ChunkWriter emits.
_RIFF_SIZE_OFFSET = 4     # "<L": 36 + data_bytes
_DATA_MARK_OFFSET = 36    # b"data"
_DATA_SIZE_OFFSET = 40    # "<L": data_bytes


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


def repair_wav_header(path: Path, spec: CaptureSpec) -> int:
    """Rewrite a chunk's RIFF/data size fields from the actual file size.

    The :class:`ChunkWriter` patches these lengths in ``close()``. A chunk whose
    session crashed before ``close()`` carries stale lengths that describe only
    the first written block, so ffmpeg — which trusts the header — would read
    just those bytes and lose the rest of the chunk. Recompute both length fields
    from the file size, frame-aligned, so the whole chunk is read
    (rpi/specs/storage.md#crash-recovery).

    Returns the frame count the header now describes: ``0`` if the file is too
    small, header-only, or not the canonical PCM WAV the ChunkWriter emits (in
    which case it is left untouched and should be skipped).
    """
    path = Path(path)
    size = path.stat().st_size
    if size < _WAV_HEADER_BYTES:
        return 0  # torn before a full header — nothing usable
    data_bytes = size - _WAV_HEADER_BYTES
    data_bytes -= data_bytes % spec.frame_bytes  # drop a torn trailing frame
    if data_bytes <= 0:
        return 0  # header only, no audio
    with open(path, "r+b") as f:
        if f.read(4) != b"RIFF":
            return 0  # not a RIFF/WAV file; do not touch it
        f.seek(_DATA_MARK_OFFSET)
        if f.read(4) != b"data":
            return 0  # non-canonical layout; leave it for a human
        f.seek(_RIFF_SIZE_OFFSET)
        f.write(struct.pack("<L", 36 + data_bytes))
        f.seek(_DATA_SIZE_OFFSET)
        f.write(struct.pack("<L", data_bytes))
    return data_bytes // spec.frame_bytes


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
