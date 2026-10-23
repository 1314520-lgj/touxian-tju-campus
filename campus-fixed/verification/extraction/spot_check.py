"""t23：把改动前后的抽取结果并排打印，便于人工核对（不写任何数据）。

    python verification/extraction/spot_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from server.campus.content import EXTRACTOR_VERSION, date_values  # noqa: E402
from server.campus.schema import uid  # noqa: E402

CASES = [
    ("交换生：日期+时刻+前，无任何 cue 词",
     "10月16日12:00前，有意向申请本项目的同学请登录境外院校官网查询相关专业及课程信息。", "2026-09-20T10:00:00+08:00"),
    ("调档函：日期+之前",
     "请登录天津大学招生管理系统下载调档函，8月31日之前将人事档案原件寄送至我校。", "2026-08-01T09:00:00+08:00"),
    ("学籍预警：日期+前（后果句）",
     "2026年6月30日前未办理结业、肄业相关手续的，学校将依据《天津大学研究生学籍管理办法》作退学处理。",
     "2026-03-01T09:00:00+08:00"),
    ("学院报送：于+日期+(周五)16：00+前",
     "请各学院(部)于2024年9月27日(周五)16：00前，将已审核通过的选题申报材料汇总后通过办公网发至教务处。",
     "2024-09-20T09:00:00+08:00"),
    ("红线：前后（模糊）",
     "本次座谈会将于3月15日前后举行，具体安排见附件。", "2026-03-01T09:00:00+08:00"),
    ("红线：前往（复合词）",
     "活动将于4月20日前往新校区举办。", "2026-04-01T09:00:00+08:00"),
    ("红线：纯背景日期",
     "新华社北京10月2日电，近日习近平总书记给天津大学全体师生回信。", "2026-10-03T09:00:00+08:00"),
    ("红线：发布日期",
     "《关于规范研究生学位论文使用人工智能工具的倡议书》发布日期：2025年11月14日", "2025-11-14T09:00:00+08:00"),
    ("回归：截止时间",
     "报名截止时间为9月7日（周日）13：00。", "2025-08-30"),
    ("回归：请于…24:00前",
     "请于11月30日24:00前提交", "2026-10-11T10:00:00+08:00"),
    ("回归：排期（event_time）",
     "报到时间为6月30日13:00-13:30", "2026-06-01T09:00:00+08:00"),
]


def main() -> int:
    print("EXTRACTOR_VERSION =", EXTRACTOR_VERSION)
    for label, text, published in CASES:
        rows = date_values(text, published, uid())
        shown = [
            {k: row[k] for k in ("kind", "precision", "date", "at")}
            for row in rows
        ]
        print(f"\n· {label}\n  {text[:60]}\n  → {shown if shown else '（没有期限）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())