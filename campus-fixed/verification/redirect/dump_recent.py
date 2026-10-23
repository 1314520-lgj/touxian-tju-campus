"""t19：把最近一次无人值守运行里带某个错误码的 fetch_run 逐条打印出来。

时间戳是 UTC（本机 07:30 = 前一天 23:30Z），所以过滤用 UTC 前缀。

    python verification/redirect/dump_recent.py <错误码> <UTC 起始前缀>
        python verification/redirect/dump_recent.py article_redirected_off_pattern 2026-10-20T23
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data-operator" / "campus.sqlite3"


def main(argv: list[str]) -> int:
    code = argv[1] if len(argv) > 1 else "article_redirected_off_pattern"
    since = argv[2] if len(argv) > 2 else "2026-10-20T23"
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data, updated_at from campus_records where collection='fetch_runs'")
    rows = []
    for key, data, updated_at in cursor.fetchall():
        if not (updated_at or "").startswith(since):
            continue
        try:
            record = json.loads(data)
        except Exception:
            continue
        codes = list(record.get("error_codes") or [])
        if record.get("error_code") and record["error_code"] not in codes:
            codes.append(record["error_code"])
        if code not in codes:
            continue
        rows.append({"key": key, "updated_at": updated_at, "record": record})
    rows.sort(key=lambda row: row["updated_at"])
    print(f"{code} since {since}: {len(rows)}")
    if rows:
        print("record fields:", sorted(rows[0]["record"].keys()))
    for row in rows:
        record = row["record"]
        print("-" * 4, row["updated_at"])
        print("   url       :", record.get("url") or record.get("request_url"))
        print("   final_url :", record.get("final_url") or record.get("resolved_url") or record.get("location"))
        print("   status    :", record.get("status") or record.get("http_status"))
        print("   error     :", record.get("error_code"), record.get("error_codes"))
        print("   source    :", record.get("source_id"), record.get("source_name"))
        print("   detail    :", json.dumps({k: v for k, v in record.items() if k not in {"url"}}, ensure_ascii=False)[:400])
    out = REPO / "verification" / "redirect" / f"recent-{code}-{since.replace(':', '')}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("wrote", out.relative_to(REPO))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))