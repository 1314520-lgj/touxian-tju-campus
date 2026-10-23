"""Independent audit: the FTS narrowing must not change what a query returns.

``test_search_index`` pins the property on a three-notice synthetic fixture.
This module re-derives it on the *shipped* sample database, so the corpus is the
real 73 published notices rather than hand-written paragraphs, and it also
asserts that the accelerated path is genuinely taken — an equivalence test that
never exercises the accelerator would pass for the wrong reason.

Nothing here changes the product. It is a second opinion on the claim in
``UPGRADE_1_9`` that narrowing reduces work but never results.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from server.campus.platform import Settings
from server.campus.tests.test_search_index import indexed, unindexed
from server.campus.zh_index import LocalTextIndex
from server.campus_app import create_app

SAMPLE = Path(__file__).resolve().parents[3] / "data"

# Queries chosen from the shipped titles: broad, narrow, ASCII (not
# narrowable), a phrase that matches nothing, and one long specific term.
QUERIES = [
    "奖学金",
    "天津大学",
    "双选会",
    "博士研究生",
    "暑期",
    "通知",
    "碳捕集",
    "机械工程学院",
    "专利证书",
    "GPA",
    "不存在的词组",
]


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    target = tmp_path_factory.mktemp("shipped-sample") / "data"
    shutil.copytree(SAMPLE, target)
    with patch.dict(os.environ, {"CAMPUS_MEILI_URL": "", "CAMPUS_DEMO": "false"}):
        app = create_app(
            Settings(
                data_dir=target, secret="corpus-audit-secret-" * 4, dev_auth=True, admin_emails={"audit@example.com"}
            ),
            start_worker=False,
        )
    yield app.state.core
    app.state.core.store.close()


def published(core):
    return [e for e in core.store.list("events") if e["publication_state"] == "published"]


def test_the_sample_really_is_the_shipped_corpus(core):
    """Guard: if this stops holding, the equivalence below proves nothing."""
    assert len(published(core)) == 73
    assert core.search.local.supported, "the sample must be able to host the index"


def test_a_full_refresh_leaves_rows_for_every_current_receipt(core):
    """Rows and receipt are written in one transaction, so they cannot diverge.

    This is the invariant that makes the early skip safe: a receipt claiming a
    version is only trustworthy because the rows for that version were committed
    with it. If they could diverge, a current receipt with no rows would hide a
    match instead of merely skipping work.
    """
    for event in published(core):
        core.search.index_event({"subject": {"kind": "public_event", "id": event["id"], "revision": event["version"]}})
    receipts = core.search.local.receipts()
    with core.store.read_connection() as connection:
        rows = connection.exec_driver_sql("SELECT DISTINCT event_id, event_version FROM campus_segment_fts").fetchall()
    indexed_versions = {(event_id, version) for event_id, version in rows}
    current = {event["id"]: event["version"] for event in published(core)}
    for event_id, version in receipts.items():
        if event_id not in current or current[event_id] != version:
            continue  # a stale receipt is allowed and only costs a full scan
        segments = list(core.pipeline.segments(core.events.current_snapshot_ids(event_id)))
        if not segments:
            continue  # an event with no paragraphs cannot match in either path
        assert (event_id, version) in indexed_versions, f"current receipt without rows would hide matches: {event_id}"


@pytest.mark.parametrize("query", QUERIES)
def test_narrowing_is_result_identical_on_the_shipped_corpus(core, query):
    indexed(core)
    with unindexed():
        expected = core.search.hits(query)
    assert core.search.hits(query) == expected


def test_per_event_title_bigram_is_result_identical(core):
    """One pure-Han query derived from every published title.

    Titles mostly contain digits and Latin, which are not narrowable, so this
    picks a Han bigram out of each title to make sure the accelerator is
    exercised across the whole corpus rather than only by the fixed list above.
    """
    indexed(core)
    queries = set()
    for event in published(core):
        run = ""
        for character in event["title"] + " ":
            if "\u4e00" <= character <= "\u9fff":
                run += character
            else:
                if len(run) >= 2:
                    queries.add(run[:2])
                run = ""
    assert len(queries) > 40, f"expected a broad query set, got {len(queries)}"
    for query in sorted(queries):
        with unindexed():
            expected = core.search.hits(query)
        assert core.search.hits(query) == expected, query


def test_the_accelerated_path_is_actually_taken(core):
    """Without this, every equivalence assertion above could be vacuous."""
    indexed(core)
    seen: list[bool] = []
    original = LocalTextIndex.lookup

    def spy(self, terms):
        keys, receipts = original(self, terms)
        seen.append(keys is not None)
        return keys, receipts

    with patch.object(LocalTextIndex, "lookup", spy):
        for query in QUERIES:
            core.search.hits(query)
    assert any(seen), "no query reached the index; the equivalence check is vacuous"
    assert not all(seen), "the ASCII query must still take the full scan"


def test_a_stale_receipt_forces_the_full_scan_and_cannot_hide_a_match(core):
    """The guard that protects correctness when the index lags behind an event."""
    indexed(core)
    target = next(event for event in published(core) if "天津" in event["title"])
    query = "天津大学"
    assert core.search.hits(query), "the corpus must match the probe query at all"
    with unindexed():
        expected = core.search.hits(query)
    # Claim the index describes an older revision of this event. Rows are left
    # in place, so only the receipt rule can keep the event from being skipped.
    core.store.put(
        "local_index_receipts",
        target["id"],
        {
            "event_id": target["id"],
            "version": target["version"] - 1,
        },
    )
    assert core.search.hits(query) == expected


def test_dropping_index_rows_for_a_stale_event_still_finds_it(core):
    """Removing an event from the index must degrade to a scan, not to a miss."""
    indexed(core)
    target = next(event for event in published(core) if "天津" in event["title"])
    query = "天津大学"
    with unindexed():
        expected = core.search.hits(query)
    assert any(hit[1]["event_id"] == target["id"] for hit in expected)
    with core.store.transaction():
        core.store.execute("DELETE FROM campus_segment_fts WHERE event_id = ?", (target["id"],))
        core.store.put(
            "local_index_receipts",
            target["id"],
            {
                "event_id": target["id"],
                "version": target["version"] - 1,
            },
        )
    assert core.search.hits(query) == expected
