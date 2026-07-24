"""Milestone 2 — the HAL stub is faithful and the whole app can run on it."""

from __future__ import annotations

import struct
import wave

import pytest

from earshot.config import Config
from earshot.hal import build_hal
from earshot.hal.led_states import READY, RECORDING, LedPattern, led_state_for
from earshot.hal.protocols import (
    AudioCaptureInterface,
    ButtonEvent,
    ButtonInterface,
    CaptureSpec,
    LEDInterface,
)
from earshot.hal.stub import StubButton, StubCapture, StubLED


# -- backend selection ------------------------------------------------------ #

def test_stub_backend_selected_by_env(monkeypatch):
    monkeypatch.setenv("EARSHOT_HAL", "stub")
    hal = build_hal(Config(), realtime=False)
    assert hal.name == "stub"
    assert isinstance(hal.button, ButtonInterface)
    assert isinstance(hal.led, LEDInterface)
    assert isinstance(hal.capture, AudioCaptureInterface)


def test_respeaker_hat_maps_to_pi_backend(monkeypatch):
    monkeypatch.delenv("EARSHOT_HAL", raising=False)
    from earshot.hal.bundle import backend_name

    assert backend_name(Config()) == "pi"  # default hat = respeaker


def test_stub_hat_maps_to_stub_backend(monkeypatch):
    monkeypatch.delenv("EARSHOT_HAL", raising=False)
    from earshot.hal.bundle import backend_name

    cfg = Config()
    cfg.hardware.hat = "stub"
    assert backend_name(cfg) == "stub"


def test_bad_hal_override_rejected():
    from earshot.hal.bundle import backend_name

    with pytest.raises(ValueError):
        backend_name(Config(), override="bogus")


# -- button ----------------------------------------------------------------- #

def test_stub_button_delivers_press_and_hold_in_order():
    btn = StubButton()
    btn.start()
    btn.press()
    btn.hold()
    assert btn.poll_event(timeout=0) is ButtonEvent.PRESS
    assert btn.poll_event(timeout=0) is ButtonEvent.HOLD
    assert btn.poll_event(timeout=0) is None


# -- LED -------------------------------------------------------------------- #

def test_stub_led_records_transitions_and_dedupes():
    led = StubLED()
    led.set(READY)
    led.set(READY)  # duplicate ignored
    led.set(RECORDING)
    assert [s.name for s in led.history] == ["ready", "recording"]
    assert led.current is RECORDING


def test_led_state_for_device_states():
    assert led_state_for("idle") is READY
    assert led_state_for("recording").pattern is LedPattern.SLOW_PULSE
    assert led_state_for("processing").pattern is LedPattern.VERY_SLOW_PULSE
    with pytest.raises(KeyError):
        led_state_for("nonsense")


# -- capture ---------------------------------------------------------------- #

def test_stub_capture_emits_valid_pcm_blocks():
    spec = CaptureSpec()
    cap = StubCapture(spec, realtime=False)
    cap.start()
    block = cap.read()
    assert len(block) == spec.block_bytes
    # decodes as little-endian signed 16-bit without error
    samples = struct.unpack("<%dh" % spec.block_frames, block)
    assert len(samples) == spec.block_frames
    cap.stop()
    assert cap.read() == b""  # exhausted once stopped


def test_stub_capture_frames_form_a_playable_wav(tmp_path):
    spec = CaptureSpec()
    cap = StubCapture(spec, realtime=False)
    cap.start()
    pcm = b"".join(cap.read() for _ in range(20))
    cap.stop()

    path = tmp_path / "chunk.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(spec.channels)
        wf.setsampwidth(spec.sample_width)
        wf.setframerate(spec.sample_rate)
        wf.writeframes(pcm)

    with wave.open(str(path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() == 20 * spec.block_frames
