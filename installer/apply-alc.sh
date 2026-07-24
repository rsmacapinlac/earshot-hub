#!/usr/bin/env bash
# Apply the WM8960 ALC speech preset to the ReSpeaker card and persist it
# (rpi/specs/recording.md#capture-front-end-wm8960-alc). Run at boot by
# earshot-alc.service, once the seeed2micvoicec card exists. Idempotent.
#
# ALC Max Gain = 5 is the v1 implementation value (provisional; ship 5 unless a
# documented experiment updates the spec).
set -euo pipefail

card=seeed2micvoicec
if ! amixer -c "$card" info >/dev/null 2>&1; then
  echo "earshot-apply-alc: card '$card' not present; skipping" >&2
  exit 0
fi

set_ctl() { amixer -c "$card" sset "$1" "$2" >/dev/null || echo "  warn: could not set '$1'=$2" >&2; }

set_ctl 'ALC Function'  Left
set_ctl 'ALC Target'    7
set_ctl 'ALC Max Gain'  5
set_ctl 'ALC Min Gain'  0
set_ctl 'ALC Attack'    2
set_ctl 'ALC Decay'     4
set_ctl 'ALC Hold Time' 0
set_ctl 'Noise Gate'    on
set_ctl 'ADC High Pass Filter' on

# Persist so seeed-voicecard.service restores it on the next boot too.
mkdir -p /etc/voicecard
alsactl --file=/etc/voicecard/wm8960_asound.state store "$card" \
  || echo "earshot-apply-alc: alsactl store failed (non-fatal)" >&2

echo "earshot-apply-alc: WM8960 ALC speech preset applied"
