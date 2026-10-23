"""t19 正对照：同一条重定向复核（_reject_redirected_off_pattern）对**同站真实文章**
不会误报。

做法：从真实快照里取几条 tju.edu.cn 的原站 URL，按采集器相同的方式现场抓一次，
用来源自己的 article_url_pattern 跑一遍复核，并核对页面标题。

    python verification/redirect/control_native.py [条数]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

DB = REPO / "data-operator" / "campus.sqlite3"


def main(argv: list[str]) -> int:
    limit = int(argv[1]) if len(argv) > 1 else 6
    from campus_collect import gateway_origins_from_config
    from server.campus.sources import FetchError, SafeFetcher, _reject_redirected_off_pattern, builtin_sources, decode_body

    origins = gateway_origins_from_config()
    fetcher = SafeFetcher(trusted_origins=origins)
    by_origin = {entry["origin_url"]: entry for entry in builtin_sources()}

    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='sources'")
    configs = {}
    for key, data in cursor.fetchall():
        record = json.loads(data)
        entry = by_origin.get(record.get("origin_url") or "")
        configs[record.get("id") or key] = {
            "name": record.get("name"),
            "origin_url": record.get("origin_url"),
            "adapter": record.get("adapter"),
            "config": (entry or {}).get("config") or {},
        }
    cursor.execute("select key, data from campus_records where collection='snapshots'")
    picked = {}
    for key, data in cursor.fetchall():
        record = json.loads(data)
        url = record.get("source_url") or ""
        source = configs.get(record.get("source_id")) or {}
        if "tju.edu.cn" not in url or not source.get("config", {}).get("article_url_pattern"):
            continue
        host = urlsplit(url).hostname or ""
        if host in picked:
            continue
        picked[host] = {"url": url, "title": record.get("title"), "source": source}
        if len(picked) >= limit:
            break

    report = []
    for host, row in picked.items():
        entry = {"host": host, "url": row["url"], "stored_title": row["title"], "source": row["source"]["name"]}
        try:
            result = fetcher.fetch(row["url"])
            entry["final_url"] = result.url
            entry["same_url"] = result.url == row["url"]
            text = decode_body(result.body, result.content_type)
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(text, "html.parser")
            entry["title"] = (soup.title.get_text(" ", strip=True) if soup.title else None)
            heading = soup.select_one("h1")
            entry["heading"] = heading.get_text(" ", strip=True) if heading else None
            entry["chars"] = len(soup.get_text(" ", strip=True))
            try:
                _reject_redirected_off_pattern(result.url, row["url"], row["source"]["config"])
                entry["guard"] = "passed"
            except FetchError as error:
                entry["guard"] = f"rejected:{error.code}"
        except FetchError as error:
            entry["guard"] = f"fetch_failed:{error.code}"
        report.append(entry)
        print(
            f"{host:<20} guard={entry['guard']:<12} same_url={entry.get('same_url')} "
            f"chars={entry.get('chars')} title={(entry.get('title') or '')[:40]}"
        )
        print(f"     url={row['url']}")
    out = REPO / "verification" / "redirect" / "control-native.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("wrote", out.relative_to(REPO))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))