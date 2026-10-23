"""t26 measurement: how much **new and still actionable** content does the product
actually surface per day?

Read-only over a product data directory. It deliberately separates three things that
are easy to confuse (t24 proved they get confused):

* **新增可见** — an event that entered ``publication_state == "published"``. Only a
  before/after snapshot diff can measure this for a run, because publications are
  not audited (only withdrawals are).
* **事件版本上升** — an existing visible event got new content (version bumped).
  The student sees the same row with refreshed content.
* **采集到达** — items the collectors fetched successfully. This is upstream of
  both and is *not* what a student sees.

Usage::

    python verification/freshness/measure_freshness.py --data-dir data-operator
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPTS = ROOT / "scripts"
TZ = ZoneInfo("Asia/Shanghai")


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


daily = load("daily_update")
collect = daily.collect


def day_of(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).astimezone(TZ).date()
    except ValueError:
        return None


def deadline_days(item):
    found = []
    for entry in item.get("deadlines") or []:
        value = entry.get("date") or (entry.get("at") or "")[:10]
        if not value:
            continue
        try:
            found.append(date.fromisoformat(value))
        except ValueError:
            continue
    return found


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="t26 freshness measurement (read-only)")
    parser.add_argument("--data-dir", default="data-operator")
    parser.add_argument("--today", help="YYYY-MM-DD (default Asia/Shanghai today)")
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else datetime.now(TZ).date()

    restore = daily.prepare_readonly_secret(Path(args.data_dir))
    try:
        core = collect.build_core(Path(args.data_dir), timeout=20)
        events = core.store.list("events")
        documents = {}
        for row in core.store.list("snapshots"):
            if row.get("source_url"):
                documents.setdefault(row["source_url"], row)
        published = [e for e in events if e.get("publication_state") == "published"]
        withdrawn = [e for e in events if e.get("publication_state") == "withdrawn"]

        # ---- M1: currently visible and still actionable -------------------
        actionable = [e for e in published if any(day >= today for day in deadline_days(e))]
        print(f"数据目录：{collect.display_path(Path(args.data_dir))} · 今天 {today.isoformat()}（Asia/Shanghai）", file=out)
        print("", file=out)
        print("M1 当前公开可见（publication_state=published）：", len(published), "条", file=out)
        print("   其中今天仍可办（有 >= 今天的期限）：", len(actionable), "条", file=out)
        print("   已下架（withdrawn）：", len(withdrawn), "条", file=out)

        # ---- M2: how old is what a student sees ---------------------------
        ages = Counter()
        unknown = 0
        for event in published:
            urls = [i.get("source_url") for i in event.get("evidence") or [] if i.get("source_url")]
            published_at = None
            for url in urls:
                doc = documents.get(url) or {}
                published_at = doc.get("published_at") or published_at
            day = day_of(published_at)
            if day is None:
                unknown += 1
                continue
            delta = (today - day).days
            ages["≤7 天" if delta <= 7 else "8–30 天" if delta <= 30 else "31–90 天" if delta <= 90 else ">90 天"] += 1
        print("", file=out)
        print("M2 可见事项的「原始通知发布日期」分布（反映学生看到的内容有多新）：", file=out)
        for bucket in ("≤7 天", "8–30 天", "31–90 天", ">90 天"):
            print(f"   {bucket:<8} {ages.get(bucket, 0)} 条", file=out)
        print(f"   没有发布日期的 {unknown} 条（列表页没给/正文没写）", file=out)

        # ---- M3: per-day activity the store does record -------------------
        reviews = core.store.list("reviews")
        per_day_accept = Counter()
        per_day_reject = Counter()
        for row in reviews:
            day = day_of(row.get("created_at"))
            if day is None:
                continue
            (per_day_accept if row.get("decision") == "accept" else per_day_reject)[day] += 1
        audits = core.store.list("publication_audits")
        per_day_withdrawn = Counter()
        for row in audits:
            day = day_of(row.get("created_at"))
            if day is not None:
                per_day_withdrawn[day] += 1
        print("", file=out)
        print("M3 库里**有记录**的每日活动（注意：accept 是决策，不等于新增可见）：", file=out)
        print(f"   {'日期':<12}{'接受决策':>8}{'拒绝决策':>8}{'下架':>6}", file=out)
        for day in sorted(set(per_day_accept) | set(per_day_reject) | set(per_day_withdrawn)):
            print(
                f"   {day.isoformat():<12}{per_day_accept.get(day, 0):>8}{per_day_reject.get(day, 0):>8}"
                f"{per_day_withdrawn.get(day, 0):>6}",
                file=out,
            )
        print("   （发布**没有**审计行：只有 withdrawn 会写 publication_audits，", file=out)
        print("     所以「某天新增了多少条可见」无法从库里直接读出来，只能对运行前后做快照差）", file=out)

        # ---- M4: collection arrivals (upstream of visibility) -------------
        arrivals = Counter()
        staged = 0
        for row in core.store.list("source_ingestion"):
            for item in (row.get("items") or {}).values():
                day = day_of(item.get("last_success_at"))
                if day is None:
                    staged += 1
                    continue
                arrivals[day] += 1
        print("", file=out)
        print("M4 采集到达（backlog 条目 last_success_at 按天；这是上游，不是学生可见）：", file=out)
        for day in sorted(arrivals)[-8:]:
            print(f"   {day.isoformat()}  {arrivals[day]} 条", file=out)
        print(f"   从未成功抓过的条目：{staged} 条", file=out)

        summary = {
            "today": today.isoformat(),
            "published": len(published),
            "actionable": len(actionable),
            "withdrawn": len(withdrawn),
            "age_buckets": dict(ages),
            "unknown_published_at": unknown,
            "per_day_accept": {d.isoformat(): n for d, n in sorted(per_day_accept.items())},
            "per_day_reject": {d.isoformat(): n for d, n in sorted(per_day_reject.items())},
            "per_day_withdrawn": {d.isoformat(): n for d, n in sorted(per_day_withdrawn.items())},
            "per_day_arrivals": {d.isoformat(): n for d, n in sorted(arrivals.items())},
            "actionable_titles": [
                {"title": e.get("title"), "deadlines": [d.isoformat() for d in deadline_days(e)]} for e in actionable
            ],
        }
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
        core.store.close()
        return 0
    finally:
        restore()


if __name__ == "__main__":
    raise SystemExit(main())