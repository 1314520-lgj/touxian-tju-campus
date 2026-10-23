"""t19：从 data-operator（只读）取出被 `article_redirected_off_pattern` 拒绝的文章 URL。

fetch_runs 只记每个来源的错误码计数；逐条的 URL 在 `documents` 集合里。

    python verification/redirect/list_rejected.py [错误码]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data-operator" / "campus.sqlite3"


def main(argv: list[str]) -> int:
    code = argv[1] if len(argv) > 1 else "article_redirected_off_pattern"
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data, updated_at from campus_records where collection='documents'")
    rows = []
    for key, data, updated_at in cursor.fetchall():
        try:
            record = json.loads(data)
        except Exception:
            continue
        if record.get("last_error_code") != code and record.get("error_code") != code:
            continue
        rows.append(
            {
                "key": key,
                "updated_at": updated_at,
                "url": record.get("url") or record.get("article_url") or record.get("request_url"),
                "source_id": record.get("source_id"),
                "state": record.get("fetch_state") or record.get("state"),
                "attempts": record.get("attempts"),
                "last_error_code": record.get("last_error_code") or record.get("error_code"),
                "final_url": record.get("final_url") or record.get("resolved_url"),
                "title": record.get("title"),
                "raw": record if "--raw" in argv else None,
            }
        )
    rows.sort(key=lambda row: (row["source_id"] or "", row["url"] or ""))
    print(f"documents with {code}: {len(rows)}")
    print("按来源:", Counter(row["source_id"] for row in rows).most_common())
    print("按最后更新:", Counter((row["updated_at"] or "")[:16] for row in rows).most_common(6))
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["source_id"]].append(row)
    for source_id, items in grouped.items():
        print(f"\n== {source_id} ({len(items)} 条)")
        for row in items:
            print("  ", row["updated_at"], row["state"], "|", row["url"])
    out = REPO / "verification" / "redirect" / "rejected-urls.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("\nwrote", out.relative_to(REPO))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))