"""t23：队列与公开事项的可核对数字（只读）。

    python verification/extraction/queue_numbers.py <campus.sqlite3> [标签] [今天=YYYY-MM-DD]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path


def day(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def main(argv: list[str]) -> int:
    db = Path(argv[1]).resolve()
    label = argv[2] if len(argv) > 2 else ""
    today = date.fromisoformat(argv[3]) if len(argv) > 3 else date(2026, 10, 21)
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select data from campus_records where collection='candidates'")
    candidates = [json.loads(row[0]) for row in cursor.fetchall()]
    cursor.execute("select data from campus_records where collection='events'")
    events = [json.loads(row[0]) for row in cursor.fetchall()]

    def future_rows(candidate):
        rows = []
        for row in candidate.get("deadlines") or []:
            moment = day(row.get("at") or row.get("date"))
            if moment and moment >= today and row.get("precision") != "unknown":
                rows.append(row)
        return rows

    with_deadline = [c for c in candidates if c.get("deadlines")]
    with_future = [c for c in candidates if future_rows(c)]
    pending = [c for c in candidates if c.get("review_status") == "pending"]
    pending_future = [c for c in pending if future_rows(c)]
    print(f"=== {label}  (今天={today}) ===")
    print(f"候选 {len(candidates)}：{dict(Counter(c.get('review_status') for c in candidates))}")
    print(f"有 deadlines 字段非空: {len(with_deadline)} · 其中有**未来**期限: {len(with_future)}")
    print(f"pending {len(pending)} · pending 且有未来期限: {len(pending_future)}")
    statuses = Counter((event.get("status") or event.get("state")) for event in events)
    print(f"事项 {len(events)}：{dict(statuses)}")
    # `active` 是公开列表里的状态，`under_review` 是还没发布的。
    published = [event for event in events if (event.get("status") or event.get("state")) == "active"]
    print(f"其中公开（active）{len(published)}")
    for candidate in pending_future[:6]:
        rows = future_rows(candidate)
        print(f"   · «{(candidate.get('title') or '')[:44]}» → {[(r['kind'], r['precision'], r.get('date') or r.get('at')) for r in rows][:2]}")

    # t20 的第二个观察：队列里同标题重复出现多少次、是否值得去重。
    titles = Counter((candidate.get("title") or "").strip() for candidate in pending)
    duplicated = {title: count for title, count in titles.items() if count > 1}
    print(
        f"pending 标题：{len(titles)} 个不同标题 · 重复标题 {len(duplicated)} 组 · 涉及 {sum(duplicated.values())} 条"
    )
    snapshots = Counter(len(set(candidate.get("snapshot_ids") or [])) for candidate in pending)
    print(f"pending 的快照数分布: {dict(sorted(snapshots.items()))}")
    for title, count in sorted(duplicated.items(), key=lambda pair: -pair[1])[:8]:
        print(f"   ×{count}  «{title[:56]}»")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))