"""t19：从 source_ingestion（backlog）里取出被某个错误码拒绝的逐条 URL。

backlog 的形状：{items: {<hash>: {url, title, published_at, sequence, status, attempts,
last_attempt_at, next_retry_at, error_code}}, ...}

    python verification/redirect/list_backlog.py [错误码]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data-operator" / "campus.sqlite3"


def main(argv: list[str]) -> int:
    code = argv[1] if len(argv) > 1 else "article_redirected_off_pattern"
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='source_ingestion'")
    rows = []
    for key, data in cursor.fetchall():
        record = json.loads(data)
        items = record.get("items") or {}
        if not isinstance(items, dict):
            continue
        for digest, item in items.items():
            if item.get("error_code") != code:
                continue
            rows.append(
                {
                    "source_id": key.split(":")[0],
                    "digest": digest,
                    "url": item.get("url"),
                    "title": item.get("title"),
                    "published_at": item.get("published_at"),
                    "status": item.get("status"),
                    "attempts": item.get("attempts"),
                    "last_attempt_at": item.get("last_attempt_at"),
                }
            )
    rows.sort(key=lambda row: (row["source_id"], row["url"] or ""))
    print(f"backlog items with {code}: {len(rows)}")
    print("按来源:", Counter(row["source_id"][:8] for row in rows).most_common())
    print("按域名:", Counter((row["url"] or "").split("/")[2] if "//" in (row["url"] or "") else "?" for row in rows).most_common())
    for row in rows:
        print(f"  {row['source_id'][:8]}  attempts={row['attempts']}  {row['url']}")
        if row["title"]:
            print(f"           title={row['title'][:60]}")
    out = REPO / "verification" / "redirect" / "rejected-urls.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("wrote", out.relative_to(REPO))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))