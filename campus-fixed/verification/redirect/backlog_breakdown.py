"""t19：看这 5 个被 article_redirected_off_pattern 刷屏的来源，backlog 到底被什么占着。

只读。按状态 / 域名 / 尝试次数分组，指出「永远抓不到的项」是否挤占了每轮抓取额度。

    python verification/redirect/backlog_breakdown.py
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data-operator" / "campus.sqlite3"

SOURCES = {
    "3434196e-1203-5543-87bb-d71e1c1fc1b2": "竞赛科创通知",
    "56cd6e50-1f74-5e7f-8a41-1e9b3f3a2a1a": "学生竞赛",
    "c4e00274-5b08-5d26-9a6d-19aed0d71571": "通知公告总览",
    "ea4fa3a4-90fc-55c4-a5aa-cc6a389cb13d": "通知公告",
    "fc64db2c-93fb-5ff8-ace0-aa0b0bcbe15c": "学业指导",
}


def host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "?"
    except Exception:
        return "?"


def main() -> int:
    import sys

    wanted = sys.argv[1] if len(sys.argv) > 1 else ""
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='source_ingestion'")
    rows = {key.split(":")[0]: json.loads(data) for key, data in cursor.fetchall()}
    cursor.execute("select key, data from campus_records where collection='sources'")
    names = {}
    for key, data in cursor.fetchall():
        record = json.loads(data)
        names[record.get("id") or key] = record.get("name")

    total_by_source = {}
    for source_id, record in sorted(rows.items()):
        items = record.get("items") or {}
        if not isinstance(items, dict) or not items:
            continue
        status = Counter(item.get("status") for item in items.values())
        hosts = Counter(host(item.get("url") or "") for item in items.values())
        codes = Counter(item.get("error_code") for item in items.values())
        attempts = Counter(item.get("attempts") for item in items.values())
        pending_native = [
            item["url"]
            for item in items.values()
            if item.get("status") == "pending" and host(item.get("url") or "").endswith("tju.edu.cn")
        ]
        total_by_source[source_id] = len(items)
        label = names.get(source_id) or SOURCES.get(source_id, source_id)
        if wanted and wanted not in label:
            continue
        print(f"\n== {label}  ({len(items)} 项)")
        print("   状态:", dict(status))
        print("   域名:", dict(hosts))
        print("   错误码:", {k: v for k, v in codes.items() if k})
        print("   尝试次数:", dict(attempts))
        print("   待抓的同站（tju.edu.cn）文章:", len(pending_native))
        for url in pending_native[:5]:
            print("      pending:", url)
    print("\n每个来源的 backlog 总数:", {names.get(k) or k[:8]: v for k, v in total_by_source.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())