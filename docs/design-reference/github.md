# GitHub source

repo: rsmacapinlac/earshot-spec
branch: main
path: rpi

The prototype `Earshot RPi Web UI.dc.html` is built to match the `rpi/` documentation
track of the earshot spec repo — the LAN-served web UI capabilities under
`rpi/requirements/web-ui/` and the HTTP contract in `rpi/specs/api.md`.

## Last sync

date: 2026-07-26T18:40:00Z
rpi docs version: v1.3.3 (repo `main`; CHANGELOG `[1.3.3]` 2026-07-26)

### Updated in this project
- **v1.3.1 Cancel a job**: Cancel on queued and running jobs (session-detail queued/
  transcribing/diarizing overlays). Job → cancelled, session → **pending, not requeued**;
  idempotent. Kept distinct from preemption (a preempted local job returns to queue front).
- **v1.3.2 Transcribe reframe**: one **Transcribe** action with a **Diarize checkbox**
  (shown only when the service is connected AND reports the capability); the checkbox reveals
  the speaker-count hint. List gains **Diarize all** (on = every undiarized session); the two
  competing modal choice buttons are gone.
- **v1.3.3 Session job overlay**: the detail overlay is driven by the session's **own job** —
  **Queued** (+ queue position, Cancel), **Processing** (local = real progress, service =
  indeterminate), **Failed** (+ Retry). Rows badge Queued/Processing distinctly from untouched
  pending. Sample data includes a queued session. Live refresh via `/v1/events` noted in code.
- Footer v1.3 → v1.3.3.

## Sync history

### 2026-07-26T16:05:00Z — v1.3
- Upload an audio file (`POST /v1/sessions`): drop-zone modal, optional name + date/time,
  400/413 validation; disabled while recording/finalizing and at the disk threshold; ingest
  surfaces Finalizing then lands the session pending. Footer v1.1 → v1.3.

### 2026-07-25T21:48:09Z — v1.1 / v1.2
- v1.2: optional per-session `occurred_at` date/time (list + transcript header).
- v1.1: indeterminate progress for opaque service jobs; transcript export; optional
  `num_speakers` diarize hint; stale copy fixes.

## Screen map

| Screen / area | Built from |
|---|---|
| Sessions list + status badges | `requirements/web-ui/list-sessions.md`, `device-status.md`, `specs/api.md` (`GET /v1/sessions`, `GET /v1/status`) |
| Session detail — play/download | `requirements/web-ui/play-and-download.md`, `specs/api.md` (`…/audio`, `…/transcript`) |
| Transcribe flow + progress | `requirements/web-ui/transcribe.md`, `specs/processing.md`, `specs/api.md` (`POST …/jobs`) |
| Diarize + name speakers | `requirements/web-ui/diarize.md`, `name-speakers.md`, `specs/api.md` (`…/speakers`) |
| Cancel a job | `requirements/web-ui/cancel-a-job.md`, `specs/api.md` (`DELETE …/job`), `specs/processing.md` (preemption vs. cancel) |
| Session job overlay (queued/processing/failed) | `requirements/web-ui/session-detail.md`, `specs/api.md` (`GET …/job`, `GET /v1/jobs`, `/v1/events`) |
| Set a session date/time | `requirements/web-ui/set-session-datetime.md`, `non-functional/clock-independence.md`, `specs/api.md` (`PATCH /v1/sessions/{id}`) |
| Upload an audio file | `requirements/web-ui/upload-audio.md`, `specs/api.md` (`POST /v1/sessions`), `specs/storage.md`, `specs/configuration.md` (`storage.max_upload_mb`) |
| Recording control + device pill | `requirements/web-ui/recording-control.md`, `device-status.md` |
| Settings — processing service | `requirements/web-ui/processing-service.md`, `specs/api.md` (`/v1/service`) |
