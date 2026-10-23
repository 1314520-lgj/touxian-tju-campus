"""t19：75 条被拒的公众号链接里，有多少其实是**同一篇文章**（只是查询串在轮换）？

关键：backlog 的键是 sha256(原始 URL)，而 wx 链接常带 sessionid/clicktime/chksm 等
每次访问都变的参数 → 同一篇文章会占多个队列条目、每轮重复消耗抓取额度。

    python verification/redirect/duplicate_wechat.py
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data-operator" / "campus.sqlite3"

# 只保留能标识文章的部分：/s/<token> 或 ?__biz=…&mid=…&idx=…&sn=…
KEEP = {"__biz", "mid", "idx", "sn"}


def article_key(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.path.rstrip("/") not in ("", "/s"):
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    from urllib.parse import parse_qsl

    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k in KEEP]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) + "?" + "&".join(f"{k}={v}" for k, v in sorted(params))


def main() -> int:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='source_ingestion'")
    rows = []
    for key, data in cursor.fetchall():
        record = json.loads(data)
        for digest, item in (record.get("items") or {}).items():
            if item.get("error_code") != "article_redirected_off_pattern":
                continue
            rows.append({"source_id": key.split(":")[0], "digest": digest, "url": item.get("url"), "attempts": item.get("attempts")})
    print("被拒条目:", len(rows))
    within = defaultdict(list)
    for row in rows:
        within[(row["source_id"], article_key(row["url"]))].append(row)
    within_dups = {k: v for k, v in within.items() if len(v) > 1}
    print("同一来源内的重复条目:", sum(len(v) for v in within_dups.values()), f"（涉及 {len(within_dups)} 篇文章）")
    for (source_id, key), items in list(within_dups.items())[:5]:
        print(f"   来源 {source_id[:8]} ×{len(items)}: {key[:90]}")
    print("跨来源重复（同一篇文章被多个栏目同时列出，属正常）:", len(rows) - sum(len(v) for v in within.values()))
    by_key = defaultdict(list)
    for row in rows:
        by_key[article_key(row["url"])].append(row)
    print("去重后（按文章标识）:", len(by_key))
    duplicated = {k: v for k, v in by_key.items() if len(v) > 1}
    print("被同一篇文章占用的条目数:", sum(len(v) for v in duplicated.values()), f"（涉及 {len(duplicated)} 篇文章）")
    waste = sum(len(v) - 1 for v in duplicated.values())
    print("因重复而多占的队列条目:", waste)
    print("重复最多的 5 篇:")
    for key, items in sorted(duplicated.items(), key=lambda pair: -len(pair[1]))[:5]:
        print(f"   ×{len(items)}  {key[:96]}")
        for item in items[:3]:
            print("        ", item["url"][:110])
    print("按来源重复:", Counter(row["source_id"][:8] for rows_ in duplicated.values() for row in rows_).most_common())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())