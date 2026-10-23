"""The local Chinese index may narrow the work, never the result.

These tests pin the property that makes it safe to run underneath the existing
substring scanner: for any query it accelerates, the hit set is identical to the
unindexed scan. Everything else — tokenisation, the stale-receipt rule, synonym
widening — exists to protect that property.
"""

from unittest.mock import patch

from server.campus.tests.test_data_audit import env, notice  # noqa: F401  (pytest fixtures)
from server.campus.zh_index import LocalTextIndex, match_expression, narrowable, tokenize


def indexed(core, *events):
    """Run the real indexing job for each event, as the worker would."""
    for event in events or core.store.list("events"):
        core.search.index_event({"subject": {"kind": "public_event", "id": event["id"], "revision": event["version"]}})
    return events


def hit_ids(core, query, **kwargs):
    return [row[1]["event_id"] for row in core.search.hits(query, **kwargs)]


def unindexed():
    """Force the scanner path, as if FTS5 were missing from the build."""
    return patch.object(LocalTextIndex, "supported", property(lambda self: False))


def test_tokenize_emits_unigrams_and_bigrams_for_han_runs():
    assert tokenize("银河计数器") == ["银", "河", "计", "数", "器", "银河", "河计", "计数", "数器"]


def test_tokenize_lowercases_ascii_and_drops_punctuation():
    assert tokenize("GPA 3.5 奖学金") == ["gpa", "3", "5", "奖", "学", "金", "奖学", "学金"]
    assert tokenize("") == [] and tokenize(None) == []


def test_only_pure_han_queries_are_narrowable():
    assert narrowable(["银河计数器", "银河"])
    assert not narrowable(["gpa"])
    assert not narrowable(["银河gpa"])
    assert not narrowable([])


def test_match_expression_treats_query_text_as_data_not_syntax():
    expression = match_expression(['银河" OR "x'])
    assert expression.count('"') % 2 == 0
    assert "银河" in expression


def test_index_returns_exactly_the_unindexed_hit_set(env):
    _, core, _, accept = env
    accept(notice(core, title="合成通知甲", extra="<p>银河计数器标记甲。</p>"))
    accept(notice(core, title="合成通知乙", extra="<p>银河计数器标记乙。</p>"))
    accept(notice(core, title="合成通知丙", extra="<p>无关的通知正文。</p>"))
    indexed(core)
    with_index = hit_ids(core, "银河计数器")
    with unindexed():
        without_index = hit_ids(core, "银河计数器")
    assert with_index and with_index == without_index


def test_index_narrows_the_segments_it_visits(env):
    _, core, _, accept = env
    extra = "".join(f"<p>无关段落编号{i:02}，与查询无关。</p>" for i in range(20))
    extra += "".join(f"<p>银河计数器编号{i:02}。</p>" for i in range(3))
    accept(notice(core, extra=extra))
    indexed(core)
    keys = core.search.local.keys(["银河计数器"])
    assert keys is not None
    ((_, matches),) = keys.items()
    assert 1 <= len(matches) < 23
    assert all(snapshot_id and segment_id for snapshot_id, segment_id in matches)


def test_ascii_query_is_answered_by_the_scanner_not_the_index(env):
    _, core, _, accept = env
    accept(notice(core, extra="<p>GPA 3.5 以上可申请。</p>"))
    indexed(core)
    assert core.search.local.keys(["gpa"]) is None
    assert hit_ids(core, "gpa")


def test_stale_receipt_scans_in_full_instead_of_trusting_old_rows(env):
    _, core, _, accept = env
    first = accept(notice(core, url="https://example.com/stable"))
    indexed(core, first["event"])
    assert core.store.get("local_index_receipts", first["event"]["id"])["version"] == 1
    # A second capture of the same document becomes version 2 without re-indexing.
    second = accept(notice(core, source=first["source"], url=first["url"], extra="<p>补充说明：银河计数器标记。</p>"))
    assert second["event"]["id"] == first["event"]["id"] and second["event"]["version"] == 2
    assert hit_ids(core, "银河计数器") == [first["event"]["id"]]


def test_withdrawn_event_leaves_no_rows_behind(env):
    _, core, _, accept = env
    published = accept(notice(core, extra="<p>银河计数器标记。</p>"))
    indexed(core, published["event"])
    assert hit_ids(core, "银河计数器")
    core.events.publication(
        published["event"]["id"],
        {
            "expected_event_version": published["event"]["version"],
            "publication_state": "withdrawn",
            "reason": "合成撤回",
        },
        "admin@example.com",
    )
    indexed(core, core.store.get("events", published["event"]["id"]))
    assert hit_ids(core, "银河计数器") == []
    assert core.search.local.keys(["银河计数器"]) == {}


