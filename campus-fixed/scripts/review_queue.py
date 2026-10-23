#!/usr/bin/env python3
"""Review workbench for the queue the machine deliberately leaves to a human.

The daily update's review gate is intentionally narrow: it accepts only what is
actionable *today* (a deadline still ahead), rejects what is provably not a
student-facing notice, and leaves the rest — undated reference material, stale
documents, ambiguous titles — in the review queue. That policy is not changed
here, and this command cannot change it: every verdict printed below comes from
``scripts/review_and_publish.classify`` and every decision goes through
``Pipeline.review`` (the function the admin endpoint calls, via
``scripts/daily_update.py``), which records a ``reviews`` row with the reason.

What this command does is make the human step fast and honest:

* **Order** the queue by how likely an entry is worth a human's attention
  (action flags, materials, notice wording, official host, freshness) and sink
  obvious long-term reference material to the bottom, printing *why* it sank.
* **Group** rows that are the same thing to decide: the same notice arrives from
  several sources and from repeated fetches, so the queue holds far more rows than
  distinct notices (measured 2026-10-21: 127 pending rows covering 94 titles). The
  default listing shows one representative per group plus "同组另有 N 条" with
  every member id. This is a *view*: no candidate, snapshot or review row is
  merged, moved or dropped — ``--flat`` prints the old one-row-per-candidate list,
  ``--show <id>`` still works for any member, and a decision still names candidate
  ids (a group decision prints all of them and asks for ``--yes``).
* **Explain** per candidate why the machine did not decide it — the policy
  verdict, why accept did not apply, why reject did not apply, and what evidence
  a human can check without opening a browser.
* **Decide** one candidate with a mandatory reason, or a batch that must name
  its ids, its filter, or one group explicitly. There is no "accept everything"
  switch: a batch prints the exact rows it will touch and refuses to write without
  ``--yes``, and a batch larger than ``--max-batch`` (default 25) is refused
  until the operator narrows it or raises the cap on purpose.

Exit codes: ``0`` ok, ``1`` something failed while working, ``2`` the command was
not usable (bad flag, missing reason, unknown id, refused batch, or a batch that
was only previewed because ``--yes`` was absent) — nothing was written.

Usage::

    python scripts/review_queue.py --data-dir data-operator --limit 10
    python scripts/review_queue.py --data-dir data-operator --limit 10 --flat
    python scripts/review_queue.py --data-dir data-operator --show <candidate_id>
    python scripts/review_queue.py --data-dir data-operator --accept <id> --reason "..." --apply
    python scripts/review_queue.py --data-dir data-operator --group <id> --accept \
        --reason "..." --yes --apply
    python scripts/review_queue.py --data-dir data-operator --batch-filter deadline=none \
        --batch-filter notice=yes --reject --reason "..." --yes --apply
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPTS = Path(__file__).resolve().parent
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

SORTS = ("priority", "deadline", "recent", "source")
# ``--accept`` / ``--reject`` carry a candidate id for a single decision and no
# value at all for a batch; argparse turns the missing value into this sentinel.
BATCH_TARGET = "__batch__"
# The vocabulary ``--batch-filter`` accepts. Every key names a fact the command
# already computes and prints per row, so a batch is always explainable in the
# same words the operator just read in the list.
FILTERS = {
    "tier": ("1", "2", "3"),
    "deadline": ("future", "none", "past"),
    "notice": ("yes", "no"),
    "action": ("yes", "no"),
    "materials": ("yes", "no"),
    "document": ("yes", "no"),
    "source": None,  # free text: source name or id fragment
    "title": None,  # free text: substring of the title
    "machine": ("accept", "reject", "review"),
}


def _load_script(name):
    """Import a sibling operator script as a module (``scripts/`` is not a package)."""
    path = SCRIPTS / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Imported, never re-implemented: the policy and the decision path must be the
# same objects the daily update uses, or the two would drift apart.
daily = _load_script("daily_update")
collect = daily.collect
policy = daily.review_policy


def _text(value, limit=None):
    text = " ".join(str(value or "").split())
    if limit and len(text) > limit:
        return text[: limit - 1] + "…"
    return text


YEAR = re.compile(r"(20\d\d)\s*年")
# Dates are shown in the body even when the extractor did not file them as a
# deadline (a "10月16日12:00前" inside a materials sentence). This is a reading
# aid for the human, never a verdict: nothing below changes what classify said.
DATE_CLUE = re.compile(
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号](?:\s*\d{1,2}\s*[:：]\s*\d{2})?"
    r"|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
)


def date_clues(candidate, limit=4):
    """Date-looking strings in the stored quotes, deduplicated, for display only."""
    found = []
    seen = set()
    for item in candidate.get("evidence") or []:
        quote = _text(item.get("quote"))
        for match in DATE_CLUE.findall(quote):
            key = _text(match)
            if key in seen:
                continue
            seen.add(key)
            found.append((key, _text(quote, 110), item.get("segment_id")))
            if len(found) >= limit:
                return found
    return found


def _stamp(value):
    """ISO timestamp -> ``YYYY-MM-DD`` (or ``—``)."""
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return str(value)[:10]


def index_snapshots(core):
    """snapshot_id -> stored document row (source_url, published_at, fetched_at)."""
    index = {}
    for row in core.store.list("snapshots"):
        snapshot_id = row.get("snapshot_id")
        if snapshot_id:
            index[snapshot_id] = row
    return index


def index_sources(core):
    return {source["id"]: source for source in core.store.list("sources")}


def article_url(candidate):
    """The original notice URL: the evidence rows all point at what was quoted."""
    for item in candidate.get("evidence") or []:
        if item.get("source_url"):
            return item["source_url"]
    return None


def quote_for(text, candidate, limit=110):
    """The stored quote that contains ``text`` — evidence, not a re-read of the page."""
    needle = _text(text)
    for item in candidate.get("evidence") or []:
        quote = _text(item.get("quote"))
        if needle and needle in quote:
            return _text(quote, limit), item.get("segment_id"), item.get("page")
    for item in candidate.get("evidence") or []:
        quote = _text(item.get("quote"))
        if needle and quote and (needle[:6] in quote or quote[:6] in needle):
            return _text(quote, limit), item.get("segment_id"), item.get("page")
    return None, None, None


def view_of(candidate, snapshots, sources, today):
    """Everything printed about one candidate, computed from stored records only."""
    title = _text(candidate.get("title"))
    evidence = candidate.get("evidence") or []
    source_id = (evidence[0].get("source_id") if evidence else None) or candidate.get("source_id")
    source = sources.get(source_id) or {}
    url = article_url(candidate)
    document = {}
    for snapshot_id in candidate.get("snapshot_ids") or []:
        row = snapshots.get(snapshot_id) or {}
        if url and row.get("source_url") == url:
            document = row
            break
        if not document and row.get("content_kind") in ("html", "text"):
            document = row
    days = policy.deadline_days(candidate)
    future = max(days) if days else None
    if future and future < today:
        deadline_state = "past"
    elif future:
        deadline_state = "future"
    else:
        deadline_state = "none"
    decision, reason = policy.classify(candidate, today=today)

    signals = []
    score = 0

    def note(points, text):
        nonlocal score
        score += points
        signals.append(f"{'+' if points > 0 else '−'}{abs(points)} {text}")

    if deadline_state == "future":
        delta = (future - today).days
        note(40, f"有未来期限 {future.isoformat()}")
        note(20 if delta <= 7 else 12 if delta <= 30 else 6, f"{delta} 天后到期")
    elif deadline_state == "past":
        note(-30, f"期限已过（最晚 {future.isoformat()}）")
    else:
        note(-25, "没有任何期限证据（机器不敢自动接受的原因）")
    if policy.STRONG_NOTICE.search(title):
        note(20, "标题含「通知/公告」")
    elif policy.NOTICE_WORD.search(title):
        note(10, "标题是办理类词（报名/申请/公示…）")
    if candidate.get("action_required"):
        note(14, "抽取判定：需要行动")
    if candidate.get("materials"):
        note(8, f"带材料 {len(candidate['materials'])} 条")
    if candidate.get("entry_urls"):
        note(4, "带入XX口链接")
    host = (source.get("origin_url") or "").split("//")[-1].split("/")[0]
    if host.endswith(".tju.edu.cn"):
        note(6, f"官方域名 {host}")
    elif host:
        note(-6, f"非校园网来源 {host}")
    if policy.DOCUMENT_TITLE.search(title) and not policy.STRONG_NOTICE.search(title):
        note(-18, "标题像表格/附件名")
    if not (policy.NOTICE_WORD.search(title) or candidate.get("action_required") or candidate.get("materials")):
        note(-10, "没有办理类信号")
    fetched = _stamp(document.get("fetched_at") or document.get("published_at"))
    if fetched != "—":
        try:
            age = (today - date.fromisoformat(fetched)).days
            if age <= 7:
                note(6, f"近 7 天抓到（{fetched}）")
            elif age <= 30:
                note(3, f"近 30 天抓到（{fetched}）")
        except ValueError:
            pass
    year = re_year(title)
    if year:
        note(4, f"标题点名 {year} 年")

    if policy.DOCUMENT_TITLE.search(title) and not policy.STRONG_NOTICE.search(title):
        tier = "3"
    elif candidate.get("action_required") or candidate.get("materials") or policy.STRONG_NOTICE.search(title):
        tier = "1"
    elif policy.NOTICE_WORD.search(title):
        tier = "2"
    else:
        tier = "3"

    return {
        "candidate_id": candidate["candidate_id"],
        "revision": candidate.get("revision"),
        "title": title,
        "source_id": source_id,
        "source_name": source.get("name") or source_id,
        "source_origin": source.get("origin_url"),
        "url": url,
        # Snapshots behind this candidate (several when a notice was republished
        # or matched the same URL twice). Displayed by the grouped list; nothing
        # is decided from it.
        "snapshot_ids": list(candidate.get("snapshot_ids") or []),
        "published": _stamp(document.get("published_at")),
        "fetched": _stamp(document.get("fetched_at")),
        "deadline_state": deadline_state,
        "deadline_days": [day.isoformat() for day in days],
        "next_deadline": future.isoformat() if future else None,
        "machine_decision": decision,
        "machine_reason": reason,
        "machine_because": explain(decision, reason, candidate, days, future, today, len(evidence)),
        "score": score,
        "signals": signals,
        "tier": tier,
        "action_required": bool(candidate.get("action_required")),
        "action_steps": candidate.get("action_steps") or [],
        "materials": candidate.get("materials") or [],
        "entry_urls": candidate.get("entry_urls") or [],
        "evidence": evidence,
        "deadlines": candidate.get("deadlines") or [],
        "change_kind": candidate.get("change_kind"),
        "target_event_id": candidate.get("target_event_id"),
        "proposed_status": candidate.get("proposed_status"),
        "event_type": candidate.get("event_type"),
        "extractor_version": candidate.get("extractor_version"),
    }


def re_year(text):
    match = YEAR.search(text or "")
    if not match:
        return None
    try:
        if 2024 <= int(match.group(1)) <= 2030:
            return match.group(1)
    except ValueError:
        return None
    return None


def explain(decision, reason, candidate, days, future, today, evidence_count):
    """Why the machine did not decide this one — in the policy's own words.

    The verdict sentence is ``classify``'s; the following lines state which of
    the policy's own conditions did not hold, so a human does not repeat work
    the machine already did.
    """
    lines = [f"策略结论：{decision} — {reason}"]
    if decision == "review":
        if not days:
            lines.append(
                "机器不能接受它：classify 只在「今天仍可办」时自动接受，需要一条还没到的期限，"
                f"本条期限证据 {len(days)} 条；也不能拒绝它：没命中任何拒绝特征"
                "（栏目名/错误页/【栏目碎片】/表格附件名/无正文依据/无关联取消/全期限已过/新闻稿）"
            )
        else:
            lines.append(
                f"机器不能接受它：期限 {len(days)} 条，但没有一条还没到（最晚 {max(days).isoformat()}）；"
                "也不能拒绝它：没命中任何拒绝特征"
            )
        lines.append(
            f"可核对依据：{evidence_count} 段正文引文 · 材料 {len(candidate.get('materials') or [])} 条 · "
            f"入口 {len(candidate.get('entry_urls') or [])} 条"
        )
    elif decision == "accept":
        lines.append(f"机器结论可直接落地：最晚期限 {future.isoformat()} 仍在今天之后（仍未写入，需人确认）")
    else:
        lines.append("机器结论可直接落地：这不是学生可办的事项（仍未写入，需人确认）")
    return lines


def sort_views(views, order):
    if order == "deadline":
        return sorted(views, key=lambda v: (v["deadline_state"] != "future", v["next_deadline"] or "9999", v["title"]))
    if order == "recent":
        return sorted(views, key=lambda v: (v["fetched"] == "—", v["fetched"]), reverse=True)
    if order == "source":
        return sorted(views, key=lambda v: (v["source_name"], v["title"]))
    return sorted(views, key=lambda v: (v["tier"], -v["score"], v["fetched"] == "—", v["title"]))


TIER_LABEL = {
    "1": "建议先看 · 像真通知（有行动/材料/通知公告词）",
    "2": "可后看 · 像通知但没有期限",
    "3": "多半是参考资料或新闻（沉底）",
}

# --- grouping: one row per *thing to decide*, not per stored copy -------------
# The queue is fed by several sources and by repeated fetches, so the same notice
# arrives more than once (measured on the real queue: 119 pending rows covering 86
# distinct titles). Deciding the same notice four times is wasted human attention,
# so the default listing groups rows that are the same thing. The grouping is a
# view: every candidate, snapshot and review row stays exactly where it was, the
# representative row is the highest-scoring copy, and every member id is printed
# before anything is written.
LEAD_ENUM = re.compile(
    r"^\s*(?:第?\s*\d+\s*[.、)）]|[(（［\[【]\s*\d+\s*[)）\]］】]|[一二三四五六七八九十]+\s*[、.])"
)
TRAILING_NOISE = re.compile(r"[\s·・…\.．,，;；:：、\-—_]+$")


def normalize_title(value):
    """Title identity for grouping: width, whitespace, a leading enumerator, tail noise.

    Deliberately narrow. It does not strip 栏目 prefixes or 转载 markers, so two
    rows only group when their titles really are the same string up to those
    differences; anything else stays a separate decision.
    """
    text = unicodedata.normalize("NFKC", _text(value) or "")
    text = LEAD_ENUM.sub("", text)
    text = re.sub(r"\s+", "", text)
    text = TRAILING_NOISE.sub("", text)
    return text.casefold()


def deadline_key(view):
    """The instants a candidate claims, as a comparable set (kind/precision aside)."""
    return tuple(
        sorted(
            {
                str(entry.get("at") or entry.get("date"))
                for entry in view.get("deadlines") or []
                if entry.get("precision") != "unknown" and (entry.get("at") or entry.get("date"))
            }
        )
    )


def group_key(view):
    """Same normalized title *and* the same deadline set = one thing to decide."""
    return (normalize_title(view["title"]), deadline_key(view))


def group_views(views):
    """Group already-computed views. No candidate is dropped: members carry them all."""
    buckets = {}
    for view in views:
        buckets.setdefault(group_key(view), []).append(view)
    groups = []
    for key, members in buckets.items():
        members = sorted(members, key=lambda item: (-item["score"], item["fetched"] == "—", item["candidate_id"]))
        titles = {member["title"] for member in members}
        groups.append(
            {
                "key": key,
                "representative": members[0],
                "members": members,
                "member_ids": [member["candidate_id"] for member in members],
                "sources": sorted({member["source_name"] for member in members if member["source_name"]}),
                "snapshots": sorted({sid for member in members for sid in member.get("snapshot_ids") or []}),
                "title_variants": sorted(titles) if len(titles) > 1 else [],
            }
        )
    groups.sort(key=lambda group: (group["representative"]["tier"], -group["representative"]["score"], group["representative"]["title"]))
    return groups


def render_groups(groups, data_dir, out, total, hidden, total_rows, title_only=None):
    counts = {"1": 0, "2": 0, "3": 0}
    deadlines = 0
    for group in groups:
        view = group["representative"]
        counts[view["tier"]] += 1
        deadlines += 1 if view["deadline_state"] == "future" else 0
    # `total` is the number of groups over the whole queue, `len(groups)` only the
    # page — the merge count must describe the queue, not the page.
    merged = total_rows - total
    print(f"待人工判断 {total_rows} 条（数据目录 {data_dir}）", file=out)
    print(
        f"按「标题 + 期限集合」归并为 {total} 组：另有 {merged} 条是同组重复"
        f"（同一件事的其它来源/快照，一次决定即可覆盖）",
        file=out,
    )
    if title_only is not None and title_only != total:
        print(
            f"（只按标题去重是 {title_only} 组；多出的 {total - title_only} 组是同标题但期限证据不同，"
            "它们确实是不同的判断）",
            file=out,
        )
    print(
        "本页 {} 组 · 建议先看 {} · 可后看 {} · 参考资料 {} · 组内有未来期限 {} 组".format(
            len(groups), counts["1"], counts["2"], counts["3"], deadlines
        ),
        file=out,
    )
    print(
        "分组只影响这一屏的排布：候选、快照、审计一条不动；对一组做决策会先把组内候选 id 全列出来，"
        "仍需 --reason，仍受 --max-batch 限制，且必须 --yes 才写入",
        file=out,
    )
    print("想看一行一条：--flat；想看某组细节：--show <组内任一 id>", file=out)
    if hidden:
        print(f"（还有 {hidden} 组未显示：--limit 0 看全部）", file=out)
    for index, group in enumerate(groups, 1):
        view = group["representative"]
        others = len(group["members"]) - 1
        sources = "、".join(group["sources"][:3]) + ("…" if len(group["sources"]) > 3 else "")
        print("", file=out)
        print(
            f"[{index}] T{view['tier']} 分 {view['score']} · 期限 {view['next_deadline'] or '无'} · "
            f"抓取 {view['fetched']} · 同组 {len(group['members'])} 条",
            file=out,
        )
        print(f"    标题：{view['title']}", file=out)
        print(f"    来源：{sources or view['source_name']}（{len(group['sources']) or 1} 个来源）", file=out)
        if others:
            print(f"    同组另有 {others} 条候选（同一件事的其它快照/来源）：{', '.join(group['member_ids'][1:])}", file=out)
            if group["title_variants"]:
                print(f"    同组标题不完全一致（归一后相同）：{' ｜ '.join(group['title_variants'])}", file=out)
            print(f"    快照合计 {len(group['snapshots'])} 份：{', '.join(group['snapshots'][:6]) or '—'}", file=out)
        print(f"    原文：{view['url'] or '（无正文链接）'}", file=out)
        print(f"    代表候选：{view['candidate_id']}（第 {view['revision']} 版）", file=out)
        for line in view["machine_because"]:
            print(f"    机器为什么不决定：{line}" if line.startswith("策略结论") else f"      · {line}", file=out)
        print(f"    打分依据：{' · '.join(view['signals']) or '（无）'}", file=out)
        print(f"    下一步：--show {view['candidate_id']} 看细节；", file=out)
        print(
            f"            整组决策：--group {view['candidate_id']} --accept --reason \"…\" [--yes --apply]"
            f"（会先列出 {len(group['members'])} 条候选 id）",
            file=out,
        )


def render_list(views, data_dir, out, total, hidden):
    counts = {"1": 0, "2": 0, "3": 0}
    deadlines = 0
    machine = {}
    for view in views:
        counts[view["tier"]] += 1
        deadlines += 1 if view["deadline_state"] == "future" else 0
        machine[view["machine_decision"]] = machine.get(view["machine_decision"], 0) + 1
    print(f"待人工判断 {total} 条（数据目录 {data_dir}）", file=out)
    print(
        "本页 {} 条 · 建议先看 {} · 可后看 {} · 参考资料 {} · 其中有未来期限 {} 条".format(
            len(views), counts["1"], counts["2"], counts["3"], deadlines
        ),
        file=out,
    )
    print(
        "机器三态（review_and_publish.classify）："
        + " · ".join(f"{key} {value}" for key, value in sorted(machine.items()))
        + "；本站策略只自动接受「今天仍可办」的，所以队列里绝大多数是 review",
        file=out,
    )
    print(
        "人工决定：accept 会发布为可检索事项，reject 会记录理由并移出队列；"
        "单条用 --accept/--reject <id> --reason \"…\" --apply，批量必须显式指定 id 或筛选条件",
        file=out,
    )
    if hidden:
        print(f"（还有 {hidden} 条未显示：--limit 0 看全部）", file=out)
    for index, view in enumerate(views, 1):
        print("", file=out)
        print(
            f"[{index}] T{view['tier']} 分 {view['score']} · {view['source_name']} · "
            f"期限 {view['next_deadline'] or '无'} · 抓取 {view['fetched']}",
            file=out,
        )
        print(f"    标题：{view['title']}", file=out)
        print(f"    原文：{view['url'] or '（无正文链接）'}", file=out)
        print(f"    候选：{view['candidate_id']}（第 {view['revision']} 版）", file=out)
        for line in view["machine_because"]:
            print(f"    机器为什么不决定：{line}" if line.startswith("策略结论") else f"      · {line}", file=out)
        print(f"    打分依据：{' · '.join(view['signals']) or '（无）'}", file=out)
        print(
            f"    下一步：--show {view['candidate_id']} 看细节；"
            f"--accept {view['candidate_id']} --reason \"…\" --apply 接受",
            file=out,
        )


def render_detail(view, core, out):
    print(f"候选 {view['candidate_id']}（第 {view['revision']} 版）· T{view['tier']} 分 {view['score']}", file=out)
    print(f"标题：{view['title']}", file=out)
    print(f"来源：{view['source_name']} · {view['source_origin']}", file=out)
    print(f"原文：{view['url'] or '（无）'}", file=out)
    print(f"类型：{view['event_type']} · 变化：{view['change_kind']} · 拟发布状态：{view['proposed_status']}", file=out)
    print(f"时间：发布 {view['published']} · 抓取 {view['fetched']} · 抽取规则 {view['extractor_version']}", file=out)
    print(f"需要行动：{'是' if view['action_required'] else '否'}", file=out)
    for step in (view.get("action_steps") or [])[:6]:
        print(f"  · 步骤：{_text(step, 120)}", file=out)
    if len(view.get("action_steps") or []) > 6:
        print(f"  · …还有 {len(view['action_steps']) - 6} 条步骤", file=out)
    print("", file=out)
    print("机器为什么不决定：", file=out)
    for line in view["machine_because"]:
        print(f"  {line}", file=out)
    print(f"打分依据：{' · '.join(view['signals']) or '（无）'}", file=out)

    print("", file=out)
    if view["deadlines"]:
        print(f"期限（{len(view['deadlines'])} 条，均有引文）：", file=out)
        for entry in view["deadlines"]:
            when = entry.get("at") or entry.get("date") or "?"
            quote = _text(entry.get("quote"), 140) or quote_for(when, {"evidence": view["evidence"]})[0] or "（无引文）"
            print(f"  · {when}（精度 {entry.get('precision') or '?'}）：{quote}", file=out)
    else:
        print("期限：无 —— 机器因此不能自动接受（这正是它留给人判断的原因）", file=out)
    clues = date_clues(view)
    if clues:
        print(f"日期线索（正文里出现、抽取未认成期限 —— 只给人看，不改变任何判定）：", file=out)
        for value, quote, segment in clues:
            print(f"  · {value}｜[{segment}] {quote}", file=out)

    if view["materials"]:
        print(f"材料（{len(view['materials'])} 条，附正文引文）：", file=out)
        for material in view["materials"]:
            quote, segment, page = quote_for(material, {"evidence": view["evidence"]})
            where = f"（{segment}{' p.' + str(page) if page else ''}）" if quote else "（正文里没有逐字引文，需人工确认）"
            print(f"  · {_text(material, 90)} {where}", file=out)
            if quote:
                print(f"      引文：{quote}", file=out)
    else:
        print("材料：无", file=out)

    evidence = view["evidence"]
    print("", file=out)
    print(f"正文证据引文（共 {len(evidence)} 段，前 8 段）：", file=out)
    for item in evidence[:8]:
        where = f"{item.get('segment_id')}{' p.' + str(item.get('page')) if item.get('page') else ''}"
        print(f"  · [{where}] {_text(item.get('quote'), 150)}", file=out)
    if view["entry_urls"]:
        print("", file=out)
        print(f"入口链接（{len(view['entry_urls'])} 条）：", file=out)
        for url in view["entry_urls"][:6]:
            print(f"  · {url}", file=out)

    audits = [row for row in core.store.list("reviews") if row.get("candidate_id") == view["candidate_id"]]
    print("", file=out)
    print(f"已有审计记录：{len(audits)} 条", file=out)
    for row in audits[-3:]:
        print(f"  · {row.get('created_at')} {row.get('decision')} by {row.get('reviewer_id')}：{_text(row.get('reason'), 80)}", file=out)

    print("", file=out)
    print("下一步（把 … 换成理由）：", file=out)
    print(f"  python scripts/review_queue.py --data-dir <数据目录> --accept {view['candidate_id']} --reason \"…\" --apply", file=out)
    print(f"  python scripts/review_queue.py --data-dir <数据目录> --reject {view['candidate_id']} --reason \"…\" --apply", file=out)


def pending_views(core, today):
    snapshots = index_snapshots(core)
    sources = index_sources(core)
    return [
        view_of(candidate, snapshots, sources, today)
        for candidate in daily.pending_candidates(core)
    ]


def find_view(views, candidate_id):
    for view in views:
        if view["candidate_id"] == candidate_id:
            return view
    return None


def published_counts(core):
    events = core.store.list("events")
    return {
        "events": len(events),
        "active": len([event for event in events if event.get("status") == "active"]),
        "versions": sum(int(event.get("version") or 0) for event in events),
    }


def decide_view(core, view, decision, reason, apply, publish, out):
    """One decision through Pipeline.review (via the daily update's helper)."""
    candidate = core.store.get("candidates", view["candidate_id"])
    if candidate is None:
        print(f"候选不存在：{view['candidate_id']}", file=out)
        return EXIT_USAGE
    before = published_counts(core) if publish else None
    if not apply:
        print(
            f"演练：将把 {view['candidate_id']} 判为 {decision}（理由：{reason}）"
            f" → Pipeline.review → candidate.review_status={ 'accepted' if decision == 'accept' else 'rejected' }",
            file=out,
        )
        print("  写入需要 --apply；本命令默认演练，不改动任何数据。", file=out)
        return EXIT_OK
    try:
        updated = daily.apply_decision(core, candidate, decision, reason)
    except Exception as error:  # noqa: BLE001 - reported, never swallowed
        print(f"决策失败 {view['candidate_id']}：{daily._code(error)}", file=out)
        return EXIT_FAILED
    print(
        f"已写入审计：{view['candidate_id']} → {decision}（第 {updated.get('revision')} 版）· 理由：{reason}",
        file=out,
    )
    if decision == "accept" and publish:
        try:
            event = core.events.reconcile_candidate(view["candidate_id"], updated.get("revision"))
        except Exception as error:  # noqa: BLE001
            print(f"发布失败：{daily._code(error)}", file=out)
            return EXIT_FAILED
        after = published_counts(core)
        print(
            f"已发布核对：事件 {event.get('id') if event else '（无）'} · 版本 {event.get('version') if event else '?'} · "
            f"公开事项 {before['active']} → {after['active']} · 事件版本合计 {before['versions']} → {after['versions']}",
            file=out,
        )
    return EXIT_OK


def match_filter(view, key, value):
    if key == "tier":
        return view["tier"] == value
    if key == "deadline":
        return view["deadline_state"] == value
    if key == "notice":
        hit = bool(policy.NOTICE_WORD.search(view["title"]))
        return hit if value == "yes" else not hit
    if key == "action":
        return view["action_required"] if value == "yes" else not view["action_required"]
    if key == "materials":
        hit = bool(view["materials"])
        return hit if value == "yes" else not hit
    if key == "document":
        hit = bool(policy.DOCUMENT_TITLE.search(view["title"]) and not policy.STRONG_NOTICE.search(view["title"]))
        return hit if value == "yes" else not hit
    if key == "machine":
        return view["machine_decision"] == value
    if key == "source":
        needle = value.lower()
        return needle in (view["source_name"] or "").lower() or needle in (view["source_id"] or "").lower()
    if key == "title":
        return value in view["title"]
    return False


def parse_filters(raw):
    filters = []
    for item in raw:
        if "=" not in item:
            return None, f"--batch-filter 需要 key=value 形式，收到：{item}"
        key, _, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if key not in FILTERS:
            return None, f"--batch-filter 不支持 {key}；可用：{', '.join(sorted(FILTERS))}"
        allowed = FILTERS[key]
        if allowed is not None and value not in allowed:
            return None, f"--batch-filter {key} 只能是 {', '.join(allowed)}，收到：{value}"
        if allowed is None and not value:
            return None, f"--batch-filter {key} 需要非空的值"
        filters.append((key, value))
    return filters, None


def render_plan(views, decision, reason, data_dir, out, note):
    print(f"批量{ '接受' if decision == 'accept' else '拒绝' }计划（{note}）", file=out)
    print(f"数据目录 {data_dir} · 将影响 {len(views)} 条 · 理由：{reason}", file=out)
    print("工具没有「全部接受」入口：这里只处理下面这些明确选出的候选。", file=out)
    for index, view in enumerate(views, 1):
        print(
            f"  {index:>3}. [{view['candidate_id']}] T{view['tier']} 分 {view['score']} · "
            f"{view['source_name']} · 期限 {view['next_deadline'] or '无'} · {view['title'][:60]}",
            file=out,
        )
    print("", file=out)
    print("确认无误后，把上面这条命令加上 --yes 与 --apply 再执行一次。", file=out)


def main(argv=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(
        prog="review_queue.py",
        description="复核工作台：列出留给人判断的候选、看细节、带理由决策（默认演练）。",
    )
    parser.add_argument("--data-dir", help="数据目录（默认环境变量 CAMPUS_DATA_DIR 或 data-operator/）")
    parser.add_argument("--limit", type=int, default=20, help="列表最多显示多少条，0 表示全部（默认 20）")
    parser.add_argument("--sort", choices=SORTS, default="priority", help="列表排序（默认按建议优先级）")
    parser.add_argument("--show", metavar="CANDIDATE_ID", help="显示一条候选的完整判断依据")
    parser.add_argument(
        "--accept",
        nargs="?",
        const=BATCH_TARGET,
        metavar="CANDIDATE_ID",
        help="接受一条候选（需要 --reason）；批量时作为目标标志，不带值",
    )
    parser.add_argument(
        "--reject",
        nargs="?",
        const=BATCH_TARGET,
        metavar="CANDIDATE_ID",
        help="拒绝一条候选（需要 --reason）；批量时作为目标标志，不带值",
    )
    parser.add_argument("--batch-ids", help="批量：逗号分隔的候选 id 列表（必须显式给出）")
    parser.add_argument(
        "--batch-filter",
        action="append",
        default=[],
        help="批量：显式筛选条件 key=value，可重复（AND）；可用键：" + ", ".join(sorted(FILTERS)),
    )
    parser.add_argument(
        "--group",
        metavar="CANDIDATE_ID",
        help="批量：以组内任一候选 id 选中「同标题+同期限」的整组（会把组内候选 id 全部列出来，仍需 --reason/--yes）",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help="列表按一行一条显示（不做同标题分组）；分组只是呈现层聚合，随时可切回",
    )
    parser.add_argument("--reason", help="决策理由，写进审计（单条与批量都必须给）")
    parser.add_argument("--apply", action="store_true", help="真正写入；缺省只演练")
    parser.add_argument("--yes", action="store_true", help="批量写入的确认开关（缺省只打印将影响的条目）")
    parser.add_argument("--max-batch", type=int, default=25, help="批量上限，超过就拒绝（默认 25，需显式提高）")
    parser.add_argument("--no-publish", action="store_true", help="接受时不发布，只写复核决策")
    parser.add_argument("--today", help="以该日期判断期限（YYYY-MM-DD，默认今天，Asia/Shanghai）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（列表与详情）")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or EXIT_USAGE)

    if args.today:
        try:
            today = date.fromisoformat(args.today)
        except ValueError:
            print(f"--today 需要 YYYY-MM-DD，收到：{args.today}", file=err)
            return EXIT_USAGE
    else:
        today = datetime.now(policy.TZ_SHANGHAI).date()

    decisions = [name for name, value in (("--accept", args.accept), ("--reject", args.reject)) if value is not None]
    batch_mode = bool(
        args.batch_ids
        or args.batch_filter
        or args.group
        or args.accept == BATCH_TARGET
        or args.reject == BATCH_TARGET
    )
    if len(decisions) > 1:
        print("--accept 与 --reject 不能同时使用。", file=err)
        return EXIT_USAGE
    if not batch_mode and decisions and (args.accept == BATCH_TARGET or args.reject == BATCH_TARGET):
        print("--accept/--reject 少了候选 id；批量请配合 --batch-ids 或 --batch-filter 使用。", file=err)
        return EXIT_USAGE
    if batch_mode and not decisions:
        print("批量入口需要同时给出 --accept 或 --reject 作为目标决策。", file=err)
        return EXIT_USAGE
    if (decisions or batch_mode) and not _text(args.reason):
        print("决策必须带 --reason（会写进 reviews 审计）；没有理由的决策不受理。", file=err)
        return EXIT_USAGE
    if (decisions or batch_mode) and len(_text(args.reason)) < 4:
        print("--reason 太短（至少 4 个字符）：审计要能回答「为什么」。", file=err)
        return EXIT_USAGE
    if args.batch_ids and args.batch_filter:
        print("--batch-ids 与 --batch-filter 二选一：显式 id 列表，或显式筛选条件。", file=err)
        return EXIT_USAGE
    if args.group and (args.batch_ids or args.batch_filter):
        print("--group 与 --batch-ids/--batch-filter 二选一：整组，或显式条件。", file=err)
        return EXIT_USAGE

    requested = Path(args.data_dir) if args.data_dir else Path(os.environ.get("CAMPUS_DATA_DIR") or collect.DEFAULT_DATA_DIR)
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    try:
        data_dir = collect._guard_data_dir(requested)
    except collect.UsageError as error:
        print(str(error), file=err)
        return EXIT_USAGE

    if not daily.has_existing_store(data_dir):
        print(f"数据目录里没有可读的产品数据：{collect.display_path(data_dir)}", file=err)
        return EXIT_USAGE

    # A batch only writes when it was explicitly confirmed; everything else is a
    # dry run. The signing key is never created by this command: the placeholder
    # reuses the stored key when the directory has one and only avoids writing a
    # new one, so even a refused write leaves the directory as it found it.
    batch_write = bool(args.apply and batch_mode and args.yes)
    restore_secret = daily.prepare_readonly_secret(data_dir)
    lock_holder = {"lock": None}
    core = None

    def ensure_lock():
        """Take the directory lock right before the first write, and only then."""
        if lock_holder["lock"] is not None:
            return True
        candidate_lock = daily.ProcessLock(data_dir / ".review-queue.lock")
        if not candidate_lock.acquire():
            print("另一个写入进程正在使用该数据目录（锁 .review-queue.lock），本次不做任何改动。", file=err)
            return False
        lock_holder["lock"] = candidate_lock
        return True

    try:
        core = collect.build_core(data_dir, timeout=20)
        views = pending_views(core, today)
        shown_dir = collect.display_path(data_dir)

        if args.json and not decisions and not batch_mode:
            if args.show:
                view = find_view(views, args.show)
                if view is None:
                    print(json.dumps({"error": "candidate_not_pending", "candidate_id": args.show}, ensure_ascii=False))
                    return EXIT_USAGE
                print(json.dumps(view, ensure_ascii=False, indent=2), file=out)
                return EXIT_OK
            listing = sort_views(views, args.sort)
            if args.flat:
                if args.limit:
                    listing = listing[: args.limit]
                print(
                    json.dumps(
                        {"data_dir": shown_dir, "pending": len(views), "candidates": listing},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    file=out,
                )
                return EXIT_OK
            all_groups = group_views(listing)
            groups = all_groups[: args.limit] if args.limit else all_groups
            print(
                json.dumps(
                    {
                        "data_dir": shown_dir,
                        "pending": len(views),
                        "pending_groups": len(all_groups),
                        "merged": len(views) - len(all_groups),
                        "groups": [
                            {
                                "representative": group["representative"],
                                "member_ids": group["member_ids"],
                                "sources": group["sources"],
                                "snapshots": group["snapshots"],
                                "merged": len(group["members"]) - 1,
                                "title_variants": group["title_variants"],
                            }
                            for group in groups
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=out,
            )
            return EXIT_OK

        if args.show:
            view = find_view(views, args.show)
            if view is None:
                print(f"待人工判断的队列里没有这个候选：{args.show}", file=err)
                return EXIT_USAGE
            render_detail(view, core, out)
            return EXIT_OK

        if decisions and not batch_mode:
            candidate_id = args.accept or args.reject
            view = find_view(views, candidate_id)
            if view is None:
                print(f"待人工判断的队列里没有这个候选：{candidate_id}", file=err)
                return EXIT_USAGE
            if args.apply and not ensure_lock():
                return EXIT_FAILED
            return decide_view(
                core, view, "accept" if args.accept else "reject", _text(args.reason), args.apply, not args.no_publish, out
            )

        if batch_mode:
            filters, problem = parse_filters(args.batch_filter)
            if problem:
                print(problem, file=err)
                return EXIT_USAGE
            if args.batch_ids:
                wanted = [item.strip() for item in args.batch_ids.split(",") if item.strip()]
                if not wanted:
                    print("--batch-ids 是空的：要么给出 id，要么用 --batch-filter。", file=err)
                    return EXIT_USAGE
                wanted = list(dict.fromkeys(wanted))
                missing = [item for item in wanted if find_view(views, item) is None]
                if missing:
                    print("这些 id 不在待人工判断的队列里（拼写或状态不符）：" + ", ".join(missing), file=err)
                    return EXIT_USAGE
                selected = [find_view(views, item) for item in wanted]
                note = f"显式 id {len(wanted)} 个"
            elif args.group:
                anchor = find_view(views, args.group)
                if anchor is None:
                    print(f"待人工判断的队列里没有这个候选：{args.group}", file=err)
                    return EXIT_USAGE
                group = next(item for item in group_views(views) if anchor["candidate_id"] in item["member_ids"])
                selected = group["members"]
                note = f"分组（代表 {group['representative']['candidate_id']}，{len(selected)} 条同标题+同期限候选）"
                print(
                    f"整组将影响 {len(selected)} 条候选（分组只是呈现层，底层候选一条不少）："
                    + ", ".join(group["member_ids"]),
                    file=out,
                )
                if group["title_variants"]:
                    print("同组原始标题：" + " ｜ ".join(group["title_variants"]), file=out)
            else:
                if not filters:
                    print("--batch-filter 至少要给出一个条件（key=value），否则就是无差别批量。", file=err)
                    return EXIT_USAGE
                selected = [view for view in views if all(match_filter(view, key, value) for key, value in filters)]
                note = "筛选 " + " · ".join(f"{key}={value}" for key, value in filters)
            if not selected:
                print("筛选条件没有命中任何候选：什么都没改。", file=err)
                return EXIT_USAGE
            if len(selected) > args.max_batch:
                print(
                    f"选中 {len(selected)} 条，超过 --max-batch {args.max_batch}：请收窄条件，"
                    "或显式提高上限（--max-batch N）。没有一步到底的批量入口。",
                    file=err,
                )
                return EXIT_USAGE
            decision = "accept" if args.accept else "reject"
            if not batch_write:
                render_plan(selected, decision, _text(args.reason), shown_dir, out, note)
                return EXIT_USAGE
            if not ensure_lock():
                return EXIT_FAILED
            render_plan(selected, decision, _text(args.reason), shown_dir, out, note)
            failed = 0
            for view in selected:
                code = decide_view(core, view, decision, _text(args.reason), True, not args.no_publish, out)
                failed += code != EXIT_OK
            print(f"\n批量完成：成功 {len(selected) - failed} · 失败 {failed}", file=out)
            return EXIT_FAILED if failed else EXIT_OK

        listing = sort_views(views, args.sort)
        total = len(listing)
        if args.flat:
            if args.limit:
                listing = listing[: args.limit]
            render_list(listing, shown_dir, out, total, total - len(listing))
            return EXIT_OK
        groups = group_views(listing)
        total_groups = len(groups)
        visible = groups[: args.limit] if args.limit else groups
        title_only = len({normalize_title(view["title"]) for view in listing})
        render_groups(visible, shown_dir, out, total_groups, total_groups - len(visible), total, title_only)
        return EXIT_OK
    except collect.UsageError as error:
        print(str(error), file=err)
        return EXIT_USAGE
    except Exception as error:  # noqa: BLE001 - a crashed command must still report
        print(f"复核工作台失败：{daily._code(error)}", file=err)
        return EXIT_FAILED
    finally:
        if lock_holder["lock"] is not None:
            lock_holder["lock"].release()
        if core is not None:
            core.store.close()
        restore_secret()


if __name__ == "__main__":
    raise SystemExit(main())