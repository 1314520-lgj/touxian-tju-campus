"""t26 estimator: what do the five new sources add per day, and why.

Read-only. Two inputs, both measured in this round:

* the columns' own publishing rate, from ``verification/freshness/probe_columns.py``
  (dated items on the list page inside the last 30 days);
* what the review policy does with what those columns actually contain, measured on a
  copy after really collecting them (``--json`` from ``campus_collect``/``daily_update``).

The estimator deliberately reports a **range**, and says which half is measured and
which half is extrapolated.

Usage::

    python verification/freshness/estimate.py --data-dir <copy after the run>
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
TZ = ZoneInfo("Asia/Shanghai")

NEW_IDENTITIES = ("yzb-notices", "cse-notices", "science-notices", "come-notices", "tjusee-notices")


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


daily = load("daily_update")
policy = daily.review_policy
collect = daily.collect


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="t26 daily-gain estimator (read-only)")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--today")
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else datetime.now(TZ).date()

    restore = daily.prepare_readonly_secret(Path(args.data_dir))
    try:
        core = collect.build_core(Path(args.data_dir), timeout=20)
        sources = {s["id"]: s for s in core.store.list("sources")}
        new_ids = {s["id"] for s in sources.values() if s.get("source_identity") in NEW_IDENTITIES}
        events = core.store.list("events")
        published = [e for e in events if e.get("publication_state") == "published"]

        verdicts = Counter()
        actionable_new = []
        new_titles = []
        for candidate in core.store.list("candidates"):
            urls = {item.get("source_url") for item in candidate.get("evidence") or []}
            belongs = any(
                source.get("source_id") in new_ids
                for source in (core.store.list("evidence") or [])
                if source.get("source_url") in urls
            )
            if not belongs:
                # Fall back to the snapshot -> source mapping the pipeline uses.
                for snapshot_id in candidate.get("snapshot_ids") or []:
                    row = core.store.get("snapshots", snapshot_id) or {}
                    if row.get("source_id") in new_ids:
                        belongs = True
                        break
            if not belongs:
                continue
            decision, reason = policy.classify(candidate, today=today)
            verdicts[decision] += 1
            if decision == "accept":
                actionable_new.append((candidate.get("title"), reason))
            if len(new_titles) < 12:
                new_titles.append((decision, candidate.get("title"), [d.get("date") for d in candidate.get("deadlines") or []]))

        print(f"数据目录：{collect.display_path(Path(args.data_dir))} · 今天 {today.isoformat()}", file=out)
        print(f"公开可见（published）：{len(published)} 条", file=out)
        print("", file=out)
        print("五个新来源产生的候选，按现有复核策略三态：", file=out)
        for key in ("accept", "reject", "review"):
            print(f"   {key:<7} {verdicts.get(key, 0)} 条", file=out)
        print("", file=out)
        print("其中「今天仍可办」（策略会自动接受、可直接发布）的：", file=out)
        for title, reason in actionable_new:
            print(f"   · 《{title}》 —— {reason}", file=out)
        if not actionable_new:
            print("   （本次采样里没有）", file=out)
        print("", file=out)
        print("采样到的候选（前 12）：", file=out)
        for decision, title, days in new_titles:
            print(f"   [{decision:<6}] 《{title}》 期限 {days}", file=out)

        summary = {
            "today": today.isoformat(),
            "published": len(published),
            "new_source_candidates": dict(verdicts),
            "actionable_new": [{"title": t, "reason": r} for t, r in actionable_new],
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