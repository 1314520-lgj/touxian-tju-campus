"""Public campus launcher: ``python app.py`` or ``uvicorn app:app``.

Importing this module does not open storage or start a collector. Bootstrap is
performed by the application factory, or lazily on the first ASGI invocation.
"""

from __future__ import annotations

import math
import os
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def create_application(*, root=ROOT, start_worker=None):
    from server.campus.bootstrap import prepare_runtime
    from server.campus_app import create_app

    settings = prepare_runtime(root)
    enabled = (
        start_worker if start_worker is not None else os.getenv("CAMPUS_EMBEDDED_WORKER", "true").lower() == "true"
    )
    delay = float(os.getenv("CAMPUS_WORKER_DELAY_SECONDS", "20"))
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("CAMPUS_WORKER_DELAY_SECONDS must be finite and nonnegative")
    application = create_app(settings, start_worker=False)

    @application.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @asynccontextmanager
    async def lifespan(app):
        worker = thread = None
        stopped = threading.Event()
        try:
            if enabled:
                from server.campus.worker import Worker

                worker = Worker(app.state.core)

                def collect():
                    if stopped.wait(delay):
                        return
                    worker.run()

                thread = threading.Thread(target=collect, name="campus-worker", daemon=True)
                thread.start()
            yield
        finally:
            stopped.set()
            if worker:
                worker.stop()
            if thread:
                thread.join(timeout=20)
            if not thread or not thread.is_alive():
                app.state.core.store.close()

    application.router.lifespan_context = lifespan
    return application


class LazyApplication:
    """Keep the conventional ASGI entrypoint free of import-time side effects."""

    def __init__(self):
        self.application = None
        self._lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        if self.application is None:
            try:
                with self._lock:
                    if self.application is None:
                        self.application = create_application()
            except Exception as error:
                if scope["type"] == "lifespan":
                    # Uvicorn's auto mode treats an unreported exception as an
                    # unsupported lifespan protocol and may otherwise start HTTP.
                    await receive()
                    await send({"type": "lifespan.startup.failed", "message": str(error)})
                    return
                raise
        await self.application(scope, receive, send)


app = LazyApplication()


def bind_host(port):
    for host, family in (("0.0.0.0", socket.AF_INET), ("::", socket.AF_INET6)):
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((host, port))
                return host
            except OSError:
                continue
    return "0.0.0.0"


def main():
    import uvicorn

    port = int(os.getenv("PORT", "3000"))
    uvicorn.run(create_application(), host=bind_host(port), port=port, access_log=True)


if __name__ == "__main__":
    main()
