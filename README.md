# earshot-hub

The **Raspberry Pi** implementation of [earshot](https://github.com/rsmacapinlac/earshot-spec) — a local-first, desk-mounted conversation recorder with a LAN web UI. Audio is captured on-device, encoded to a single per-session file, and transcribed locally with no internet or API keys required. An optional LAN processing service adds speed and speaker diarization.

> **Status: scaffolding.** This repository is being set up against the `rpi/` track of the spec. The layout and commands below describe the **intended** structure; items marked _(planned)_ do not exist yet.

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

## Repository layout _(intended)_

```
earshot-hub/
├── earshot/            # Python package — run via `python -m earshot`   (planned)
│   ├── hal/            # button / LED / capture interfaces + dev stub   (planned)
│   ├── api/            # /v1 HTTP API + SSE                             (planned)
│   ├── ...             # state machine, storage, recording, jobs, processing
├── installer/
│   └── install.sh      # on-device installer                           (planned)
├── AGENTS.md           # agent contributor instructions
└── README.md
```

At runtime the code is split across two locations (see `rpi/specs/install-service.md`):

- **install_dir** (git checkout, e.g. `~/earshot-hub`) — read-only at runtime
- **data_dir** (e.g. `~/earshot-data`) — writable: `config.toml`, state DB, `session.m4a` artifacts, transcription cache

## Installation (on the Pi) _(planned)_

```bash
git clone https://github.com/rsmacapinlac/earshot-hub.git ~/earshot-hub
bash ~/earshot-hub/installer/install.sh
```

The installer prompts for HAT selection, writes `config.toml`, installs the ReSpeaker (seeed-voicecard) driver, `ffmpeg`, and `faster-whisper` (with model pre-download), creates the venv, installs dependencies, and enables the `earshot.service` systemd unit. **A reboot is required** for audio driver initialization.

> **Naming note:** the spec's `install-service.md` currently clones `rsmacapinlac/earshot.git` into `~/earshot`. This repository is `earshot-hub`; the commands above use the actual repo name. This discrepancy should be reconciled with the spec.

## Development (off-device) _(planned)_

Most logic — state machine, storage, API, and the job worker — can be exercised on a workstation using the HAL stub, without the Pi hardware.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python -m earshot            # run with the HAL stub
```

Behavior that needs real hardware (WM8960 driver, GPIO/SPI, live capture, reboot-dependent driver init) must be validated on the Pi.

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
