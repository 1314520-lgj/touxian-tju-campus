"""候选栏目标题体检：用产品自己的 feed_items + choose_card_title 判断能否出可辨识卡片。

对每个候选栏目：抓列表页 → 取前 3 条 → 逐条抓正文 → 跑 choose_card_title，
输出每条最终卡片标题与告警。标题不可辨识（栏目名/站点名/空）就不建议注册。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


def main(argv):
    import argparse

    from bs4 import BeautifulSoup

    from campus_collect import gateway_origins_from_config
    from server.campus.content import choose_card_title, html_body_segments
    from server.campus.sources import SafeFetcher, decode_body, feed_items

    parser = argparse.ArgumentParser(prog="title_check_columns.py")
    parser.add_argument("--columns", required=True, help="逗号分隔的列表页 URL")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv[1:])

    columns = [c.strip() for c in args.columns.split(",") if c.strip()]
    origins = gateway_origins_from_config()
    for column in columns:
        parts = column.split("/")
        origins.append(parts[0] + "//" + parts[2])
    fetcher = SafeFetcher(trusted_origins=tuple(sorted(set(origins))))

    report = []
    for column in columns:
        entry = {"column": column, "items": []}
        try:
            listing = fetcher.fetch(column)
        except Exception as error:  # noqa: BLE001 - diagnostic script
            entry["error"] = str(error)
            report.append(entry)
            continue
        config = {"list_selector": "a[href]", "article_url_pattern": r"https?://[^ ]+/info/\d+/\d+\.htm"}
        try:
            items = feed_items(listing, "website", config, 3)
        except Exception as error:  # noqa: BLE001
            entry["error"] = "feed_items:" + str(error)
            report.append(entry)
            continue
        entry["listing_title"] = items[0]["title"] if items else None
        for item in items[:3]:
            row = {"listing_title": item["title"], "url": item["url"]}
            try:
                page = fetcher.fetch(item["url"])
                soup = BeautifulSoup(decode_body(page.body, page.content_type), "html.parser")
                page_title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:120]
                segments, _state, _warnings = html_body_segments(page.body, item["url"])
                heading = next((s["text"] for s in segments if s["id"] == "title"), "")
                # 生产路径：候选标题直接取解析出的 "title" 段（content.py 第 1682 行），
                # 所以这里报告的就是卡片上会显示的文字。
                final, warning = heading, None
                row.update(page_title=page_title[:80], heading=heading[:80], card_title=final, warning=warning)
            except Exception as error:  # noqa: BLE001
                row["error"] = str(error)
            entry["items"].append(row)
        report.append(entry)

    payload = {"columns": len(columns), "report": report}
    out = Path(args.out) if args.out else REPO / "verification" / "sources" / "title-check-2026-10-23.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for entry in report:
        print("==", entry["column"], entry.get("error", ""))
        for row in entry.get("items", []):
            print("   card:", (row.get("card_title") or "")[:60], "| warn:", row.get("warning"), "| err:", row.get("error", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
