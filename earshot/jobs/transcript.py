"""Render segments into ``transcript.md`` (rpi/specs/processing.md#fr-16).

The device renders the transcript whichever route produced the segments — the
service returns segments only, never rendered text, because the Pi is what knows
the session's name and format. The format is identical for local and service.

Timestamps are derived from the audio (segment start), and Duration from the
``session.m4a``; the only clock-derived field is ``Processed``, which nothing
reads back (rpi/specs/processing.md#time-independence).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None  # set only by diarization (M7)

    def api(self) -> dict:
        d = {"start": self.start, "end": self.end, "text": self.text}
        if self.speaker is not None:
            d["speaker"] = self.speaker
        return d


def segments_from_raw(raw: list[dict]) -> list[Segment]:
    return [
        Segment(
            start=float(s["start"]), end=float(s["end"]), text=s["text"],
            speaker=s.get("speaker"),
        )
        for s in raw
    ]


def normalize_speaker_labels(segments: list[Segment]) -> list[Segment]:
    """Map raw service speaker labels to ``Speaker N`` by first appearance."""
    mapping: dict[str, str] = {}
    normalized: list[Segment] = []
    for seg in segments:
        speaker = seg.speaker
        if speaker:
            speaker = mapping.setdefault(speaker, f"Speaker {len(mapping) + 1}")
        normalized.append(Segment(seg.start, seg.end, seg.text, speaker))
    return normalized


def format_offset(seconds: float) -> str:
    """``[MM:SS]`` under an hour, ``[HH:MM:SS]`` at or beyond one hour."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_duration(seconds: float) -> str:
    """``Xh Xm Xs`` from the audio duration."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def render(
    *,
    header: str,
    session_dirname: str,
    duration: float,
    segments: list[Segment],
    speaker_names: dict[str, str] | None = None,
    processed_at: datetime | None = None,
) -> str:
    """Render ``transcript.md``. *header* is the session name, or its id when unnamed.

    *speaker_names* substitutes an assigned name for a ``Speaker N`` label; a label
    with no name keeps the label (rpi/requirements/web-ui/name-speakers.md).
    """
    names = speaker_names or {}
    processed = (processed_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# {header}",
        f"**Session:** {session_dirname}",
        f"**Duration:** {format_duration(duration)}",
        f"**Processed:** {processed}",
        "",
        "---",
        "",
    ]
    for seg in segments:
        text = seg.text.strip()
        prefix = f"[{format_offset(seg.start)}]"
        if seg.speaker:
            who = names.get(seg.speaker, seg.speaker)
            lines.append(f"{prefix} {who}: {text}")
        else:
            lines.append(f"{prefix} {text}")
    return "\n".join(lines) + "\n"
