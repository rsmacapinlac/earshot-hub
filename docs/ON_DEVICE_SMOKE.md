# On-device smoke tests

Behaviour that needs the real Raspberry Pi 4B + ReSpeaker 2-Mic HAT, and so is
**not** exercised by the off-device test suite. Run these after `installer/install.sh`
and a reboot. Nothing here is faked as passing in CI — the automated suite uses the
HAL stub and injected fakes; this file is the manual counterpart.

Prereqs: install complete, rebooted, `sudo systemctl status earshot` shows
`active (running)`.

---

## 1. Audio driver initialisation (reboot-dependent)

The seeed-voicecard driver only appears in ALSA after a reboot.

```sh
arecord -l                     # expect a card named 'seeed2micvoicec'
cat /proc/asound/seeed2micvoicec/pcm0c/info   # capture PCM present
```

- [ ] `seeed2micvoicec` is listed.
- [ ] `dmesg | grep -i seeed` shows the module bound (kernel taint from the
      out-of-tree module is expected).

## 2. WM8960 ALC capture front-end

`earshot-alc.service` applies the speech preset on boot.

```sh
systemctl status earshot-alc.service          # active (exited)
amixer -c seeed2micvoicec sget 'ALC Function'  # -> Left
amixer -c seeed2micvoicec sget 'ALC Max Gain'  # -> 5 (v1 value)
```

- [ ] `ALC Function = Left`, `ALC Target = 7`, `ALC Max Gain = 5`, `Noise Gate = on`.
- [ ] Settings survive a reboot (persisted to `/etc/voicecard/wm8960_asound.state`).

## 3. Live capture (WM8960, left mic, 16 kHz mono)

```sh
arecord -D plughw:CARD=seeed2micvoicec,DEV=0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/mic.wav
aplay /tmp/mic.wav             # or copy off-device to listen
```

- [ ] 5 s of intelligible speech is captured from the **left** mic.
- [ ] Level is reasonable (ALC lifts quiet speech, no hard clipping on loud speech).

## 4. GPIO17 button

With the service running, watch the logs while pressing the button:

```sh
journalctl -u earshot -f
```

- [ ] A short **press** while idle starts a recording (LED → red); a second press
      stops and finalises it (LED → amber briefly, then green).
- [ ] `GET /v1/status` reflects `recording` then `idle` (from another machine:
      `curl http://<pi-ip>:8080/v1/status`).
- [ ] A press shorter than `min_duration_seconds` (default 3 s) is discarded
      (LED double-flashes green).

## 5. APA102 LEDs (SPI)

Confirm the LED colour/pattern matches the state (rpi/specs/state-machine.md):

- [ ] Boot: white slow pulse → solid green when ready.
- [ ] Recording: red. Finalizing/processing: amber. Disk threshold: orange.
- [ ] LEDs match the header LED shown in the web UI at `http://<pi-ip>:8080/`.

## 6. Safe shutdown (CAP_SYS_BOOT)

- [ ] **Hold** the button for `shutdown_hold_seconds` (default 3 s) **while idle**:
      LED goes white and fades, and the Pi powers off cleanly (`reboot(2)`
      `POWER_OFF` via the unit's `AmbientCapabilities=CAP_SYS_BOOT`).
- [ ] A hold **during** recording or processing is ignored (no shutdown).
- [ ] After power is restored, the service comes back up on boot and any session
      interrupted mid-record is reconciled (finalised) — check `GET /v1/sessions`.

## 7. Local transcription (real model)

```sh
# record a short session (button), then from another machine:
curl -X POST http://<pi-ip>:8080/v1/sessions/rec-000001/jobs -H 'Content-Type: application/json' -d '{"kind":"transcribe"}'
watch -n2 'curl -s http://<pi-ip>:8080/v1/jobs'
```

- [ ] The job runs `faster-whisper` in a subprocess (LED amber, `state=processing`),
      completes, and a `transcript.md` appears (roughly 7–13 min per 15 min of audio).
- [ ] Starting a **new recording** during a local job preempts it immediately; the
      job returns to `queued` and re-runs after the recording ends.

## 8. Processing service (optional, if you have one)

- [ ] Set the URL in Settings → the status shows *Connected* with capabilities.
- [ ] A transcribe job routes to the service (faster); the device stays *Ready*
      (green) while it runs — recording alongside it is unaffected.
- [ ] Diarize is offered only when the service reports `diarize: true`.
