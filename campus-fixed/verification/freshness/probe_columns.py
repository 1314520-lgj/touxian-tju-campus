"""Probe official columns for publishing frequency — by fetching them, not by guessing.

Pipeline (all read-only, no credentials, default UA):

1. read the school's 学院（部） hub (``https://www.tju.edu.cn/xy_b_.htm``) and follow each
   college's description page to the college's own ``*.tju.edu.cn`` site;
2. on each college site try a small ordered list of common notice-column paths and keep
   the first that answers 200 with dated list items;
3. report reachability, the newest dated item, how many items fall inside the last 30/90
   days, and the first titles — that is the frequency evidence.

Usage::

    python verification/freshness/probe_columns.py [--hub URL] [--limit N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

UA = "Mozilla/5.0 (compatible; tju-campus-source-check)"
# The 学院（部） list lives on the homepage's 院系 section, which links one
# description page per college under /info/1044/; that page carries the college site.
HUB = "https://www.tju.edu.cn/"
PATHS = ("/tzgg.htm", "/xwzx/tzgg.htm", "/index/tzgg.htm", "/tzgg/", "/news/tzgg.htm", "/tzgg/index.htm")
DATE_PATTERNS = (
    re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
)
COLLEGE_PAGE = re.compile(r'href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)
SKIP_HOSTS = {
    "www.tju.edu.cn", "news.tju.edu.cn", "gs.tju.edu.cn", "oaa.tju.edu.cn", "e.tju.edu.cn",
    "mail.tju.edu.cn", "vpn.tju.edu.cn", "faculty.tju.edu.cn", "alumni.tju.edu.cn",
    "xxgkw.tju.edu.cn", "vr.tju.edu.cn", "130.tju.edu.cn", "zhuanti.tju.edu.cn",
}


def fetch(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.url, response.read(500000)


def decode(raw):
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def dates_in(text):
    found = []
    for pattern in DATE_PATTERNS:
        for year, month, day in pattern.findall(text):
            try:
                found.append(date(int(year), int(month), int(day)))
            except ValueError:
                continue
    return found


def titles_in(html):
    """Anchor texts that look like notice titles (bounded, deduplicated)."""
    titles = []
    seen = set()
    for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S | re.I):
        text = " ".join(re.sub(r"<[^>]+>", "", label).split())
        if len(text) < 8 or text in seen:
            continue
        seen.add(text)
        titles.append((text, urljoin("", href)))
    return titles


def college_hosts(hub, limit, out):
    status, final, raw = fetch(hub)
    print(f"院系 hub {hub} -> HTTP {status} bytes={len(raw)}", file=out)
    pages = [
        (urljoin(final, href), label)
        for href, label in COLLEGE_PAGE.findall(decode(raw))
        if "/info/1044/" in urljoin(final, href)
    ]
    # The same college is often linked several times (nav + list); keep one page each.
    unique_pages = []
    for href, label in pages:
        if href not in {existing for existing, _ in unique_pages}:
            unique_pages.append((href, label))
    pages = unique_pages
    print(f"学院描述页 {len(pages)} 个", file=out)
    hosts = {}
    for href, label in pages[:limit]:
        url = urljoin(final, href)
        name = " ".join(re.sub(r"<[^>]+>", "", label).split())
        try:
            _, _, body = fetch(url)
        except Exception as error:  # noqa: BLE001
            print(f"  {name} 描述页 -> {type(error).__name__}", file=out)
            continue
        page = decode(body)
        found_here = []
        for link in re.findall(r'href=["\']([^"\']+)["\']', page, re.I):
            host = urlsplit(urljoin(url, link)).netloc
            if host.endswith(".tju.edu.cn") and host not in SKIP_HOSTS and host not in found_here:
                found_here.append(host)
        for host in found_here:
            hosts.setdefault(host, name)
        time.sleep(0.15)
    return hosts


def probe(host, today, out):
    for path in PATHS:
        url = f"https://{host}{path}"
        try:
            status, final, raw = fetch(url)
        except urllib.error.HTTPError as error:
            continue
        except Exception:  # noqa: BLE001
            continue
        if status != 200 or len(raw) < 800:
            continue
        html = decode(raw)
        days = dates_in(html)
        recent = [day for day in days if (today - day).days <= 90]
        if len(days) < 5:
            continue
        titles = [title for title, _ in titles_in(html)][:4]
        return {
            "host": host,
            "url": final,
            "bytes": len(raw),
            "dated_items": len(days),
            "newest": max(days).isoformat(),
            "last7": sum(1 for day in days if (today - day).days <= 7),
            "last30": sum(1 for day in days if (today - day).days <= 30),
            "last90": len(recent),
            "titles": titles,
        }
    return None


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="official column frequency probe (read-only)")
    parser.add_argument("--hub", default=HUB)
    parser.add_argument("--limit", type=int, default=16, help="最多读多少个学院描述页")
    parser.add_argument("--max-hosts", type=int, default=24, help="最多探测多少个主机")
    parser.add_argument("--host", action="append", default=[], help="直接探测指定主机（可重复）")
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    today = datetime.now().date()

    hosts = {host: "（直接给定）" for host in args.host}
    if not hosts:
        hosts = college_hosts(args.hub, args.limit, out)
    if args.max_hosts:
        hosts = dict(list(hosts.items())[: args.max_hosts])
    print(f"\n待探测主机 {len(hosts)} 个", file=out)
    rows = []
    for host, name in hosts.items():
        result = probe(host, today, out)
        if result is None:
            print(f"   {host:<26} ✗ 没有找到可用的通知列表（探测路径全部失败）", file=out)
            continue
        result["college"] = name
        rows.append(result)
        print(
            f"   {host:<26} ✓ {result['url']}\n"
            f"        条目(有日期) {result['dated_items']} · 最新 {result['newest']} · "
            f"近7天 {result['last7']} · 近30天 {result['last30']} · 近90天 {result['last90']}",
            file=out,
        )
        for title in result["titles"]:
            print(f"        · {title}", file=out)
    rows.sort(key=lambda row: (-row["last30"], -row["last7"], row["host"]))
    print("\n按「近 30 天条目数」排序：", file=out)
    for row in rows:
        print(f"   {row['last30']:>3} 条/30天 · {row['last7']:>2} 条/7天 · {row['host']:<26} {row['college']}", file=out)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"hub": args.hub, "probed": len(hosts), "columns": rows}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())