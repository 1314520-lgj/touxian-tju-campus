"""t21：用产品自己的 feed_items 判定每个栏目「实际会入队什么」，
再把教务处站点上所有候选栏目页扫一遍，找出发得出**同站原生文章**（/info/N/N.htm）的页面。

    python verification/sources/discover_native.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

OUT = REPO / "verification" / "sources"

COLUMNS = {
    "教务处 · 通知公告": "https://oaa.tju.edu.cn/bszy/tzgg.htm",
    "教务处 · 学业指导": "https://oaa.tju.edu.cn/tzgg/xyzd.htm",
    "教务处 · 学生竞赛": "https://oaa.tju.edu.cn/cxsj/xsjs.htm",
    "教务处 · 通知公告总览": "https://oaa.tju.edu.cn/tzgg.htm",
    "教务处 · 竞赛科创通知": "https://oaa.tju.edu.cn/tzgg/js_kc.htm",
}
PATTERN = r"^https?://oaa\.tju\.edu\.cn/info/\d+/\d+\.htm(?:[?#]|$)|^https://mp\.weixin\.qq\.com/s(?:[/?#]|$)"


def main() -> int:
    from campus_collect import gateway_origins_from_config
    from server.campus.sources import SafeFetcher, decode_body, feed_items

    origins = gateway_origins_from_config()
    fetcher = SafeFetcher(trusted_origins=origins)
    config = {"list_selector": "a[href]", "article_url_pattern": PATTERN}
    from bs4 import BeautifulSoup

    def get(url):
        result = fetcher.fetch(url)
        return result, decode_body(result.body, result.content_type)

    # 1) 五个栏目：产品实际会入队什么
    column_report = {}
    section_links: dict[str, str] = {}
    for label, url in COLUMNS.items():
        result, html = get(url)
        items = feed_items(result, "website", config, 200)
        native = [item for item in items if "/info/" in item["url"]]
        wechat = [item for item in items if item["url"].startswith("https://mp.weixin.qq.com/s")]
        print(f"\n== {label}\n   会入队 {len(items)} 条：同站原生 {len(native)} · 公众号 {len(wechat)}")
        for item in native[:8]:
            print(f"     原生: {item['url'][:100]}  «{item['title'][:40]}»")
        for item in items[:3]:
            print(f"     入队: {item['url'][:100]}")
        column_report[label] = {
            "url": url,
            "queued": len(items),
            "native": [{"url": item["url"], "title": item["title"]} for item in native],
            "wechat": [{"url": item["url"], "title": item["title"]} for item in wechat],
        }
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.select("a[href]"):
            target = urljoin(result.url, link.get("href") or "")
            if urlsplit(target).hostname == "oaa.tju.edu.cn" and target.endswith(".htm"):
                section_links.setdefault(target, link.get_text(" ", strip=True)[:30])

    # 2) 教务处站点上所有候选栏目页：谁能发同站原生文章
    print(f"\n=== 扫描教务处站点候选页 {len(section_links)} 个 ===")
    page_report = []
    for url, text in sorted(section_links.items()):
        try:
            result, _ = get(url)
            items = feed_items(result, "website", config, 200)
        except Exception as error:  # noqa: BLE001
            page_report.append({"url": url, "text": text, "error": getattr(error, "code", str(error))})
            print(f"   {url[:88]:<88} 抓取失败 {getattr(error, 'code', error)}")
            continue
        hosts = Counter(urlsplit(item["url"]).hostname for item in items)
        native = [item for item in items if "/info/" in item["url"]]
        page_report.append(
            {
                "url": url,
                "text": text,
                "queued": len(items),
                "native": len(native),
                "hosts": dict(hosts),
                "native_sample": [{"url": item["url"], "title": item["title"]} for item in native[:5]],
            }
        )
        if items:
            print(f"   {url[:88]:<88} 入队 {len(items):>3} · 同站原生 {len(native):>3} · 域名 {dict(hosts)}")
    (OUT / "columns-scan.json").write_text(
        json.dumps({"columns": column_report, "pages": page_report}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("\nwrote", (OUT / "columns-scan.json").relative_to(REPO))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())