"""Audio capture pipeline: chunked WAV -> single session.m4a.

Capture is lossless PCM WAV chunks for in-session crash resilience; at session end
they are concatenated and encoded in one ffmpeg pass into ``session.m4a`` and then
deleted (rpi/specs/recording.md, rpi/adr/audio-storage-format.md).
"""

from earshot.recording.wav import ChunkWriter, wav_frame_count
from earshot.recording.encode import (
    EncodeError,
    encode_session,
    probe_duration,
)
from earshot.recording.recorder import FinalizeResult, Recorder

__all__ = [
    "ChunkWriter",
    "wav_frame_count",
    "EncodeError",
    "encode_session",
    "probe_duration",
    "FinalizeResult",
    "Recorder",
]
