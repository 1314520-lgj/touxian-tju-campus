"""官网栏目补扫：对 t28 主机池里「没被清点过栏目」的主机再找一遍通知栏目。

只读探测：用产品自己的 SafeFetcher，逐个主机抓首页 + 常见栏目路径，
记录可达性、列表条数、最近 3 条日期、栏目标题，判断是否有活跃通知栏目。
输出 JSON 到 verification/sources/rescan-<date>.json，供人工决定是否注册。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

NOTICE_WORDS = re.compile(r"通知|公告|公示")
DATE = re.compile(r"(20\d\d)[-./年](\d{1,2})[-./月](\d{1,2})")
PATTERNS = [
    "/index/tzgg.htm",
    "/xwzx/tzgg.htm",
    "/tzgg.htm",
    "/tzgg/tzgg.htm",
    "/xwgg/tzgg.htm",
    "/xwgg/notices.htm",
    "/index/tztg.htm",
    "/xwzx/tztg.htm",
    "/tzgg/index.htm",
    "/news/tzgg.htm",
    "/xxgg.htm",
    "/gggs.htm",
]


def load_origins():
    config = json.loads((REPO / "config" / "campus-sources.json").read_text(encoding="utf-8"))
    return config["sources"]


def main(argv):
    import argparse

    from campus_collect import gateway_origins_from_config
    from server.campus.sources import FetchError, SafeFetcher, decode_body
    from bs4 import BeautifulSoup

    parser = argparse.ArgumentParser(prog="rescan_columns.py")
    parser.add_argument("--hosts", required=True, help="逗号分隔的主机名")
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv[1:])
    today = date.fromisoformat(args.today)

    origins = gateway_origins_from_config()
    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    origins += ["https://" + h for h in hosts]
    fetcher = SafeFetcher(trusted_origins=tuple(sorted(set(origins))))

    registered = {entry["origin_url"] for entry in load_origins()}
    results = []
    for host in hosts:
        for path in PATTERNS:
            url = "https://" + host + path
            if url in registered:
                continue
            row = {"host": host, "url": url}
            try:
                result = fetcher.fetch(url)
            except FetchError as error:
                row["error"] = error.code
                results.append(row)
                continue
            text = decode_body(result.body, result.content_type)
            soup = BeautifulSoup(text, "html.parser")
            title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:80]
            links = [
                a
                for a in soup.select("a[href]")
                if a.get_text(" ", strip=True) and NOTICE_WORDS.search(a.get_text(" ", strip=True) + title)
            ]
            dates = []
            for a in soup.select("a[href]"):
                nearby = a.parent.get_text(" ", strip=True) if a.parent else ""
                for match in DATE.finditer(nearby):
                    try:
                        dates.append(date(int(match[1]), int(match[2]), int(match[3])))
                    except ValueError:
                        pass
            dates = sorted(set(dates), reverse=True)[:3]
            newest = dates[0].isoformat() if dates else None
            age = (today - dates[0]).days if dates else None
            row.update(
                reachable=True,
                page_title=title,
                notice_links=len(links),
                list_items=len(soup.select("a[href]")),
                newest=newest,
                age_days=age,
                tier=("active" if age is not None and age <= 30 else "low" if age is not None and age <= 90 else "dead"),
            )
            results.append(row)
    active = [r for r in results if r.get("tier") == "active"]
    payload = {"today": args.today, "hosts": hosts, "scanned": len(results), "active": len(active), "results": results}
    out = Path(args.out) if args.out else REPO / "verification" / "sources" / f"rescan-{args.today}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"scanned={len(results)} active={len(active)} -> {out}")
    for row in active:
        print(f"  ACTIVE {row['url']}  items={row.get('list_items')} newest={row.get('newest')} title={row.get('page_title')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
