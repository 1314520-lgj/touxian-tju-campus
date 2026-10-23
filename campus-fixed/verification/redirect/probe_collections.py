"""t19：看看各集合里哪一张记录了「逐条 URL + 错误码」。

    python verification/redirect/probe_collections.py
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data-operator" / "campus.sqlite3"

WANT = "article_redirected_off_pattern"


def main() -> int:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select collection, key, data from campus_records")
    samples: dict[str, dict] = {}
    code_hits: Counter[str] = Counter()
    url_hits: Counter[str] = Counter()
    for collection, key, data in cursor.fetchall():
        try:
            record = json.loads(data)
        except Exception:
            continue
        samples.setdefault(collection, record)
        blob = data
        if WANT in blob:
            code_hits[collection] += 1
        if '"url"' in blob or '"article_url"' in blob:
            url_hits[collection] += 1
    for collection in sorted(samples):
        record = samples[collection]
        keys = sorted(record.keys())
        print(f"{collection:<22} n_in_text={code_hits[collection]:<4} url_keys={url_hits[collection]:<4} sample_keys={keys[:14]}")
    print("\n含 url 的集合里，看看 documents / source_ingestion 的一条样例：")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())