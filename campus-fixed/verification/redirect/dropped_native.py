"""t19：这些教务处来源的 backlog 被「永远抓不到的公众号链接」占满后，
新发现的**同站文章**还能不能进队列？（ingestion.discover 在 known 达到 MAX_KNOWN
且没有 completed 项时会静默丢弃新条目）

做法：对每个来源实时抓一次栏目页，按产品自己的 feed_items 取出文章链接，
再和 backlog 里已知的键比对。

    python verification/redirect/dropped_native.py
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

DB = REPO / "data-operator" / "campus.sqlite3"
SRC = {
    "3434196e": "天津大学教务处 · 竞赛科创通知",
    "56cd6e50": "天津大学教务处 · 学生竞赛",
    "c4e00274": "天津大学教务处 · 通知公告总览",
    "ea4fa3a4": "天津大学教务处 · 通知公告",
    "fc64db2c": "天津大学教务处 · 学业指导",
}


def main() -> int:
    from campus_collect import gateway_origins_from_config
    from server.campus.ingestion import MAX_KNOWN
    from server.campus.sources import SafeFetcher, builtin_sources, feed_items

    origins = gateway_origins_from_config()
    fetcher = SafeFetcher(trusted_origins=origins)
    by_origin = {entry["origin_url"]: entry for entry in builtin_sources()}

    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='source_ingestion'")
    backlogs = {key.split(":")[0]: json.loads(data) for key, data in cursor.fetchall()}

    print(f"MAX_KNOWN per source = {MAX_KNOWN}")
    report = []
    for prefix, label in SRC.items():
        source_id = next((key for key in backlogs if key.startswith(prefix)), None)
        backlog = backlogs.get(source_id) if source_id else None
        if not backlog:
            continue
        known = backlog.get("items") or {}
        completed = sum(1 for item in known.values() if item.get("status") == "completed")
        entry = by_origin.get(backlog.get("origin_url") or "")
        # The store row carries the origin; builtin_sources() carries the config.
        if entry is None:
            cursor.execute("select data from campus_records where collection='sources' and key=?", (source_id,))
            row = cursor.fetchone()
            record = json.loads(row[0]) if row else {}
            entry = by_origin.get(record.get("origin_url") or "")
        if entry is None:
            print(f"{label}: 找不到来源配置，跳过")
            continue
        config = entry.get("config") or {}
        try:
            result = fetcher.fetch(entry["origin_url"])
            discovered = feed_items(result, entry.get("adapter", "website"), config)
        except Exception as error:  # noqa: BLE001
            print(f"{label}: 栏目页抓取失败 {error}")
            continue
        native = [item for item in discovered if "tju.edu.cn" in item["url"]]
        missing_native = [
            item for item in native if hashlib.sha256(item["url"].encode()).hexdigest() not in known
        ]
        cap_full = len(known) >= MAX_KNOWN and completed == 0
        print(
            f"{label}: backlog={len(known)} (completed {completed}) | MAX_KNOWN 满且无 completed={cap_full} | "
            f"栏目页发现 {len(discovered)}（同站 {len(native)}）| 其中不在队列 {len(missing_native)}"
        )
        for item in missing_native[:5]:
            print("    会丢:", item["url"])
        report.append(
            {
                "source": label,
                "backlog": len(known),
                "completed": completed,
                "cap_full": cap_full,
                "discovered": len(discovered),
                "native": len(native),
                "native_missing": len(missing_native),
                "missing_urls": [item["url"] for item in missing_native],
            }
        )
    out = REPO / "verification" / "redirect" / "dropped-native.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("wrote", out.relative_to(REPO))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())