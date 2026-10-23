"""Transactional records and durable at-least-once job/outbox queues."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    delete,
    event,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import StaticPool

from .schema import now_iso, uid, validate

# Two connections opening the same *fresh* WAL database at once can be told
# either SQLITE_BUSY or, on Windows, SQLITE_READONLY while the winner switches
# the journal mode. `busy_timeout` only covers the busy form, so both are
# retried explicitly. Anything else — a genuinely read-only directory, a corrupt
# file, a bad path — must still fail loudly rather than be retried away.
TRANSIENT_SQLITE_CONTENTION = ("locked", "readonly", "read-only")


def _is_transient_contention(error):
    """True only for the concurrent-first-startup races described above."""
    original = getattr(error, "orig", error)
    message = str(original).lower()
    return any(marker in message for marker in TRANSIENT_SQLITE_CONTENTION)


def _error(status, code, message):
    from .platform import APIError

    return APIError(status, code, message)


def _stamp(value=None):
    if value is None:
        return now_iso()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("run_at must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Store:
    MAX_ATTEMPTS = 6

    def __init__(self, database_url: str):
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        self.database_url = database_url
        self._local = threading.local()
        self._memory_lock = threading.RLock()
        self._memory = database_url in {"sqlite://", "sqlite:///:memory:"}
        options = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if self._memory:
                options["poolclass"] = StaticPool
            elif database_url.startswith("sqlite:///"):
                Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(database_url, **options)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA foreign_keys=ON")
                if not self._memory:
                    # journal_mode does not reliably honor busy_timeout during
                    # concurrent first startup, so retry only this lock race.
                    for attempt in range(60):
                        try:
                            cursor.execute("PRAGMA journal_mode=WAL")
                            break
                        except sqlite3.OperationalError as exc:
                            if not _is_transient_contention(exc) or attempt == 59:
                                raise
                            time.sleep(0.05)
                cursor.close()

        self.metadata = MetaData()
        self.records = Table(
            "campus_records",
            self.metadata,
            Column("collection", String(80), primary_key=True),
            Column("key", String(255), primary_key=True),
            Column("data", JSON, nullable=False),
            Column("revision", Integer, nullable=False),
            Column("updated_at", String(40), nullable=False),
        )
        self.job_table = Table(
            "campus_jobs",
            self.metadata,
            Column("id", String(36), primary_key=True),
            Column("kind", String(64), nullable=False, index=True),
            Column("idempotency_key", String(512), nullable=False, unique=True),
            Column("payload_hash", String(64), nullable=False),
            Column("data", JSON, nullable=False),
            Column("payload", JSON, nullable=False),
            Column("state", String(20), nullable=False, index=True),
            Column("run_at", String(40), nullable=False, index=True),
            Column("lease_until", String(40)),
            Column("lease_token", String(64)),
            Column("worker_id", String(128)),
            Column("created_at", String(40), nullable=False),
        )
        self.outbox_table = Table(
            "campus_outbox",
            self.metadata,
            Column("id", String(36), primary_key=True),
            Column("data", JSON, nullable=False),
            Column("dispatched", Boolean, nullable=False, default=False, index=True),
            Column("created_at", String(40), nullable=False),
        )
        self.migration_table = Table(
            "campus_schema_versions",
            self.metadata,
            Column("version", Integer, primary_key=True),
            Column("applied_at", String(40), nullable=False),
        )
        self._bootstrap()

    BOOTSTRAP_ATTEMPTS = 60

    def _bootstrap(self):
        """Create the schema and record its version.

        Two processes — or two development cores — can open the same fresh data
        directory at once. The loser of that race can be told SQLITE_READONLY
        rather than SQLITE_BUSY while the winner switches the journal mode, so
        `busy_timeout` does not cover it and the `BEGIN IMMEDIATE` in
        `transaction()` fails outright. Retrying is safe rather than merely
        convenient: once the peer has committed, `create_all` is a no-op and the
        version row is already present, so the retry converges on exactly the
        state the winner produced instead of duplicating work.
        """
        attempts = self.BOOTSTRAP_ATTEMPTS if self.engine.dialect.name == "sqlite" else 1
        for attempt in range(attempts):
            try:
                with self.transaction():
                    if self.engine.dialect.name == "postgresql":
                        self._local.connection.exec_driver_sql("SELECT pg_advisory_xact_lock(734122017)")
                    self.metadata.create_all(self._local.connection)
                    versions = self._local.connection.execute(select(self.migration_table.c.version)).scalars().all()
                    if any(version > 1 for version in versions):
                        raise RuntimeError("Database schema is newer than this application")
                    if not versions:
                        self._local.connection.execute(
                            insert(self.migration_table).values(version=1, applied_at=now_iso())
                        )
                return
            except OperationalError as exc:
                if attempt == attempts - 1 or not _is_transient_contention(exc):
                    raise
                # A connection that lost the race can stay read-only for its
                # whole lifetime, so retrying on the pooled connection would
                # spin against the same broken handle. Drop the pool and open a
                # fresh one; the peer has usually finished by the next attempt.
                self.engine.dispose()
                time.sleep(0.05)

    def close(self):
        self.engine.dispose()

    @contextmanager
    def transaction(self):
        if getattr(self._local, "connection", None) is not None:
            try:
                yield self
            except BaseException:
                self._local.rollback_only = True
                raise
            return
        lock = self._memory_lock if self._memory else nullcontext()
        with lock:
            with self.engine.connect() as connection:
                transaction = connection.begin()
                self._local.connection = connection
                self._local.rollback_only = False
                try:
                    if self.engine.dialect.name == "sqlite":
                        connection.exec_driver_sql("BEGIN IMMEDIATE")
                    yield self
                    if self._local.rollback_only:
                        transaction.rollback()
                        raise _error(409, "TRANSACTION_ABORTED", "事务内操作失败，所有修改已回滚")
                    transaction.commit()
                except BaseException:
                    if transaction.is_active:
                        transaction.rollback()
                    raise
                finally:
                    self._local.connection = None
                    self._local.rollback_only = False

    @property
    def connection(self):
        """The connection bound to the active transaction, or None outside one."""
        return getattr(self._local, "connection", None)

    @contextmanager
    def read_connection(self):
        """Read without reserving the SQLite writer; reuse enclosing writes.

        Explicit BEGIN keeps multiple SQLite SELECTs on one committed snapshot.
        A standalone read is not bound as a write transaction in thread-local
        state. In-memory SQLite still serializes access to its single connection.
        """
        if self.connection is not None:
            with self.transaction():
                yield self.connection
            return
        lock = self._memory_lock if self._memory else nullcontext()
        with lock:
            with self.engine.connect() as connection, connection.begin():
                if self.engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN")
                yield connection

    def execute(self, statement, parameters=()):
        """Run raw SQL inside the active transaction.

        Used by the local FTS5 index, which is a virtual table and therefore
        cannot be expressed through the metadata-defined tables above. Raw SQL
        is only accepted while a transaction is open so it can never escape one.
        """
        connection = self.connection
        if connection is None:
            raise RuntimeError("raw SQL requires an open transaction")
        return connection.exec_driver_sql(statement, parameters)

    def _row(self, collection, key, lock=False, connection=None):
        statement = select(self.records).where(self.records.c.collection == collection, self.records.c.key == str(key))
        if lock and self.engine.dialect.name == "postgresql":
            statement = statement.with_for_update()
        if connection is None:
            connection = self._local.connection
        return connection.execute(statement).mappings().first()

    def get(self, collection, key):
        with self.read_connection() as connection:
            row = self._row(collection, key, lock=self.connection is not None, connection=connection)
            return copy.deepcopy(row["data"]) if row else None

    def list(self, collection):
        with self.read_connection() as connection:
            result = (
                connection.execute(
                    select(self.records.c.data)
                    .where(self.records.c.collection == collection)
                    .order_by(self.records.c.key)
                )
                .scalars()
                .all()
            )
            return copy.deepcopy(result)

    def revision(self, collection, key):
        with self.read_connection() as connection:
            row = self._row(collection, key, lock=self.connection is not None, connection=connection)
            return row["revision"] if row else 0

    def put(self, collection, key, data, expected_revision=None):
        data = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
        if not isinstance(data, dict):
            raise ValueError("Record data must be a JSON object")
        with self.transaction():
            row = self._row(collection, key, lock=True)
            revision = row["revision"] if row else 0
            if expected_revision is not None and expected_revision != revision:
                raise _error(409, "REVISION_CONFLICT", "数据已更新，请重新读取后再提交")
            values = {"data": data, "revision": revision + 1, "updated_at": now_iso()}
            if row:
                result = self._local.connection.execute(
                    update(self.records)
                    .where(
                        self.records.c.collection == collection,
                        self.records.c.key == str(key),
                        self.records.c.revision == revision,
                    )
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise _error(409, "REVISION_CONFLICT", "数据已更新，请重新读取后再提交")
            else:
                try:
                    with self._local.connection.begin_nested():
                        self._local.connection.execute(
                            insert(self.records).values(collection=collection, key=str(key), **values)
                        )
                except IntegrityError:
                    raise _error(409, "REVISION_CONFLICT", "记录已由其他请求创建") from None
            return copy.deepcopy(data)

    def delete(self, collection, key, expected_revision=None):
        with self.transaction():
            row = self._row(collection, key, lock=True)
            revision = row["revision"] if row else 0
            if expected_revision is not None and expected_revision != revision:
                raise _error(409, "REVISION_CONFLICT", "数据已更新，请重新读取后再提交")
            if row:
                result = self._local.connection.execute(
                    delete(self.records).where(
                        self.records.c.collection == collection,
                        self.records.c.key == str(key),
                        self.records.c.revision == revision,
                    )
                )
                if result.rowcount != 1:
                    raise _error(409, "REVISION_CONFLICT", "数据已更新，请重新读取后再提交")

    def emit(self, topic, aggregate_id, aggregate_revision, **fields):
        data = {
            "id": uid(),
            "schema_version": "1.0",
            "topic": topic,
            "aggregate_id": aggregate_id,
            "aggregate_revision": aggregate_revision,
            "user_id": None,
            "subject": None,
            "change_kind": "updated",
            "source_id": None,
            "snapshot_id": None,
            "candidate_id": None,
            "candidate_revision": None,
            "occurred_at": now_iso(),
            **fields,
        }
        validate("OutboxEvent", data)
        with self.transaction():
            self._local.connection.execute(
                insert(self.outbox_table).values(
                    id=data["id"], data=data, dispatched=False, created_at=data["occurred_at"]
                )
            )
        return copy.deepcopy(data)

    def enqueue(self, kind, payload, idempotency_key, run_at=None):
        if not idempotency_key or len(idempotency_key) > 512:
            raise _error(422, "IDEMPOTENCY_KEY_INVALID", "任务幂等键不能为空或超过512字符")
        supplied = copy.deepcopy(payload)
        forbidden = {"job_id", "schema_version", "kind", "idempotency_key"} & set(supplied)
        if forbidden:
            if (
                supplied.get("kind", kind) != kind
                or supplied.get("schema_version", "1.0") != "1.0"
                or supplied.get("idempotency_key", idempotency_key) != idempotency_key
            ):
                raise _error(422, "JOB_PAYLOAD_INVALID", "任务固定字段与队列参数冲突")
            for name in forbidden:
                supplied.pop(name)
        digest = hashlib.sha256(
            json.dumps(
                {"kind": kind, "payload": supplied}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        with self.transaction():
            connection = self._local.connection
            existing = (
                connection.execute(select(self.job_table).where(self.job_table.c.idempotency_key == idempotency_key))
                .mappings()
                .first()
            )
            if existing:
                if existing["payload_hash"] != digest:
                    raise _error(409, "IDEMPOTENCY_CONFLICT", "同一幂等键不能提交不同任务")
                return copy.deepcopy(existing["data"])
            job_id = uid()
            full = {
                "job_id": job_id,
                "schema_version": "1.0",
                "kind": kind,
                "correlation_id": supplied.get("correlation_id") or uid(),
                "source_id": None,
                "snapshot_id": None,
                "candidate_id": None,
                "candidate_revision": None,
                "subject": None,
                "user_id": None,
                "delivery_id": None,
                **supplied,
                "idempotency_key": idempotency_key,
            }
            validate("JobPayload", full)
            job = {
                "id": job_id,
                "kind": kind,
                "state": "queued",
                "idempotency_key": idempotency_key,
                "attempts": 0,
                "run_at": _stamp(run_at),
                "last_error_code": None,
            }
            validate("Job", job)
            try:
                with connection.begin_nested():
                    connection.execute(
                        insert(self.job_table).values(
                            id=job_id,
                            kind=kind,
                            idempotency_key=idempotency_key,
                            payload_hash=digest,
                            data=job,
                            payload=full,
                            state="queued",
                            run_at=job["run_at"],
                            created_at=now_iso(),
                        )
                    )
            except IntegrityError:
                existing = (
                    connection.execute(
                        select(self.job_table).where(self.job_table.c.idempotency_key == idempotency_key)
                    )
                    .mappings()
                    .first()
                )
                if not existing or existing["payload_hash"] != digest:
                    raise _error(409, "IDEMPOTENCY_CONFLICT", "同一幂等键不能提交不同任务") from None
                return copy.deepcopy(existing["data"])
            return copy.deepcopy(job)

    def jobs(self):
        with self.read_connection() as connection:
            return copy.deepcopy(
                connection.execute(
                    select(self.job_table.c.data).order_by(self.job_table.c.created_at, self.job_table.c.id)
                )
                .scalars()
                .all()
            )

    def get_job(self, job_id):
        with self.read_connection() as connection:
            value = connection.execute(
                select(self.job_table.c.data).where(self.job_table.c.id == job_id)
            ).scalar_one_or_none()
            return copy.deepcopy(value)

    def claim_job(self, worker_id, kinds=None, lease_seconds=120):
        if not worker_id or not 1 <= lease_seconds <= 3600:
            raise ValueError("A worker id and a lease of 1..3600 seconds are required")
        with self.transaction():
            now = now_iso()
            condition = or_(
                and_(self.job_table.c.state == "queued", self.job_table.c.run_at <= now),
                and_(self.job_table.c.state == "running", self.job_table.c.lease_until <= now),
            )
            statement = select(self.job_table).where(condition)
            if kinds is not None:
                if not kinds:
                    return None
                statement = statement.where(self.job_table.c.kind.in_(kinds))
            statement = statement.order_by(self.job_table.c.run_at, self.job_table.c.created_at).limit(1)
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            row = self._local.connection.execute(statement).mappings().first()
            if not row:
                return None
            data = copy.deepcopy(row["data"])
            if data["attempts"] >= self.MAX_ATTEMPTS:
                data.update(state="failed", last_error_code="LEASE_RETRY_EXHAUSTED")
                self._local.connection.execute(
                    update(self.job_table)
                    .where(self.job_table.c.id == row["id"])
                    .values(data=data, state="failed", lease_until=None, lease_token=None, worker_id=None)
                )
                return None
            data.update(state="running", attempts=data["attempts"] + 1)
            token = uid()
            lease_until = _stamp(datetime.now(UTC) + timedelta(seconds=lease_seconds))
            self._local.connection.execute(
                update(self.job_table)
                .where(self.job_table.c.id == row["id"])
                .values(data=data, state="running", lease_until=lease_until, lease_token=token, worker_id=worker_id)
            )
            if not hasattr(self._local, "leases"):
                self._local.leases = {}
            self._local.leases[row["id"]] = token
            return {"job": data, "payload": copy.deepcopy(row["payload"])}

    def _owned_job(self, job_id):
        statement = select(self.job_table).where(self.job_table.c.id == job_id)
        if self.engine.dialect.name == "postgresql":
            statement = statement.with_for_update()
        row = self._local.connection.execute(statement).mappings().first()
        if not row:
            raise _error(404, "JOB_NOT_FOUND", "任务不存在")
        if row["state"] != "running":
            raise _error(409, "JOB_STATE_CONFLICT", "任务未由当前执行器运行")
        if row["lease_token"] != getattr(self._local, "leases", {}).get(job_id):
            raise _error(409, "JOB_LEASE_LOST", "任务租约已失效，不能覆盖其他执行器的结果")
        return row

    def complete_job(self, job_id):
        with self.transaction():
            row = self._owned_job(job_id)
            data = copy.deepcopy(row["data"])
            data.update(state="succeeded", last_error_code=None)
            self._local.connection.execute(
                update(self.job_table)
                .where(self.job_table.c.id == job_id)
                .values(data=data, state="succeeded", lease_until=None, lease_token=None, worker_id=None)
            )
            self._local.leases.pop(job_id, None)

    def fail_job(self, job_id, error_code, retry_delay=30):
        with self.transaction():
            row = self._owned_job(job_id)
            data = copy.deepcopy(row["data"])
            terminal = data["attempts"] >= self.MAX_ATTEMPTS or retry_delay is None
            run_at = _stamp(datetime.now(UTC) + timedelta(seconds=max(0, retry_delay or 0)))
            data.update(state="failed" if terminal else "queued", run_at=run_at, last_error_code=str(error_code)[:100])
            self._local.connection.execute(
                update(self.job_table)
                .where(self.job_table.c.id == job_id)
                .values(
                    data=data, state=data["state"], run_at=run_at, lease_until=None, lease_token=None, worker_id=None
                )
            )
            self._local.leases.pop(job_id, None)

    def retry_job(self, job_id):
        with self.transaction():
            statement = select(self.job_table).where(self.job_table.c.id == job_id)
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update()
            row = self._local.connection.execute(statement).mappings().first()
            if not row:
                raise _error(404, "JOB_NOT_FOUND", "任务不存在")
            if row["state"] != "failed":
                raise _error(409, "JOB_STATE_CONFLICT", "只有失败任务可以人工重放")
            replay_key = "job-replay:" + job_id
            replay = self.get("idempotency", replay_key) or {"count": 0}
            if replay["count"] >= 3:
                raise _error(409, "JOB_REPLAY_EXHAUSTED", "任务已人工重放3次，请先修复原因")
            self.put("idempotency", replay_key, {"count": replay["count"] + 1})
            data = copy.deepcopy(row["data"])
            data.update(state="queued", run_at=now_iso(), attempts=0, last_error_code=None)
            self._local.connection.execute(
                update(self.job_table)
                .where(self.job_table.c.id == job_id)
                .values(data=data, state="queued", run_at=data["run_at"], lease_until=None, lease_token=None)
            )
            return data

    def pending_outbox(self):
        with self.transaction():
            statement = select(self.outbox_table.c.data).where(self.outbox_table.c.dispatched.is_(False))
            statement = statement.order_by(self.outbox_table.c.created_at, self.outbox_table.c.id)
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            return copy.deepcopy(self._local.connection.execute(statement).scalars().all())

    def mark_outbox_dispatched(self, event_id):
        with self.transaction():
            result = self._local.connection.execute(
                update(self.outbox_table).where(self.outbox_table.c.id == event_id).values(dispatched=True)
            )
            if result.rowcount != 1:
                raise _error(404, "OUTBOX_NOT_FOUND", "事务事件不存在")
