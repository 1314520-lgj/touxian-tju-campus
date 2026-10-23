"""t19：把某个来源的 source_ingestion（backlog）记录原样打印出来，看清结构。

    python verification/redirect/show_backlog.py [source_id 前缀]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data-operator" / "campus.sqlite3"


def main(argv: list[str]) -> int:
    prefix = argv[1] if len(argv) > 1 else ""
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='source_ingestion'")
    for key, data in cursor.fetchall():
        if prefix and not key.startswith(prefix):
            continue
        record = json.loads(data)
        summary = {k: (f"<list of {len(v)}>" if isinstance(v, list) else v) for k, v in record.items()}
        print("key:", key)
        print("summary:", json.dumps(summary, ensure_ascii=False)[:600])
        items = record.get("items")
        if isinstance(items, list) and items:
            print("first item:", json.dumps(items[0], ensure_ascii=False)[:400])
        print("other keys:", [k for k in record if k != "items"])
        for k in record:
            if k != "items":
                print(f"   {k}:", json.dumps(record[k], ensure_ascii=False)[:300])
        print("-" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))