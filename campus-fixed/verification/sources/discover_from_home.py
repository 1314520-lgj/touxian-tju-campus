"""从主机首页找「通知/公告」入口，再逐条实测列表页（补 rescan_columns.py 的盲区）。

很多学院的栏目路径不符合常见命名（arch/law/math/mse… 全 404），必须读首页导航。
只读探测，输出 JSON 供人工决定是否注册。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

NOTICE_WORDS = re.compile(r"通知|公告|公示|新闻|动态")
DATE = re.compile(r"(20\d\d)[-./年](\d{1,2})[-./月](\d{1,2})")


def main(argv):
    import argparse

    from bs4 import BeautifulSoup

    from campus_collect import gateway_origins_from_config
    from server.campus.sources import FetchError, SafeFetcher, decode_body

    parser = argparse.ArgumentParser(prog="discover_from_home.py")
    parser.add_argument("--hosts", required=True)
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv[1:])
    today = date.fromisoformat(args.today)

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    origins = gateway_origins_from_config() + ["https://" + h for h in hosts]
    fetcher = SafeFetcher(trusted_origins=tuple(sorted(set(origins))))

    rows = []
    for host in hosts:
        home = "https://" + host + "/"
        try:
            result = fetcher.fetch(home)
        except FetchError as error:
            rows.append({"host": host, "stage": "home", "error": error.code})
            continue
        soup = BeautifulSoup(decode_body(result.body, result.content_type), "html.parser")
        seen = set()
        for anchor in soup.select("a[href]"):
            text = anchor.get_text(" ", strip=True)
            href = anchor.get("href") or ""
            if not text or not NOTICE_WORDS.search(text) or len(text) > 24:
                continue
            url = urljoin(home, href)
            if urlsplit(url).hostname != host or url in seen:
                continue
            seen.add(url)
            entry = {"host": host, "url": url, "anchor": text}
            try:
                listing = fetcher.fetch(url)
            except FetchError as error:
                entry["error"] = error.code
                rows.append(entry)
                continue
            page = BeautifulSoup(decode_body(listing.body, listing.content_type), "html.parser")
            dates = []
            for a in page.select("a[href]"):
                nearby = a.parent.get_text(" ", strip=True) if a.parent else ""
                for match in DATE.finditer(nearby):
                    try:
                        dates.append(date(int(match[1]), int(match[2]), int(match[3])))
                    except ValueError:
                        pass
            dates = sorted(set(dates), reverse=True)[:3]
            age = (today - dates[0]).days if dates else None
            entry.update(
                page_title=(page.title.get_text(" ", strip=True) if page.title else "")[:80],
                list_items=len(page.select("a[href]")),
                newest=dates[0].isoformat() if dates else None,
                age_days=age,
                tier=("active" if age is not None and age <= 30 else "low" if age is not None and age <= 90 else "dead"),
            )
            rows.append(entry)
        # 每台主机最多取 4 个候选，避免打爆
        rows = rows[: len(rows)]
    active = [r for r in rows if r.get("tier") == "active"]
    payload = {"today": args.today, "hosts": hosts, "rows": len(rows), "active": len(active), "results": rows}
    out = Path(args.out) if args.out else REPO / "verification" / "sources" / f"home-discover-{args.today}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"rows={len(rows)} active={len(active)} -> {out}")
    for row in active:
        print(f"  ACTIVE {row['url']}  anchor={row.get('anchor')} items={row.get('list_items')} newest={row.get('newest')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
