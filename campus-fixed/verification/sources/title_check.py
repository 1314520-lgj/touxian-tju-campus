"""t28：活跃栏目逐个检查两件事，都用产品自己的代码：

  1) 用 `feed_items` + 一条从列表页自身推导出的 `article_url_pattern` 看这个栏目**会入队什么**
     （这同时也给出准备注册的 pattern，避免「凭 URL 猜」）；
  2) 抓该栏目入队的第一条，检查文章页的第一个非空 `<h1>` 是不是一条真正的通知标题
     ——产品的卡片标题就是它（`server/campus/sources.py` 采集循环）；没有 h1 时会退回列表文字，
     所以「列表文字本身就是通知标题」也算可注册。

    python verification/sources/title_check.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
OUT = REPO / "verification" / "sources"

NAV_WORDS = {
    "首页", "通知公告", "媒体报道", "人才培养", "新闻中心", "人才招聘", "招聘公告",
    "学院新闻", "新闻动态", "学术活动", "友情链接", "联系我们", "部门简介", "学位与导师工作",
}
NOTICE_WORD = ("关于", "通知", "公告", "公示", "报名", "选拔", "招聘", "安排", "申请", "评定", "比赛", "公布")


def pattern_from_samples(host, samples):
    """从列表页自己的链接形状推导文章地址规律：同目录 + 文件名里含数字。"""
    dirs = Counter()
    for item in samples:
        path = urlsplit(item["url"]).path
        if not re.search(r"\d", path.rsplit("/", 1)[-1] or ""):
            continue
        directory = path.rsplit("/", 1)[0]
        dirs[directory] += 1
    if not dirs:
        return None, []
    directory, count = dirs.most_common(1)[0]
    return directory, count


def main(argv: list[str]) -> int:
    from bs4 import BeautifulSoup

    from campus_collect import gateway_origins_from_config
    from server.campus.sources import SafeFetcher, decode_body, feed_items

    payload = json.loads((OUT / "site-inventory.json").read_text(encoding="utf-8"))
    rows = [row for row in payload["candidates"] if row.get("tier") in {"active", "low"}]
    if "--urls" in argv:
        wanted = set(argv[argv.index("--urls") + 1].split(","))
        rows = [row for row in rows if row["url"] in wanted]
    depth = int(argv[argv.index("--items") + 1]) if "--items" in argv else 1
    base = tuple(gateway_origins_from_config() or ())
    fetcher = SafeFetcher(trusted_origins=base)
    report = []
    for row in rows:
        origin = "https://" + row["host"]
        fetcher.trusted_origins = base + (origin,)
        entry = {"url": row["url"], "unit": row.get("unit"), "tier": row.get("tier")}
        try:
            listing = fetcher.fetch(row["url"])
        except Exception as error:  # noqa: BLE001
            entry["error"] = getattr(error, "code", str(error))
            report.append(entry)
            continue
        # 规律从**刚抓到的列表页**上推导（不用库存里的旧样本）：
        # 收集本页所有含数字的站内链接，取目录出现最多的那个。
        from urllib.parse import urljoin

        soup_listing = BeautifulSoup(decode_body(listing.body, listing.content_type), "html.parser")
        fresh = []
        for link in soup_listing.select("a[href]"):
            target = urljoin(row["url"], link.get("href") or "")
            if urlsplit(target).hostname != row["host"]:
                continue
            fresh.append({"url": target, "title": link.get_text(" ", strip=True)[:90]})
        directory, hits = pattern_from_samples(row["host"], fresh)
        if not directory:
            entry["error"] = "列表页里没有含数字的条目链接"
            entry["queued"] = 0
            report.append(entry)
            print(f"?? {row['url'][:58]:<58} 推导不出文章地址规律（列表页没有含数字的条目链接）")
            continue
        pattern = r"^https?://" + re.escape(row["host"]) + re.escape(directory) + r"/[^/]*\d[^/]*\.html?(?:[?#]|$)"
        items = feed_items(
            listing, "website", {"list_selector": "a[href]", "article_url_pattern": pattern}, 5
        )
        entry.update(
            {
                "pattern": pattern,
                "directory": directory,
                "directory_hits": hits,
                "queued": len(items),
                "queued_sample": items[:3],
                "column_title": (BeautifulSoup(decode_body(listing.body, listing.content_type), "html.parser").title.get_text(" ", strip=True)[:70] if True else ""),
            }
        )
        if items:
            checks = []
            for item in items[:depth]:
                try:
                    page_result = fetcher.fetch(item["url"])
                except Exception as error:  # noqa: BLE001
                    checks.append({"url": item["url"], "error": getattr(error, "code", str(error))})
                    continue
                soup = BeautifulSoup(decode_body(page_result.body, page_result.content_type), "html.parser")
                node = soup.select_one("h1")
                checks.append(
                    {
                        "url": item["url"],
                        "list_title": item["title"],
                        "h1": node.get_text(" ", strip=True)[:100] if node else "",
                        "title_tag": soup.title.get_text(" ", strip=True)[:100] if soup.title else "",
                    }
                )
            first = checks[0]
            h1_text = first.get("h1") or ""
            title_tag = first.get("title_tag") or ""
            list_title = first.get("list_title") or ""
            site_names = {part.strip() for part in re.split(r"[-—|｜]", entry["column_title"] or "") if part.strip()}
            looks_like_notice = (
                bool(h1_text)
                and h1_text not in NAV_WORDS
                and len(h1_text) >= 8
                and (
                    any(word in h1_text for word in NOTICE_WORD)
                    or h1_text[:8] in list_title
                    or list_title[:8] in h1_text
                )
            )
            entry.update(
                {
                    "article": first["url"],
                    "list_title": list_title,
                    "h1": h1_text,
                    "title_tag": title_tag,
                    "checks": checks,
                    "independent_title": bool(looks_like_notice and h1_text not in site_names),
                    "fallback_to_list_title": not bool(h1_text),
                    # 三条里至少一条有 h1，就把整列当作「有独立标题」
                    "any_h1": any(check.get("h1") for check in checks),
                }
            )
        report.append(entry)
        verdict = "OK " if entry.get("independent_title") else ("--" if entry.get("fallback_to_list_title") else "no ")
        print(f"{verdict} {row['url'][:58]:<58} 入队 {entry['queued']:>2} · 目录 {directory}")
        if items:
            print(f"     列表标题：{entry['list_title'][:60]}")
            print(f"     <h1>    ：{entry.get('h1') or '（无 h1，卡片用列表标题）'}")

    (OUT / "title-check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    good = [entry for entry in report if entry.get("independent_title")]
    print(f"\n有独立标题 {len(good)}/{len(report)} · 已写 title-check.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))