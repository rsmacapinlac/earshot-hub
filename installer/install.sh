#!/usr/bin/env bash
# earshot-hub installer (rpi/specs/install-service.md FR-8).
#
# Full setup on a fresh Raspberry Pi OS Lite, run as the normal login user
# (privileged steps use sudo):
#
#   git clone https://github.com/rsmacapinlac/earshot-hub.git ~/earshot-hub
#   bash ~/earshot-hub/installer/install.sh
#
# A reboot is required at the end — the WM8960 sound card only appears in ALSA
# after the wm8960-soundcard overlay loads at boot.
#
# Flags:
#   --no-transcription   Skip faster-whisper + model download (service-only device).
#   --yes                Non-interactive: accept defaults, no prompts.
set -euo pipefail

# ---- identity -------------------------------------------------------------
# Derive the install identity from the non-root login user (SUDO_USER when run
# through sudo), never from root.
INSTALL_USER="${SUDO_USER:-${USER}}"
if [[ "$INSTALL_USER" == "root" || -z "$INSTALL_USER" ]]; then
  echo "Run this as your normal login user (not root); it uses sudo where needed." >&2
  exit 1
fi
INSTALL_HOME="$(getent passwd "$INSTALL_USER" | cut -d: -f6)"
INSTALL_UID="$(id -u "$INSTALL_USER")"
# install_dir is this checkout (wherever it was cloned); data_dir stays separate.
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${EARSHOT_DATA_DIR:-$INSTALL_HOME/earshot-data}"
CONFIG="$DATA_DIR/config.toml"
VENV="$INSTALL_DIR/.venv"

WITH_TRANSCRIPTION=1
SKIP_AUDIO_DRIVER=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --no-transcription) WITH_TRANSCRIPTION=0 ;;
    --no-audio-driver) SKIP_AUDIO_DRIVER=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ask() { # ask <prompt> <default>  -> echoes the answer
  local prompt="$1" default="$2" reply
  if [[ "$ASSUME_YES" == 1 ]]; then echo "$default"; return; fi
  read -r -p "$prompt [$default]: " reply </dev/tty || reply=""
  echo "${reply:-$default}"
}

say "earshot-hub installer"
echo "  install_user : $INSTALL_USER (uid $INSTALL_UID)"
echo "  install_dir  : $INSTALL_DIR   (git checkout, read-only at runtime)"
echo "  data_dir     : $DATA_DIR      (config + database + recordings)"

# ---- config.toml ----------------------------------------------------------
say "Writing config.toml"
mkdir -p "$DATA_DIR"
HAT="$(ask "Audio HAT" "respeaker")"
SERVICE_URL=""
if [[ "$ASSUME_YES" != 1 ]]; then
  echo "  Optional: a processing service on your LAN speeds transcription and enables"
  echo "  diarization. Leave blank to transcribe locally (the normal case)."
  SERVICE_URL="$(ask "Processing service URL (optional)" "")"
fi

if [[ -f "$CONFIG" ]]; then
  say "config.toml already exists — leaving it untouched ($CONFIG)"
else
  cat > "$CONFIG" <<TOML
[hardware]
hat = "$HAT"

[audio]
sample_rate = 16000
channels = 1
bit_depth = 16
alsa_pcm = "plughw:CARD=wm8960soundcard,DEV=0"

[recording]
chunk_duration_seconds = 900
min_duration_seconds = 3
encode_bitrate_kbps = 32
shutdown_hold_seconds = 3

[storage]
data_dir = "$DATA_DIR"
disk_threshold_percent = 90

[transcription]
enabled = $([[ "$WITH_TRANSCRIPTION" == 1 ]] && echo true || echo false)
model = "base.en"
threads = 2

[processing]
service_url = "$SERVICE_URL"
request_timeout_seconds = 0
max_failures = 3

[web]
enabled = true
bind_address = "0.0.0.0"
port = 8080
TOML
  echo "  wrote $CONFIG"
fi
chown -R "$INSTALL_USER":"$INSTALL_USER" "$DATA_DIR"

# ---- system packages ------------------------------------------------------
say "Installing system packages (apt)"
sudo apt-get update
sudo apt-get -y upgrade
# Required — the app, the AAC encode, and ALSA capture tooling. A failure here
# is fatal (the device cannot run without these).
sudo apt-get install -y \
  git python3 python3-venv python3-dev build-essential \
  ffmpeg alsa-utils i2c-tools dkms
