"""Crash recovery: finalize a session interrupted before its encode completed.

Runs the **same concatenate-and-encode pass** as the end-of-session path
(rpi/specs/storage.md#crash-recovery, recording.md#fr-3--fr-6-end-of-session--
encode-to-one-m4a): repair any chunk left with an unfinalized header, encode the
ordered chunks to ``session.m4a``, then delete the chunks. If the encode fails
(no disk space, say), the chunks are left in place and the caller retries on the
next boot — this module raises rather than swallowing the error.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from earshot.hal.protocols import CaptureSpec
from earshot.recording.encode import encode_session, probe_duration
from earshot.recording.recorder import FinalizeResult
from earshot.recording.wav import repair_wav_header
from earshot.storage.paths import m4a_name

log = logging.getLogger("earshot.recording")

_CHUNK_RE = re.compile(r"^recording-(\d+)\.wav$")


def ordered_chunks(session_dir: Path) -> list[Path]:
    """The session's ``recording-NNN.wav`` chunks, ordered by index."""
    chunks: list[tuple[int, Path]] = []
    for p in session_dir.iterdir():
        m = _CHUNK_RE.match(p.name)
        if m:
            chunks.append((int(m.group(1)), p))
    return [p for _, p in sorted(chunks)]


def recover_session_audio(
    session_dir: Path, spec: CaptureSpec, *, bitrate_kbps: int
) -> FinalizeResult | None:
    """Encode a crashed session's chunks into ``session.m4a`` and delete them.

    Returns the :class:`FinalizeResult` on success, or ``None`` if there was no
    usable audio to encode (no chunks, or only header-only/torn chunks). Raises
    :class:`earshot.recording.encode.EncodeError` if the encode itself fails,
    leaving the chunks in place for the next boot.
    """
    session_dir = Path(session_dir)
    chunks = ordered_chunks(session_dir)
    if not chunks:
        return None

    usable = [c for c in chunks if repair_wav_header(c, spec) > 0]
    if not usable:
        log.warning("no usable audio in %s (%d empty/torn chunks)", session_dir.name, len(chunks))
        return None

    out = session_dir / m4a_name()
    encode_session(usable, out, bitrate_kbps=bitrate_kbps)  # raises EncodeError; leaves no partial
    duration = probe_duration(out)
    size = out.stat().st_size
    for c in chunks:
        c.unlink(missing_ok=True)
    log.info("recovered %s: %.2fs, %d bytes from %d chunk(s)",
             session_dir.name, duration, size, len(usable))
    return FinalizeResult(m4a_path=out, duration=duration, size=size)
