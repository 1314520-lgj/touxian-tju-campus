"""Committed reads coexist with writers without weakening transaction safety."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.campus.db import Store
from server.campus.platform import APIError
from server.campus.schema import uid


@pytest.mark.parametrize("operation", ["get", "list", "revision", "jobs", "get_job"])
def test_committed_read_completes_while_another_connection_holds_a_write(tmp_path, operation):
    url = f"sqlite:///{tmp_path / 'concurrent.sqlite3'}"
    writer, reader = Store(url), Store(url)
    try:
        writer.put("items", "a", {"value": ["committed"]})
        job = writer.enqueue("parse_document", {"snapshot_id": uid()}, "read-fixture")
        reads = {
            "get": lambda: reader.get("items", "a"),
            "list": lambda: reader.list("items"),
            "revision": lambda: reader.revision("items", "a"),
            "jobs": reader.jobs,
            "get_job": lambda: reader.get_job(job["id"]),
        }
        before = reads[operation]()
        blocked = False
        with ThreadPoolExecutor(max_workers=1) as pool:
            with writer.transaction():
                writer.put("items", "a", {"value": ["uncommitted"]})
                writer.claim_job("fixture-worker")
                future = pool.submit(reads[operation])
                try:
                    during = future.result(timeout=1)
                except TimeoutError:
                    blocked = True
            # Release the writer before joining even if the regression recurs.
            future.result(timeout=5)
        assert not blocked, "A read waited for the unrelated writer to commit"
        assert during == before, "Readers must not expose uncommitted writes"
        assert reads[operation]() != before
    finally:
        reader.close()
        writer.close()


def test_read_connection_keeps_one_sqlite_snapshot_without_blocking_a_commit(tmp_path):
    url = f"sqlite:///{tmp_path / 'snapshot.sqlite3'}"
    reader, writer = Store(url), Store(url)
    try:
        writer.put("items", "a", {"value": 1})
        query = select(reader.records.c.data).where(reader.records.c.collection == "items")
        with reader.read_connection() as connection:
            assert connection.execute(query).scalar_one() == {"value": 1}
            writer.put("items", "a", {"value": 2})
            assert connection.execute(query).scalar_one() == {"value": 1}
        assert reader.get("items", "a") == {"value": 2}
    finally:
        reader.close()
        writer.close()


@pytest.fixture(params=["file", "memory"])
def store(request, tmp_path):
    value = Store("sqlite://" if request.param == "memory" else f"sqlite:///{tmp_path / 'local.sqlite3'}")
    yield value
    value.close()


def test_reads_inside_a_write_see_own_changes_and_rollback_together(store):
    store.put("items", "a", {"value": 1})
    with pytest.raises(ValueError):
        with store.transaction():
            store.put("items", "a", {"value": 2})
            assert store.get("items", "a") == {"value": 2}
            assert store.list("items") == [{"value": 2}]
            assert store.revision("items", "a") == 2
            raise ValueError("synthetic rollback")
    assert store.get("items", "a") == {"value": 1}
    assert store.revision("items", "a") == 1


def test_caught_read_failure_still_aborts_its_outer_write_transaction(store):
    store.put("items", "a", {"value": 1})
    with pytest.raises(APIError) as caught:
        with store.transaction():
            store.put("items", "a", {"value": 2})
            with patch.object(store, "_row", side_effect=ValueError("synthetic read failure")):
                with pytest.raises(ValueError):
                    store.get("items", "a")
    assert caught.value.code == "TRANSACTION_ABORTED"
    assert store.get("items", "a") == {"value": 1}


def test_read_results_remain_detached_and_missing_values_unchanged(store):
    store.put("items", "a", {"value": [1]})
    store.get("items", "a")["value"].append(2)
    store.list("items")[0]["value"].append(3)
    assert store.get("items", "a") == {"value": [1]}
    assert store.get("items", "missing") is None
    assert store.revision("items", "missing") == 0
    assert store.get_job(uid()) is None
