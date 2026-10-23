"""Synthetic structure regressions; these are not a live-site accuracy sample."""

from unittest.mock import patch

import pytest

from server.campus.content import MAX_SEGMENTS, MAX_TEXT, Pipeline, html_body_segments
from server.campus.platform import Core, Settings
from server.campus.schema import uid
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source


@pytest.fixture
def core(tmp_path):
    value = Core(Settings(data_dir=tmp_path, secret="body-quality-fixture-" * 3, dev_auth=True))
    value.pipeline = Pipeline(value)
    yield value
    value.store.close()


def document(core, html):
    source = synthetic_source(core)
    snapshot, _ = core.pipeline.sources.persist(
        source,
        FetchResult("https://example.com/notices/" + uid(), html.encode(), "text/html"),
        "Fixture",
        "2026-10-08T00:00:00+08:00",
    )
    return snapshot, core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})


def text(parsed):
    return "\n".join(segment["text"] for segment in parsed["segments"])


def test_semantic_article_keeps_own_header_and_drops_recommendation_deadlines(core):
    snapshot, parsed = document(
        core,
        """<html><title>学校网站</title><header><h1>学校首页</h1></header>
    <main><article><header><h1>本科生奖学金申请通知</h1></header>
    <p>全体本科生请于2026年10月20日前提交申请表。</p>
    <div class="related-posts"><h2>相关推荐</h2><a href="/old">报名截止2025年10月1日</a></div>
    <aside><a href="/jobs">博士申请截止2026年12月1日</a></aside>
    <footer>历史报名截止2020年1月1日</footer></article></main></html>""",
    )
    assert parsed["state"] == "ok"
    assert parsed["segments"][0]["text"] == "本科生奖学金申请通知"
    assert "2025" not in text(parsed) and "博士" not in text(parsed) and "2020" not in text(parsed)
    candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    assert [deadline["date"] for deadline in candidate["deadlines"]] == ["2026-10-20"]
    assert candidate["audience"]["education_levels"] == ["undergraduate"]


def test_absolute_href_is_retained_as_literal_entry_evidence(core):
    snapshot, parsed = document(
        core,
        """<h1>合成申请通知</h1><article>
    <p>全体本科生请于2026年10月20日前提交申请表。</p>
    <p><a href="https://example.com/apply?kind=student&amp;term=2026">办理入口</a></p>
    <a href="javascript:alert(1)">错误入口</a><a href="/relative">相对链接</a></article>""",
    )
    candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    assert candidate["entry_urls"] == ["https://example.com/apply?kind=student&term=2026"]
    proof = next(proof for proof in candidate["evidence"] if proof["id"] in candidate["field_evidence"]["entry_urls"])
    assert proof["quote"] == candidate["entry_urls"][0]
    assert proof["quote"] in next(s["text"] for s in parsed["segments"] if s["id"] == proof["segment_id"])
    assert any(segment["text"] == "/relative" for segment in parsed["segments"])


def test_relative_entry_is_retained_for_review_without_fabricated_absolute_evidence(core):
    snapshot, parsed = document(
        core,
        """<h1>合成申请通知</h1><article>
    <p>全体本科生请于2026年10月20日前提交申请表。</p>
    <p><a href="../apply?kind=student">办理入口</a></p>
    <a href="https://example.com/博士报名截止2026年10月1日">系统入口</a></article>""",
    )
    assert parsed["state"] == "partial" and "relative_entry_link_requires_review" in parsed["warnings"]
    assert any(segment["text"] == "../apply?kind=student" for segment in parsed["segments"])
    candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    assert candidate["review_status"] == "pending"
    assert candidate["entry_urls"] == ["https://example.com/博士报名截止2026年10月1日"]
    assert candidate["audience"]["education_levels"] == ["undergraduate"]
    assert [deadline["date"] for deadline in candidate["deadlines"]] == ["2026-10-20"]


