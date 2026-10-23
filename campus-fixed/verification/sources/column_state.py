"""t21：按栏目列出 backlog 的真实状态（completed / failed / 错误码 / 原生文章数）。

只读打开给定的 sqlite；真实 data-operator 不写。

    python verification/sources/column_state.py <campus.sqlite3> [标签]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

COLUMNS = {
    "c4e00274": "教务处 · 通知公告",
    "fc64db2c": "教务处 · 学业指导",
    "56cd6e50": "教务处 · 学生竞赛",
    "ea4fa3a4": "教务处 · 通知公告总览",
    "3434196e": "教务处 · 竞赛科创通知",
    "5a11fe0a": "新工科教育创新中心 · 竞赛通知",
}


def main(argv: list[str]) -> int:
    db = Path(argv[1]).resolve()
    label = argv[2] if len(argv) > 2 else ""
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='source_ingestion'")
    rows = {key.split(":")[0]: json.loads(data) for key, data in cursor.fetchall()}
    print(f"=== {label}  {db.name} ===")
    for prefix, name in COLUMNS.items():
        source_id = next((key for key in rows if key.startswith(prefix)), None)
        state = rows.get(source_id)
        if state is None:
            print(f"{name:<28} 无记录")
            continue
        items = list(state["items"].values())
        status = Counter(item["status"] for item in items)
        codes = Counter(item.get("error_code") for item in items if item.get("status") == "failed")
        native = [item for item in items if "oaa.tju.edu.cn/info/" in item["url"]]
        native_done = [item for item in native if item["status"] == "completed"]
        print(
            f"{name:<28} 条目 {len(items):>3} | {dict(status)} | 原生 {len(native)}(completed {len(native_done)}) | "
            f"错误码 { {k: v for k, v in codes.items() if k} }"
        )
        for item in native_done[:2]:
            print(f"      ✔ {item['url']}  «{(item.get('title') or '')[:52]}»")
        if prefix == "5a11fe0a":
            for item in [i for i in items if i["status"] == "completed"][:3]:
                print(f"      ✔ {item['url']}  «{(item.get('title') or '')[:52]}»")
    # 库里真正落地的原生文章（documents 里 state != failed 的）
    cursor.execute("select key, data from campus_records where collection='snapshots'")
    snapshots = [(key, json.loads(data)) for key, data in cursor.fetchall()]
    native_snaps = [
        (key, snap)
        for key, snap in snapshots
        if "oaa.tju.edu.cn/info/" in (snap.get("source_url") or "")
    ]
    print(f"库里 oaa 原生文章快照: {len(native_snaps)} 条")
    for key, snap in native_snaps[-3:]:
        print(f"   {snap['source_url'][:88]}  «{(snap.get('title') or '')[:48]}»  state={snap.get('state')}")
    oaa_hosts = Counter(urlsplit(snap["source_url"]).hostname for _, snap in native_snaps)
    print("  域名:", dict(oaa_hosts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))