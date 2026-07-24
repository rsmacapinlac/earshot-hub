"""`config.toml` schema, defaults, and loading (rpi/specs/configuration.md).

All keys have defaults; omitting a key uses the default. The file lives in the
**data directory** (``~/earshot-data/config.toml`` by default), not the install
directory. Full validation with clear errors is hardened in the config milestone;
this module already loads, type-checks, and applies defaults.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - project targets 3.11+
    import tomli as tomllib

DEFAULT_DATA_DIR = "~/earshot-data"
# HAT values the HAL knows (rpi/adr/hardware-abstraction-layer.md): the real
# ReSpeaker backend, and the dev stub. Kept in sync with earshot.hal.bundle.
_KNOWN_HATS = {"respeaker", "stub"}


class ConfigError(ValueError):
    """Raised when config.toml cannot be parsed or fails validation."""


@dataclass
class HardwareConfig:
    hat: str = "respeaker"


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1
    bit_depth: int = 16
    alsa_pcm: str = "plughw:CARD=wm8960soundcard,DEV=0"


@dataclass
class RecordingConfig:
    chunk_duration_seconds: int = 900
    min_duration_seconds: int = 3
    encode_bitrate_kbps: int = 32
    shutdown_hold_seconds: int = 3


@dataclass
class StorageConfig:
    data_dir: str = DEFAULT_DATA_DIR
    disk_threshold_percent: int = 90


@dataclass
class TranscriptionConfig:
    enabled: bool = True
    model: str = "base.en"
    threads: int = 2


@dataclass
class ProcessingConfig:
    service_url: str = ""
    poll_interval_seconds: int = 5
    max_failures: int = 3


@dataclass
class WebConfig:
    enabled: bool = True
    bind_address: str = "0.0.0.0"
    port: int = 8080


@dataclass
class Config:
    hardware: HardwareConfig = field(default_factory=HardwareConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    web: WebConfig = field(default_factory=WebConfig)
    # Where this config was loaded from (None if all-defaults).
    source_path: Path | None = None

    # -- Derived paths ----------------------------------------------------- #

    @property
    def data_dir(self) -> Path:
        """Absolute, user-expanded data directory."""
        env = os.environ.get("EARSHOT_DATA_DIR")
        raw = env if env else self.storage.data_dir
        return Path(raw).expanduser().resolve()

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "earshot.db"

    # -- Loading ----------------------------------------------------------- #

    @classmethod
    def default_path(cls) -> Path:
        env = os.environ.get("EARSHOT_CONFIG")
        if env:
            return Path(env).expanduser()
        data_dir = os.environ.get("EARSHOT_DATA_DIR", DEFAULT_DATA_DIR)
        return Path(data_dir).expanduser() / "config.toml"

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        """Load config from *path* (or the default), applying defaults.

        A missing file is not an error — the device runs on all-defaults, which is
        a fully supported configuration.
        """
        config_path = Path(path) if path is not None else cls.default_path()
        if not config_path.exists():
            cfg = cls()
            cfg.source_path = None
            return cfg
        try:
            with config_path.open("rb") as fh:
                raw = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {config_path}: {exc}") from exc
        try:
            cfg = cls.from_dict(raw)
        except ConfigError as exc:
            # Point the operator straight at the offending file.
            raise ConfigError(f"{config_path}: {exc}") from exc
        cfg.source_path = config_path
        return cfg

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        sections = {
            "hardware": HardwareConfig,
            "audio": AudioConfig,
            "recording": RecordingConfig,
            "storage": StorageConfig,
            "transcription": TranscriptionConfig,
            "processing": ProcessingConfig,
            "web": WebConfig,
        }
        unknown = [s for s in raw if s not in sections]
        if unknown:
            names = ", ".join(f"[{s}]" for s in unknown)
            raise ConfigError(
                f"unknown config section(s): {names} "
                f"(valid: {', '.join('[' + s + ']' for s in sections)})"
            )
        kwargs: dict[str, Any] = {}
        for name, section_cls in sections.items():
            kwargs[name] = _build_section(name, section_cls, raw.get(name, {}))
        cfg = cls(**kwargs)
        cfg.validate()
        return cfg

    # -- Live service-URL persistence -------------------------------------- #

    def persist_service_url(self, url: str) -> None:
        """Write ``[processing].service_url`` to ``config.toml`` in place.

        This is the single config value the HTTP API may change (rpi/specs/
        api.md#scope). The edit is targeted — it preserves every other line,
        including comments and unrelated sections — so a hand-maintained
        ``config.toml`` is not clobbered by an operational URL change.
        """
        path = self.source_path or (self.data_dir / "config.toml")
        path.parent.mkdir(parents=True, exist_ok=True)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = _set_service_url(current, url)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(updated, encoding="utf-8")
        tmp.replace(path)
        self.source_path = path

    # -- Validation -------------------------------------------------------- #

    def validate(self) -> None:
        """Full validation against configuration.md, with clear per-key errors."""
        # [hardware]
        if self.hardware.hat not in _KNOWN_HATS:
            raise ConfigError(
                f"hardware.hat must be one of {sorted(_KNOWN_HATS)}, got {self.hardware.hat!r}"
            )
        # [audio] — fixed capture format: mono, 16-bit PCM (recording.md).
        a = self.audio
        if a.sample_rate <= 0:
            raise ConfigError("audio.sample_rate must be > 0")
        if a.channels != 1:
            raise ConfigError("audio.channels must be 1 (mono, left mic)")
        if a.bit_depth != 16:
            raise ConfigError("audio.bit_depth must be 16 (16-bit PCM)")
        if not a.alsa_pcm:
            raise ConfigError("audio.alsa_pcm must not be empty")
        # [recording]
        r = self.recording
        if r.chunk_duration_seconds <= 0:
            raise ConfigError("recording.chunk_duration_seconds must be > 0")
        if r.min_duration_seconds < 0:
            raise ConfigError("recording.min_duration_seconds must be >= 0")
        if r.encode_bitrate_kbps <= 0:
            raise ConfigError("recording.encode_bitrate_kbps must be > 0")
        if r.shutdown_hold_seconds <= 0:
            raise ConfigError("recording.shutdown_hold_seconds must be > 0")
        # [storage]
        if not self.storage.data_dir:
            raise ConfigError("storage.data_dir must not be empty")
        if not (0 < self.storage.disk_threshold_percent <= 100):
            raise ConfigError("storage.disk_threshold_percent must be in (0, 100]")
        # [transcription]
        t = self.transcription
        if not t.model:
            raise ConfigError("transcription.model must not be empty")
        if t.threads < 1:
            raise ConfigError("transcription.threads must be >= 1")
        # [processing]
        p = self.processing
        url = p.service_url.strip()
        if url and not (url.startswith("http://") or url.startswith("https://")):
            raise ConfigError("processing.service_url must start with http:// or https:// (or be empty)")
        if p.poll_interval_seconds <= 0:
            raise ConfigError("processing.poll_interval_seconds must be > 0")
        if p.max_failures < 0:
            raise ConfigError("processing.max_failures must be >= 0 (0 = retry forever)")
        # [web]
        if not self.web.bind_address:
            raise ConfigError("web.bind_address must not be empty")
        if not (0 < self.web.port < 65536):
            raise ConfigError("web.port must be in (0, 65535]")


def _set_service_url(text: str, url: str) -> str:
    """Return *text* with ``[processing].service_url`` set to *url*, in place."""
    value = 'service_url = "{}"'.format(url.replace("\\", "\\\\").replace('"', '\\"'))
    lines = text.splitlines()

    header = next((i for i, ln in enumerate(lines) if ln.strip() == "[processing]"), None)
    if header is None:
        base = text if (text == "" or text.endswith("\n")) else text + "\n"
        sep = "" if text == "" else "\n"
        return f"{base}{sep}[processing]\n{value}\n"

    end = len(lines)
    for j in range(header + 1, len(lines)):
        if lines[j].lstrip().startswith("["):
            end = j
            break
    for j in range(header + 1, end):
        if lines[j].split("#", 1)[0].strip().startswith("service_url"):
            lines[j] = value
            return "\n".join(lines) + "\n"
    lines.insert(header + 1, value)
    return "\n".join(lines) + "\n"


def _build_section(name: str, section_cls: type, values: Any):
    if not isinstance(values, dict):
        raise ConfigError(f"[{name}] must be a table")
    known = {f.name: f for f in fields(section_cls)}
    kwargs: dict[str, Any] = {}
    for key, value in values.items():
        if key not in known:
            raise ConfigError(f"unknown key [{name}].{key}")
        expected = known[key].type
        kwargs[key] = _coerce(name, key, value, expected)
    return section_cls(**kwargs)


def _coerce(section: str, key: str, value: Any, expected: Any):
    # dataclass field types are strings under `from __future__ import annotations`.
    type_name = expected if isinstance(expected, str) else getattr(expected, "__name__", str(expected))
    if type_name == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"[{section}].{key} must be a boolean")
        return value
    if type_name == "int":
        # A TOML bool is an int subclass; reject it explicitly.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"[{section}].{key} must be an integer")
        return value
    if type_name == "str":
        if not isinstance(value, str):
            raise ConfigError(f"[{section}].{key} must be a string")
        return value
    return value


assert is_dataclass(Config)