def test_inline_date_and_table_material_row_survive_segmentation(core):
    snapshot, parsed = document(
        core,
        """<h1>合成申请</h1><div id="vsb_content">
    <p>全体本科生请于<span>2026年</span><b>10月</b>20日前提交申请表。</p>
    <table><tr><th>材料：</th><td>申请表、成绩单</td></tr><tr><th>办理地点：</th><td>学院办公室</td></tr></table>
    </div>""",
    )
    assert "全体本科生请于2026年10月20日前提交申请表。" in text(parsed)
    assert "材料： 申请表、成绩单" in text(parsed)
    candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
    assert any(deadline["date"] == "2026-10-20" for deadline in candidate["deadlines"])
    assert "申请表、成绩单" in candidate["materials"]


@pytest.mark.parametrize(
    "html",
    [
        '<title>统一身份认证</title><article><h1>用户登录</h1><form><input type="password"><button>登录</button></form></article>',
        '<title>安全验证</title><div>访问过于频繁，请完成验证码后继续访问。</div><form id="captcha"><input name="captcha"></form>',
        "<title>微信公众平台</title><div>该内容已被发布者删除</div>",
    ],
)
def test_access_interstitial_is_failed_and_has_no_candidate(core, html):
    snapshot, parsed = document(core, html)
    assert parsed["state"] == "failed"
    assert "html_access_interstitial" in parsed["warnings"]
    assert parsed["segments"] == []
    assert core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]}) is None


def test_normal_login_instructions_are_not_a_login_wall(core):
    _, parsed = document(
        core,
        """<h1>选课办理说明</h1><article>
    <p>全体本科生请于2026年10月20日前登录选课系统，输入验证码后提交申请表。</p>
    <p>如登录失败，请联系学院教务老师。</p></article>""",
    )
    assert parsed["state"] == "ok"
    assert "html_access_interstitial" not in parsed["warnings"]


def test_title_and_scripts_are_not_an_extractable_body(core):
    snapshot, parsed = document(
        core, '<title>合成通知</title><h1>合成通知</h1><div id="js_content"></div><script>render()</script>'
    )
    assert parsed["state"] == "failed"
    assert core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]}) is None


def test_wechat_structure_preserves_body_and_removes_hidden_traps(core):
    _, parsed = document(
        core,
        """<h1 class="rich_media_title">合成公众号通知</h1>
    <div class="rich_media_content"><section><p>全体本科生请于2026年10月20日前提交申请表。</p></section>
    <p style="display: none">报名截止2026年10月1日</p><p hidden>博士仅限报名</p>
    <div class="comments"><a href="/comment">报名截止2026年10月2日</a></div></div>""",
    )
    assert parsed["state"] == "ok"
    assert "2026年10月1日" not in text(parsed) and "博士" not in text(parsed)
    assert "2026年10月2日" not in text(parsed)


def test_ambiguous_navigation_only_main_never_reports_complete_article(core):
    _, parsed = document(
        core,
        """<title>招生栏目</title><main><ul>
    <li><a href="/1">本科报名截止2026年10月1日</a></li>
    <li><a href="/2">博士申请截止2026年11月1日</a></li></ul></main>""",
    )
    assert parsed["state"] in {"partial", "failed"}
    assert "html_link_collection_requires_review" in parsed["warnings"]


def test_body_class_with_sidebar_word_does_not_remove_legitimate_materials(core):
    _, parsed = document(
        core,
        """<h1>合成申请通知</h1><article><div class="material-sidebar-content">
    <p>全体本科生请于2026年10月20日前提交申请表。</p>
    <p>材料：申请表、成绩单。</p></div></article>""",
    )
    assert parsed["state"] == "ok"
    assert "材料：申请表、成绩单。" in text(parsed)


@pytest.mark.parametrize(
    "opening,closing",
    [
        ("<article>", "</article>"),
        ('<div id="vsb_content">', "</div>"),
    ],
)
def test_article_footer_contact_facts_survive_while_site_footer_is_removed(core, opening, closing):
    _, parsed = document(
        core,
        "<h1>合成申请通知</h1>"
        + opening
        + """
    <p>全体本科生请于2026年10月20日前提交申请表。</p>
    <footer role="contentinfo"><p>承办单位：学院教务办公室，联系电话：022-12345678。</p></footer>
    """
        + closing
        + """<footer>学校总机联系电话：022-87654321。</footer>""",
    )
    assert parsed["state"] == "ok"
    assert "承办单位：学院教务办公室" in text(parsed)
    assert "022-12345678" in text(parsed) and "022-87654321" not in text(parsed)


