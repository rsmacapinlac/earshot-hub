"""Subprocess entry point for a local faster-whisper transcription.

Spawned by :class:`earshot.jobs.transcribe.LocalTranscriber` (rpi/adr/job-
execution.md): a child loads the model, transcribes ``session.m4a``, and writes
the segments to stdout as JSON. Isolation is the point — an OOM kill takes this
child, not the recorder, and cancellation is termination of this process.

Kept dependency-light and importable off-device; ``faster_whisper`` is imported
lazily inside :func:`main` so the parent module imports without the extra.

Usage: ``python -m earshot.jobs._whisper_child <m4a> <model> <threads> <download_root>``
stdout (on success): ``{"segments": [{"start", "end", "text"}, ...]}``
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: _whisper_child <m4a> <model> <threads> <download_root>", file=sys.stderr)
        return 2
    m4a, model, threads_s, download_root = argv
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - requires the optional extra
        print(f"faster-whisper not installed: {exc}", file=sys.stderr)
        return 3

    try:
        wm = WhisperModel(
            model, device="cpu", download_root=download_root, cpu_threads=int(threads_s)
        )
        segments, _info = wm.transcribe(m4a)
        out = [
            {"start": float(s.start), "end": float(s.end), "text": s.text}
            for s in segments  # lazy: iterated as decoding proceeds
        ]
    except Exception as exc:  # pragma: no cover - real-model path, on-device
        print(f"transcription failed: {exc}", file=sys.stderr)
        return 1

    json.dump({"segments": out}, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
