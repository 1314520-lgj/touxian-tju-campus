"""t23：daily_update 摘要里的复核决定计数 + 事项状态变化。

    python verification/extraction/daily_decisions.py <daily-after.json> <campus.sqlite3> [今天]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path


def main(argv: list[str]) -> int:
    payload = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    db = Path(argv[2]).resolve()
    today = argv[3] if len(argv) > 3 else "2026-10-21"
    review = (payload.get("stages") or {}).get("review") or {}
    decisions = review.get("decisions") or []
    counts = Counter(item.get("decision") for item in decisions)
    print(f"今天={today} 复核决定 {len(decisions)} 条：{dict(counts)}")
    print(f"发布 {review.get('published')} 条 · 计划发布 {review.get('planned_publish')} · 发布错误 {len(review.get('publish_errors') or [])}")
    accepts = [item for item in decisions if item.get("decision") == "accept"]
    print(f"\n自动接受 {len(accepts)} 条：")
    for item in accepts[:14]:
        print(f"   · «{(item.get('title') or '')[:44]}»  {item.get('reason')}")
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = [json.loads(row[0]) for row in connection.execute("select data from campus_records where collection='events'").fetchall()]
    print(f"\n事项 {len(rows)}：{dict(Counter(row.get('status') for row in rows))}")
    live = [row for row in rows if row.get("status") == "active" and row.get("published_at")]
    print(f"公开且已发布: {len(live)}")
    cursor = connection.execute("select data from campus_records where collection='publication_audits'")
    audits = [json.loads(row[0]) for row in cursor.fetchall()]
    print(f"发布审计 {len(audits)} 条；最近 5 条：")
    for audit in audits[-5:]:
        print(f"   · {json.dumps({k: audit.get(k) for k in ('status', 'reason', 'published_at', 'event_id')}, ensure_ascii=False)[:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))