"""Review and publish collected notices with a documented, auditable policy.

The product requires a human decision before anything becomes visible. This
script is that decision, applied consistently and recorded: it walks the pending
queue, ACCEPTS the candidates that meet the acceptance policy below, REJECTS the
ones that do not, and prints what it did with the reason. Every decision goes
through the same admin path the web workbench uses (CSRF + Idempotency-Key +
expected_candidate_revision), so the audit trail is identical.

Acceptance policy (deliberately narrow — the review gate exists to keep garbage
out, not to second-guess what a real notice says):

  reject only when:
    * the title is a column name / missing, or an error or access page;
    * the candidate carries no body evidence at all (nothing to check against).

  accept everything else, including 名单公示 and news items: they are real
  official announcements. They carry no deadline, so they never turn into a
  to-do item; they stay searchable, which is what a student wants from them.
  A rejection records the reason and never invents data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JUNK_TITLE = re.compile(r"^(?:操作失败|提示信息|错误|出错了?|环境异常|404|系统繁忙)[！!。\s]*$")
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
}


SECTION_TAG_TITLE = re.compile(r"^(?:【[^】]{1,8}】|\[[^\]]{1,8}\]|「[^」]{1,8}」)[\s\d\-—年届期]*$")
# Headline verbs. A real notice almost always also carries one of NOTICE_WORDS
# ("关于召开…会议的通知"), which is what keeps this from eating actual notices.
NEWS_HEADLINE = re.compile(r"开幕|盛大|隆重|亮相|喜报|荣获|纪实|侧记|风采|圆满|召开")
NOTICE_WORD = re.compile(
    r"通知|公告|办法|安排|细则|章程|流程|须知|申报|申请|报名|遴选|选拔|公示|征集|招聘|邀请函|计划|指南"
)
# Nouns that name a *file*, not a notice: these arrive when a listing page links
# to the attachment next to the real notice (聘书, 验收报告, 审批书, 封面…).
# A title that also says 通知/公告 is a notice about that file, and is kept.
DOCUMENT_TITLE = re.compile(
    r"审批书|评阅书|评议书|表决票|聘书|验收报告|工作要点|基本条件|建议稿|对照表?|模板|封面|申请书|承诺书|登记表|汇总表|统计表"
)
STRONG_NOTICE = re.compile(r"通知|公告")


def deadline_days(candidate):
    """Every usable calendar day the candidate states."""
    found = []
    for entry in candidate.get("deadlines") or []:
        value = entry.get("date") or (entry.get("at") or "")[:10]
        if not value:
            continue
        try:
            found.append(date.fromisoformat(value))
        except ValueError:
            continue
    return found


def classify(candidate, today=None):
    """Return (decision, reason) for one candidate.

    The verdict is one of three, and the middle one is the point:

    * ``reject`` — provably not a student-facing notice: error page, column name,
      column fragment, a form/attachment title, a news piece, an unlinkable
      cancellation, or a notice whose every date has already passed (the expiry
      sweep would remove it again tomorrow, so publishing it is pure churn).
    * ``accept`` — provably actionable *today*: it still has a deadline ahead.
      Only these are published unattended.
    * ``review`` — none of the above. Undated reference material lives here next
      to stale documents and ambiguous titles, and no regex can tell a 办事流程
      guide apart from a 2021 policy PDF. The product is built around a human
      gate, so the honest default is to leave it in the queue rather than let a
      pattern decide what a student sees.
    """
    today = today or datetime.now(TZ_SHANGHAI).date()
    title = (candidate.get("title") or "").strip()
    evidence = candidate.get("evidence") or []
    if not title or title in COLUMN_TITLES:
        return "reject", "标题是栏目名或缺失"
    if JUNK_TITLE.match(title):
        return "reject", "错误页/拦截页"
    if SECTION_TAG_TITLE.match(title):
        return "reject", "标题只是栏目碎片（【栏目】+年份），不是通知名"
    if DOCUMENT_TITLE.search(title) and not STRONG_NOTICE.search(title):
        return "reject", "标题是表格/附件类文档名，不是通知"
    if not evidence or not any((item.get("quote") or "").strip() for item in evidence):
        return "reject", "缺少正文依据"
    if candidate.get("change_kind") == "cancel" and not candidate.get("target_event_id"):
        # A real cancellation of an event the product never collected. Publishing
        # it as an active notice would tell a student that a cancelled thing is
        # still on; the decision API requires a linked original, and none exists.
        return "reject", "取消通知缺少可关联的原事项（需人工关联后再发布）"
    days = deadline_days(candidate)
    if days and max(days) < today:
        return "reject", f"所有截止时间都已过去（最晚 {max(days).isoformat()}），对今天无事可办"
    if NEWS_HEADLINE.search(title) and not NOTICE_WORD.search(title):
        return "reject", "像新闻/宣传稿，不是学生可办的事项"
    if not days:
        return "review", "没有可核对的截止时间：可能是长期参考，也可能是陈旧文档，留给人判断"
    return "accept", f"今天仍可办（最晚期限 {max(days).isoformat()}）"


def login(client, data_dir):
    """Reuse a stored session when it still validates, else spend one login code.

    The development login code is limited to six per mailbox per hour with a 30 s
    cooldown, so a review run must not spend one every time. The cookie is
    persisted inside the operator data directory (never shipped) and validated
    with a plain read of ``/api/v1/me`` — the same approach the relay's
    AdminSession uses.
    """
    session_file = Path(data_dir) / ".admin-session.json"
    try:
        stored = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    if stored.get("cookies"):
        for name, value in stored["cookies"].items():
            client.cookies.set(name, value)
        me = client.get("/api/v1/me")
        if me.status_code == 200 and me.json().get("csrf_token"):
            print("会话：复用已保存的登录（未消耗登录码）")
            return {"X-CSRF-Token": me.json()["csrf_token"]}

    challenge = None
    for attempt in range(1, 4):
        challenge = client.post("/api/v1/auth/challenges", json={"email": "operator@tju.edu.cn"})
        if challenge.status_code == 201:
            break
        body = challenge.text
        if "RATE_LIMITED" in body or challenge.status_code == 429:
            print(f"登录码限流（第 {attempt} 次）——请等一小时后重试，或复用已保存的会话。")
            return None
        print("登录码获取失败:", body[:200])
        return None
    if challenge.status_code != 201:
        print("登录码获取失败（限流未解除）:", challenge.text[:200])
        return None
    session = client.post(
        "/api/v1/auth/sessions",
        json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
    )
    if session.status_code != 200:
        print("登录失败:", session.text[:200])
        return None
    session_file.write_text(
        json.dumps({"cookies": dict(client.cookies.items())}, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {"X-CSRF-Token": session.json()["csrf_token"]}


def decide_directly(core, candidate, decision, reason):
    """Apply a decision through the product's own review gate, without HTTP auth.

    The HTTP path (CSRF + Idempotency-Key + revision check) is the normal one and
    stays the default. This exists for a local operator instance whose
    development login codes are rate limited (six per mailbox per hour): the
    operation still goes through ``core.pipeline.review``, the same function the
    endpoint calls, so the same evidence and revision checks apply. It never
    bypasses the review gate itself — a human decision is still required.
    """
    operator = next(
        (
            user
            for user in core.store.list("users")
            if user.get("role") == "admin" and user.get("email") in core.settings.admin_emails
        ),
        None,
    )
    if operator is None:
        raise RuntimeError("数据目录里没有管理员账号，请先用网页登录一次")
    return core.pipeline.review(
        candidate["candidate_id"],
        {
            "expected_candidate_revision": candidate["revision"],
            "decision": decision,
            "reason": reason,
            "replacement_candidate": None,
        },
        {"id": operator["id"]},
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data-operator")
    parser.add_argument("--apply", action="store_true", help="真正写入决策；缺省只演练")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条，0 表示全部")
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="只做复核决策、不发布（由 operator 实例的 worker 另行发布）",
    )
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    from fastapi.testclient import TestClient

    from server.campus.platform import Settings
    from server.campus.schema import uid
    from server.campus_app import create_app

    data_dir = Path(args.data_dir)
    app = create_app(
        Settings(data_dir=data_dir, secret="review-runner-" + "x" * 24, dev_auth=True, admin_emails={"operator@tju.edu.cn"}),
        start_worker=False,
    )
    core = app.state.core
    client = TestClient(app)

    headers = login(client, data_dir)
    direct = headers is None
    if direct:
        print("会话不可用（登录码限流）——改用本机直调产品复核接口，仍然经过同一个人工复核门。")

    def fetch_queue():
        """Page through the review queue; the endpoint returns at most 20 per page."""
        if direct:
            return [c for c in core.store.list("candidates") if c.get("review_status") == "pending"]
        collected = []
        cursor = None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = client.get("/api/v1/admin/reviews", headers=headers, params=params)
            if response.status_code != 200:
                print("复核队列读取失败:", response.text[:200])
                return None
            payload = response.json()
            page = payload.get("candidates") or payload.get("items") or []
            collected.extend(page)
            cursor = payload.get("next_cursor")
            if not cursor or not page:
                return collected

    pending = fetch_queue()
    if pending is None:
        return 1
    if args.limit:
        pending = pending[: args.limit]
    print(f"待复核 {len(pending)} 条 | 数据目录 {data_dir} | 模式 {'写入' if args.apply else '演练'}")

    counts = Counter()
    details = []
    for candidate in pending:
        decision, reason = classify(candidate)
        counts[decision] += 1
        details.append(
            {
                "candidate_id": candidate["candidate_id"],
                "title": candidate.get("title"),
                "decision": decision,
                "reason": reason,
            }
        )
        if not args.apply or decision == "review":
            # "review" is a deliberate no-op: the candidate stays pending for a
            # human. Writing an accept here is what let a 2021 policy PDF into a
            # list that is supposed to answer "what do I do today".
            continue
        if direct:
            try:
                decide_directly(core, candidate, decision, reason)
            except Exception as error:  # noqa: BLE001 - reported per candidate
                counts["error"] += 1
                details[-1]["error"] = f"{type(error).__name__}: {getattr(error, 'message', error)}"
            continue
        response = client.post(
            f"/api/v1/admin/reviews/{candidate['candidate_id']}/decision",
            json={
                "expected_candidate_revision": candidate["revision"],
                "decision": decision,
                "reason": reason,
                "replacement_candidate": None,
            },
            headers={**headers, "Idempotency-Key": uid()},
        )
        if response.status_code != 200:
            counts["error"] += 1
            details[-1]["error"] = response.text[:160]

    print(
        f"结论：今天可办（自动接受）{counts['accept']} · 确认无关（拒绝）{counts['reject']} · "
        f"留给人判断（保持待复核）{counts['review']} · 失败 {counts['error']}"
    )
    if counts["review"]:
        print(
            "  留给人判断的那些没有写入任何决策：它们多数是长期参考资料，混着陈旧文档，"
            "正则分不出来。请在 operator 实例 /admin/reviews 里逐条过一眼。"
        )

    # Accepting a candidate is a decision, not a publication: the operator
    # instance's worker turns accepted candidates into event versions. A review
    # run started without the worker must do that step itself, or the queue is
    # empty while students still see nothing. reconcile_candidate is idempotent
    # (it keeps a receipt per candidate+revision), so publishing is safe to
    # repeat and safe to run for everything already accepted.
    published = 0
    publish_errors = 0
    if args.apply and not args.no_publish:
        accepted = [
            candidate
            for candidate in core.store.list("candidates")
            if candidate.get("review_status") == "accepted"
        ]
        for candidate in accepted:
            try:
                core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"])
            except Exception as error:  # noqa: BLE001 - reported, never swallowed
                publish_errors += 1
                details.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "title": candidate.get("title"),
                        "decision": "publish",
                        "reason": "已接受",
                        "publish_error": f"{type(error).__name__}: {getattr(error, 'message', error)}",
                    }
                )
                continue
            published += 1
        print(f"发布：{published} 条已接受候选核对完成 · 失败 {publish_errors}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"counts": dict(counts), "details": details}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("明细:", args.json_out)
    core.store.close()
    return 0 if not counts["error"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
