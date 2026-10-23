"""t28：从 www.tju.edu.cn 首页里找「院系/机构」导航页，再从中收集全部单位主机名。

    python verification/sources/find_units.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

KEY = re.compile(r"院系|学院|机构|部门|单位|设置|直属|科研")
CANDIDATE_PAGES = [
    "https://www.tju.edu.cn/xxgk/yxsz.htm",
    "https://www.tju.edu.cn/yxsz.htm",
    "https://www.tju.edu.cn/xxgk.htm",
    "https://www.tju.edu.cn/jgsz.htm",
    "https://www.tju.edu.cn/zzjg.htm",
]


def main() -> int:
    from bs4 import BeautifulSoup

    from campus_collect import gateway_origins_from_config
    from server.campus.sources import SafeFetcher, decode_body

    base_trusted = tuple(gateway_origins_from_config() or ())
    fetcher = SafeFetcher(trusted_origins=base_trusted)
    requests = {"n": 0}

    def get(url):
        origin = "https://" + (urlsplit(url).hostname or "")
        fetcher.trusted_origins = base_trusted + (origin,)
        requests["n"] += 1
        try:
            result = fetcher.fetch(url)
        except Exception as error:  # noqa: BLE001
            return None, getattr(error, "code", type(error).__name__)
        return decode_body(result.body, result.content_type), None

    home, error = get("https://www.tju.edu.cn/")
    if error:
        print("首页不可达：", error)
        return 1
    soup = BeautifulSoup(home, "html.parser")
    nav = []
    for link in soup.select("a[href]"):
        text = link.get_text(" ", strip=True)
        if text and KEY.search(text):
            nav.append((text[:20], urljoin("https://www.tju.edu.cn/", link.get("href") or "")))
    print(f"首页里含「院系/机构/设置」字样的导航 {len(nav)} 条：")
    for text, url in nav[:20]:
        print(f"  {text:<20} {url}")

    pages = [url for _, url in nav if url.startswith("https://www.tju.edu.cn")][:3] + CANDIDATE_PAGES
    units = {}
    for page in dict.fromkeys(pages):
        html, error = get(page)
        if error:
            print(f"\n{page} → {error}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        found = {}
        for link in soup.select("a[href]"):
            text = link.get_text(" ", strip=True)
            target = urljoin(page, link.get("href") or "")
            host = urlsplit(target).hostname or ""
            if host.endswith("tju.edu.cn") and host not in ("www.tju.edu.cn",) and text:
                found.setdefault(host, text[:24])
        print(f"\n{page} → 单位主机 {len(found)} 个")
        for host, text in sorted(found.items()):
            print(f"  {host:<32} {text}")
        units.update(found)

    outline = REPO / "verification" / "sources" / "unit-hosts.json"
    outline.write_text(
        json.dumps({"pages": list(dict.fromkeys(pages)), "units": units, "requests": requests["n"]}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"\n合计单位主机 {len(units)} 个 · 请求 {requests['n']} 次 · 已写 {outline.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())