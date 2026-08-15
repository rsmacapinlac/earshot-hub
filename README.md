# earshot-hub

The **Raspberry Pi** implementation of [earshot](https://github.com/rsmacapinlac/earshot-spec) — a local-first, desk-mounted conversation recorder with a LAN web UI. Audio is captured on-device, encoded to a single per-session file, and transcribed locally with no internet or API keys required. An optional LAN processing service adds speed and speaker diarization.

> **Status: complete (v1.3.4.1).** Built to the `rpi/` track of the spec across 10 milestones — API contract, HAL + stub, recording, storage recovery, state machine, jobs + local transcription, processing-service integration + diarization, web UI, installer + systemd, and config validation. Hardware-dependent behaviour is validated per [`docs/ON_DEVICE_SMOKE.md`](docs/ON_DEVICE_SMOKE.md); everything else has an off-device test suite (`pytest`).

## Hardware

| Component | Detail |
|-----------|--------|
| Compute | Raspberry Pi 4 Model B |
| Audio HAT | Seeed ReSpeaker 2-Mic Pi HAT — WM8960 codec, dual MEMS mics, GPIO17 button, 3× APA102 LEDs |
| Operation | Headless `systemd` service; web UI served at the Pi's IP address |

## Documentation

The specification is the source of truth for behavior. The canonical copy is the GitHub repository; keep a local checkout in sync before relying on it.

- Spec repo: <https://github.com/rsmacapinlac/earshot-spec> (`rpi/` track)
- Authoritative behavior: `rpi/specs/` — configuration, state machine, recording, storage, processing, API, install-service
- Decisions: `rpi/adr/` — binding technical decisions the implementation must respect
- Scope: `rpi/requirements/` — supported hardware, capabilities, open `TD-n`/`UX-n` questions

```bash
# refresh a local checkout (adjust path to your setup)
git -C ~/workspace/earshot-spec pull --ff-only
```

Contributor conventions for AI agents working in this repo live in [`AGENTS.md`](AGENTS.md).

## Tech stack

- **Python 3.11+** (OS default interpreter), isolated in a project venv
- Runs as `python -m earshot` under **systemd**; **venv over Docker** for direct GPIO/SPI/ALSA access
- **SQLite** for state, files for artifacts; **in-process** job worker (no external broker)
- HTTP **`/v1` API** (JSON + Server-Sent Events; Range-request audio) — the single interface behind both the web UI and any client
- `ffmpeg` for AAC-LC encoding; `faster-whisper` for local transcription
- Hardware (button, LEDs, capture) behind a **HAL** with a development **stub** for off-device work

## Repository layout

```
earshot-hub/
├── earshot/            # Python package — run via `python -m earshot`
│   ├── hal/            # button / LED / capture interfaces + dev stub
│   ├── api/            # /v1 HTTP API + SSE
│   ├── ...             # state machine, storage, recording, jobs, processing, service, power
│   └── web/            # vanilla web UI (index.html + app.js)
├── installer/
│   ├── install.sh      # on-device installer
│   └── apply-alc.sh    # WM8960 ALC front-end (applied at boot)
├── AGENTS.md           # agent contributor instructions
└── README.md
```

At runtime the code is split across two locations (see `rpi/specs/install-service.md`):

- **install_dir** (git checkout, e.g. `~/earshot-hub`) — read-only at runtime
- **data_dir** (e.g. `~/earshot-data`) — writable: `config.toml`, state DB, `session.m4a` artifacts, transcription cache

## Installation (on the Pi)

```bash
git clone https://github.com/rsmacapinlac/earshot-hub.git ~/earshot-hub
bash ~/earshot-hub/installer/install.sh
```

The installer prompts for HAT selection, writes `config.toml`, installs the ReSpeaker (seeed-voicecard) driver, `ffmpeg`, and `faster-whisper` (with model pre-download), creates the venv, installs dependencies, and enables the `earshot.service` systemd unit. **A reboot is required** for audio driver initialization.

> **Naming note:** the spec's `install-service.md` currently clones `rsmacapinlac/earshot.git` into `~/earshot`. This repository is `earshot-hub`; the commands above use the actual repo name. This discrepancy should be reconciled with the spec.

## Development (off-device)

Most logic — state machine, storage, recording pipeline, and the `/v1` API — runs on a
workstation using the HAL stub, without the Pi hardware. The stub emits real PCM frames,
so the full record → chunk → encode → `session.m4a` path exercises for real (it needs
`ffmpeg`/`ffprobe` on `PATH`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# Run the whole app against the stub HAL (writes to ./earshot-data by default).
EARSHOT_HAL=stub EARSHOT_DATA_DIR=./earshot-data python -m earshot
# then browse to http://localhost:8080/  (API under /v1)

pytest                       # off-device test suite (contract, HAL, config, skeleton)
```

Useful env vars for development: `EARSHOT_HAL` (`stub`|`pi`, overrides `hardware.hat`),
`EARSHOT_DATA_DIR` (overrides `[storage].data_dir`), `EARSHOT_CONFIG` (config path),
`EARSHOT_LOG_LEVEL`.

Behavior that needs real hardware (WM8960 driver, GPIO/SPI, live capture, reboot-dependent
driver init) must be validated on the Pi — see [`docs/ON_DEVICE_SMOKE.md`](docs/ON_DEVICE_SMOKE.md).

## Service management

```bash
sudo systemctl status earshot     # service state
journalctl -u earshot -f          # follow logs
sudo systemctl restart earshot    # restart after changes
```

## Web UI & API

Once running, browse to `http://<pi-ip>/` for the web UI. All capabilities are exposed through the `/v1` HTTP API (device status/events, sessions, recording control, jobs, speakers, processing-service config). There is no authentication in v1 — it assumes a trusted LAN. See `rpi/specs/api.md`.

## Validation

Use the development and service commands above to validate changes. Prefer the HAL stub for anything that does not require hardware; clearly note what still needs on-device testing. See [`AGENTS.md`](AGENTS.md) for planning and validation expectations.
