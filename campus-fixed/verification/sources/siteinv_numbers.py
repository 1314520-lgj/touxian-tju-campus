"""t28：注册新来源前后的对照（只读给定数据目录）。

区分两个数字（第 24 轮踩过的坑）：
  · **新增可见** = 公开（active）事项里新出现的那些（学生真的多看到几条）
  · 事件版本上升 / 重新发布 **不算**新增可见

    python verification/sources/siteinv_numbers.py <campus.sqlite3> [标签]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

NEW_HOSTS = ("marxism.", "mstu.", "spst.", "tjusfl.", "sdem.", "synbm.")


def rows(connection, collection):
    cursor = connection.execute("select data from campus_records where collection=?", (collection,))
    return [json.loads(row[0]) for row in cursor.fetchall()]


def main(argv: list[str]) -> int:
    db = Path(argv[1]).resolve()
    label = argv[2] if len(argv) > 2 else ""
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    sources = rows(connection, "sources")
    events = rows(connection, "events")
    snapshots = rows(connection, "snapshots")
    active = [event for event in events if event.get("status") == "active"]
    print(f"=== {label} ({db.parent.name}) ===")
    print(f"注册来源 {len(sources)} 个（启用 {sum(1 for s in sources if s.get('enabled'))}）")
    print(f"事项 {len(events)} · 公开（active）{len(active)} · 快照 {len(snapshots)}")
    new_source_ids = {
        source["id"] for source in sources if any(host in (source.get("origin_url") or "") for host in NEW_HOSTS)
    }
    if new_source_ids:
        fresh = [snap for snap in snapshots if snap.get("source_id") in new_source_ids]
        print(f"本轮 6 个新来源的快照 {len(fresh)} 份（去重后主机 {len({snap['source_url'].split('/')[2] for snap in fresh if snap.get('source_url')})} 个）")
        for snap in fresh[:6]:
            print(f"   · {snap.get('source_url')}  «{(snap.get('title') or '')[:46]}»")
    published = [
        event
        for event in active
        if any(host in json.dumps(event, ensure_ascii=False) for host in NEW_HOSTS)
    ]
    if published:
        print(f"公开事项里来自新来源的 {len(published)} 条：")
        for event in published[:8]:
            print(f"   · «{(event.get('title') or '')[:52]}»")
    print("公开事项标题前 5：")
    for event in active[:5]:
        print(f"   · «{(event.get('title') or '')[:52]}»  {event.get('id')}")
    status = Counter(event.get("status") for event in events)
    print("事项状态分布:", dict(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))