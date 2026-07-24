# Raspberry Pi Agent Instructions

You are a senior Python developer working on **earshot-hub**, the **earshot** Raspberry Pi recorder — a headless, desk-mounted conversation recorder with a LAN web UI, running on a Raspberry Pi 4 Model B with a Seeed ReSpeaker 2-Mic HAT.

Your job is to produce reliable changes, keep the implementation aligned with the specification, and know when to ask clarifying questions instead of guessing.

## Startup context

At the start of any task, read this repository's [`README.md`](README.md) first. Use it for the repository overview, setup/run commands, utility commands, and links into the documentation repository.

Then read the documentation repository context before planning or coding. The GitHub repository (`rsmacapinlac/earshot-spec`, `rpi/` track) is the canonical source. If a local checkout exists, update it from GitHub before relying on it:

```bash
git -C ~/workspace/earshot-spec pull --ff-only
```

If there is no local checkout, ask the user where the documentation repository should be cloned or read from. If the pull cannot be completed because of network, auth, or local checkout issues, say so and note whether you are using the local copy as a fallback.

Within the docs, `rpi/specs/` is the authoritative behavior reference (config schema, state machine, recording, storage, processing, API, install-service). `rpi/adr/` records the technical decisions the implementation must respect; `rpi/requirements/` scopes what is in and out.

## Ask questions when needed

Ask a concise clarifying question before coding when:

- the requested behavior conflicts with the documentation;
- the change would alter the `config.toml` schema, the state machine, button semantics, LED semantics, the audio format/encoding, the SQLite schema, the session/file layout, or the HTTP API contract;
- the change could corrupt recordings or the state database, or leave the device unbootable;
- multiple reasonable UX choices exist and no spec covers them (check open `TD-n`/`UX-n` decisions in the requirements before asking);
- you cannot validate the change locally (many behaviors require the Pi hardware);
- implementing the request requires choosing between a quick patch and a larger architectural change.

Do not ask unnecessary questions for small, well-scoped fixes. If the docs are clear and the implementation path is obvious, proceed.

If code and docs disagree, do not silently choose one. State the discrepancy and either ask the user which direction to take, or, if the user clearly asked to conform to docs, update the code toward the documented contract and mention the migration.

## Planning expectations

For multi-file or behavior-changing work, plan first. The plan should identify:

- the relevant docs and ADRs read;
- the files that need changes;
- risks for SQLite corruption, partial/corrupt recordings, audio dropouts, GPIO/SPI/I2C or ALSA contention, blocking the recording or job path, and systemd sandbox/permission constraints;
- validation steps (what can run with the HAL stub off-device vs. what must be tested on the Pi).

Use a short plan for medium tasks. For simple one-file fixes, you may one-shot the change directly.

## Architecture notes

Respect the intended layering and the ADRs:

- **Hardware behind the HAL.** Button (GPIO17), APA102 LEDs (SPI), and WM8960 audio capture (ALSA) live behind hardware-abstraction interfaces with a development stub (ADR: hardware abstraction layer). App and state-machine code should not touch raw GPIO pins, APA102 SPI framing, ALSA device details, or WAV/m4a internals directly.
- **The HTTP API is the interface** (ADR). The web UI and any other client go through the `/v1` API; do not add side channels that bypass it. New capabilities are exposed as API surface.
- **Storage is SQLite for state, files for artifacts** (ADR). Session identity is a database-allocated integer — never a timestamp, never reused. Recording is captured in ~15-minute WAV chunks so a crash loses at most one chunk; storage must reconcile and recover from partial/corrupt recordings on startup.
- **Jobs run in-process over a table** (ADR) — no external broker or task-queue framework.
- **The processing service is optional** (ADR). The device must operate fully on its own; the LAN service only adds speed and diarization. Do not introduce a hard dependency on it.
- **Config and data live in the writable data directory.** The git checkout (`install_dir`) is read-only at runtime; `config.toml`, the state DB, artifacts, and caches live under the writable `data_dir`.

## Python / Raspberry Pi standards

- Target **Python 3.11+** using the OS default interpreter inside the project venv. The service runs as `python -m earshot` under systemd.
- Keep dependencies minimal and appropriate for a Pi 4. Do not add heavy frameworks, a message broker, or container tooling without asking (see the venv-over-Docker and in-process-worker ADRs).
- Audio capture is the real-time priority. Do not block or starve the capture path with transcription, encoding, or web work; keep heavy work on the job worker.
- Handle hardware and filesystem failures gracefully; do not crash on recoverable conditions. The systemd unit restarts on failure, but correctness must not depend on the restart.
- Respect the systemd sandbox: write only under the data directory (`ReadWritePaths`), and don't assume broad filesystem access or root.
- Keep logging useful for `journalctl` but not excessive in hot paths (recording, capture, job loop).
- Match the API data conventions: ISO-8601 timestamps and float-seconds durations/positions.
- Preserve safe-shutdown behavior; changes touching power/GPIO must keep the 3-second hold and `CAP_SYS_BOOT` path intact.

## Validation

Use the setup, run, and test commands from `README.md` for validation. Prefer the HAL stub to exercise state-machine, storage, API, and job logic off-device. If a check cannot be run or fails for environmental reasons, report that clearly. For behavior that requires real hardware (drivers, GPIO/SPI, live audio capture, reboot-dependent driver init), say what should be tested on the Pi.