def test_article_footer_author_and_source_are_retained(core):
    _, parsed = document(
        core,
        """<h1>合成申请通知</h1><article>
    <p>全体本科生请于2026年10月20日前提交申请表。</p>
    <footer>作者：教务老师；来源：学院教务办公室；邮箱：office@example.com。</footer>
    </article>""",
    )
    assert parsed["state"] == "ok"
    assert "作者：教务老师" in text(parsed) and "office@example.com" in text(parsed)


def test_multiline_table_cell_keeps_inline_words_and_complex_spans_require_review(core):
    _, parsed = document(
        core,
        """<h1>合成材料表</h1><article><table>
    <tr><th>材料：</th><td><p>申<span>请</span>表</p><p>成绩单</p></td></tr>
    <tr><td colspan="2">全体本科生请于2026年10月20日前提交申请。</td></tr>
    </table></article>""",
    )
    assert "材料： 申请表 成绩单" in text(parsed)
    assert parsed["state"] == "partial"
    assert "html_table_spans_require_review" in parsed["warnings"]


def test_wechat_static_visibility_is_kept_for_review_instead_of_silently_discarded(core):
    _, parsed = document(
        core,
        """<h1>合成公众号通知</h1><div id="js_content" style="visibility: hidden;">
    <p>全体本科生请于2026年10月20日前提交申请表。</p></div>""",
    )
    assert parsed["state"] == "partial"
    assert "html_visibility_requires_review" in parsed["warnings"]
    assert "提交申请表" in text(parsed)


def test_unknown_template_is_always_partial_even_if_fallback_finds_text(core):
    _, parsed = document(
        core,
        """<title>合成通知</title><div id="unconfigured-template">
    全体本科生请于2026年10月20日前提交申请表。材料：申请表、成绩单。
    </div>""",
    )
    assert parsed["state"] == "partial"
    assert "generic_body_fallback_requires_review" in parsed["warnings"]


def test_pure_parser_caps_segments_and_marks_truncation():
    html = (
        "<article>"
        + "".join("<p>保留材料说明 " + str(index) + "</p>" for index in range(MAX_SEGMENTS + 10))
        + "</article>"
    ).encode()
    segments, state, warnings = html_body_segments(html, "https://example.com/notice")
    assert len(segments) == MAX_SEGMENTS and state == "partial"
    assert "text_limit_requires_review" in warnings
    assert sum(len(segment["text"]) for segment in segments) <= MAX_TEXT


def test_pure_parser_caps_text_and_rejects_excessive_structure():
    segments, state, warnings = html_body_segments(
        ("<article><p>" + ("x" * (MAX_TEXT + 1)) + "</p></article>").encode(), "https://example.com/notice"
    )
    assert state == "partial" and "text_limit_requires_review" in warnings
    assert sum(len(segment["text"]) for segment in segments) == MAX_TEXT
    assert html_body_segments(b"<" * 100_001, "https://example.com/notice") == (
        [],
        "failed",
        ["html_structure_limit_requires_review"],
    )


def test_parser_upgrade_revises_candidate_and_preserves_historical_version(core):
    source = synthetic_source(core)
    html = "<h1>合成申请</h1><article><p>全体本科生请于2026年10月20日前提交申请表。</p></article>".encode()
    snapshot, _ = core.pipeline.sources.persist(
        source, FetchResult("https://example.com/" + uid(), html, "text/html"), "合成申请", "2026-10-08T00:00:00+08:00"
    )
    sid = snapshot["snapshot_id"]
    with patch("server.campus.content.PARSER_VERSION", "fixture-old-parser"):
        old = core.pipeline.extract_candidates({"snapshot_id": sid})
    updated = core.pipeline.extract_candidates({"snapshot_id": sid})
    assert updated["revision"] == old["revision"] + 1
    assert updated["review_status"] == "pending"
    assert "parsed_input_changed_requires_review" in updated["warnings"]
    assert core.store.get("candidate_versions", old["candidate_id"] + ":1") == old
