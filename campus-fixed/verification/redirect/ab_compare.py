"""t19：修复前后「被拒项是否会在下一次运行里被重复请求」的对照。

做法：跑两次日常更新（A、B），然后只看数据目录里 **每一条 URL 的最后尝试时间**：
- 在 A 窗口尝试过、又在 B 窗口被尝试的条目 = 被重复请求（修复前的行为）
- 只在 B 窗口出现的条目 = 本轮新发现的链接（不属于重复）

    python verification/redirect/ab_compare.py <数据目录> <run-a.json> <run-b.json>
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BLOCKED = "article_redirected_off_pattern"


def window(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("started_at"), payload.get("finished_at")


def main(argv: list[str]) -> int:
    data_dir = Path(argv[1]).resolve()
    run_a, run_b = window(Path(argv[2])), window(Path(argv[3]))
    print("run A:", run_a)
    print("run B:", run_b)

    connection = sqlite3.connect(f"file:{data_dir / 'campus.sqlite3'}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='source_ingestion'")
    items = []
    for key, data in cursor.fetchall():
        record = json.loads(data)
        for digest, item in (record.get("items") or {}).items():
            if item.get("error_code") != BLOCKED:
                continue
            items.append({"source_id": key.split(":")[0], "url": item["url"], "attempts": item.get("attempts"),
                          "last_attempt_at": item.get("last_attempt_at"), "next_retry_at": item.get("next_retry_at")})

    def in_window(value, bounds):
        start, finish = bounds
        if not value or not start:
            return False
        stamp = value.replace("+00:00", "")
        return start[:19] <= stamp[:19] <= (finish or "9")[:19]

    attempted_a = [item for item in items if in_window(item["last_attempt_at"], run_a)]
    attempted_b = [item for item in items if in_window(item["last_attempt_at"], run_b)]
    repeated = [item for item in attempted_b if item["url"] in {row["url"] for row in attempted_a}]
    fresh = [item for item in attempted_b if item["url"] not in {row["url"] for row in attempted_a}]
    print(f"\n被拒条目总数: {len(items)}")
    print(f"run A 里尝试过的被拒条目: {len(attempted_a)}")
    print(f"run B 里尝试过的被拒条目: {len(attempted_b)}（其中 A 已尝试过的重复请求 {len(repeated)}，本轮新发现 {len(fresh)}）")
    deferred = [item for item in items if item["next_retry_at"] and item["next_retry_at"] > (run_b[1] or "")]
    print(f"下次重试时间在 run B 之后（即被推迟）的条目: {len(deferred)}")
    # Where did the repeated requests end up? If they are now deferred, the repeat came
    # from a transient failure in run A (short backoff is correct), not from a missed deferral.
    for item in repeated[:8]:
        print(
            "   重复请求:",
            item["url"][:80],
            "attempts=", item["attempts"],
            "next_retry_at=", item["next_retry_at"],
            "已推迟=", bool(item["next_retry_at"] and item["next_retry_at"] > (run_b[1] or "")),
        )
    not_deferred = [item for item in items if item not in deferred]
    print(f"未被推迟的条目: {len(not_deferred)}")
    for item in not_deferred[:8]:
        print("   ", item["url"][:78], "attempts=", item["attempts"], "next=", item["next_retry_at"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))