"""
ai-media-workflow — entry point

Launches the FastAPI application via uvicorn.
Usage:
    python main.py          # dev server with auto-reload
    uvicorn app.main:app    # production-style launch

Environment overrides:
    HOST                     bind host (default 127.0.0.1)
    PORT                     bind port (default 8000, use 0 for an available port)
    AI_MEDIA_AUTO_MIGRATE    set 0 to skip automatic `alembic upgrade head`
"""

import logging
import os
import signal
import sys

from uvicorn.config import Config
from uvicorn.server import Server
from uvicorn.supervisors import ChangeReload

from app.database import upgrade_database

logger = logging.getLogger(__name__)


class _StartupAwareReload(ChangeReload):
    """Reload supervisor that exits when the child fails during startup.

    Uvicorn's default reloader waits forever for file changes when a lifespan
    startup error kills the worker. This subclass registers a SIGUSR1 handler
    so the worker child can notify the supervisor of a startup failure; the
    supervisor then stops instead of waiting for file changes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.startup_failed = False

    def startup(self) -> None:
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, self.signal_handler)
        super().startup()

    def signal_handler(self, sig: int, frame) -> None:  # pragma: full coverage
        if hasattr(signal, "SIGUSR1") and sig == signal.SIGUSR1:
            logger.error("Server process failed to start; shutting down reloader.")
            self.startup_failed = True
            self.should_exit.set()
        else:
            super().signal_handler(sig, frame)


def main():
    # Auto-apply pending Alembic migrations by default. Disable with
    # AI_MEDIA_AUTO_MIGRATE=0 if you prefer to run `alembic upgrade head`
    # manually.
    if os.environ.get("AI_MEDIA_AUTO_MIGRATE", "1") != "0":
        try:
            upgrade_database()
        except Exception as exc:
            logger.error("Database migration failed: %s", exc)
            sys.exit(1)

    # Marker used by app.main.lifespan to know the app is running under
    # main.py's auto-reload supervisor. This lets startup failures terminate
    # the whole command instead of leaving the reloader process alive.
    os.environ["AI_MEDIA_WORKFLOW_RELOAD"] = "1"

    config = Config(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=True,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
    server = Server(config=config)

    try:
        if config.should_reload:
            sock = config.bind_socket()
            reloader = _StartupAwareReload(config, target=server.run, sockets=[sock])
            reloader.run()
            if reloader.startup_failed:
                sys.exit(1)
        else:
            server.run()
            if not server.started:
                sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
