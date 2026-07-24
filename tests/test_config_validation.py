"""Milestone 10 — config.toml validation with clear errors (configuration.md)."""

from __future__ import annotations

import copy

import pytest

from earshot.config import Config, ConfigError

BASE = {
    "hardware": {"hat": "respeaker"},
    "audio": {"sample_rate": 16000, "channels": 1, "bit_depth": 16, "alsa_pcm": "plughw:CARD=x,DEV=0"},
    "recording": {"chunk_duration_seconds": 900, "min_duration_seconds": 3,
                  "encode_bitrate_kbps": 32, "shutdown_hold_seconds": 3},
    "storage": {"data_dir": "~/earshot-data", "disk_threshold_percent": 90},
    "transcription": {"enabled": True, "model": "base.en", "threads": 2},
    "processing": {"service_url": "", "poll_interval_seconds": 5, "max_failures": 3},
    "web": {"enabled": True, "bind_address": "0.0.0.0", "port": 8080},
}


def _with(section, **over):
    raw = copy.deepcopy(BASE)
    raw[section].update(over)
    return raw


def test_base_is_valid():
    Config.from_dict(copy.deepcopy(BASE))  # no raise


@pytest.mark.parametrize("raw, needle", [
    (_with("hardware", hat="bogus"), "hardware.hat"),
    (_with("audio", sample_rate=0), "audio.sample_rate"),
    (_with("audio", bit_depth=24), "audio.bit_depth"),
    (_with("audio", alsa_pcm=""), "audio.alsa_pcm"),
    (_with("recording", chunk_duration_seconds=0), "recording.chunk_duration_seconds"),
    (_with("recording", min_duration_seconds=-1), "recording.min_duration_seconds"),
    (_with("recording", encode_bitrate_kbps=0), "recording.encode_bitrate_kbps"),
    (_with("recording", shutdown_hold_seconds=0), "recording.shutdown_hold_seconds"),
    (_with("storage", data_dir=""), "storage.data_dir"),
    (_with("storage", disk_threshold_percent=0), "storage.disk_threshold_percent"),
    (_with("storage", disk_threshold_percent=101), "storage.disk_threshold_percent"),
    (_with("transcription", model=""), "transcription.model"),
    (_with("transcription", threads=0), "transcription.threads"),
    (_with("processing", service_url="ftp://x"), "processing.service_url"),
    (_with("processing", poll_interval_seconds=0), "processing.poll_interval_seconds"),
    (_with("processing", max_failures=-1), "processing.max_failures"),
    (_with("web", bind_address=""), "web.bind_address"),
    (_with("web", port=0), "web.port"),
    (_with("web", port=70000), "web.port"),
])
def test_invalid_value_rejected_with_named_key(raw, needle):
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(raw)
    assert needle in str(exc.value)


def test_valid_service_url_accepted():
    Config.from_dict(_with("processing", service_url="http://homelab.local:9000"))
    Config.from_dict(_with("processing", service_url="https://svc:9000"))


def test_max_failures_zero_allowed():
    Config.from_dict(_with("processing", max_failures=0))  # 0 = retry forever


def test_stub_hat_allowed():
    Config.from_dict(_with("hardware", hat="stub"))


def test_unknown_section_rejected():
    raw = copy.deepcopy(BASE)
    raw["encoding"] = {"bitrate": 32}  # a superseded/stale section
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(raw)
    assert "[encoding]" in str(exc.value)


def test_load_error_names_the_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[web]\nport = 70000\n', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        Config.load(p)
    assert str(p) in str(exc.value) and "web.port" in str(exc.value)
