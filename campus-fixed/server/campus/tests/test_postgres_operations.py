"""Opt-in real PostgreSQL checks in a unique disposable schema on loopback.

Use deploy/cloudrun/test_postgres_local.py to provision an isolated server.
No production URL is read; tests do not reuse or drop existing schemas.
"""

import concurrent.futures
import importlib.util
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, text, update
from sqlalchemy.engine import make_url

from server.campus.cloud_run import scheduled_drain, worker_execution
from server.campus.db import Store
from server.campus.platform import APIError
from server.campus.schema import uid
from server.campus.worker import Worker

TEST_URL = os.getenv("CAMPUS_TEST_POSTGRES_URL")


@unittest.skipUnless(TEST_URL, "explicit isolated loopback PostgreSQL test URL required")
class PostgresOperationsTests(unittest.TestCase):
    def setUp(self):
        url = make_url(TEST_URL)
        if url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "::1", "localhost"}:
            self.fail("PostgreSQL acceptance URL must use loopback")
        if not (url.database or "").endswith("_test"):
            self.fail("Use an explicit disposable database ending in _test")
        url = url.set(drivername="postgresql+psycopg")
        self.schema = "campus_test_" + uid().replace("-", "")
        self.control = create_engine(url)
        with self.control.begin() as connection:
            connection.execute(text("CREATE SCHEMA " + self.schema))
        # Fresh schema names are generated locally, never interpolated user input.
        self.url = url.update_query_dict({"options": "-csearch_path=" + self.schema}).render_as_string(
            hide_password=False
        )
        self.stores = [Store(self.url), Store(self.url)]
        self.a, self.b = self.stores

    def tearDown(self):
        for store in getattr(self, "stores", []):
            store.close()
        if hasattr(self, "control"):
            with self.control.begin() as connection:
                connection.execute(text("DROP SCHEMA " + self.schema + " CASCADE"))
            self.control.dispose()

    def race(self, action):
        barrier = threading.Barrier(2)

        def run(store):
            barrier.wait(timeout=10)
            try:
                return action(store)
            except APIError as error:
                return error.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, self.stores))

    def test_cas_competing_updates_and_initial_insert(self):
        self.a.put("acceptance", "existing", {"value": 0})
        results = self.race(lambda store: store.put("acceptance", "existing", {"value": 1}, expected_revision=1))
        self.assertEqual(results.count("REVISION_CONFLICT"), 1)
        self.assertEqual(self.b.revision("acceptance", "existing"), 2)
        results = self.race(lambda store: store.put("acceptance", "new", {"value": 1}, expected_revision=0))
        self.assertEqual(results.count("REVISION_CONFLICT"), 1)
        self.assertEqual(self.b.revision("acceptance", "new"), 1)

    def test_concurrent_enqueue_replays_same_job_and_conflicting_body_fails(self):
        sid = uid()
        results = self.race(lambda store: store.enqueue("parse_document", {"snapshot_id": sid}, "same-receipt"))
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(len(self.b.jobs()), 1)
        with self.assertRaises(APIError) as caught:
            self.b.enqueue("parse_document", {"snapshot_id": uid()}, "same-receipt")
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_record_job_and_outbox_roll_back_together(self):
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.a.transaction():
                self.a.put("acceptance", "rollback", {"value": 1})
                sid = uid()
                self.a.emit("source.snapshot.created", sid, 1, snapshot_id=sid, source_id=uid())
                self.a.enqueue("parse_document", {"snapshot_id": sid}, "rollback")
                raise RuntimeError("abort")
        self.assertIsNone(self.b.get("acceptance", "rollback"))
        self.assertEqual(self.b.jobs(), [])
        self.assertEqual(self.b.pending_outbox(), [])
        with self.assertRaises(APIError) as caught:
            with self.a.transaction():
                self.a.put("acceptance", "nested", {"value": 1})
                try:
                    with self.a.transaction():
                        raise RuntimeError("inner")
                except RuntimeError:
                    pass
        self.assertEqual(caught.exception.code, "TRANSACTION_ABORTED")
        self.assertIsNone(self.b.get("acceptance", "nested"))

    def test_skip_locked_claims_and_expired_lease_fence(self):
        for i in range(2):
            self.a.enqueue("parse_document", {"snapshot_id": uid()}, "claim:" + str(i))
        claims = self.race(lambda store: store.claim_job("test-worker", lease_seconds=60))
        self.assertNotEqual(claims[0]["job"]["id"], claims[1]["job"]["id"])
        # Claim separately on this thread: Store deliberately owns leases by thread.
        self.a.enqueue("parse_document", {"snapshot_id": uid()}, "expire")
        first = self.a.claim_job("old-worker")
        jid = first["job"]["id"]
        with self.a.transaction():
            self.a._local.connection.execute(
                update(self.a.job_table)
                .where(self.a.job_table.c.id == jid)
                .values(lease_until="2000-01-01T00:00:00.000000Z")
            )
        self.assertEqual(self.b.claim_job("replacement")["job"]["id"], jid)
        with self.assertRaises(APIError) as caught:
            self.a.complete_job(jid)
        self.assertEqual(caught.exception.code, "JOB_LEASE_LOST")
        self.b.complete_job(jid)
        self.assertEqual(self.a.get_job(jid)["state"], "succeeded")

    def test_worker_outbox_fanout_is_durable_and_idempotent(self):
        sid, source = uid(), uid()
        self.a.emit("source.snapshot.created", sid, 1, snapshot_id=sid, source_id=source)
        workers = [Worker(SimpleNamespace(store=s, handlers={})) for s in self.stores]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(lambda worker: worker.dispatch_outbox(), workers))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(len(self.a.jobs()), 1)
        third = Store(self.url)
        try:
            self.assertEqual(third.pending_outbox(), [])
            self.assertEqual(len(third.jobs()), 1)
        finally:
            third.close()

    def test_live_read_only_preflight_checks_real_schema_and_queue(self):
        path = Path(__file__).resolve().parents[3] / "deploy/cloudrun/preflight.py"
        spec = importlib.util.spec_from_file_location("postgres_preflight", path)
        preflight = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(preflight)
        self.a.enqueue("parse_document", {"snapshot_id": uid()}, "preflight-existing")
        before = self.a.jobs()
        result = preflight.database_checks(self.url)
        self.assertTrue(all(value["ok"] for value in result), result)
        self.assertEqual(self.b.jobs(), before)
        self.assertEqual(self.b.list("acceptance"), [])

    def test_execution_gate_skips_overlapping_worker_and_recovers(self):
        calls = []

        class Probe:
            store = self.b

            def run_once(self, **kwargs):
                calls.append(kwargs)
                return dict(dispatched=0, scheduled=0, processed=0, succeeded=0, failed=0, lease_lost=0)

        with worker_execution(self.a) as acquired:
            self.assertTrue(acquired)
            self.a.put("acceptance", "independent", {"value": 1})
            self.assertEqual(scheduled_drain(Probe())["skipped"], 1)
            self.assertEqual(calls, [])
        self.assertEqual(scheduled_drain(Probe())["skipped"], 0)
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with worker_execution(self.a):
                raise RuntimeError("interrupted")
        with worker_execution(self.b) as acquired:
            self.assertTrue(acquired)

    def test_abrupt_process_exit_releases_execution_lock(self):
        child_code = """import os,sys
from server.campus.db import Store
from server.campus.cloud_run import worker_execution
store=Store(os.environ['CAMPUS_TEST_POSTGRES_URL'])
with worker_execution(store) as acquired:
 print('acquired' if acquired else 'failed',flush=True)
 sys.stdin.read()
"""
        env = {**os.environ, "CAMPUS_TEST_POSTGRES_URL": self.url}
        child = subprocess.Popen(
            [sys.executable, "-c", child_code],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "acquired")
            with worker_execution(self.b) as acquired:
                self.assertFalse(acquired)
            child.terminate()
            child.wait(timeout=10)
            deadline = time.monotonic() + 5
            while True:
                with worker_execution(self.b) as acquired:
                    if acquired:
                        break
                if time.monotonic() >= deadline:
                    self.fail("terminated session lock was not released")
                time.sleep(0.05)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            for stream in [child.stdin, child.stdout, child.stderr]:
                stream.close()


if __name__ == "__main__":
    unittest.main()