# Hardware Python libs for the pi backend (GPIO17 button, APA102 SPI LEDs).
# Installed from apt — they ship there on Raspberry Pi OS, and the venv is created
# with --system-site-packages so it can import them. Best-effort: a rename on a
# future OS warns rather than aborting the whole install.
for pkg in python3-gpiozero python3-spidev python3-lgpio libgpiod3 gpiod; do
  sudo apt-get install -y "$pkg" || echo "  (optional) could not install $pkg — verify for the pi backend"
done

# ---- ReSpeaker WM8960 audio + boot overlay --------------------------------
# The WM8960 codec is driven by the in-tree kernel driver plus the RPi-official
# `wm8960-soundcard` overlay. This replaces the out-of-tree seeed-voicecard DKMS
# driver, which does not build against modern kernels (6.x+). SPI/I2C are needed
# for the APA102 LEDs and the codec control bus.
BOOT_CFG=/boot/firmware/config.txt
[[ -f "$BOOT_CFG" ]] || BOOT_CFG=/boot/config.txt
say "Ensuring boot overlay in $BOOT_CFG"
ensure_line() { grep -qxF "$1" "$BOOT_CFG" 2>/dev/null || echo "$1" | sudo tee -a "$BOOT_CFG" >/dev/null; }
ensure_line "dtparam=i2c_arm=on"
ensure_line "dtparam=i2s=on"
ensure_line "dtparam=spi=on"
if [[ "$SKIP_AUDIO_DRIVER" == 1 ]]; then
  say "Skipping the WM8960 audio overlay (--no-audio-driver) — capture will be unavailable"
else
  ensure_line "dtoverlay=wm8960-soundcard"
fi

# ---- Python venv + dependencies -------------------------------------------
say "Creating the Python venv and installing earshot"
# --system-site-packages so the venv can import the apt-provided GPIO/SPI libs
# (gpiozero, spidev, lgpio); earshot and its PyPI deps install on top.
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --upgrade pip
if [[ "$WITH_TRANSCRIPTION" == 1 ]]; then
  "$VENV/bin/pip" install -e "$INSTALL_DIR[pi,transcription]"
else
  "$VENV/bin/pip" install -e "$INSTALL_DIR[pi]"
fi
chown -R "$INSTALL_USER":"$INSTALL_USER" "$VENV"

# ---- pre-download the transcription model ---------------------------------
if [[ "$WITH_TRANSCRIPTION" == 1 ]]; then
  say "Pre-downloading the transcription model (base.en)"
  MODEL_DIR="$INSTALL_HOME/.local/share/earshot/models"
  sudo -u "$INSTALL_USER" mkdir -p "$MODEL_DIR"
  sudo -u "$INSTALL_USER" "$VENV/bin/python" - "$MODEL_DIR" <<'PY' || echo "  model download failed — retry later or set a processing service"
import sys
from faster_whisper import WhisperModel
WhisperModel("base.en", device="cpu", download_root=sys.argv[1])
print("  model ready")
PY
else
  say "Skipping transcription model (--no-transcription)"
fi

# ---- WM8960 ALC front-end (applied on boot, once the card exists) ---------
say "Installing the WM8960 ALC front-end service"
sudo install -m 0755 "$INSTALL_DIR/installer/apply-alc.sh" /usr/local/bin/earshot-apply-alc
sudo tee /etc/systemd/system/earshot-alc.service >/dev/null <<UNIT
[Unit]
Description=earshot — apply WM8960 ALC capture front-end
After=sound.target
Before=earshot.service
ConditionPathExists=/proc/asound/wm8960soundcard

[Service]
Type=oneshot
ExecStart=/usr/local/bin/earshot-apply-alc
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

# ---- systemd service ------------------------------------------------------
say "Installing the earshot.service unit"
sudo tee /etc/systemd/system/earshot.service >/dev/null <<UNIT
[Unit]
Description=Earshot — on-device conversation recorder and transcriber
After=sound.target network.target
Wants=sound.target

[Service]
Type=simple
User=$INSTALL_USER
Group=audio
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV/bin/python -m earshot
Restart=on-failure
RestartSec=10
TimeoutStartSec=90
SupplementaryGroups=gpio spi i2c audio
AmbientCapabilities=CAP_SYS_BOOT
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$DATA_DIR
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable earshot-alc.service earshot.service

say "Install complete."
cat <<DONE

  A reboot is required — the WM8960 sound card only appears in ALSA after reboot
  (the wm8960-soundcard overlay loads at boot).

    sudo reboot

  After it comes back up:
    sudo systemctl status earshot     # should be active (running)
    journalctl -u earshot -f          # follow logs
    arecord -l                        # expect card 'wm8960soundcard'

  Then browse to  http://<pi-ip>:8080/  (or http://\$(hostname).local:8080/).
  Update later with:  cd $INSTALL_DIR && git pull && bash installer/install.sh
DONE
