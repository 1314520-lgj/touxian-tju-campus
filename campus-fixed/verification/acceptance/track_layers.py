"""Layer tracker for the t24 acceptance: where does one article actually stand?

Read-only over a product data directory (``--data-dir``). For each requested
article URL it prints every layer it passed through, because "collected" is not
"published" and "published" is not "visible on the student's home page":

    backlog → document/snapshot → candidate (review_status) → decision (reviews)
            → event (publication_state) → public list (GET /api/v1/events)

The public list predicate is copied from ``server/campus/events.py`` (`/events`):
events whose ``publication_state == "published"``, ordered by ``updated_at``.
Nothing here writes to the data directory.

Usage::

    python verification/acceptance/track_layers.py --data-dir <dir> [--url URL ...]
    python verification/acceptance/track_layers.py --data-dir <dir> --counts
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

# The six native articles t21 rescued, as (column source_identity, url, label).
RESCUED = (
    ("oaa", "https://oaa.tju.edu.cn/info/1059/9030.htm", "通知公告 · 9030"),
    ("oaa", "https://oaa.tju.edu.cn/info/1059/9000.htm", "通知公告 · 9000"),
    ("oaa-academic", "https://oaa.tju.edu.cn/info/1056/8760.htm", "学业指导 · 8760"),
    ("oaa-academic", "https://oaa.tju.edu.cn/info/1056/8700.htm", "学业指导 · 8700"),
    ("oaa-academic", "https://oaa.tju.edu.cn/info/1056/8440.htm", "学业指导 · 8440"),
    ("oaa-notices", "https://oaa.tju.edu.cn/info/1059/9030.htm", "通知公告总览 · 9030"),
)


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


daily = load("daily_update")
collect = daily.collect


def public_events(core):
    """The exact predicate the anonymous ``GET /api/v1/events`` uses."""
    return sorted(
        [e for e in core.store.list("events") if e.get("publication_state") == "published"],
        key=lambda e: e.get("updated_at") or "",
        reverse=True,
    )


def counts(core):
    events = core.store.list("events")
    candidates = core.store.list("candidates")
    documents = core.store.list("snapshots")
    return {
        "documents": len(documents),
        "articles": len([d for d in documents if not d.get("parent_snapshot_id")]),
        "attachments": len([d for d in documents if d.get("parent_snapshot_id")]),
        "candidates": len(candidates),
        "candidates_by_status": dict(Counter(c.get("review_status") for c in candidates)),
        "events": len(events),
        "events_by_publication_state": dict(Counter(e.get("publication_state") for e in events)),
        "events_by_status": dict(Counter(e.get("status") for e in events)),
        "public_list": len(public_events(core)),
        "reviews": len(core.store.list("reviews")),
        "backlog_items": sum(len(row.get("items") or {}) for row in core.store.list("source_ingestion")),
    }


def backlog_index(core):
    """url -> (source_id, status, error_code) for every discovered item."""
    index = {}
    for row in core.store.list("source_ingestion"):
        for item in (row.get("items") or {}).values():
            if item.get("url"):
                index.setdefault(item["url"], []).append(
                    {"source_id": row.get("source_id"), "status": item.get("status"), "error_code": item.get("error_code")}
                )
    return index


def event_index(core):
    """article url -> event rows that carry it in their evidence."""
    index = {}
    for event in core.store.list("events"):
        urls = set()
        for item in event.get("evidence") or []:
            if item.get("source_url"):
                urls.add(item["source_url"])
        for url in urls:
            index.setdefault(url, []).append(event)
    return index


def track(core, source_identity, url, label, out):
    sources = {s["id"]: s for s in core.store.list("sources")}
    by_identity = {s.get("source_identity"): s for s in sources.values()}
    source = by_identity.get(source_identity) or {}
    print(f"\n[{label}] {url}", file=out)
    print(f"  栏目来源：{source.get('name') or '(未注册)'} · {source.get('origin_url') or ''}", file=out)

    documents = [d for d in core.store.list("snapshots") if (d.get("source_url") or "") == url]
    if not documents:
        print("  文档层：库里没有这个 URL 的快照（没有被采到）", file=out)
    for doc in documents:
        owner = sources.get(doc.get("source_id")) or {}
        print(
            f"  文档层：snapshot {doc.get('snapshot_id')} · 来源 {owner.get('name')} · "
            f"标题《{doc.get('title')}》· 抓取于 {doc.get('fetched_at')} · kind {doc.get('content_kind')}",
            file=out,
        )
    snapshot_ids = {d.get("snapshot_id") for d in documents}

    for item in backlog_index(core).get(url, []):
        owner = sources.get(item["source_id"]) or {}
        print(
            f"  backlog：{owner.get('name')} · 状态 {item['status']} · 错误码 {item['error_code']}",
            file=out,
        )

    candidates = [
        c for c in core.store.list("candidates") if snapshot_ids & set(c.get("snapshot_ids") or [])
    ]
    if not candidates:
        print("  候选层：没有候选（正文/抽取没产出候选）", file=out)
    reviews = core.store.list("reviews")
    for candidate in candidates:
        decision = [
            row
            for row in reviews
            if row.get("candidate_id") == candidate.get("candidate_id")
        ]
        latest = sorted(decision, key=lambda row: row.get("created_at") or "")[-1:] if decision else []
        print(
            f"  候选层：{candidate.get('candidate_id')} · 《{candidate.get('title')}》· "
            f"review_status={candidate.get('review_status')} · 版本 {candidate.get('revision')} · "
            f"期限 {[d.get('date') or d.get('at') for d in candidate.get('deadlines') or []]} · "
            f"引文 {len(candidate.get('evidence') or [])} 段",
            file=out,
        )
        for row in latest:
            print(
                f"           人工决策：{row.get('decision')} · 理由「{row.get('reason')}」· {row.get('created_at')}",
                file=out,
            )

    events = event_index(core).get(url, [])
    if not events:
        print("  事项层：没有关联事件（候选没有走完发布）", file=out)
    public_ids = {e["id"] for e in public_events(core)}
    for event in events:
        print(
            f"  事项层：{event.get('id')} · 《{event.get('title')}》· status={event.get('status')} · "
            f"publication_state={event.get('publication_state')} · 版本 {event.get('version')} · {event.get('updated_at')}",
            file=out,
        )
        print(
            f"  公开列表：{'✅ 在匿名 /api/v1/events 里' if event.get('id') in public_ids else '❌ 不在公开列表里'}"
            f"（publication_state={'published' if event.get('id') in public_ids else event.get('publication_state')}）",
            file=out,
        )


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="t24 layer tracker (read-only)")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--counts", action="store_true", help="只打印各层计数")
    parser.add_argument("--url", action="append", default=[], help="额外追踪的 URL（可重复）")
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    restore = daily.prepare_readonly_secret(data_dir)
    try:
        core = collect.build_core(data_dir, timeout=20)
        snapshot = counts(core)
        print(f"数据目录：{collect.display_path(data_dir)}", file=out)
        print(json.dumps(snapshot, ensure_ascii=False, indent=1), file=out)
        if not args.counts:
            for source_identity, url, label in RESCUED:
                track(core, source_identity, url, label, out)
            for url in args.url:
                track(core, "", url, "自定义", out)
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
        core.store.close()
    finally:
        restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())