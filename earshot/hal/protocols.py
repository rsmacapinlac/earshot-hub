"""HAL interfaces — the contract the app and state machine depend on.

Application and state-machine code touch only these interfaces, never raw GPIO
pins, APA102 SPI framing, ALSA device details, or WAV internals
(rpi/adr/hardware-abstraction-layer.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from earshot.hal.led_states import LedState


class ButtonEvent(str, Enum):
    """A semantic button gesture. Timing/debounce is the button's concern.

    The two gestures the hardware button has (rpi/specs/state-machine.md):
    a short PRESS toggles record/stop; a HOLD (>= shutdown_hold_seconds) while
    idle triggers safe shutdown. A completed HOLD does not also emit a PRESS.
    """

    PRESS = "press"
    HOLD = "hold"


@runtime_checkable
class ButtonInterface(Protocol):
    """Press-and-hold detection behind the HAL."""

    def start(self) -> None:
        """Begin watching the button."""

    def poll_event(self, timeout: float | None = None) -> ButtonEvent | None:
        """Return the next queued gesture, or None if none arrives within *timeout*.

        Thread-safe; the control loop calls this. ``timeout=None`` blocks; ``0``
        polls without blocking.
        """

    def stop(self) -> None:
        """Stop watching and release resources."""


@runtime_checkable
class LEDInterface(Protocol):
    """Colour and pattern — the device's sole local feedback channel."""

    def start(self) -> None:
        """Acquire the LED device and begin driving it. The pi backend opens SPI
        and starts its animator here; the control loop calls this at startup."""

    def set(self, state: LedState) -> None:
        """Display *state* (rgb + pattern). Idempotent for the same state."""

    @property
    def current(self) -> LedState | None:
        """The state currently displayed, or None before the first set()."""

    def stop(self) -> None:
        """Turn the LED off and release resources."""


@dataclass(frozen=True)
class CaptureSpec:
    """The capture format (rpi/specs/recording.md#capture-spec)."""

    sample_rate: int = 16000
    channels: int = 1          # mono, left mic only
    sample_width: int = 2      # bytes; 16-bit PCM
    block_frames: int = 1024   # read block

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sample_width

    @property
    def block_bytes(self) -> int:
        return self.block_frames * self.frame_bytes


@runtime_checkable
class AudioCaptureInterface(Protocol):
    """Microphone capture — raw mono 16-bit PCM frames.

    WAV chunking and m4a encoding live in :mod:`earshot.recording`, not here; the
    capture interface only yields PCM bytes at :attr:`spec`.
    """

    spec: CaptureSpec

    def start(self) -> None:
        """Open the capture device and begin producing frames."""

    def read(self, block_frames: int | None = None) -> bytes:
        """Return one block of little-endian 16-bit PCM. Blocks until available.

        Returns ``b""`` only when the source is exhausted/closed.
        """

    def stop(self) -> None:
        """Stop capture and release the device."""
