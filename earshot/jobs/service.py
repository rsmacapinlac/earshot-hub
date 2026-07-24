"""Optional processing-service client and its live configuration.

A service is an upgrade, never a dependency (rpi/adr/optional-processing-service.md):
setting ``processing.service_url`` routes transcription to a faster machine and
unlocks diarization; clearing it falls back to local, losing nothing. The client
speaks the asynchronous job API in ``service/specs/api.md`` (submit → poll →
result) over the standard library only — no new dependency.

``ServiceManager`` owns the current client, applies URL changes live (no restart),
persists the URL to ``config.toml`` — the one operational setting the HTTP API is
allowed to write (rpi/specs/api.md#scope) — and reports connection status. An
**unreachable** service is a connection problem, never a session failure
(rpi/specs/processing.md#failure).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from earshot.jobs.transcript import Segment, segments_from_raw

log = logging.getLogger("earshot.jobs.service")

_HEALTH_TIMEOUT = 3.0
_SUBMIT_TIMEOUT = 60.0
_POLL_TIMEOUT = 10.0


class ServiceUnreachable(RuntimeError):
    """The service could not be contacted — a connection problem, not a failure."""


class ServiceJobGone(RuntimeError):
    """The service returned 404 — the remote job was reaped or lost; resubmit."""


class ServiceJobFailed(RuntimeError):
    """The service reported the job ``failed``."""


class ServiceClient:
    """A thin client for one service URL. Stateless beyond the base URL."""

    def __init__(self, url: str, *, opener: urllib.request.OpenerDirector | None = None):
        self.url = url.rstrip("/")
        self._opener = opener or urllib.request.build_opener()

    # -- health ------------------------------------------------------------ #

    def health(self) -> dict:
        """``GET /v1/health``. Raises :class:`ServiceUnreachable` if unreachable."""
        return self._get_json("/v1/health", timeout=_HEALTH_TIMEOUT)

    def reachable(self) -> bool:
        try:
            self.health()
            return True
        except ServiceUnreachable:
            return False

    def capabilities(self) -> dict | None:
        try:
            caps = self.health().get("capabilities") or {}
        except ServiceUnreachable:
            return None
        return {"transcribe": bool(caps.get("transcribe")), "diarize": bool(caps.get("diarize"))}

    # -- jobs -------------------------------------------------------------- #

    def submit(self, m4a_path: Path, kind: str) -> str:
        """``POST /v1/jobs`` (multipart). Returns the opaque remote job id."""
        body, content_type = _multipart(
            fields={"kind": kind},
            file_field="audio",
            file_path=Path(m4a_path),
        )
        resp = self._request("POST", "/v1/jobs", data=body,
                             headers={"Content-Type": content_type}, timeout=_SUBMIT_TIMEOUT)
        return str(resp["job_id"])

    def poll(self, remote_job_id: str) -> dict:
        """``GET /v1/jobs/{id}`` → ``{status, stage?, progress?, error?}``."""
        return self._get_json(f"/v1/jobs/{remote_job_id}", timeout=_POLL_TIMEOUT)

    def result(self, remote_job_id: str) -> list[Segment]:
        """``GET /v1/jobs/{id}/result`` → segments (raw, unrendered)."""
        data = self._get_json(f"/v1/jobs/{remote_job_id}/result", timeout=_POLL_TIMEOUT)
        return segments_from_raw(data.get("segments", []))

    def cancel(self, remote_job_id: str) -> None:
        """``DELETE /v1/jobs/{id}`` — best-effort; the service is idempotent."""
        try:
            self._request("DELETE", f"/v1/jobs/{remote_job_id}", timeout=_POLL_TIMEOUT)
        except (ServiceUnreachable, ServiceJobGone):
            pass

    # -- transport --------------------------------------------------------- #

    def _get_json(self, path: str, *, timeout: float) -> dict:
        return self._request("GET", path, timeout=timeout)

    def _request(self, method: str, path: str, *, data: bytes | None = None,
                 headers: dict[str, str] | None = None, timeout: float) -> dict:
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers=headers or {})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ServiceJobGone(path) from exc
            raise ServiceUnreachable(f"{method} {path}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ServiceUnreachable(f"{method} {path}: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ServiceUnreachable(f"{method} {path}: bad JSON") from exc


def _multipart(*, fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    """Encode ``multipart/form-data`` with the standard library (no requests dep)."""
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{file_path.name}"\r\n'.encode()
    )
    parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class ServiceManager:
    """Owns the current service client; applies URL changes live and persists them."""

    def __init__(self, config, *, client_factory=ServiceClient):
        self._config = config
        self._client_factory = client_factory
        url = (config.processing.service_url or "").strip()
        self._client: ServiceClient | None = self._client_factory(url) if url else None

    @property
    def configured(self) -> bool:
        return self._client is not None

    def client(self) -> ServiceClient | None:
        return self._client

    def reachable(self) -> bool:
        return self._client is not None and self._client.reachable()

    def set_url(self, url: str) -> dict:
        url = url.strip()
        self._client = self._client_factory(url) if url else None
        self._config.processing.service_url = url
        self._config.persist_service_url(url)
        return self.status()

    def clear(self) -> None:
        self.set_url("")

    def status(self) -> dict[str, Any]:
        """The ``Service`` API shape (rpi/specs/api.md#get-v1service)."""
        if self._client is None:
            return {"configured": False, "url": None, "reachable": False, "capabilities": None}
        caps = self._client.capabilities()
        return {
            "configured": True,
            "url": self._client.url,
            "reachable": caps is not None,
            "capabilities": caps,
        }

    def diarize_available(self) -> bool:
        caps = self._client.capabilities() if self._client is not None else None
        return bool(caps and caps.get("diarize"))
