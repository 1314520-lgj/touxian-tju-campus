"""t23：看清 parsed / evidence 的形状，确定「候选正文」从哪里取。

    python verification/extraction/probe_text.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data-operator" / "campus.sqlite3"


def main() -> int:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='parsed' limit 2")
    for key, data in cursor.fetchall():
        record = json.loads(data)
        print("parsed 字段:", sorted(record.keys()))
        print("  snapshot_id:", record.get("snapshot_id"), "state:", record.get("state"))
        segments = record.get("segments") or []
        print("  segments:", len(segments))
        for segment in segments[:2]:
            print("   ", json.dumps(segment, ensure_ascii=False)[:240])

    cursor.execute("select key, data from campus_records where collection='evidence' limit 2")
    for key, data in cursor.fetchall():
        record = json.loads(data)
        print("\nevidence 字段:", sorted(record.keys()))
        print("  ", json.dumps({k: v for k, v in record.items() if k != "text"}, ensure_ascii=False)[:200])
        print("  text:", (record.get("text") or "")[:160])

    cursor.execute("select key, data from campus_records where collection='candidates'")
    candidates = [(key, json.loads(data)) for key, data in cursor.fetchall()]
    print("\n候选总数:", len(candidates))
    empty = [c for _, c in candidates if not c.get("deadlines")]
    print("deadlines 空:", len(empty))
    print("review_status 分布:", dict(__import__("collections").Counter(c.get("review_status") for _, c in candidates)))
    print("warnings 分布:", dict(__import__("collections").Counter(w for _, c in candidates for w in (c.get("warnings") or []))))
    sample = empty[0]
    print("\n样例候选:", json.dumps({k: v for k, v in sample.items() if k in ("title", "snapshot_ids", "warnings", "review_status")}, ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())