"""Isolated campus HTTP/worker entrypoint; never imports the desktop system APIs."""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server.campus.content import Pipeline
from server.campus.content import make_router as content_router
from server.campus.delivery import Delivery
from server.campus.delivery import make_router as delivery_router
from server.campus.events import Events
from server.campus.events import make_router as events_router
from server.campus.platform import Core, Settings, error_handlers
from server.campus.platform import make_router as platform_router
from server.campus.schema import uid
from server.campus.search import Search
from server.campus.search import make_router as search_router
from server.campus.source_requests import SourceRequests
from server.campus.source_requests import make_router as requests_router
from server.campus.sources import make_router as source_router

ROOT = Path(__file__).resolve().parents[1]


def assemble(settings=None):
    core = Core(settings or Settings.from_env())
    core.pipeline = Pipeline(core)
    core.events = Events(core)
    core.search = Search(core)
    core.delivery = Delivery(core)
    core.source_requests = SourceRequests(core)
    core.handlers.update(core.pipeline.handlers)
    core.handlers.update(
        reconcile_event=lambda p: core.events.reconcile_candidate(p["candidate_id"], p["candidate_revision"]),
        index_event=core.search.index_event,
        plan_reminders=core.delivery.plan_reminders,
        build_digest=core.delivery.build_digest,
        deliver_message=core.delivery.deliver_message,
    )
    return core


def create_app(settings=None, start_worker=None):
    core = assemble(settings)
    enabled = (
        start_worker if start_worker is not None else os.getenv("CAMPUS_EMBEDDED_WORKER", "true").lower() == "true"
    )

    @asynccontextmanager
    async def lifespan(app):
        worker = thread = None
        if enabled:
            from server.campus.worker import Worker

            worker = Worker(core)
            thread = threading.Thread(target=worker.run, name="campus-worker", daemon=True)
            thread.start()
        yield
        if worker:
            worker.stop()
        if thread:
            thread.join(timeout=20)
        if not thread or not thread.is_alive():
            core.store.close()

    # The interactive schema explorer and the derived OpenAPI document are
    # developer surfaces. They describe every admin route and payload shape, so
    # a public read-only instance must not advertise them; operators enable them
    # explicitly with CAMPUS_PUBLIC_API_DOCS=true (default: development only).
    expose_docs = (
        os.getenv("CAMPUS_PUBLIC_API_DOCS", "true" if core.settings.env != "production" else "false").lower() == "true"
    )
    docs_kwargs = {} if expose_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="偷闲·天大版", version="1.8.0", lifespan=lifespan, **docs_kwargs)
    app.state.core = core
    error_handlers(app)

    def boundary_error(request, status, code, message):
        request_id = getattr(request.state, "request_id", None) or uid()
        return JSONResponse(
            {"code": code, "message": message, "request_id": request_id, "details": {}},
            status_code=status,
            headers={
                "X-Request-ID": request_id,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.exception_handler(json.JSONDecodeError)
    async def malformed_json(request, exc):
        return boundary_error(request, 400, "INVALID_JSON", "请求必须为有效JSON")

    @app.exception_handler(UnicodeDecodeError)
    async def malformed_encoding(request, exc):
        return boundary_error(request, 400, "INVALID_JSON", "请求必须为有效JSON")

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in ["POST", "PUT", "PATCH"]:
            chunks = []
            size = 0
            attachment_upload = request.method == "POST" and re.fullmatch(
                r"/api/v1/admin/snapshots/[0-9a-fA-F-]{36}/replacement", request.url.path
            )
            limit = 8 * 1024 * 1024 + 262144 if attachment_upload else 262144
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    return boundary_error(request, 413, "BODY_TOO_LARGE", "提交内容过大")
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith(("/api/", "/calendar/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=core.settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "If-Match", "X-Request-ID"],
        expose_headers=["X-Dev-Login-Code", "ETag", "X-Request-ID"],
    )
    for router in [
        platform_router,
        source_router,
        content_router,
        events_router,
        search_router,
        delivery_router,
        requests_router,
    ]:
        app.include_router(router(core), prefix="/api/v1")
    dist = ROOT / "dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/campus.html", include_in_schema=False)
    def homepage():
        if (dist / "campus.html").exists():
            return FileResponse(dist / "campus.html")
        return JSONResponse({"message": "请先构建前端，或在开发服务打开 /campus.html"}, status_code=503)

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "worker", "init-sources", "demo"])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(create_app(), host="127.0.0.1", port=args.port, access_log=False)
        return
    selected = Settings.from_env()
    if args.command == "demo" and (selected.production or not selected.database_url.startswith("sqlite:")):
        parser.error("演示数据只允许写入本地 SQLite 环境")
    core = assemble(selected)
    if args.command == "init-sources":
        core.pipeline.sources.seed()
        print("已注册官方来源；尚未实际采集。")
    elif args.command == "demo":
        from server.campus.demo import seed_demo

        seed_demo(core)
        print("已写入明确标记的合成演示事项。")
    else:
        from server.campus.worker import Worker

        try:
            Worker(core).run()
        except KeyboardInterrupt:
            pass
    core.store.close()


if __name__ == "__main__":
    main()
