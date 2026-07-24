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

- **Timestamps.** ISO-8601 local wall-clock via `datetime.now().isoformat()` (microsecond
  precision), matching the `status.json` example in `storage.md`. Descriptive only — nothing
  reads them back for identity or ordering (clock-independence).

- **Speaker label form.** Labels are `"Speaker N"` (with a space), as in `api.md` and
  `status.json`. In the `PUT /v1/sessions/{id}/speakers/{label}` path the label is
  URL-encoded (`Speaker%201`).

## References

- **Design mockup imported.** The "Earshot Raspberry Pi UI" Claude Design project was
  imported to `docs/design-reference/earshot-rpi-web-ui.dc.html` (UX/interaction reference
  only — the `/v1` API is the contract). Used to build the web UI milestone.
