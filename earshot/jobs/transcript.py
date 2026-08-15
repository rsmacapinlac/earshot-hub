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


# -- voice samples ---------------------------------------------------------- #

# Selection bounds for a speaker's voice samples (rpi/specs/api.md
# #get-v1sessionsidspeakers). The longest turn is a bad sample: turns grow long
# through rambling or merged noise, and their opening seconds are the least
# characteristic part — so candidates are ranked toward IDEAL_SAMPLE_SEC instead.
MIN_SAMPLE_SEC = 2.0
IDEAL_SAMPLE_SEC = 4.0
MIN_DISTINCT_RATIO = 0.5
SAMPLE_SPACING_SEC = 60.0
MAX_SAMPLES = 5
MAX_CLIP_SEC = 6.0


def _distinct_word_ratio(text: str) -> float:
    """Distinct words over total words — low for transcriber stutter artifacts
    (``"if we can, if we can, if we can, …"``), which are unusable as samples."""
    words = "".join(c if c.isalnum() else " " for c in text.lower()).split()
    return len(set(words)) / len(words) if words else 0.0


def voice_samples(turns: list[Segment]) -> list[Segment]:
    """Up to ``MAX_SAMPLES`` representative turns for one speaker, start-ordered.

    A sample's index in the returned list is the ``n`` taken by the sample
    endpoint, so selection must be deterministic from the transcript alone
    (rpi/specs/api.md#get-v1sessionsidspeakers)."""
    usable = [
        t for t in turns
        if (t.end - t.start) >= MIN_SAMPLE_SEC
        and _distinct_word_ratio(t.text) >= MIN_DISTINCT_RATIO
    ]
    # Stable sort: equally-ranked turns keep transcript order, so `n` is stable.
    usable.sort(key=lambda t: abs((t.end - t.start) - IDEAL_SAMPLE_SEC))

    def take(gap: float) -> list[Segment]:
        held: list[Segment] = []
        for t in usable:
            if len(held) >= MAX_SAMPLES:
                break
            if any(abs(h.start - t.start) < gap for h in held):
                continue
            held.append(t)
        return held

    picks = take(SAMPLE_SPACING_SEC)
    if len(picks) < 2:  # relax the spread rather than return almost nothing
        picks = take(0.0)
    if not picks and turns:  # never return nothing for a label that has turns
        picks = [max(turns, key=lambda t: t.end - t.start)]
    return sorted(picks, key=lambda t: t.start)


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


def _format_occurred_at(value: str | None) -> str | None:
    """Transcript header form for the user-asserted session date/time."""
    if not value:
        return None
    # API/storage use ISO (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`); the markdown
    # header uses a space separator, matching the user-facing spec examples.
    return value.replace("T", " ", 1)


def render(
    *,
    header: str,
    session_dirname: str,
    duration: float,
    segments: list[Segment],
    occurred_at: str | None = None,
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
    ]
    occurred = _format_occurred_at(occurred_at)
    if occurred:
        lines.append(f"**Date:** {occurred}")
    lines += [
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
