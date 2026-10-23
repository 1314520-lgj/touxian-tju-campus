"""A shortened deadline quote must still prove itself.

The candidate is re-validated against its own ``raw_text``: the recorded date
has to be re-derivable from the quote that was stored. Shortening the quote to
the clause the date came from broke that when the cue lived in the previous
clause ("报名截止：报名学生需于2026年4月15日前提交…"), and the whole notice
failed extraction with 时间及其用途无法从原文解析 — no candidate reached the
reviewer at all. These tests pin both halves: the quote stays short *and* it
re-derives the same deadline.
"""

import pytest

from server.campus.content import date_values

ANCHOR = "2026-03-27T00:00:00+08:00"
EVIDENCE = "ev1"


@pytest.mark.parametrize(
    "text",
    [
        # The cue ("报名截止") is in the previous clause; the date is not.
        "报名截止：报名学生需于2026年4月15日前提交电子版申请材料。",
        # The cue ("请于") leads the sentence the date sits in.
        "请于2026年10月20日前将材料交至学院办公室，逾期不予受理。",
        # The deadline label is a separate clause from the date.
        "报名时间：2026年4月21日至4月28日16:00。请按时完成。",
        # A paragraph whose date sits deep inside it.
        (
            "各位同学请注意，现将有关事项通知如下，请仔细阅读并按要求办理，"
            "报名学生需于2026年4月15日前提交电子版申请材料，逾期不予受理，特此通知。"
        ),
    ],
)
def test_stored_quote_re_derives_the_same_deadline(text):
    rows = date_values(text, ANCHOR, EVIDENCE)
    assert rows, f"no deadline derived from {text!r}"
    for row in rows:
        again = date_values(row["raw_text"], ANCHOR, EVIDENCE)
        assert any(
            (value["kind"], value["precision"], value["at"], value["date"])
            == (row["kind"], row["precision"], row["at"], row["date"])
            for value in again
        ), f"raw_text {row['raw_text']!r} no longer derives {row['date']} {row['precision']}"


def test_quote_is_still_shorter_than_the_paragraph():
    """The self-check must not silently fall back to dumping the paragraph."""
    text = (
        "各位同学请注意，现将有关事项通知如下，请仔细阅读并按要求办理，"
        "报名学生需于2026年4月15日前提交电子版申请材料，逾期不予受理，特此通知。"
    )
    rows = date_values(text, ANCHOR, EVIDENCE)
    assert rows
    assert all(len(row["raw_text"]) < len(text) for row in rows), [row["raw_text"] for row in rows]
