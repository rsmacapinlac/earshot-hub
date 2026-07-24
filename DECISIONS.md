# Decisions

Choices made while building **earshot-hub** to the `rpi/` spec. Each is either a
spec-gap default (the spec was silent; recorded here with a rationale) or a spec
reconciliation (the spec contradicted itself or the build prompt; how it was resolved).

The spec always wins where it speaks. Nothing here overrides `rpi/specs/`.

## Reconciliations (spec was contradictory or superseded by the build prompt)

- **Repo / install identity.** `install-service.md` still clones `rsmacapinlac/earshot.git`
  into `~/earshot`. This repository is `earshot-hub`. Per the build prompt, the installer
  targets `install_dir = ~/earshot-hub`, keeping `data_dir = ~/earshot-data` (unchanged).
  The Python **package** remains `earshot` (`python -m earshot`) — only the checkout
  directory name changes. Noted in README.md as well.

- **Web port.** `configuration.md` `[web].port` defaults to `8080`; some prose elsewhere
  writes `http://<pi-ip>/` without a port. The config default wins: **8080**.

## Spec-gap defaults (spec silent; interim default chosen)

- **Web stack: Flask + waitress.** The spec mandates the `/v1` API and SSE but names no
  framework, and the ADRs require minimal deps. Flask (served by `waitress` under systemd)
  is lightweight, handles Range requests via `send_file`, and streams SSE from a generator.
  Confirmed with the maintainer before adding (AGENTS.md requires asking before a framework).

- **API contract is the single source of truth, authored as OpenAPI 3.1.** OpenAPI 3.1's
  `components.schemas` *are* JSON Schema (Draft 2020-12), so `earshot/api/openapi.yaml` is
  both the OpenAPI spec and the JSON Schemas the build prompt asks for — one artifact, no
  drift. Runtime request/response validation binds directly to those component schemas
  (`earshot/api/validation.py`).

- **Device `state` vs. a running service job (build prompt §3).** `GET /v1/status.state`
  is `processing` only while **local** CPU-bound work runs. A job on the processing service
  leaves `state = idle` (LED green) — or `recording` if capture is active — with the
  `processing` object still populated so the UI can surface it. Encoded as an explicit rule
  in the state machine. This follows the state-machine/processing specs (Processing LED is
  for local work; a service job alongside recording shows Recording).

- **Local transcription dependency handling.** `faster-whisper` lives behind the
  `[transcription]` extra; the job worker spawns a subprocess that imports it. Off-device
  tests inject a **fake transcriber** so queue/route/retry/preemption logic is fully
  CI-testable without the model. The real faster-whisper path requires the model and is
  validated on-device (see `docs/ON_DEVICE_SMOKE.md`).

- **Frontend: vanilla HTML/CSS/JS, no build step.** Minimal deps for a Pi appliance; served
  statically by the app and bound to the `/v1` API + SSE.

- **HAL backend selection.** `hardware.hat = "respeaker"` selects the real `pi` backend;
  `hardware.hat = "stub"` (a value the spec doesn't enumerate but the HAL ADR's two-backend
  model implies) selects the stub. `EARSHOT_HAL=stub` overrides config so off-device dev runs
  `EARSHOT_HAL=stub python -m earshot` without editing config. The real backend never silently
  falls back to the stub — on a device, missing hardware must fail loudly. Pi hardware deps
  (`gpiozero`, `spidev`, `arecord`) are imported lazily so `earshot.hal.pi` imports off-device.

- **Dev data dir / config overrides.** `EARSHOT_DATA_DIR` overrides `[storage].data_dir` and
  `EARSHOT_CONFIG` overrides the config path, so off-device runs and tests use a scratch
  directory without touching `~/earshot-data`.

- **Timestamps.** ISO-8601 local wall-clock via `datetime.now().isoformat()` (microsecond
  precision), matching the `status.json` example in `storage.md`. Descriptive only — nothing
  reads them back for identity or ordering (clock-independence).

- **Speaker label form.** Labels are `"Speaker N"` (with a space), as in `api.md` and
  `status.json`. In the `PUT /v1/sessions/{id}/speakers/{label}` path the label is
  URL-encoded (`Speaker%201`).

- **Reconciliation edge cases (M4).** `storage.md`'s table covers the four
  disagreements; these silent-spot rulings fill the gaps:
  - **Empty orphan directory** (a `rec-*` dir with no `session.m4a`, no usable chunks, and
    no DB row) has nothing to adopt — it is **left in place and logged**, not adopted or
    deleted. Adoption requires audio to reconstruct a row from.
  - **`created_at` on adoption** comes from `status.json`; if that is absent it falls back
    to `datetime.now()`. Per the spec `created_at` is descriptive and clock-independent —
    nothing recovers or orders by it — so a fabricated value is harmless.
  - **`size` on adoption** is read from the `session.m4a` file itself (authoritative);
    `status.json` does not record size. `duration` prefers the recovery probe, then
    `status.json`, then a fresh `ffprobe`.
  - **Unfinalized-chunk header repair.** A chunk whose session crashed before `close()`
    keeps stale RIFF/`data` length fields describing only its first block. Recovery rewrites
    both length fields from the file size, frame-aligned (dropping a torn trailing frame),
    so ffmpeg reads the whole chunk — the spec's "read tolerantly, frame count from the file
    size." The repair only touches canonical `RIFF…data` WAVs (the ChunkWriter's own output).
  - **Reappearing directory** clears a previously-set `missing` flag; ids are never reused,
    so the row is reactivated in place rather than re-created.

