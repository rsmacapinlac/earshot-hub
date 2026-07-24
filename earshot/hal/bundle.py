"""Select and assemble a HAL backend at startup.

The backend is chosen by ``hardware.hat`` (rpi/adr/hardware-abstraction-layer.md):
``respeaker`` -> the real ``pi`` backend, ``stub`` -> the in-memory stub. The
``EARSHOT_HAL`` environment variable overrides config, so off-device development
runs ``EARSHOT_HAL=stub python -m earshot`` regardless of the configured HAT.

The real backend is never a silent fallback: on a device it must fail loudly if
hardware is missing, rather than pretend to work.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from earshot.config import Config
from earshot.hal.protocols import (
    AudioCaptureInterface,
    ButtonInterface,
    CaptureSpec,
    LEDInterface,
)

log = logging.getLogger("earshot.hal")

# hardware.hat value -> backend name.
_HAT_BACKENDS = {"respeaker": "pi", "stub": "stub"}


@dataclass
class Hal:
    button: ButtonInterface
    led: LEDInterface
    capture: AudioCaptureInterface
    name: str


def backend_name(config: Config, override: str | None = None) -> str:
    chosen = override or os.environ.get("EARSHOT_HAL")
    if chosen:
        if chosen not in {"pi", "stub"}:
            raise ValueError(f"EARSHOT_HAL must be 'pi' or 'stub', got {chosen!r}")
        return chosen
    hat = config.hardware.hat
    if hat not in _HAT_BACKENDS:
        raise ValueError(f"unknown hardware.hat {hat!r}; expected one of {sorted(_HAT_BACKENDS)}")
    return _HAT_BACKENDS[hat]


def _capture_spec(config: Config) -> CaptureSpec:
    return CaptureSpec(
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
        sample_width=config.audio.bit_depth // 8,
    )


def build_hal(config: Config, *, override: str | None = None, realtime: bool = True) -> Hal:
    """Build the HAL for *config*.

    ``realtime`` only affects the stub capture (paces reads to wall-clock); tests
    pass ``realtime=False``.
    """
    name = backend_name(config, override)
    spec = _capture_spec(config)
    if name == "stub":
        from earshot.hal.stub import StubButton, StubCapture, StubLED

        log.info("HAL backend: stub")
        return Hal(
            button=StubButton(),
            led=StubLED(),
            capture=StubCapture(spec, realtime=realtime),
            name="stub",
        )
    if name == "pi":
        from earshot.hal.pi import PiAudioCapture, PiButton, PiLED

        log.info("HAL backend: pi (ReSpeaker)")
        return Hal(
            button=PiButton(hold_seconds=config.recording.shutdown_hold_seconds),
            led=PiLED(),
            capture=PiAudioCapture(config.audio.alsa_pcm, spec),
            name="pi",
        )
    raise ValueError(f"unknown HAL backend {name!r}")
