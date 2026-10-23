"""t23：把「重抽」的效果和「重新排队」的效果分开。

重抽会把 315 条候选重新变回 pending（规则版本变了，候选身份按 (输入, 版本) 取键），
所以比较「pending 里有未来期限的条数」会把两件事混在一起。真正要回答的是：

    同样的输入，新规则比旧规则**多认出了**多少条（未来）期限？有没有反而少认的？

做法：用另一份 data-operator 副本（旧规则抽出来的候选）当基线，按快照集合配对，
逐条比较「未来期限」集合。

    python verification/extraction/attribute_rules_change.py <旧副本.sqlite3> <新副本.sqlite3> [今天]
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
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def load(db: Path):
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select data from campus_records where collection='candidates'")
    rows = [json.loads(row[0]) for row in cursor.fetchall()]
    connection.close()
    return rows


def future_set(candidate, today):
    keys = set()
    for row in candidate.get("deadlines") or []:
        if row.get("precision") == "unknown":
            continue
        moment = day(row.get("at") or row.get("date"))
        if moment and moment >= today:
            keys.add((row["kind"], row.get("date") or row.get("at")))
    return keys


def all_set(candidate):
    return {
        (row["kind"], row.get("date") or row.get("at"))
        for row in candidate.get("deadlines") or []
        if row.get("precision") != "unknown"
    }


def key_of(candidate):
    snapshots = tuple(sorted(candidate.get("snapshot_ids") or []))
    return snapshots or ("title:" + (candidate.get("title") or ""),)


def main(argv: list[str]) -> int:
    before_db, after_db = Path(argv[1]).resolve(), Path(argv[2]).resolve()
    today = date.fromisoformat(argv[3]) if len(argv) > 3 else date(2026, 10, 21)
    before = {key_of(c): c for c in load(before_db)}
    after = {key_of(c): c for c in load(after_db)}
    print(f"旧候选 {len(before)} · 新候选 {len(after)} · 可按快照配对 {len(set(before) & set(after))}")

    counts = Counter()
    gained, lost = [], []
    for key in set(before) & set(after):
        old_future = future_set(before[key], today)
        new_future = future_set(after[key], today)
        if new_future > old_future:
            counts["新认出未来期限"] += 1
            gained.append((after[key].get("title"), sorted(new_future - old_future), sorted(old_future)))
        elif new_future < old_future:
            counts["少认了未来期限"] += 1
            lost.append((after[key].get("title"), sorted(old_future - new_future)))
        elif new_future:
            counts["本来就有（不变）"] += 1
        else:
            counts["两边都没有"] += 1
        old_all, new_all = all_set(before[key]), all_set(after[key])
        if new_all > old_all:
            counts["新认出期限（含过去）"] += 1
        if new_all < old_all:
            counts["少认了期限（含过去）"] += 1
    print("逐条对照:", dict(counts))
    print(f"\n新认出未来期限的 {len(gained)} 条：")
    for title, added, before_keys in gained[:12]:
        print(f"   · «{(title or '')[:46]}»")
        print(f"       新增 {added}    （原有 {before_keys}）")
    if lost:
        print(f"\n!! 少认了未来期限的 {len(lost)} 条：")
        for title, removed in lost[:12]:
            print(f"   · «{(title or '')[:46]}» 丢掉 {removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))