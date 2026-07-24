"""Real ReSpeaker 2-Mic HAT backend (rpi/reference/respeaker-2mic-hat.md).

**On-device only.** Every hardware dependency (``gpiozero``, ``spidev``, ``arecord``)
is imported lazily inside ``start()``/``__init__`` bodies, so importing this module
off-device is safe; constructing and starting the backend is not. These paths are
validated on the Pi — see docs/ON_DEVICE_SMOKE.md — never faked as passing here.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time

from earshot.hal.led_states import LedPattern, LedState
from earshot.hal.protocols import ButtonEvent, CaptureSpec

log = logging.getLogger("earshot.hal.pi")

BUTTON_GPIO = 17
LED_COUNT = 3


class PiButton:
    """GPIO17 tactile button (active-low) via gpiozero hold detection."""

    def __init__(self, hold_seconds: float = 3.0) -> None:
        self._hold_seconds = hold_seconds
        self._events: "queue.Queue[ButtonEvent]" = queue.Queue()
        self._button = None
        self._held = False

    def start(self) -> None:
        from gpiozero import Button  # on-device dependency

        self._button = Button(BUTTON_GPIO, pull_up=True, hold_time=self._hold_seconds)
        self._button.when_held = self._on_held
        self._button.when_released = self._on_released

    def _on_held(self) -> None:
        self._held = True
        self._events.put(ButtonEvent.HOLD)

    def _on_released(self) -> None:
        # A completed hold already emitted HOLD; swallow its release so it does
        # not also register as a PRESS (rpi/specs/state-machine.md).
        if self._held:
            self._held = False
            return
        self._events.put(ButtonEvent.PRESS)

    def poll_event(self, timeout: float | None = None) -> ButtonEvent | None:
        try:
            if timeout == 0:
                return self._events.get_nowait()
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._button is not None:
            self._button.close()
            self._button = None


class PiLED:
    """APA102 LED chain over SPI, with a background pattern animator.

    Only LED index 0 is driven in v1 (full RGB + patterns); the chain is 3 long.
    """

    def __init__(self, brightness: float = 0.4) -> None:
        self._brightness = brightness
        self._spi = None
        self._current: LedState | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import spidev  # on-device dependency

        self._spi = spidev.SpiDev()
        self._spi.open(0, 0)
        self._spi.max_speed_hz = 8_000_000
        self._stop.clear()
        self._thread = threading.Thread(target=self._animate, name="earshot-led", daemon=True)
        self._thread.start()

    def set(self, state: LedState) -> None:
        with self._lock:
            self._current = state

    @property
    def current(self) -> LedState | None:
        return self._current

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._spi is not None:
            self._write_rgb((0, 0, 0))
            self._spi.close()
            self._spi = None

    # -- animation --------------------------------------------------------- #

    def _animate(self) -> None:
        phase = 0.0
        while not self._stop.is_set():
            with self._lock:
                state = self._current
            if state is None:
                self._write_rgb((0, 0, 0))
                time.sleep(0.05)
                continue
            level = self._level_for(state.pattern, phase)
            r, g, b = state.rgb
            self._write_rgb((int(r * level), int(g * level), int(b * level)))
            phase += 0.05
            time.sleep(0.05)

    @staticmethod
    def _level_for(pattern: LedPattern, phase: float) -> float:
        import math

        if pattern is LedPattern.SOLID:
            return 1.0
        if pattern is LedPattern.SLOW_PULSE:
            return 0.5 + 0.5 * math.sin(phase * (2 * math.pi / 1.0))
        if pattern is LedPattern.VERY_SLOW_PULSE:
            return 0.5 + 0.5 * math.sin(phase * (2 * math.pi / 1.75))
        if pattern is LedPattern.DOUBLE_FLASH:
            frac = phase % 1.0
            return 1.0 if frac < 0.1 or 0.2 <= frac < 0.3 else 0.0
        if pattern is LedPattern.FADE_TO_OFF:
            return max(0.0, 1.0 - phase / 2.0)
        return 1.0

    def _write_rgb(self, rgb: tuple[int, int, int]) -> None:
        if self._spi is None:
            return
        r, g, b = rgb
        bright = 0b11100000 | int(self._brightness * 31)
        led0 = [bright, b, g, r]
        off = [0b11100000, 0, 0, 0]
        frame = [0, 0, 0, 0] + led0 + off * (LED_COUNT - 1) + [0xFF, 0xFF, 0xFF, 0xFF]
        self._spi.xfer2(frame)


class PiAudioCapture:
    """WM8960 capture via ``arecord`` reading raw little-endian 16-bit PCM.

    Opens ``audio.alsa_pcm`` (``plughw:`` handles rate/format conversion) and reads
    raw frames from arecord's stdout. WAV/m4a handling is elsewhere.
    """

    def __init__(self, alsa_pcm: str, spec: CaptureSpec | None = None) -> None:
        self.spec = spec or CaptureSpec()
        self._alsa_pcm = alsa_pcm
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        fmt = {8: "S8", 16: "S16_LE", 24: "S24_LE", 32: "S32_LE"}[self.spec.sample_width * 8]
        cmd = [
            "arecord",
            "-D", self._alsa_pcm,
            "-t", "raw",
            "-f", fmt,
            "-r", str(self.spec.sample_rate),
            "-c", str(self.spec.channels),
            "-q",
        ]
        log.info("starting capture: %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self, block_frames: int | None = None) -> bytes:
        if self._proc is None or self._proc.stdout is None:
            return b""
        n = (block_frames or self.spec.block_frames) * self.spec.frame_bytes
        return self._proc.stdout.read(n)

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
