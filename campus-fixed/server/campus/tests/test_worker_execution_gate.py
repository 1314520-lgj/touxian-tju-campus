"""Scheduled execution fencing; SQL protocol tests complement real PG tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from server.campus.cloud_run import WORKER_EXECUTION_LOCK, scheduled_drain, worker_execution


class ExecutionGateTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.engine = MagicMock()
        self.engine.dialect.name = "postgresql"
        self.engine.connect.return_value.execution_options.return_value.__enter__.return_value = self.connection
        self.store = SimpleNamespace(engine=self.engine)

    def acquired(self, value):
        self.connection.exec_driver_sql.return_value.scalar_one.return_value = value

    def test_losing_execution_does_not_touch_worker(self):
        self.acquired(False)
        worker = MagicMock(store=self.store)
        with patch("server.campus.cloud_run.drain") as drain:
            result = scheduled_drain(worker)
        drain.assert_not_called()
        worker.run_once.assert_not_called()
        self.assertEqual(
            result,
            dict(rounds=0, dispatched=0, scheduled=0, processed=0, succeeded=0, failed=0, lease_lost=0, skipped=1),
        )
        self.assertEqual(self.connection.exec_driver_sql.call_count, 1)

    def test_lock_is_held_around_entire_drain_and_unlocked_after_error(self):
        self.acquired(True)

        def fail(*args, **kwargs):
            self.assertEqual(self.connection.exec_driver_sql.call_count, 1)
            raise RuntimeError("handler failed")

        with patch("server.campus.cloud_run.drain", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "handler failed"):
                scheduled_drain(SimpleNamespace(store=self.store))
        self.connection.exec_driver_sql.assert_called_with("SELECT pg_advisory_unlock(%s)", (WORKER_EXECUTION_LOCK,))
        self.engine.connect.return_value.execution_options.assert_called_with(isolation_level="AUTOCOMMIT")

    def test_unlock_failure_invalidates_connection_before_pool_return(self):
        self.connection.exec_driver_sql.side_effect = [
            SimpleNamespace(scalar_one=lambda: True),
            RuntimeError("connection lost"),
        ]
        with worker_execution(self.store) as acquired:
            self.assertTrue(acquired)
        self.connection.invalidate.assert_called_once()

    def test_scheduled_cloud_worker_rejects_sqlite(self):
        self.engine.dialect.name = "sqlite"
        with self.assertRaisesRegex(ValueError, "require PostgreSQL"):
            with worker_execution(self.store):
                self.fail("SQLite must not imply cross-instance protection")


if __name__ == "__main__":
    unittest.main()
