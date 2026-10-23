"""Cloud Run service and bounded queue Job entrypoints. No public scheduler route."""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
from contextlib import contextmanager

from .blob_store import public_origin, validate_bucket_name

WORKER_EXECUTION_LOCK = 734122018


@contextmanager
def worker_execution(store):
    """Only one scheduled execution drains this database at a time.

    This is a session lock on a dedicated connection, never a job transaction.
    PostgreSQL releases it when the process/connection dies. Job leases still
    fence individual tasks after abrupt termination.
    """
    if store.engine.dialect.name != "postgresql":
        raise ValueError("Scheduled cloud workers require PostgreSQL")
    with store.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        acquired = bool(
            connection.exec_driver_sql("SELECT pg_try_advisory_lock(%s)", (WORKER_EXECUTION_LOCK,)).scalar_one()
        )
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    connection.exec_driver_sql("SELECT pg_advisory_unlock(%s)", (WORKER_EXECUTION_LOCK,))
                except Exception:
                    # Never return a connection carrying a session lock to the pool.
                    connection.invalidate()


def scheduled_drain(worker, **limits):
    with worker_execution(worker.store) as acquired:
        if not acquired:
            return dict(
                rounds=0, dispatched=0, scheduled=0, processed=0, succeeded=0, failed=0, lease_lost=0, skipped=1
            )
        return {**drain(worker, **limits), "skipped": 0}


def cloud_settings():
    from .platform import Settings

    settings = Settings.from_env()
    if not settings.production:
        raise ValueError("Cloud Run requires CAMPUS_ENV=production")
    if os.getenv("CAMPUS_BLOB_STORE") != "gcs" or not os.getenv("CAMPUS_GCS_BUCKET"):
        raise ValueError("Cloud Run requires private GCS snapshot storage")
    validate_bucket_name(os.getenv("CAMPUS_GCS_BUCKET"))
    origin = public_origin()
    if settings.allowed_origins != [origin]:
        raise ValueError("CAMPUS_ALLOWED_ORIGINS must match CAMPUS_PUBLIC_ORIGIN exactly")
    return settings


def create_cloud_app():
    from server.campus_app import create_app

    return create_app(cloud_settings(), start_worker=False)


def validate_limits(max_seconds, max_rounds, max_jobs):
    if not 1 <= max_seconds <= 3300 or not 1 <= max_rounds <= 10000 or not 1 <= max_jobs <= 10000:
        raise ValueError("Invalid bounded worker limits")


def drain(worker, *, max_seconds=240, max_rounds=500, max_jobs=100, stop=None, monotonic=time.monotonic):
    """Budgets are checked at each job boundary; an acquired job finishes normally."""
    validate_limits(max_seconds, max_rounds, max_jobs)
    stop = stop or threading.Event()
    started = monotonic()
    totals = dict(rounds=0, dispatched=0, scheduled=0, processed=0, succeeded=0, failed=0, lease_lost=0)
    while not stop.is_set() and totals["rounds"] < max_rounds and totals["processed"] < max_jobs:
        if monotonic() - started >= max_seconds:
            break
        result = worker.run_once(max_jobs=1, sweep=totals["rounds"] == 0)
        totals["rounds"] += 1
        for key, value in result.items():
            totals[key] += value
        if not result["processed"] and not result["dispatched"]:
            break
    return totals


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cloud Run campus service / queue job")
    parser.add_argument("command", choices=["serve", "worker", "init-sources"])
    parser.add_argument("--max-seconds", type=int, default=240)
    parser.add_argument("--max-rounds", type=int, default=500)
    parser.add_argument("--max-jobs", type=int, default=100)
    args = parser.parse_args(argv)
    validate_limits(args.max_seconds, args.max_rounds, args.max_jobs)
    settings = cloud_settings()  # Validate before DB construction or writes.
    if args.command == "serve":
        import uvicorn

        port = int(os.getenv("PORT", "8080"))
        if not 1 <= port <= 65535:
            parser.error("PORT must be 1..65535")
        # Canonical origin supplies external URLs; no arbitrary proxy-header trust.
        uvicorn.run(
            "server.campus.cloud_run:create_cloud_app",
            factory=True,
            host="0.0.0.0",
            port=port,
            access_log=False,
            proxy_headers=False,
        )
        return 0
    from server.campus_app import assemble

    from .worker import Worker

    core = assemble(settings)
    stop = threading.Event()
    previous = {}

    def halt(signum, frame):
        stop.set()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, halt)
        if args.command == "init-sources":
            core.pipeline.sources.seed()
            print("Official source catalog registered; collection success not yet verified.")
            return 0
        result = scheduled_drain(
            Worker(core), max_seconds=args.max_seconds, max_rounds=args.max_rounds, max_jobs=args.max_jobs, stop=stop
        )
        print(json.dumps(result))  # Counts only: no student content, tokens, URLs or secrets.
        return int(bool(result["failed"] or result["lease_lost"]))
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        core.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
