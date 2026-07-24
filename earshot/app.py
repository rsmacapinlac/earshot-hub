"""Application wiring: config -> HAL -> storage -> control loop -> API.

``python -m earshot`` calls :func:`main`, which builds the application, starts the
control loop, and serves the web UI/API with waitress. Off-device this runs against
the stub HAL (``EARSHOT_HAL=stub``).
"""

from __future__ import annotations

import logging
import os
import signal
from dataclasses import dataclass

from flask import Flask

from earshot.api.server import create_app
from earshot.config import Config
from earshot.hal.bundle import Hal, build_hal
from earshot.statemachine.machine import Controller
from earshot.storage.db import Database
from earshot.storage.store import Store

log = logging.getLogger("earshot")


@dataclass
class Application:
    config: Config
    hal: Hal
    db: Database
    store: Store
    controller: Controller
    flask_app: Flask

    def start(self) -> None:
        self.controller.start()
        self.controller.wait_ready(timeout=5)

    def stop(self) -> None:
        self.controller.stop()
        self.db.close()


def build_application(
    *,
    config: Config | None = None,
    hal_override: str | None = None,
    realtime: bool = True,
    shutdown_fn=None,
    reconcile_on_start: bool = True,
) -> Application:
    config = config or Config.load()
    hal = build_hal(config, override=hal_override, realtime=realtime)
    db = Database(config.db_path)
    store = Store(config, db)
    if reconcile_on_start:
        # Recover from crashes / a lost DB before the control loop can allocate
        # new ids (rpi/specs/storage.md#reconciliation).
        from earshot.storage.reconcile import reconcile

        reconcile(store, hal.capture.spec)
    controller = Controller(config, hal, store, shutdown_fn=shutdown_fn)
    flask_app = create_app(controller, store, config)
    return Application(
        config=config, hal=hal, db=db, store=store,
        controller=controller, flask_app=flask_app,
    )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("EARSHOT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = build_application()
    app.start()

    def _handle_signal(signum, _frame):
        log.info("received signal %s; shutting down", signum)
        app.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if not app.config.web.enabled:
        log.info("web disabled; running headless")
        signal.pause()
        return

    from waitress import serve

    log.info(
        "serving on http://%s:%s (HAL=%s)",
        app.config.web.bind_address, app.config.web.port, app.hal.name,
    )
    serve(
        app.flask_app,
        host=app.config.web.bind_address,
        port=app.config.web.port,
        threads=8,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
