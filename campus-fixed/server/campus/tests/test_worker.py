"""Worker tests against the real durable SQLite queue, not an in-memory job list."""

import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.campus.platform import Core, Settings
from server.campus.schema import uid
from server.campus.worker import TZ, Worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = Core(Settings(data_dir=Path(self.tmp.name), secret="s" * 48))
        self.calls = []
        self.core.handlers = {
            kind: (lambda payload, k=kind: self.calls.append((k, payload)))
            for kind in [
                "fetch_source",
                "parse_document",
                "extract_candidates",
                "reconcile_event",
                "index_event",
                "plan_reminders",
                "build_digest",
                "deliver_message",
            ]
        }
        self.worker = Worker(self.core)

    def tearDown(self):
        self.core.store.close()
        self.tmp.cleanup()

    def emit(self, topic, **fields):
        return self.core.store.emit(topic, uid(), 1, **fields)

    def test_all_topics_fanout_and_correlations_are_stable(self):
        sid, source, candidate, user, event = uid(), uid(), uid(), uid(), uid()
        subject = {"kind": "public_event", "id": event, "revision": 1}
        emitted = [
            self.emit("source.snapshot.created", snapshot_id=sid, source_id=source),
            self.emit("content.parsed", snapshot_id=sid),
            self.emit("candidate.accepted", candidate_id=candidate, candidate_revision=1),
            self.emit("public_event.changed", subject=subject),
        ]
        for topic in [
            "user_event_state.changed",
            "profile.changed",
            "subscriptions.changed",
            "delivery_preferences.changed",
        ]:
            emitted.append(
                self.emit(topic, user_id=user, subject=subject if topic == "user_event_state.changed" else None)
            )
        emitted.append(
            self.emit(
                "personal_item.changed", user_id=user, subject={"kind": "personal_item", "id": uid(), "revision": 1}
            )
        )
        result = self.worker.run_once(max_jobs=20, sweep=False)
        self.assertEqual(result["dispatched"], 9)
        self.assertEqual(result["succeeded"], 10)
        self.assertEqual(len(self.calls), 10)
        self.assertEqual(self.core.store.pending_outbox(), [])
        counts = {kind: sum(k == kind for k, p in self.calls) for kind in self.core.handlers}
        self.assertEqual(counts["plan_reminders"], 6)
        self.assertTrue(all(p["correlation_id"] in {e["id"] for e in emitted} for k, p in self.calls))
        self.assertEqual(self.worker.dispatch_outbox(), 0)
        self.assertEqual(len(self.core.store.jobs()), 10)

    def test_fanout_and_mark_roll_back_together(self):
        event = self.emit("public_event.changed", subject={"kind": "public_event", "id": uid(), "revision": 1})
        with patch.object(
            self.core.store, "mark_outbox_dispatched", side_effect=RuntimeError("fixture transaction fail")
        ):
            with self.assertRaises(RuntimeError):
                self.worker.dispatch_outbox()
        self.assertEqual(self.core.store.jobs(), [])
        self.assertEqual(self.core.store.pending_outbox()[0]["id"], event["id"])
        self.assertEqual(self.worker.dispatch_outbox(), 1)
        self.assertEqual(len(self.core.store.jobs()), 2)

    def test_job_failure_is_retryable_with_sanitized_error(self):
        def fail(payload):
            raise RuntimeError("SYNTHETIC_SECRET_MUST_NOT_BE_LOGGED")

        self.worker.handlers = {"parse_document": fail}
        job = self.core.store.enqueue("parse_document", {"snapshot_id": uid()}, "fixture-fail")
        result = self.worker.run_once(sweep=False)
        self.assertEqual(result["failed"], 1)
        current = self.core.store.get_job(job["id"])
        self.assertEqual(current["state"], "queued")
        self.assertEqual(current["last_error_code"], "JOB_EXECUTION_FAILED")
        self.assertEqual(current["attempts"], 1)

    def test_unknown_handler_stays_queued_for_correct_worker(self):
        job = self.core.store.enqueue("parse_document", {"snapshot_id": uid()}, "fixture-handler")
        worker = Worker(self.core, handlers={"index_event": lambda p: None})
        self.assertEqual(worker.run_once(sweep=False)["processed"], 0)
        self.assertEqual(self.core.store.get_job(job["id"])["state"], "queued")

    def test_source_sweep_is_six_hour_deduplicated_and_enabled_only(self):
        now = datetime.now(UTC)
        for source in [
            {"id": uid(), "enabled": True, "last_checked_at": None},
            {"id": uid(), "enabled": False, "last_checked_at": None},
            {"id": uid(), "enabled": True, "last_checked_at": (now - timedelta(hours=1)).isoformat()},
        ]:
            self.core.store.put("sources", source["id"], source)
        worker = Worker(self.core, clock=lambda: now)
        worker.sweep()
        worker.sweep()
        jobs = [j for j in self.core.store.jobs() if j["kind"] == "fetch_source"]
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["idempotency_key"].startswith("source-refresh:"))
        worker.clock = lambda: now + timedelta(hours=7)
        worker.sweep()
        self.assertEqual(sum(j["kind"] == "fetch_source" for j in self.core.store.jobs()), 3)

    def test_daily_digest_once_at_local_time_and_disabled_preference(self):
        user1, user2 = uid(), uid()
        self.core.store.put("users", user1, {"id": user1})
        self.core.store.put("users", user2, {"id": user2})
        prefs = {
            user1: {"daily_digest_enabled": True, "digest_time": "08:00"},
            user2: {"daily_digest_enabled": False, "digest_time": "08:00"},
        }
        self.core.delivery = SimpleNamespace(preferences=lambda uid: prefs[uid])
        worker = Worker(self.core, clock=lambda: datetime(2026, 10, 8, 7, 59, tzinfo=TZ))
        worker.sweep()
        self.assertFalse(self.core.store.jobs())
        worker.clock = lambda: datetime(2026, 10, 8, 8, 0, tzinfo=TZ)
        worker.sweep()
        worker.sweep()
        self.assertEqual(len(self.core.store.jobs()), 1)
        self.assertTrue(self.core.store.jobs()[0]["idempotency_key"].endswith("2026-10-08"))
        worker.clock = lambda: datetime(2026, 10, 9, 8, 0, tzinfo=TZ)
        worker.sweep()
        self.assertEqual(len(self.core.store.jobs()), 2)

    def test_delayed_digest_does_not_build_wrong_day(self):
        user = uid()
        self.core.store.enqueue("build_digest", {"user_id": user}, f"daily-digest:{user}:2000-01-01")
        result = self.worker.run_once(sweep=False)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(self.calls, [])

    def test_due_delivery_key_tracks_schedule_and_never_queues_unknown_sending_sent(self):
        now = datetime.now(UTC)
        due = (now - timedelta(minutes=1)).isoformat()
        user = uid()
        identifier = uid()
        for status in ["scheduled", "sending", "unknown", "sent", "cancelled", "failed"]:
            ident = identifier if status == "scheduled" else uid()
            self.core.store.put(
                "deliveries",
                ident,
                {"id": ident, "user_id": user, "subject": None, "status": status, "scheduled_at": due},
            )
        worker = Worker(self.core, clock=lambda: now)
        worker.sweep()
        worker.sweep()
        self.assertEqual(len(self.core.store.jobs()), 1)
        worker.run_once(sweep=False)
        self.assertEqual(len(self.calls), 1)
        d = self.core.store.get("deliveries", identifier)
        d["scheduled_at"] = (now - timedelta(seconds=1)).isoformat()
        self.core.store.put("deliveries", identifier, d)
        worker.sweep()
        self.assertEqual(len(self.core.store.jobs()), 2)

    def test_delivery_revision_change_does_not_conflict_with_pending_queue_key(self):
        now = datetime.now(UTC)
        identifier, user = uid(), uid()
        subject = {"kind": "public_event", "id": uid(), "revision": 1}
        row = {
            "id": identifier,
            "user_id": user,
            "subject": subject,
            "status": "scheduled",
            "scheduled_at": (now - timedelta(minutes=1)).isoformat(),
        }
        self.core.store.put("deliveries", identifier, row)
        worker = Worker(self.core, clock=lambda: now)
        worker.sweep()
        row["subject"]["revision"] = 2
        self.core.store.put("deliveries", identifier, row)
        worker.sweep()
        self.assertEqual(len(self.core.store.jobs()), 1)

    def test_two_workers_claim_one_delivery_only_once(self):
        user, identifier = uid(), uid()
        self.core.store.enqueue(
            "deliver_message", {"delivery_id": identifier, "user_id": user}, "delivery:fixture:same-time"
        )
        barrier = threading.Barrier(2)
        calls = []
        errors = []
        lock = threading.Lock()

        def handler(payload):
            with lock:
                calls.append(payload["delivery_id"])
            time.sleep(0.05)

        def run():
            worker = Worker(self.core, handlers={"deliver_message": handler})
            barrier.wait()
            try:
                worker.run_once(sweep=False)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(calls, [identifier])
        self.assertEqual(self.core.store.jobs()[0]["state"], "succeeded")

    def test_same_thread_lease_and_stop_control(self):
        seen = []
        worker = Worker(self.core, handlers={"parse_document": lambda p: seen.append(threading.get_ident())})
        self.core.store.enqueue("parse_document", {"snapshot_id": uid()}, "fixture-thread")
        worker.run_once(sweep=False)
        self.assertEqual(seen, [threading.get_ident()])
        worker.stop()
        self.core.store.enqueue("parse_document", {"snapshot_id": uid()}, "fixture-stopped")
        self.assertEqual(worker.run_once(sweep=False)["processed"], 0)

    def test_run_stop_interrupts_idle_wait(self):
        worker = Worker(self.core, handlers={}, poll_seconds=30)
        thread = threading.Thread(target=worker.run)
        thread.start()
        time.sleep(0.05)
        worker.stop()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())

    def test_async_handler_is_awaited(self):
        result = []

        async def handler(payload):
            result.append(payload["snapshot_id"])

        sid = uid()
        self.core.store.enqueue("parse_document", {"snapshot_id": sid}, "fixture-async")
        worker = Worker(self.core, handlers={"parse_document": handler})
        self.assertEqual(worker.run_once(sweep=False)["succeeded"], 1)
        self.assertEqual(result, [sid])


if __name__ == "__main__":
    unittest.main()
