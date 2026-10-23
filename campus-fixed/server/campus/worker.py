"""Durable queue runner and transactional outbox dispatcher.

Handlers execute on the same thread that acquired the Store lease. External
sending remains an at-least-once operation whose owner must use a send claim.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import logging
import os
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
LOGGER = logging.getLogger(__name__)
REFRESH_SECONDS = 6 * 60 * 60
USER_TOPICS = {
    "user_event_state.changed",
    "personal_item.changed",
    "profile.changed",
    "subscriptions.changed",
    "delivery_preferences.changed",
}


def _moment(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _stamp(value):
    return _moment(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _correlation(key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "campus-worker:" + key))


def _safe_code(error):
    value = getattr(error, "code", None)
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", value) else "JOB_EXECUTION_FAILED"


class Worker:
    def __init__(
        self,
        core,
        handlers=None,
        worker_id=None,
        clock=None,
        poll_seconds=1.0,
        lease_seconds=3600,
        source_refresh_seconds=None,
    ):
        if not 0.05 <= poll_seconds <= 60:
            raise ValueError("poll_seconds must be 0.05..60")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be 1..3600")
        self.source_refresh_seconds = int(
            source_refresh_seconds
            if source_refresh_seconds is not None
            else os.getenv("CAMPUS_SOURCE_REFRESH_SECONDS", str(REFRESH_SECONDS))
        )
        if not 300 <= self.source_refresh_seconds <= 86400:
            raise ValueError("CAMPUS_SOURCE_REFRESH_SECONDS must be 300..86400")
        self.core, self.store = core, core.store
        self.handlers = handlers if handlers is not None else core.handlers
        self.worker_id = worker_id or "worker-" + str(uuid.uuid4())
        self.clock = clock or (lambda: datetime.now(UTC))
        self.poll_seconds, self.lease_seconds = poll_seconds, lease_seconds
        self._stop = threading.Event()
        self._run_lock = threading.Lock()

    def stop(self):
        self._stop.set()

    def dispatch_outbox(self):
        count = 0
        # Reading while locked and committing fanout with the dispatched bit is one unit.
        # Unique per-event/per-kind keys also make a transaction replay harmless.
        with self.store.transaction():
            events = self.store.pending_outbox()
            for event in events:
                topic = event["topic"]
                base = {"correlation_id": event["id"]}
                if topic == "source.snapshot.created":
                    plans = [
                        (
                            "parse_document",
                            {**base, "snapshot_id": event["snapshot_id"], "source_id": event["source_id"]},
                        )
                    ]
                elif topic == "content.parsed":
                    plans = [
                        (
                            "extract_candidates",
                            {**base, "snapshot_id": event["snapshot_id"], "source_id": event["source_id"]},
                        )
                    ]
                elif topic == "candidate.accepted":
                    plans = [
                        (
                            "reconcile_event",
                            {
                                **base,
                                "candidate_id": event["candidate_id"],
                                "candidate_revision": event["candidate_revision"],
                            },
                        )
                    ]
                elif topic == "public_event.changed":
                    plans = [
                        (kind, {**base, "subject": event["subject"]}) for kind in ("index_event", "plan_reminders")
                    ]
                elif topic in USER_TOPICS:
                    plans = [("plan_reminders", {**base, "user_id": event["user_id"], "subject": event["subject"]})]
                else:
                    raise ValueError("UNSUPPORTED_OUTBOX_TOPIC")
                for kind, payload in plans:
                    self.store.enqueue(kind, payload, f"outbox:{event['id']}:{kind}")
                self.store.mark_outbox_dispatched(event["id"])
                count += 1
        return count

    def sweep(self):
        if getattr(self.core, "delivery", None) and hasattr(self.core.delivery, "recover_abandoned_sends"):
            self.core.delivery.recover_abandoned_sends()
        now = _moment(self.clock())
        local = now.astimezone(TZ)
        count = 0
        slot = int(now.timestamp() // self.source_refresh_seconds)
        for source in self.store.list("sources"):
            if not source.get("enabled"):
                continue
            checked = source.get("last_checked_at")
            if checked:
                try:
                    if _moment(checked) > now - timedelta(seconds=self.source_refresh_seconds):
                        continue
                except (ValueError, TypeError):
                    pass
            key = f"source-refresh:{source['id']}:{slot}"
            payload = {"source_id": source["id"], "correlation_id": _correlation(key)}
            self.store.enqueue("fetch_source", payload, key, run_at=_stamp(now))
            count += 1
        for user in self.store.list("users"):
            prefs = (
                self.core.delivery.preferences(user["id"])
                if getattr(self.core, "delivery", None)
                else self.store.get("preferences", user["id"])
            )
            if not prefs or not prefs.get("daily_digest_enabled"):
                continue
            try:
                hour, minute = map(int, prefs["digest_time"].split(":"))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    continue
            except (TypeError, ValueError, KeyError):
                continue
            scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            day = local.date().isoformat()
            if local < scheduled or self.store.get("digests", f"{user['id']}:{day}"):
                continue
            generation = self.store.revision("preferences", user["id"])
            key = f"daily-digest:{user['id']}:{generation}:{prefs['digest_time']}:{day}"
            self.store.enqueue(
                "build_digest",
                {"user_id": user["id"], "correlation_id": _correlation(key)},
                key,
                run_at=_stamp(scheduled),
            )
            count += 1
        for delivery in self.store.list("deliveries"):
            if delivery.get("status") != "scheduled":
                continue
            try:
                if _moment(delivery["scheduled_at"]) > now:
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            key = f"delivery:{delivery['id']}:{delivery['scheduled_at']}"
            payload = {
                "delivery_id": delivery["id"],
                "user_id": delivery["user_id"],
                "correlation_id": _correlation(key),
            }
            self.store.enqueue("deliver_message", payload, key, run_at=_stamp(delivery["scheduled_at"]))
            count += 1
        return count

    def _invoke(self, job, payload):
        # Queue delay across midnight must not turn yesterday's requested digest into
        # an additional digest for today; today's sweep has its own stable key.
        key = job["idempotency_key"]
        if (
            job["kind"] == "build_digest"
            and key.startswith("daily-digest:")
            and key.rsplit(":", 1)[-1] != _moment(self.clock()).astimezone(TZ).date().isoformat()
        ):
            return
        handler = self.handlers[job["kind"]]
        result = handler(payload)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    def run_once(self, max_jobs=1, sweep=True):
        if not 1 <= max_jobs <= 1000:
            raise ValueError("max_jobs must be 1..1000")
        # A Worker object cannot share its Store lease across concurrently running threads.
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("WORKER_ALREADY_RUNNING")
        result = {"dispatched": 0, "scheduled": 0, "processed": 0, "succeeded": 0, "failed": 0, "lease_lost": 0}
        try:
            result["dispatched"] = self.dispatch_outbox()
            if sweep:
                result["scheduled"] = self.sweep()
            for _ in range(max_jobs):
                if self._stop.is_set():
                    break
                claimed = self.store.claim_job(
                    self.worker_id, kinds=list(self.handlers), lease_seconds=self.lease_seconds
                )
                if not claimed:
                    break
                job, payload = claimed["job"], claimed["payload"]
                result["processed"] += 1
                try:
                    self._invoke(job, payload)
                except Exception as error:
                    try:
                        self.store.fail_job(
                            job["id"], _safe_code(error), retry_delay=min(300, 30 * 2 ** max(0, job["attempts"] - 1))
                        )
                        result["failed"] += 1
                    except Exception as lost:
                        if getattr(lost, "code", None) not in {"JOB_LEASE_LOST", "JOB_STATE_CONFLICT"}:
                            raise
                        result["lease_lost"] += 1
                else:
                    try:
                        self.store.complete_job(job["id"])
                        result["succeeded"] += 1
                    except Exception as lost:
                        if getattr(lost, "code", None) not in {"JOB_LEASE_LOST", "JOB_STATE_CONFLICT"}:
                            raise
                        result["lease_lost"] += 1
            return result
        finally:
            self._run_lock.release()

    def run(self):
        while not self._stop.is_set():
            try:
                result = self.run_once(max_jobs=10)
                if not result["processed"] and not result["dispatched"]:
                    self._stop.wait(self.poll_seconds)
            except Exception as error:
                # Never log message text, auth headers, student content, or SMTP secrets.
                LOGGER.error("Campus worker cycle failed: %s", _safe_code(error))
                self._stop.wait(self.poll_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the campus durable worker")
    parser.add_argument("--app", default="server.campus_app:create_app", help="Module:app or Module:create_app")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-jobs", type=int, default=10)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    module, name = args.app.split(":", 1)
    value = getattr(importlib.import_module(module), name)
    app = value if hasattr(value, "state") else value()
    worker = Worker(app.state.core, poll_seconds=args.poll_seconds)
    try:
        if args.once:
            import json

            print(json.dumps(worker.run_once(max_jobs=args.max_jobs)))
        else:
            worker.run()
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":
    main()
