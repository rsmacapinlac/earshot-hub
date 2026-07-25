"""Optional processing-service client and its live configuration.

The adopted processing service is an off-the-shelf WhisperX service
(``ahmetoner/whisper-asr-webservice``): capabilities are discovered from
``/openapi.json`` and jobs are synchronous ``POST /asr`` requests. The device
owns queuing, retry, cancellation/abandonment, transcript rendering, and speaker
label normalization (rpi/specs/processing.md#fr-15b-process--service).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from earshot.jobs.transcript import Segment, normalize_speaker_labels, segments_from_raw

log = logging.getLogger("earshot.jobs.service")

_PROBE_TIMEOUT = 3.0


class ServiceUnreachable(RuntimeError):
    """The service could not be contacted — a connection problem, not a failure."""


class ServiceJobFailed(RuntimeError):
    """The service returned a non-2xx response or an unusable payload."""


class ServiceClient:
    """A thin client for one service URL. Stateless beyond the base URL."""

    def __init__(self, url: str, *, opener: urllib.request.OpenerDirector | None = None,
                 request_timeout_seconds: int = 0):
        self.url = url.rstrip("/")
        self._opener = opener or urllib.request.build_opener()
        self._request_timeout = None if request_timeout_seconds == 0 else request_timeout_seconds

    # -- capability discovery --------------------------------------------- #

    def openapi(self) -> dict:
        """Fetch ``/openapi.json``. Raises :class:`ServiceUnreachable` if unreachable."""
        try:
            return self._get_json("/openapi.json", timeout=_PROBE_TIMEOUT)
        except ServiceJobFailed as exc:
            raise ServiceUnreachable(str(exc)) from exc

    def reachable(self) -> bool:
        try:
            self.openapi()
            return True
        except ServiceUnreachable:
            return False

    def capabilities(self) -> dict | None:
        try:
            doc = self.openapi()
        except ServiceUnreachable:
            return None
        return {"transcribe": True, "diarize": _openapi_has_diarize(doc)}

    # -- synchronous processing ------------------------------------------- #

    def process(self, m4a_path: Path, kind: str, *, num_speakers: int | None = None) -> list[Segment]:
        """Submit ``session.m4a`` to synchronous ``POST /asr`` and return segments."""
        query: dict[str, str] = {
            "output": "json",
            "encode": "true",
            "task": "transcribe",
        }
        if kind == "diarize":
            query["diarize"] = "true"
            if num_speakers is not None:
                query["min_speakers"] = str(num_speakers)
                query["max_speakers"] = str(num_speakers)
        path = "/asr?" + urllib.parse.urlencode(query)
        body, content_type = _multipart(file_field="audio_file", file_path=Path(m4a_path))
        try:
            data = self._request("POST", path, data=body,
                                 headers={"Content-Type": content_type},
                                 timeout=self._request_timeout)
        except ServiceUnreachable as exc:
            # Reachability is checked before dequeue. A drop/timeout during the
            # synchronous ASR request is an attempt failure.
            raise ServiceJobFailed(str(exc)) from exc
        segments = _segments_payload(data)
        if kind == "diarize":
            segments = normalize_speaker_labels(segments)
        return segments

    # -- transport --------------------------------------------------------- #

    def _get_json(self, path: str, *, timeout: float | None) -> dict:
        return self._request("GET", path, timeout=timeout)

    def _request(self, method: str, path: str, *, data: bytes | None = None,
                 headers: dict[str, str] | None = None, timeout: float | None) -> Any:
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers=headers or {})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 600:
                raise ServiceJobFailed(f"{method} {path}: HTTP {exc.code}") from exc
            raise ServiceUnreachable(f"{method} {path}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ServiceUnreachable(f"{method} {path}: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ServiceJobFailed(f"{method} {path}: bad JSON") from exc


def _multipart(*, file_field: str, file_path: Path) -> tuple[bytes, str]:
    """Encode ``multipart/form-data`` with the standard library (no requests dep)."""
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts: list[bytes] = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        file_path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _segments_payload(data: Any) -> list[Segment]:
    if isinstance(data, dict):
        raw = data.get("segments", [])
    elif isinstance(data, list):
        raw = data
    else:
        raise ServiceJobFailed("service response did not contain segments")
    if not isinstance(raw, list):
        raise ServiceJobFailed("service response segments is not a list")
    return segments_from_raw(raw)


def _openapi_has_diarize(doc: dict) -> bool:
    try:
        params = doc["paths"]["/asr"]["post"].get("parameters", [])
    except Exception:
        return False
    return any(p.get("name") == "diarize" for p in params if isinstance(p, dict))


class ServiceManager:
    """Owns the current service client; applies URL changes live and persists them."""

    def __init__(self, config, *, client_factory=ServiceClient):
        self._config = config
        self._client_factory = client_factory
        url = (config.processing.service_url or "").strip()
        self._client: ServiceClient | None = self._make_client(url) if url else None

    def _make_client(self, url: str) -> ServiceClient:
        try:
            return self._client_factory(
                url, request_timeout_seconds=self._config.processing.request_timeout_seconds
            )
        except TypeError:
            # Test fakes and older call sites may accept only the URL.
            return self._client_factory(url)

    @property
    def configured(self) -> bool:
        return self._client is not None

    def client(self) -> ServiceClient | None:
        return self._client

    def reachable(self) -> bool:
        return self._client is not None and self._client.reachable()

    def set_url(self, url: str) -> dict:
        url = url.strip()
        self._client = self._make_client(url) if url else None
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
