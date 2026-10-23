"""Index acceleration preserves the scanner's results, including concurrent refreshes."""

from unittest.mock import patch

import pytest
from sqlalchemy import event as sql_event

from server.campus.db import Store
from server.campus.tests.test_data_audit import (  # noqa: F401 -- env/notice are pytest fixtures registered by importing them into this module namespace; parameters do not count as "usage" for linters.
    env,
    notice,
)
from server.campus.tests.test_search_index import indexed, unindexed
from server.campus.zh_index import LocalTextIndex


def test_current_index_no_match_does_not_load_notice_bodies(env):
    _, core, _, accept = env
    for title in ["合成通知甲", "合成通知乙", "合成通知丙"]:
        accept(notice(core, title=title))
    indexed(core)
    with patch.object(core.events, "current_snapshot_ids", side_effect=AssertionError("unneeded snapshots")):
        with patch.object(core.pipeline, "segments", side_effect=AssertionError("unneeded body read")):
            assert core.search.hits("银河计数器") == []


def test_current_index_loads_only_events_with_possible_matches(env):
    _, core, _, accept = env
    matched = accept(notice(core, title="合成通知甲", extra="<p>银河计数器标记。</p>"))
    accept(notice(core, title="合成通知乙"))
    indexed(core)
    with patch.object(core.events, "current_snapshot_ids", wraps=core.events.current_snapshot_ids) as snapshots:
        rows = core.search.hits("银河计数器")
    assert [row[1]["event_id"] for row in rows] == [matched["event"]["id"]]
    assert snapshots.call_args_list == [((matched["event"]["id"],), {})]


def test_index_refresh_between_lookup_and_scanning_cannot_hide_a_new_match(env):
    _, core, _, accept = env
    first = accept(notice(core))
    indexed(core, first["event"])
    current = accept(notice(core, source=first["source"], url=first["url"], extra="<p>银河计数器标记。</p>"))
    queried, refreshed = False, False

    def observe_match(connection, cursor, statement, parameters, context, executemany):
        nonlocal queried
        if "WHERE campus_segment_fts MATCH" in statement:
            queried = True

    def refresh_after_connection_returns(connection, record):
        nonlocal refreshed
        if queried and not refreshed:
            refreshed = True
            indexed(core, current["event"])

    # Deterministically commit a refresh after the key SELECT's connection is
    # returned. Separate keys()/receipts() calls then combine old keys with a
    # new receipt, incorrectly skipping the new paragraph.
    sql_event.listen(core.store.engine, "after_cursor_execute", observe_match)
    sql_event.listen(core.store.engine, "checkin", refresh_after_connection_returns)
    try:
        rows = core.search.hits("银河计数器")
    finally:
        sql_event.remove(core.store.engine, "after_cursor_execute", observe_match)
        sql_event.remove(core.store.engine, "checkin", refresh_after_connection_returns)
    assert refreshed
    assert [row[1]["event_id"] for row in rows] == [current["event"]["id"]]
    assert rows[0][1]["event_version"] == current["event"]["version"]


def test_index_keys_and_receipts_share_a_snapshot_during_a_concurrent_commit(env):
    _, core, _, accept = env
    first = accept(notice(core))
    indexed(core, first["event"])
    current = accept(notice(core, source=first["source"], url=first["url"], extra="<p>银河计数器标记。</p>"))
    segments = core.pipeline.segments(core.events.current_snapshot_ids(current["event"]["id"]))
    writer = Store(core.store.database_url)
    other_index = LocalTextIndex(writer)
    assert other_index.supported
    refreshed = False

    def refresh_during_select(connection, cursor, statement, parameters, context, executemany):
        nonlocal refreshed
        if "WHERE campus_segment_fts MATCH" in statement and not refreshed:
            refreshed = True
            with writer.transaction():
                other_index.replace(current["event"], segments)
                writer.put(
                    "local_index_receipts",
                    current["event"]["id"],
                    {
                        "event_id": current["event"]["id"],
                        "version": current["event"]["version"],
                    },
                )

    sql_event.listen(core.store.engine, "after_cursor_execute", refresh_during_select)
    try:
        keys, receipts = core.search.local.lookup(["银河计数器"])
    finally:
        sql_event.remove(core.store.engine, "after_cursor_execute", refresh_during_select)
        writer.close()
    assert refreshed
    assert keys == {}
    assert receipts[first["event"]["id"]] == first["event"]["version"]
    assert core.search.local.receipts()[first["event"]["id"]] == current["event"]["version"]


@pytest.mark.parametrize("query", ["银河计数器", "GPA", "申请", "不存在词", "合成通知甲"])
def test_index_preserves_scores_order_and_verbatim_evidence(env, query):
    _, core, _, accept = env
    accept(notice(core, title="合成通知甲", extra="<p>银河计数器 GPA3.5 报名说明。</p>"))
    accept(notice(core, title="合成通知乙", extra="<p>银河计数器 申请说明。</p>"))
    accept(notice(core, title="合成通知丙", extra="<p>无关的正文。</p>"))
    indexed(core)
    with unindexed():
        expected = core.search.hits(query)
    actual = core.search.hits(query)
    assert actual == expected
    for _, hit, evidence, _ in actual:
        parsed = core.pipeline.parsed(evidence["snapshot_id"])
        segment = next(s for s in parsed["segments"] if s["id"] == evidence["segment_id"])
        assert hit["excerpt"] == evidence["quote"]
        assert evidence["quote"] in segment["text"]
