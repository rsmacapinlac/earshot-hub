"""Hardware Abstraction Layer (rpi/adr/hardware-abstraction-layer.md).

Button (GPIO17), APA102 LEDs (SPI), and WM8960 audio capture (ALSA) live behind
the interfaces in :mod:`earshot.hal.protocols`. Two backends ship: ``pi`` (real
ReSpeaker hardware) and ``stub`` (in-memory, for off-device development and tests).
The active backend is chosen by :func:`earshot.hal.bundle.build_hal`.
"""

from earshot.hal.protocols import (
    AudioCaptureInterface,
    ButtonEvent,
    ButtonInterface,
    CaptureSpec,
    LEDInterface,
)
from earshot.hal.led_states import LedPattern, LedState, LED_STATES, led_state_for
from earshot.hal.bundle import Hal, build_hal

__all__ = [
    "AudioCaptureInterface",
    "ButtonEvent",
    "ButtonInterface",
    "CaptureSpec",
    "LEDInterface",
    "LedPattern",
    "LedState",
    "LED_STATES",
    "led_state_for",
    "Hal",
    "build_hal",
]
