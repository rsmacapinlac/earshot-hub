"""Data-directory layout and the ``rec-NNNNNN`` rendering (rpi/specs/storage.md).

Padding is presentation only; the database holds the integer. Nothing parses the
directory name back into an integer for identity purposes, but a tolerant parser
is provided for reconciliation (adopting directories on the disk).
"""

from __future__ import annotations

import re
from pathlib import Path

SESSION_ID_RE = re.compile(r"^rec-(\d{6,})$")
_PAD = 6


def render_session_id(n: int) -> str:
    """Render an integer id as ``rec-`` plus at least six zero-padded digits."""
    if n < 0:
        raise ValueError(f"session id must be non-negative, got {n}")
    return f"rec-{n:0{_PAD}d}"


def parse_session_id(value: str) -> int | None:
    """Parse ``rec-NNNNNN`` back to an integer, or None if it doesn't match.

    Used only by reconciliation to adopt on-disk directories; identity is the DB id.
    """
    m = SESSION_ID_RE.match(value)
    return int(m.group(1)) if m else None


def session_dirname(n: int) -> str:
    return render_session_id(n)


def session_dir(recordings_dir: Path, n: int) -> Path:
    return recordings_dir / render_session_id(n)


def chunk_name(index: int) -> str:
    """Sequential chunk filename, 1-based: ``recording-001.wav``."""
    if index < 1:
        raise ValueError("chunk index is 1-based")
    return f"recording-{index:03d}.wav"


def m4a_name() -> str:
    return "session.m4a"
