"""Milestone 9 — safe-shutdown wiring and the installer's config.toml.

The real power-off and the on-device systemd/driver steps are validated in
docs/ON_DEVICE_SMOKE.md; here we only check the parts that are safe off-device:
the shutdown callable is selected for the pi backend and never for the stub, and
the config.toml the installer writes parses to the documented defaults.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from earshot.config import Config
from earshot.power import poweroff, select_shutdown_fn

REPO = Path(__file__).resolve().parent.parent


def _cfg(hat: str) -> Config:
    c = Config()
    c.hardware.hat = hat
    return c


def test_shutdown_fn_only_on_pi(monkeypatch):
    monkeypatch.delenv("EARSHOT_HAL", raising=False)
    assert select_shutdown_fn(_cfg("stub")) is None          # dev machine safe
    assert select_shutdown_fn(_cfg("respeaker")) is poweroff  # real power-off


def test_env_override_forces_stub(monkeypatch):
    monkeypatch.setenv("EARSHOT_HAL", "stub")
    assert select_shutdown_fn(_cfg("respeaker")) is None


def test_installer_config_toml_parses_to_defaults(tmp_path, monkeypatch):
    """The exact config.toml the installer writes loads and matches configuration.md."""
    monkeypatch.delenv("EARSHOT_DATA_DIR", raising=False)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[hardware]\nhat = "respeaker"\n\n'
        "[audio]\nsample_rate = 16000\nchannels = 1\nbit_depth = 16\n"
        'alsa_pcm = "plughw:CARD=wm8960soundcard,DEV=0"\n\n'
        "[recording]\nchunk_duration_seconds = 900\nmin_duration_seconds = 3\n"
        "encode_bitrate_kbps = 32\nshutdown_hold_seconds = 3\n\n"
        '[storage]\ndata_dir = "~/earshot-data"\ndisk_threshold_percent = 90\n\n'
        '[transcription]\nenabled = true\nmodel = "base.en"\nthreads = 2\n\n'
        '[processing]\nservice_url = ""\npoll_interval_seconds = 5\nmax_failures = 3\n\n'
        '[web]\nenabled = true\nbind_address = "0.0.0.0"\nport = 8080\n',
        encoding="utf-8",
    )
    c = Config.load(cfg_path)
    assert c.hardware.hat == "respeaker"
    assert c.audio.alsa_pcm == "plughw:CARD=wm8960soundcard,DEV=0"
    assert c.recording.encode_bitrate_kbps == 32 and c.recording.shutdown_hold_seconds == 3
    assert c.transcription.model == "base.en"
    assert c.processing.max_failures == 3 and c.web.port == 8080


def test_installer_scripts_have_valid_syntax():
    for script in ("installer/install.sh", "installer/apply-alc.sh"):
        r = subprocess.run(["bash", "-n", str(REPO / script)], capture_output=True, text=True)
        assert r.returncode == 0, f"{script}: {r.stderr}"


def test_installer_uses_wm8960_overlay_not_dkms():
    """Audio comes from the in-tree WM8960 codec + the wm8960-soundcard overlay,
    not the out-of-tree seeed-voicecard DKMS driver (which won't build on kernel 6.x)."""
    install = (REPO / "installer" / "install.sh").read_text()
    alc = (REPO / "installer" / "apply-alc.sh").read_text()
    assert "dtoverlay=wm8960-soundcard" in install
    assert "seeed-voicecard.git" not in install          # no out-of-tree build
    assert "card=wm8960soundcard" in alc
    assert "Input Boost Mixer" in alc                     # enables the muted input path


def test_installer_renders_hardened_unit_fields():
    """The unit template carries the contract's hardening/capability fields."""
    text = (REPO / "installer" / "install.sh").read_text()
    for needle in (
        "AmbientCapabilities=CAP_SYS_BOOT",
        "SupplementaryGroups=gpio spi i2c audio",
        "ReadWritePaths=$DATA_DIR",
        "NoNewPrivileges=true",
        "ProtectSystem=full",
        "Restart=on-failure",
        "ExecStart=$VENV/bin/python -m earshot",
        "Group=audio",
    ):
        assert needle in text, f"unit missing {needle!r}"
