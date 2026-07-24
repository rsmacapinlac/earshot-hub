"""End-of-session encode: concat WAV chunks -> single session.m4a (rpi/specs/recording.md).

One ffmpeg pass over the ordered chunk list (concat demuxer -> AAC-LC), so no
intermediate full-length WAV is written. Container and codec are fixed
(rpi/adr/audio-storage-format.md); only the bitrate is configurable.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class EncodeError(RuntimeError):
    """Raised when the ffmpeg concat/encode pass fails."""


def encode_session(
    chunk_paths: list[Path],
    out_path: Path,
    *,
    bitrate_kbps: int,
    ffmpeg: str = "ffmpeg",
) -> None:
    """Concatenate *chunk_paths* and encode to AAC-LC m4a at *out_path*.

    Raises :class:`EncodeError` on failure, leaving no partial output.
    """
    if not chunk_paths:
        raise EncodeError("no chunks to encode")
    out_path = Path(out_path)

    # concat demuxer list file; absolute paths + -safe 0.
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, dir=out_path.parent) as lf:
        for p in chunk_paths:
            abspath = str(Path(p).resolve()).replace("'", "'\\''")
            lf.write(f"file '{abspath}'\n")
        list_path = Path(lf.name)

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c:a", "aac", "-b:a", f"{bitrate_kbps}k", "-ac", "1",
        "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        list_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        out_path.unlink(missing_ok=True)  # no partial artifact (FR-6a)
        raise EncodeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()}")


def cut_sample(
    src: Path, *, start: float, duration: float,
    bitrate_kbps: int = 32, ffmpeg: str = "ffmpeg",
) -> bytes:
    """Extract ``[start, start+duration]`` of *src* as a small mono m4a, returned as
    bytes. Used for speaker voice samples (rpi/specs/api.md)."""
    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tf:
        out = Path(tf.name)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
        "-c:a", "aac", "-b:a", f"{bitrate_kbps}k", "-ac", "1",
        "-movflags", "+faststart", str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise EncodeError(f"ffmpeg sample failed ({proc.returncode}): {proc.stderr.strip()}")
        return out.read_bytes()
    finally:
        out.unlink(missing_ok=True)


def probe_duration(path: Path, *, ffprobe: str = "ffprobe") -> float:
    """Return the media duration in seconds, derived from the file itself."""
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise EncodeError(f"ffprobe failed ({proc.returncode}): {proc.stderr.strip()}")
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise EncodeError(f"ffprobe returned no duration: {proc.stdout!r}") from exc
