"""Config loading and validation (finalized in the config milestone)."""

from __future__ import annotations

import textwrap

import pytest

from earshot.config import Config, ConfigError


def test_defaults_match_spec():
    cfg = Config()
    assert cfg.hardware.hat == "respeaker"
    assert cfg.audio.sample_rate == 16000
    assert cfg.audio.channels == 1
    assert cfg.audio.bit_depth == 16
    assert cfg.audio.alsa_pcm == "plughw:CARD=wm8960soundcard,DEV=0"
    assert cfg.recording.chunk_duration_seconds == 900
    assert cfg.recording.min_duration_seconds == 3
    assert cfg.recording.encode_bitrate_kbps == 32
    assert cfg.recording.shutdown_hold_seconds == 3
    assert cfg.storage.disk_threshold_percent == 90
    assert cfg.transcription.model == "base.en"
    assert cfg.transcription.threads == 2
    assert cfg.processing.service_url == ""
    assert cfg.processing.poll_interval_seconds == 5
    assert cfg.processing.max_failures == 3
    assert cfg.web.bind_address == "0.0.0.0"
    assert cfg.web.port == 8080


def test_missing_file_uses_defaults(tmp_path):
    cfg = Config.load(tmp_path / "nope.toml")
    assert cfg.source_path is None
    assert cfg.web.port == 8080


def test_loads_and_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent("""
        [web]
        port = 9090
        [recording]
        encode_bitrate_kbps = 64
    """))
    cfg = Config.load(p)
    assert cfg.web.port == 9090
    assert cfg.recording.encode_bitrate_kbps == 64
    assert cfg.transcription.model == "base.en"  # untouched default


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[web]\nbogus = 1\n")
    with pytest.raises(ConfigError):
        Config.load(p)


def test_wrong_type_rejected(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[web]\nport = "eighty"\n')
    with pytest.raises(ConfigError):
        Config.load(p)


def test_bool_not_accepted_as_int(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[web]\nport = true\n")
    with pytest.raises(ConfigError):
        Config.load(p)


def test_channels_must_be_mono():
    with pytest.raises(ConfigError):
        Config.from_dict({"audio": {"channels": 2}})


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("EARSHOT_DATA_DIR", str(tmp_path))
    cfg = Config()
    assert cfg.data_dir == tmp_path.resolve()
    assert cfg.db_path == tmp_path.resolve() / "earshot.db"
