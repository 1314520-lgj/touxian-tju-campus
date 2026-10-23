"""t21：现场抓五个教务处栏目页，列出它们实际给出的链接（判定「同站 0 条」的证据）。

同时抓教务处首页与若干候选原生栏目，用于寻找原生通路。
只用产品自己的 SafeFetcher（受信网关来自 config/campus-sources.json，等价 --gateway-from-config）。

    python verification/sources/scan_columns.py [额外要抓的 URL ...]
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

EXTRA = [
    "https://oaa.tju.edu.cn/",
    "https://oaa.tju.edu.cn/tzgg/",
    "https://oaa.tju.edu.cn/xwzx/",
    "https://oaa.tju.edu.cn/index.htm",
]


def host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or "?"
    except Exception:
        return "?"


def links_of(html: str, base: str):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for link in soup.select("a[href]"):
        href = link.get("href") or ""
        text = link.get_text(" ", strip=True)[:80]
        url = urljoin(base, href)
        if url.startswith("javascript:") or url.startswith("#"):
            continue
        rows.append({"url": url, "text": text, "host": host_of(url)})
    return rows


def fetch(fetcher, url: str):
    try:
        result = fetcher.fetch(url)
    except Exception as error:  # noqa: BLE001
        code = getattr(error, "code", type(error).__name__)
        return None, f"fetch_failed:{code}"
    from server.campus.sources import decode_body

    return decode_body(result.body, result.content_type), None


def main(argv: list[str]) -> int:
    from campus_collect import gateway_origins_from_config
    from server.campus.sources import SafeFetcher

    origins = gateway_origins_from_config()
    fetcher = SafeFetcher(trusted_origins=origins)
    targets = {**COLUMNS, **{url: url for url in EXTRA}, **{url: url for url in argv[1:]}}
    report = {}
    OUT.mkdir(parents=True, exist_ok=True)
    for label, url in targets.items():
        html, error = fetch(fetcher, url)
        if error:
            print(f"\n== {label}  {url}\n   {error}")
            report[label] = {"url": url, "error": error}
            continue
        rows = links_of(html, url)
        hosts = Counter(row["host"] for row in rows)
        native = [row for row in rows if row["host"].endswith("tju.edu.cn")]
        wechat = [row for row in rows if row["host"] == "mp.weixin.qq.com"]
        print(f"\n== {label}  {url}")
        print(f"   链接 {len(rows)} 条；域名分布 {dict(hosts)}")
        print(f"   同站(tju.edu.cn) {len(native)} 条 / 公众号 {len(wechat)} 条")
        for row in native[:12]:
            print(f"     同站: {row['url'][:104]}  «{row['text'][:40]}»")
        for row in wechat[:4]:
            print(f"     公众号: {row['url'][:104]}  «{row['text'][:40]}»")
        report[label] = {
            "url": url,
            "links": len(rows),
            "hosts": dict(hosts),
            "native": native,
            "wechat": wechat[:12],
            "all": rows,
        }
    path = OUT / "columns-scan.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("\nwrote", path.relative_to(REPO))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))