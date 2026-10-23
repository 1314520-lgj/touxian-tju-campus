"""What did a daily update actually change for the student?

Read-only comparison of two product data directories (``--before`` / ``--after``),
built on the same public-list predicate the anonymous ``GET /api/v1/events`` uses:
``publication_state == "published"``.

It answers the question this acceptance exists for: not "how many documents were
collected" but "what can a student see now that they could not see before".

Usage::

    python verification/acceptance/compare_public.py --before <dir> --after <dir>
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPTS = ROOT / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


daily = load("daily_update")
collect = daily.collect


def read(data_dir):
    """Everything the comparison needs, read-only."""
    restore = daily.prepare_readonly_secret(Path(data_dir))
    try:
        core = collect.build_core(Path(data_dir), timeout=20)
        events = core.store.list("events")
        published = {e["id"]: e for e in events if e.get("publication_state") == "published"}
        candidates = {c["candidate_id"]: c for c in core.store.list("candidates")}
        backlog = {}
        for row in core.store.list("source_ingestion"):
            for item in (row.get("items") or {}).values():
                if item.get("url"):
                    backlog.setdefault(item["url"], []).append((item.get("status"), item.get("error_code")))
        documents = {d.get("source_url"): d for d in core.store.list("snapshots") if not d.get("parent_snapshot_id")}
        reviews = core.store.list("reviews")
        core.store.close()
        return {
            "published": published,
            "candidates": candidates,
            "backlog": backlog,
            "documents": documents,
            "reviews": reviews,
            "events_by_state": dict(Counter(e.get("publication_state") for e in events)),
            "candidates_by_status": dict(Counter(c.get("review_status") for c in candidates.values())),
        }
    finally:
        restore()


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="public-list diff across a daily update (read-only)")
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)

    before, after = read(args.before), read(args.after)
    before_ids, after_ids = set(before["published"]), set(after["published"])
    gained = sorted(after_ids - before_ids, key=lambda eid: after["published"][eid].get("updated_at") or "")
    lost = sorted(before_ids - after_ids, key=lambda eid: before["published"][eid].get("updated_at") or "")

    print(f"公开列表（publication_state=published）：{len(before_ids)} → {len(after_ids)}", file=out)
    print(f"学生新增可见 {len(gained)} 条 · 学生失去可见 {len(lost)} 条", file=out)
    print("", file=out)
    print("学生新增可见的事项：", file=out)
    for event_id in gained:
        event = after["published"][event_id]
        urls = sorted({item.get("source_url") for item in event.get("evidence") or [] if item.get("source_url")})
        print(f"  + [{event_id}] 《{event.get('title')}》· 版本 {event.get('version')} · {event.get('updated_at')}", file=out)
        print(f"      期限 {[d.get('date') or d.get('at') for d in event.get('deadlines') or []]} · 原文 {urls[:1]}", file=out)
    if not gained:
        print("  （没有：这一次运行没有让任何新事项对匿名访客可见）", file=out)
    print("", file=out)
    print("学生失去可见的事项：", file=out)
    for event_id in lost:
        event = before["published"][event_id]
        print(f"  - [{event_id}] 《{event.get('title')}》· {event.get('updated_at')}", file=out)
    if not lost:
        print("  （没有）", file=out)

    print("", file=out)
    print("t21 救回的 6 条原生文章在这次运行前后的层级：", file=out)
    rescued = (
        ("通知公告 · 9030", "https://oaa.tju.edu.cn/info/1059/9030.htm"),
        ("通知公告 · 9000", "https://oaa.tju.edu.cn/info/1059/9000.htm"),
        ("学业指导 · 8760", "https://oaa.tju.edu.cn/info/1056/8760.htm"),
        ("学业指导 · 8700", "https://oaa.tju.edu.cn/info/1056/8700.htm"),
        ("学业指导 · 8440", "https://oaa.tju.edu.cn/info/1056/8440.htm"),
        ("通知公告总览 · 9030", "https://oaa.tju.edu.cn/info/1059/9030.htm"),
    )
    for label, url in rescued:
        document = after["documents"].get(url) or {}
        snapshots = {d.get("snapshot_id") for d in [document] if d}
        candidates = [c for c in after["candidates"].values() if snapshots & set(c.get("snapshot_ids") or [])]
        statuses = ", ".join(sorted({c.get("review_status") for c in candidates})) or "无候选"
        states = sorted(
            {e.get("publication_state") for e in after["published"].values() if e.get("title") == (document.get("title") or "")}
        )
        visible = any(
            e.get("title") == (document.get("title") or "") for e in after["published"].values()
        )
        print(
            f"  {label}: backlog {before['backlog'].get(url, '—')} → {after['backlog'].get(url, '—')} · "
            f"候选 {statuses} · 公开可见 {'✅' if visible else '❌'}"
            + (f" · 事项状态 {states}" if states else ""),
            file=out,
        )

    print("", file=out)
    print("其它层计数：", file=out)
    for key in ("events_by_state", "candidates_by_status"):
        print(f"  {key}: 前 {json.dumps(before[key], ensure_ascii=False)} → 后 {json.dumps(after[key], ensure_ascii=False)}", file=out)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "before": {"public": len(before_ids), "events_by_state": before["events_by_state"]},
                    "after": {"public": len(after_ids), "events_by_state": after["events_by_state"]},
                    "gained": [{"id": eid, "title": after["published"][eid].get("title")} for eid in gained],
                    "lost": [{"id": eid, "title": before["published"][eid].get("title")} for eid in lost],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())