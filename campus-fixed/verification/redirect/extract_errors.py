"""t19：从 data-operator 只读导出 2026-10-21 那次无人值守运行里
`article_redirected_off_pattern` 的每一条记录（fetch_runs），供逐条复判。

只读打开（file: URI + mode=ro），绝不写真实数据目录。

    python verification/redirect/extract_errors.py [数据目录] [错误码] [日期前缀]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def main(argv: list[str]) -> int:
    data_dir = Path(argv[1]).resolve() if len(argv) > 1 else REPO / "data-operator"
    code = argv[2] if len(argv) > 2 else "article_redirected_off_pattern"
    day = argv[3] if len(argv) > 3 else "2026-10-21"
    db = data_dir / "campus.sqlite3"
    if not db.is_file():
        print(f"no database at {db}")
        return 2
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data, updated_at from campus_records where collection='fetch_runs'")
    rows = []
    for key, data, updated_at in cursor.fetchall():
        try:
            record = json.loads(data)
        except Exception:
            continue
        codes = record.get("error_codes") or []
        single = record.get("error_code")
        if single and single not in codes:
            codes = [*codes, single]
        if code not in codes:
            continue
        rows.append(
            {
                "key": key,
                "updated_at": updated_at,
                "url": record.get("url") or record.get("request_url") or record.get("article_url"),
                "final_url": record.get("final_url") or record.get("resolved_url"),
                "status": record.get("status") or record.get("http_status"),
                "error_codes": codes,
                "source_id": record.get("source_id"),
                "title": record.get("title"),
                "keys": sorted(record.keys()),
            }
        )
    print(f"fetch_runs with {code}: {len(rows)}")
    print("日期分布:", Counter((row["updated_at"] or "")[:10] for row in rows).most_common())
    today = [row for row in rows if (row["updated_at"] or "").startswith(day)]
    print(f"{day} 当天: {len(today)}")
    print("字段样例:", today[0]["keys"] if today else None)
    for row in today:
        print(json.dumps({k: v for k, v in row.items() if k != "keys"}, ensure_ascii=False))
    out = REPO / "verification" / "redirect" / f"errors-{day}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(today, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))