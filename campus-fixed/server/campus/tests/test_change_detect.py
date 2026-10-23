"""Block-level change detection: only literal differences may be reported."""

from server.campus import change_detect


def test_identical_text_is_not_a_change():
    report = change_detect.diff("第一段\n第二段", "第一段\n第二段")
    assert report["changed"] is False
    assert report["counts"] == {"added": 0, "removed": 0, "rewritten": 0, "unchanged": 2}
    assert change_detect.summarize(report) is None


def test_whitespace_and_width_variants_do_not_count_as_edits():
    report = change_detect.diff("报名时间：１０月１０日", "报名时间:10月10日")
    assert report["changed"] is False


def test_added_block_is_reported_verbatim():
    report = change_detect.diff("第一段", "第一段\n新增：请携带学生证")
    assert report["changed"] is True
    assert report["added"] == ["新增：请携带学生证"]
    assert report["removed"] == []
    assert "content_added" in report["kinds"]


def test_removed_block_is_not_reported_as_added():
    report = change_detect.diff("第一段\n第二段", "第一段")
    assert report["added"] == []
    assert report["removed"] == ["第二段"]
    assert "content_removed" in report["kinds"]


def test_deadline_move_is_classified_separately_from_prose():
    report = change_detect.diff("截止时间：2026年10月10日", "截止时间：2026年10月12日")
    assert report["changed"] is True
    assert "deadline_or_date_touched" in report["kinds"]
    summary = change_detect.summarize(report)
    assert summary.startswith("时间或期限可能有变化")


def test_pure_wording_revision_is_not_labelled_as_addition():
    report = change_detect.diff("请于本周提交材料", "请于下周提交材料")
    assert report["counts"]["added"] == 0
    assert report["counts"]["removed"] == 0
    assert "wording_revised" in report["kinds"]


def test_summary_is_bounded_and_never_invents_content():
    old = "\n".join(f"第{i}段" for i in range(200))
    new = old + "\n" + "\n".join(f"新增{i}段" for i in range(200))
    report = change_detect.diff(old, new)
    assert report["truncated"] is True
    assert len(report["added"]) == change_detect.MAX_REPORTED
    summary = change_detect.summarize(report)
    assert len(summary) <= 400
    assert "新增内容：" in summary


def test_punctuation_only_lines_are_not_blocks():
    assert change_detect.blocks("———\n。。。\n正文") == ["正文"]


def test_fingerprint_ignores_incidental_whitespace_but_not_content():
    assert change_detect.fingerprint("a  b") == change_detect.fingerprint("a b")
    assert change_detect.fingerprint("a b") != change_detect.fingerprint("a c")


def test_compare_returns_report_and_summary_together():
    report, summary = change_detect.compare("甲", "甲\n乙")
    assert report["changed"] is True
    assert summary and "新增 1 段" in summary


def test_blocks_are_capped_to_avoid_unbounded_payloads():
    text = "\n".join(f"第{i}行" for i in range(change_detect.MAX_BLOCKS + 50))
    assert len(change_detect.blocks(text)) == change_detect.MAX_BLOCKS
