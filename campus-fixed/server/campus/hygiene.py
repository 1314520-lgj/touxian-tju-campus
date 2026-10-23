"""One definition of "this title is not a notice".

Two consumers need the same judgement and must never drift apart:

* the review gate (``scripts/review_and_publish.py``), which decides what may be
  published for the first time;
* the hygiene sweep (``Events.withdraw_unusable``), which takes back what an
  earlier, more permissive gate let through.

Keeping the patterns here is what makes "would today's policy publish this?"
answerable for content that is already live.
"""

from __future__ import annotations

import re

# The whole title is a section marker plus an optional year: "【考试安排】2022".
SECTION_TAG_TITLE = re.compile(r"^(?:【[^】]{1,8}】|\[[^\]]{1,8}\]|「[^」]{1,8}」)[\s\d\-—年届期]*$")

# Names of files, not notices: a listing page links the attachment beside the
# real notice, and the attachment title arrives as a candidate.
DOCUMENT_TITLE = re.compile(
    r"审批书|评阅书|评议书|表决票|聘书|验收报告|工作要点|基本条件|建议稿|对照表?|模板|封面"
    r"|申请书|承诺书|登记表|汇总表|统计表|情况表"
)

# A title that also says 通知/公告 is a notice *about* that file.
STRONG_NOTICE = re.compile(r"通知|公告")

# Headline verbs of a news piece. A genuine notice almost always also carries a
# NOTICE_WORD ("关于召开…会议的通知"), which keeps this from eating real ones.
NEWS_HEADLINE = re.compile(r"开幕|盛大|隆重|亮相|喜报|荣获|纪实|侧记|风采|圆满|召开")

NOTICE_WORD = re.compile(
    r"通知|公告|办法|安排|细则|章程|流程|须知|申报|申请|报名|遴选|选拔|公示|征集|招聘|邀请函|计划|指南"
)

COLUMN_TITLES = {
    "培养工作",
    "学位与导师工作",
    "研究生招生",
    "研院简介",
    "非学历继续教育",
    "综合事务",
    "通知公告",
    "学业指导",
    "学生竞赛",
    "竞赛科创",
    "新闻动态",
    "综合新闻",
    "招聘会信息",
    "本科教学",
    "下载中心",
    "总体情况",
}

# A title that is really the first line of the body, or a bare link.
BODY_FRAGMENT_TITLE = re.compile(r"^\d+[.、]")
BARE_LINK_TITLE = re.compile(r"^(?:文件来源|来源|原文链接)\s*[:：]?\s*(?:https?://|www\.)|^https?://")


def title_problems(title: str) -> list[str]:
    """Reasons this title is not a student-facing notice; empty when it is fine."""
    text = (title or "").strip()
    problems: list[str] = []
    if not text:
        problems.append("空标题")
    if text in COLUMN_TITLES:
        problems.append("栏目名/导航页")
    if SECTION_TAG_TITLE.match(text):
        problems.append("栏目碎片")
    if DOCUMENT_TITLE.search(text) and not STRONG_NOTICE.search(text):
        problems.append("文档/附件名")
    if BARE_LINK_TITLE.search(text):
        problems.append("裸链接")
    if BODY_FRAGMENT_TITLE.match(text):
        problems.append("正文条目片段")
    if NEWS_HEADLINE.search(text) and not NOTICE_WORD.search(text):
        problems.append("新闻稿")
    return problems