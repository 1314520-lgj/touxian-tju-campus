"""t28：天津大学官网通知栏目清点（枚举 → 逐个实测 → 分档）。

方法（可照着重跑）：
  1) 宿主发现：抓 www.tju.edu.cn 的首页与「院系设置/机构设置」导航，收集其中所有
     *.tju.edu.cn 主机名；再把已注册的 24 个源的主机名并进来（反查命名规律）。
  2) 栏目候选：每个主机上（a）已知规律路径（/index/tzgg.htm、/xwzx/tzgg.htm、/tzgg.htm…），
     （b）首页里文字含「通知/公告/公示」的链接。
  3) 逐个实测：用产品自己的 SafeFetcher 抓一次列表页（每个候选的 origin 通过
     trusted_origins 显式声明 —— 与 config/ 声明官方 origin 同一机制，不改地址安全判定），
     记录可达性、列表条数、最近 5 条日期、栏目名。
  4) 分档：活跃（最新条目 ≤30 天）/ 低频（≤90 天）/ 已废弃（>90 天或没有列表结构）。
  5) 对活跃候选再抓一篇正文，检查是否有独立标题（<h1> 或 h1 等价物）——没有独立标题的
     栏目不准注册（卡片会显示站点名）。

    python verification/sources/site_inventory.py [--max-hosts N] [--today YYYY-MM-DD]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

OUT = REPO / "verification" / "sources"
SEEDS = ["https://www.tju.edu.cn/", "https://www.tju.edu.cn/xxgk/yxsz.htm", "https://www.tju.edu.cn/xxgk/jgsz.htm"]
# 已知规律（第 14–26 轮注册的源反查出来的路径形状）
PATTERNS = ["/index/tzgg.htm", "/xwzx/tzgg.htm", "/tzgg.htm", "/tzgg/", "/xxgg.htm", "/index/tzgg/"]
NOTICE_WORDS = re.compile(r"通知|公告|公示")
ARTICLE_HINT = re.compile(r"/(?:info|content|tzgg|xxgg|xwzx|news|article)/", re.I)
DATE = re.compile(r"(20\d\d)[-./年](\d{1,2})[-./月](\d{1,2})")


def load_registered():
    config = json.loads((REPO / "config" / "campus-sources.json").read_text(encoding="utf-8"))
    return config["sources"]


def main(argv: list[str]) -> int:
    from campus_collect import gateway_origins_from_config
    from server.campus.sources import SafeFetcher, decode_body

    import argparse

    parser = argparse.ArgumentParser(prog="site_inventory.py")
    parser.add_argument("--max-hosts", type=int, default=45)
    parser.add_argument("--today", default="2026-10-21")
    parser.add_argument("--phases", default="discover,probe")
    parser.add_argument("--hosts-file", help="额外主机清单（unit-hosts.json 形式的 {units:[{host}]}）")
    parser.add_argument("--titles", action="store_true", help="对活跃档再抓一篇正文，检查有没有独立标题")
    args = parser.parse_args(argv[1:])
    today = date.fromisoformat(args.today)
    phases = set(args.phases.split(","))

    registered = load_registered()
    known_hosts = {urlsplit(entry["origin_url"]).hostname for entry in registered}
    base_trusted = list(gateway_origins_from_config() or ())
    fetcher = SafeFetcher(trusted_origins=base_trusted)
    requests = {"count": 0}

    def get(url, extra_trusted=()):
        fetcher.trusted_origins = tuple(base_trusted) + tuple(extra_trusted)
        requests["count"] += 1
        try:
            result = fetcher.fetch(url)
        except Exception as error:  # noqa: BLE001
            return None, getattr(error, "code", type(error).__name__)
        return decode_body(result.body, result.content_type), None

    # --- 1) 宿主发现 -------------------------------------------------------
    hosts = set(known_hosts)
    unit_names = {}
    if args.hosts_file:
        payload = json.loads(Path(args.hosts_file).read_text(encoding="utf-8"))
        for unit in payload.get("units") or []:
            if unit.get("host"):
                hosts.add(unit["host"])
                unit_names[unit["host"]] = unit["name"]
        print(f"从 {Path(args.hosts_file).name} 并入单位主机 {len(unit_names)} 个")
    discover_pages = []
    if "discover" in phases:
        from bs4 import BeautifulSoup

        for seed in SEEDS:
            html, error = get(seed, extra_trusted=("https://" + (urlsplit(seed).hostname or ""),))
            if error:
                print(f"种子页不可达 {seed}：{error}")
                continue
            soup = BeautifulSoup(html, "html.parser")
            found = 0
            for link in soup.select("a[href]"):
                target = urljoin(seed, link.get("href") or "")
                host = urlsplit(target).hostname or ""
                if host.endswith("tju.edu.cn") and host not in hosts:
                    hosts.add(host)
                    found += 1
            titles = [link.get_text(" ", strip=True)[:16] for link in soup.select("a[href]")]
            interesting = [text for text in titles if NOTICE_WORDS.search(text)]
            print(f"{seed} → 新增主机 {found} · 含通知/公告字样的链接 {len(interesting)} 条")
            discover_pages.append({"seed": seed, "new_hosts": found, "notice_links": interesting[:12]})

    ordered = sorted(hosts)[: args.max_hosts]
    print(f"\n候选主机 {len(hosts)} 个（本轮实测前 {len(ordered)} 个）· 其中已注册 {len(known_hosts & hosts)} 个")

    # A column, not an article: the path names a listing (tzgg/xxgg/gsgg/…) or the
    # policy passes a column-shaped file. Article pages (/info/1031/2233.htm,
    # /content/id/2616.html) are what the column *links to*, never the column.
    ARTICLE_URL = re.compile(r"/(?:info|content)/|/\d{3,}\.html?$|/id/\d+", re.I)
    COLUMN_HINT = re.compile(r"tzgg|xxgg|gsgg|tzge|jgdt|notice|/tz/", re.I)

    # --- 2) 栏目候选 -------------------------------------------------------
    candidates = []
    host_table = {}
    for host in ordered:
        origin = "https://" + host
        html, error = get(origin + "/", extra_trusted=(origin,))
        host_row = {"host": host, "homepage": origin + "/", "reachable": error is None, "error": error,
                    "unit": unit_names.get(host)}
        host_table[host] = host_row
        from_home = []
        if not error and html:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
            host_row["homepage_title"] = soup.title.get_text(" ", strip=True)[:60] if soup.title else ""
            notice_links = []
            for link in soup.select("a[href]"):
                text = link.get_text(" ", strip=True)
                target = urljoin(origin + "/", link.get("href") or "")
                path = urlsplit(target).path or ""
                if urlsplit(target).hostname != host or not NOTICE_WORDS.search(text or ""):
                    continue
                if not target.endswith((".htm", ".html", "/")):
                    continue
                if ARTICLE_URL.search(path):
                    notice_links.append({"url": target, "text": text[:24], "kind": "article"})
                    continue
                from_home.append({"host": host, "url": target, "text": text[:24], "how": "首页通知字样链接"})
            host_row["notice_links"] = notice_links[:6]
            host_row["column_links"] = [item["url"] for item in from_home[:6]]
        candidates.extend(from_home[:4])
        if not from_home:
            for pattern in PATTERNS[:3]:
                candidates.append({"host": host, "url": origin + pattern, "text": "", "how": "规律路径"})
    dedup = {}
    for item in candidates:
        dedup.setdefault(item["url"], item)
    candidates = list(dedup.values())
    print(f"栏目候选 {len(candidates)} 个（去重后）\n")

    # --- 3) 逐个实测 -------------------------------------------------------
    from bs4 import BeautifulSoup

    results = []
    for item in candidates:
        origin = "https://" + item["host"]
        html, error = get(item["url"], extra_trusted=(origin,))
        row = {**item, "reachable": error is None}
        if error:
            row["error"] = error
            results.append(row)
            continue
        soup = BeautifulSoup(html, "html.parser")
        list_links = []
        for link in soup.select("a[href]"):
            target = urljoin(item["url"], link.get("href") or "")
            if urlsplit(target).hostname != item["host"]:
                continue
            text = link.get_text(" ", strip=True)
            if not text or len(text) < 4:
                continue
            if not ARTICLE_HINT.search(urlsplit(target).path or ""):
                continue
            list_links.append({"url": target, "title": text[:90]})
        dates = []
        for link in soup.select("a[href]"):
            context = link.parent.get_text(" ", strip=True) if link.parent else ""
            for match in DATE.finditer(context or ""):
                try:
                    dates.append(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
                except ValueError:
                    continue
        dates = sorted(set(dates), reverse=True)
        newest = dates[0] if dates else None
        age = (today - newest).days if newest else None
        if not list_links:
            tier = "dead"
        elif age is not None and age <= 30:
            tier = "active"
        elif age is not None and age <= 90:
            tier = "low"
        else:
            tier = "dead"
        row.update(
            {
                "column_title": (soup.title.get_text(" ", strip=True)[:60] if soup.title else ""),
                "list_items": len(list_links),
                "recent_dates": [d.isoformat() for d in dates[:5]],
                "newest": newest.isoformat() if newest else None,
                "age_days": age,
                "tier": tier,
                "sample": list_links[:5],
            }
        )
        results.append(row)

    reachable = [row for row in results if row["reachable"]]
    # 活跃档再抓一篇正文：文章页必须有独立标题（<h1> 或内容区标题），
    # 否则卡片会显示站点名——研招网 yzb 就是因为这个没注册。
    if args.titles:
        for row in results:
            if row.get("tier") != "active" or not row.get("sample"):
                continue
            article = row["sample"][0]["url"]
            origin = "https://" + row["host"]
            html, error = get(article, extra_trusted=(origin,))
            if error:
                row["article_error"] = error
                continue
            page = BeautifulSoup(html, "html.parser")
            heading = None
            for selector in ("#vsb_content h1", ".v_news_content h1", "h1", ".arti_title", "#vsb_content h2", ".content h2"):
                node = page.select_one(selector)
                if node and node.get_text(" ", strip=True):
                    heading = node.get_text(" ", strip=True)[:80]
                    break
            row["article_url"] = article
            row["article_h1"] = heading
            row["article_title_tag"] = (page.title.get_text(" ", strip=True)[:80] if page.title else None)
            row["independent_title"] = bool(heading) and len(heading) >= 6

    tiers = Counter(row.get("tier", "unreachable") for row in results)
    errors = Counter(row.get("error") for row in results if not row["reachable"])
    hosts_live = [host for host, row in host_table.items() if row["reachable"]]
    print(f"实测完成：{len(results)} 个候选（请求 {requests['count']} 次）")
    print(f"主机：实测 {len(host_table)} 个 / 首页可达 {len(hosts_live)} 个 / 首页失败 {len(host_table) - len(hosts_live)} 个")
    print(f"候选分档：{dict(tiers)}")
    print(f"不可达原因：{dict(errors)}")
    print("\n活跃档（近 30 天有条目）：")
    for row in results:
        if row.get("tier") == "active":
            print(f"  {row['url'][:76]:<76} 条 {row['list_items']:>3} · 最新 {row['newest']} · «{row['column_title'][:30]}»")
    print("\n低频档（近 90 天有条目）：")
    for row in results:
        if row.get("tier") == "low":
            print(f"  {row['url'][:76]:<76} 条 {row['list_items']:>3} · 最新 {row['newest']}")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "site-inventory.json").write_text(
        json.dumps(
            {
                "today": today.isoformat(),
                "hosts": ordered,
                "known_hosts": sorted(known_hosts),
                "host_table": host_table,
                "discover": discover_pages,
                "requests": requests["count"],
                "tiers": {key: value for key, value in tiers.items()},
                "candidates": results,
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("\nwrote verification/sources/site-inventory.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))