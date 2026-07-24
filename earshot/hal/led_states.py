"""Canonical LED states (rpi/specs/state-machine.md#led-states).

One place defines the colour + pattern for every device condition, so the real
APA102 driver, the stub, and the ``/v1/status`` LED field cannot disagree.

The status API reports a single steady-state pattern per condition. Where the
spec describes a compound animation ("snap to solid, then slow pulse";
"slow pulse -> fade to off"), the reported pattern is the steady state the LED
settles into; the animation nuance is the real driver's concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LedPattern(str, Enum):
    SOLID = "solid"
    SLOW_PULSE = "slow_pulse"           # ~1 s cycle
    VERY_SLOW_PULSE = "very_slow_pulse"  # ~1.5-2 s cycle
    DOUBLE_FLASH = "double_flash"        # sharp on/off, brief
    FADE_TO_OFF = "fade_to_off"          # slow decrease to off


@dataclass(frozen=True)
class LedState:
    name: str
    rgb: tuple[int, int, int]
    pattern: LedPattern


# The table from state-machine.md, keyed by a stable name.
BOOTING = LedState("booting", (255, 255, 255), LedPattern.SLOW_PULSE)
READY = LedState("ready", (0, 255, 0), LedPattern.SOLID)
RECORDING = LedState("recording", (255, 0, 0), LedPattern.SLOW_PULSE)
FINALIZING = LedState("finalizing", (255, 180, 0), LedPattern.SLOW_PULSE)
PROCESSING = LedState("processing", (255, 179, 0), LedPattern.VERY_SLOW_PULSE)
DISK_FULL = LedState("disk_full", (255, 128, 0), LedPattern.SLOW_PULSE)
DISCARDED = LedState("discarded", (0, 255, 0), LedPattern.DOUBLE_FLASH)
SHUTTING_DOWN = LedState("shutting_down", (255, 255, 255), LedPattern.FADE_TO_OFF)

LED_STATES: dict[str, LedState] = {
    s.name: s
    for s in (
        BOOTING, READY, RECORDING, FINALIZING, PROCESSING,
        DISK_FULL, DISCARDED, SHUTTING_DOWN,
    )
}

# Device state (rpi/specs/api.md DeviceState) -> the LED it shows.
_DEVICE_STATE_LED: dict[str, LedState] = {
    "booting": BOOTING,
    "idle": READY,
    "recording": RECORDING,
    "finalizing": FINALIZING,
    "processing": PROCESSING,
    "disk_full": DISK_FULL,
}


def led_state_for(device_state: str) -> LedState:
    """The steady LED for a device state (for ``/v1/status.led``)."""
    try:
        return _DEVICE_STATE_LED[device_state]
    except KeyError:
        raise KeyError(f"no LED mapping for device state {device_state!r}")