- **`sqlite_sequence` upsert fix (M4).** `set_sqlite_sequence` originally used
  `INSERT … ON CONFLICT(name)`, but SQLite's internal `sqlite_sequence` table has no UNIQUE
  constraint, so the upsert raised at runtime. It now reads the current `seq` and writes the
  max explicitly. This path was first exercised by reconciliation (adoption), so the latent
  bug surfaced in M4.

- **State machine is an explicit table (M5).** Transitions live in
  `earshot/statemachine/transitions.py` as `TABLE[(State, Trigger)] -> Transition(target,
  guard, action)`; the Controller routes every input through it. An **absent** `(state,
  trigger)` pair means the trigger is ignored in that state — the spec's usual phrasing for
  its prohibitions (holds ignored while recording, presses ignored while finalizing). The
  table is verified against an independent transcription of `state-machine.md` in the tests,
  so drift in either fails.

- **`shutting_down` is terminal and never reported (M5).** It is not an api.md `DeviceState`,
  so it is deliberately absent from the OpenAPI enum and from `GET /v1/status`. The safe-
  shutdown action shows the white fade LED and calls the shutdown hook; on real hardware the
  process is powered off and never returns. If the hook is a no-op (stub) or fails, the
  device restores its resting LED and stays put, so `status` never surfaces an unreportable
  state.

- **Disk gating is a guard, not a duplicated branch (M5).** `START`/`PRESS` from both `idle`
  and `disk_full` route through the `disk_ok` guard. A blocked disk fails the guard: a button
  press is ignored, a web `start` returns the `disk_full` error (409). Recording can never be
  entered without passing this guard (asserted in tests).

- **`processing` = a local job; preemption falls out of the table (M5).** Because the device
  is only `processing` while local work runs, the FR-2 preemption rule is just
  `PROCESSING + START/PRESS -> RECORDING` via a `preempt_and_record` action (cancel + requeue
  the local job to the front), while a service job — never a device state — leaves the
  ordinary `IDLE + START` path untouched. The job triggers (`job_started`/`job_finished`) and
  the preempt hook are encoded and tested now; the Controller starts emitting them with the
  job worker (M6).

- **Illegal-command error mapping (M5).** When a web command has no transition in the current
  state: `start` while `recording` → `already_recording`; `stop` while not recording →
  `not_recording`; anything else (e.g. during `finalizing`) → `busy`. All 409. Commands that
  queue during a synchronous finalize are rejected `busy`, honouring "ignored during
  post-recording processing."

- **Worker ↔ control-loop coordination (M6).** All device-state changes stay on the control
  loop thread. The job worker asks the loop to enter/leave `processing` via serialized
  commands (`proc_begin`/`proc_end`), so a local job is granted the `processing` state only
  when the device is idle — a local job can never start during a recording. `proc_*` commands
  are never rejected by the finalize drain (only `start`/`stop` are), so the worker is not
  starved. Preemption is a direct `worker.preempt()` call from the loop's `preempt_and_record`
  action; it terminates the child immediately (a signal, not a cooperative flag), the job
  returns to `queued` with **no** attempt bump, and recording begins without waiting on the
  worker.

- **Local transcription seam (M6).** The worker builds a transcriber per job via an injectable
  `transcriber_factory`. Production uses `LocalTranscriber`, which spawns
  `python -m earshot.jobs._whisper_child` (loads faster-whisper, prints JSON segments); a
  missing model or a decode failure exits non-zero and surfaces as a `TranscribeError` that
  fails the job without touching the recorder. Off-device tests inject a fake transcriber, so
  queue/route/retry/preemption/cancel logic is fully CI-testable without the model.

- **Route decision & diarize gating (M6).** `decide_route` picks `service` when a reachable
  service is configured (M7) else `local`; `diarize` has no local route. With no service
  client yet, `diarize` enqueues (single or bulk) return **409 `diarize_unavailable`**, and an
  unreachable service is treated as fall-back-to-local, never a session failure.

- **Enqueue rules & new error codes (M6).** Jobs enqueue **only** from the API, never on
  finalize. `POST /v1/sessions/{id}/jobs` returns **409 `not_finalized`** if the session has no
  `session.m4a`, and **409 `job_exists`** if a job is already queued/running for it. Retry:
  `attempts`/`last_error` recorded on the row; requeued until `processing.max_failures` (0 =
  forever). A `running` job left by a crash is reset to `queued` on boot (M6 is local-only;
  the service-resume-by-`remote_job_id` refinement is M7).

- **`GET /v1/sessions/{id}/transcript` added in M6.** The read side of what M6 produces:
  content-negotiated `text/markdown` (rendered `transcript.md`, the export path) or
  `application/json` (segments). A plain transcribe reverts any prior diarization (removes the
  diarized raw, clears speaker labels), which is how a diarized session is reverted even
  locally.

## References

- **Design mockup imported.** The "Earshot Raspberry Pi UI" Claude Design project was
  imported to `docs/design-reference/earshot-rpi-web-ui.dc.html` (UX/interaction reference
  only — the `/v1` API is the contract). Used to build the web UI milestone.
