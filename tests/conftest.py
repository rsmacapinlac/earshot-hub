"""Shared fixtures: build the whole application against the stub HAL, off-device."""

from __future__ import annotations

import pytest

from earshot.app import Application, build_application
from earshot.config import Config


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    """Factory that builds and starts an Application on the stub HAL in tmp_path."""
    for var in ("EARSHOT_HAL", "EARSHOT_CONFIG", "EARSHOT_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)

    started: list[Application] = []

    def _make(
        *,
        min_duration: int = 0,
        realtime: bool = True,
        disk_threshold: int = 90,
        service_url: str = "",
    ) -> Application:
        cfg = Config()
        cfg.storage.data_dir = str(tmp_path)
        cfg.recording.min_duration_seconds = min_duration
        cfg.storage.disk_threshold_percent = disk_threshold
        cfg.processing.service_url = service_url
        cfg.recording.chunk_duration_seconds = 900
        app = build_application(config=cfg, hal_override="stub", realtime=realtime)
        app.start()
        started.append(app)
        return app

    yield _make

    for app in started:
        app.stop()


@pytest.fixture
def app(app_factory):
    return app_factory(min_duration=0)


@pytest.fixture
def client(app):
    return app.flask_app.test_client()
