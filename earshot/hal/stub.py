"""Stub HAL — the whole app runs off-device against this, no Pi required.

- :class:`StubButton` is triggerable from tests/dev (``press()`` / ``hold()``).
- :class:`StubLED` logs every transition and keeps history for assertions.
- :class:`StubCapture` emits real little-endian 16-bit PCM frames (a quiet tone),
  optionally paced to wall-clock so elapsed/duration are meaningful when running live.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time

from earshot.hal.led_states import LedState
from earshot.hal.protocols import ButtonEvent, CaptureSpec

log = logging.getLogger("earshot.hal.stub")


class StubButton:
    """A fake button whose gestures are injected by test/dev code."""

    def __init__(self) -> None:
        self._events: "queue.Queue[ButtonEvent]" = queue.Queue()
        self._started = False

    def start(self) -> None:
        self._started = True

    def poll_event(self, timeout: float | None = None) -> ButtonEvent | None:
        try:
            if timeout == 0:
                return self._events.get_nowait()
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._started = False

    # -- test/dev triggers -------------------------------------------------- #

    def press(self) -> None:
        """Inject a short press (record/stop toggle)."""
        self._events.put(ButtonEvent.PRESS)

    def hold(self) -> None:
        """Inject a hold (>= shutdown_hold_seconds), e.g. safe shutdown."""
        self._events.put(ButtonEvent.HOLD)


class StubLED:
    """A fake LED that records the sequence of states it was set to."""

    def __init__(self) -> None:
        self._current: LedState | None = None
        self.history: list[LedState] = []

    def set(self, state: LedState) -> None:
        if state == self._current:
            return
        self._current = state
        self.history.append(state)
        log.info("LED -> %s rgb=%s pattern=%s", state.name, state.rgb, state.pattern.value)

    @property
    def current(self) -> LedState | None:
        return self._current

    def stop(self) -> None:
        self._current = None
        log.info("LED off")


class StubCapture:
    """A fake capture source producing valid PCM at the configured spec.

    Generates a low-amplitude sine so downstream WAV/encode/transcribe paths get
    real, non-empty audio. With ``realtime=True`` each ``read`` sleeps for the
    block's wall-clock duration, so a live run's elapsed time is realistic; tests
    use ``realtime=False`` and read as fast as possible.
    """

    def __init__(
        self,
        spec: CaptureSpec | None = None,
        *,
        realtime: bool = False,
        frequency_hz: float = 220.0,
        amplitude: float = 0.05,
    ) -> None:
        self.spec = spec or CaptureSpec()
        self.realtime = realtime
        self._frequency = frequency_hz
        self._amplitude = amplitude
        self._phase = 0  # absolute frame index, for a continuous waveform
        self._open = False
        self._lock = threading.Lock()

    def start(self) -> None:
        self._open = True
        self._phase = 0

    def read(self, block_frames: int | None = None) -> bytes:
        if not self._open:
            return b""
        n = block_frames or self.spec.block_frames
        with self._lock:
            start = self._phase
            self._phase += n
        buf = bytearray(n * self.spec.frame_bytes)
        sr = self.spec.sample_rate
        amp = int(self._amplitude * 32767)
        two_pi_f = 2.0 * math.pi * self._frequency
        for i in range(n):
            t = (start + i) / sr
            sample = int(amp * math.sin(two_pi_f * t))
            # little-endian signed 16-bit
            buf[2 * i] = sample & 0xFF
            buf[2 * i + 1] = (sample >> 8) & 0xFF
        if self.realtime:
            time.sleep(n / sr)
        return bytes(buf)

    def stop(self) -> None:
        self._open = False
