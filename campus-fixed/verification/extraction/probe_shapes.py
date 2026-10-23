"""t23 取样：真实队列里「正文有日期串、候选却没有未来期限」的案例。

只读打开 data-operator。先看清 candidates / snapshots / parsed 的形状，再逐条对照正文。

    python verification/extraction/probe_shapes.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data-operator" / "campus.sqlite3"


def main() -> int:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select collection, count(*) from campus_records group by collection order by 2 desc")
    print("集合:", cursor.fetchall())

    cursor.execute("select key, data from campus_records where collection='candidates' limit 200")
    rows = [(key, json.loads(data)) for key, data in cursor.fetchall()]
    print(f"\ncandidates: {len(rows)} 条样本")
    if rows:
        key, sample = rows[0]
        print("字段:", sorted(sample.keys()))
        for field in ("candidate_id", "state", "decision", "event_id", "deadlines", "evidence_ids", "title"):
            value = sample.get(field)
            print(f"  {field} = {json.dumps(value, ensure_ascii=False)[:220]}")

    states = Counter(row.get("state") for _, row in rows)
    print("state 分布:", dict(states))
    no_deadline = [row for _, row in rows if not row.get("deadlines")]
    print(f"没有 deadlines 字段/空: {len(no_deadline)}/{len(rows)}")

    decision = Counter(json.dumps(row.get("decision"), ensure_ascii=False)[:40] for _, row in rows)
    print("decision 样本:", decision.most_common(4))

    cursor.execute("select key, data from campus_records where collection='snapshots' limit 3")
    for key, data in cursor.fetchall():
        snap = json.loads(data)
        print("\nsnapshot 字段:", sorted(snap.keys()))
        print("  source_url:", snap.get("source_url"))
        print("  title:", snap.get("title"))
        parsed = snap.get("parsed") or {}
        if isinstance(parsed, dict):
            print("  parsed keys:", sorted(parsed.keys())[:14])
            segments = parsed.get("segments") or []
            print("  segments:", len(segments), json.dumps(segments[:1], ensure_ascii=False)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())