def test_search_still_answers_when_fts5_is_unavailable(env):
    _, core, _, accept = env
    accept(notice(core, extra="<p>银河计数器标记。</p>"))
    with unindexed():
        assert hit_ids(core, "银河计数器")
        assert core.search.local.keys(["银河计数器"]) is None


def test_near_synonym_widens_a_query_only_after_a_literal_search(env):
    _, core, _, accept = env
    literal = accept(notice(core, extra="<p>报名截止时间为2030年1月3日。</p>"))
    equivalent = accept(
        notice(core, title="另一合成专业确认", date="2030-01-05", extra="<p>报名截至时间为2030年1月5日。</p>")
    )
    found = hit_ids(core, "截止")
    assert {literal["event"]["id"], equivalent["event"]["id"]} <= set(found)
    # A literal match always outranks one reached only through a synonym.
    assert found[0] == literal["event"]["id"]


def test_synonym_expansion_never_fires_when_literal_matches_are_plentiful(env):
    _, core, _, accept = env
    accept(notice(core, extra="<p>报名截止时间为2030年1月3日。</p>"))
    accept(notice(core, title="第二个合成通知", extra="<p>报名截止时间为2030年1月4日。</p>"))
    accept(notice(core, title="第三个合成通知", extra="<p>报名截止时间为2030年1月5日。</p>"))
    assert core.search.synonyms(["截止"]) == ["截至", "截止日期"]
    with patch.object(core.search, "_scan", wraps=core.search._scan) as scan:
        core.search.hits("截止")
    assert scan.call_count == 1


def test_hostile_query_text_does_not_break_the_match_expression(env):
    _, core, _, accept = env
    accept(notice(core, extra="<p>银河计数器标记。</p>"))
    indexed(core)
    for query in ['银河" OR "银河', "银河*", "银河 AND", "银河 NEAR/3 计数器"]:
        assert isinstance(hit_ids(core, query), list)


def test_local_index_lag_is_reported_rather_than_hidden(env):
    """The shipped default has no Meilisearch, so the local index is the only one.

    A stale accelerator still answers correctly — the event is scanned in full —
    but the caller is told it is behind instead of being shown ``current``.
    """
    _, core, _, accept = env
    accept(notice(core, extra="<p>银河计数器标记。</p>"))
    lagging = core.search.search("银河计数器")
    assert lagging["index_status"] == "lagging"
    assert lagging["hits"], "a stale index must still answer"
    indexed(core)
    assert core.search.search("银河计数器")["index_status"] == "current"


def test_index_status_is_current_when_fts5_is_unavailable(env):
    """With no local index there is nothing to be behind."""
    _, core, _, accept = env
    accept(notice(core, extra="<p>银河计数器标记。</p>"))
    with unindexed():
        assert core.search.search("银河计数器")["index_status"] == "current"


def test_excerpt_closes_on_the_next_sentence_break_when_one_is_near():
    from server.campus.search import EXCERPT_WINDOW, Search

    head = "银河计数器须于九月前提交。"
    # The next full stop lands just past the window target, inside the budget.
    body = head + "中" * (EXCERPT_WINDOW - len(head) + 17) + "。" + "尾" * 300
    quote = Search._excerpt(body, ["银河计数器"])
    assert quote in body, "the quote must stay a verbatim slice of the notice"
    assert "银河计数器" in quote
    assert quote.endswith("。"), "a quote should not appear to end where the notice does not"


def test_excerpt_takes_a_short_tail_whole_instead_of_cutting_it():
    from server.campus.search import EXCERPT_OVERSHOOT, EXCERPT_WINDOW, Search

    body = "银河计数器须于九月前提交。" + "中" * (EXCERPT_WINDOW - 13 - EXCERPT_OVERSHOOT // 2)
    quote = Search._excerpt(body, ["银河计数器"])
    assert quote == body


def test_excerpt_never_overshoots_the_window_by_more_than_the_budget():
    from server.campus.search import EXCERPT_OVERSHOOT, EXCERPT_WINDOW, Search

    body = "银河计数器" + "中" * 4000
    quote = Search._excerpt(body, ["银河计数器"])
    assert len(quote) <= EXCERPT_WINDOW + EXCERPT_OVERSHOOT
    assert quote in body and "银河计数器" in quote


def test_excerpt_of_a_title_only_match_opens_the_notice(env):
    """No query term in the body means the match came from the title alone."""
    from server.campus.search import Search

    body = "本段与查询词无关。" + "正文" * 300
    quote = Search._excerpt(body, ["银河计数器"])
    assert quote.startswith("本段与查询词无关。")
    assert quote in body
