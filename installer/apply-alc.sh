#!/usr/bin/env bash
# Configure the WM8960 (ReSpeaker 2-Mic HAT) capture front-end and persist it.
#
# The codec is driven by the in-tree kernel driver + the wm8960-soundcard overlay
# (the out-of-tree seeed-voicecard DKMS driver does not build on modern kernels).
# That generic overlay leaves the mic **input path muted**, so this script both
# enables the input routing and applies the ALC speech preset
# (rpi/specs/recording.md#capture-front-end-wm8960-alc). Idempotent; run at boot
# by earshot-alc.service, once the card exists.
#
# ALC Max Gain = 5 is the v1 implementation value (provisional). The Capture /
# Input Boost Mixer gains are a sensible starting point — tune to your room.
set -euo pipefail

card=wm8960soundcard
if ! amixer -c "$card" info >/dev/null 2>&1; then
  echo "earshot-apply-alc: card '$card' not present; skipping" >&2
  exit 0
fi

set_ctl() { amixer -c "$card" sset "$1" "$2" >/dev/null || echo "  warn: could not set '$1'=$2" >&2; }

# Input path: mics (LINPUT1/RINPUT1) -> boost mixer -> ADC. Muted by default on
# the generic overlay, so capture is silent until these are enabled.
set_ctl 'Left Input Mixer Boost'  on
set_ctl 'Right Input Mixer Boost' on
set_ctl 'Left Boost Mixer LINPUT1'  on
set_ctl 'Right Boost Mixer RINPUT1' on
set_ctl 'Left Input Boost Mixer LINPUT1'  50%
set_ctl 'Right Input Boost Mixer RINPUT1' 50%
set_ctl 'Capture' 40
set_ctl 'ADC PCM' 195

# ALC speech preset (rpi/specs/recording.md).
set_ctl 'ALC Function'  Left
set_ctl 'ALC Target'    7
set_ctl 'ALC Max Gain'  5
set_ctl 'ALC Min Gain'  0
set_ctl 'ALC Attack'    2
set_ctl 'ALC Decay'     4
set_ctl 'ALC Hold Time' 0
set_ctl 'Noise Gate'    on
set_ctl 'ADC High Pass Filter' on

# Persist to the standard restore location (alsa-restore reloads it on boot too).
alsactl store 2>/dev/null || echo "earshot-apply-alc: alsactl store failed (non-fatal)" >&2

echo "earshot-apply-alc: WM8960 capture front-end applied"
