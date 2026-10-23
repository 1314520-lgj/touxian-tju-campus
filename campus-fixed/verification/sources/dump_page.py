"""t28：打印一个官方页面里的全部链接（找学院/单位主机用）。

    python verification/sources/dump_page.py <url> [关键字]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


def main(argv: list[str]) -> int:
    from bs4 import BeautifulSoup

    from campus_collect import gateway_origins_from_config
    from server.campus.sources import SafeFetcher, decode_body

    url = argv[1]
    keyword = re.compile(argv[2]) if len(argv) > 2 else None
    base = tuple(gateway_origins_from_config() or ())
    origin = "https://" + (urlsplit(url).hostname or "")
    fetcher = SafeFetcher(trusted_origins=base + (origin,))
    try:
        result = fetcher.fetch(url)
    except Exception as error:  # noqa: BLE001
        print("不可达：", getattr(error, "code", error))
        return 1
    html = decode_body(result.body, result.content_type)
    soup = BeautifulSoup(html, "html.parser")
    print(f"标题：{soup.title.get_text(' ', strip=True) if soup.title else '—'} · 字节 {len(result.body)}")
    rows = []
    for link in soup.select("a[href]"):
        text = link.get_text(" ", strip=True)
        target = urljoin(url, link.get("href") or "")
        if keyword and not (keyword.search(text) or keyword.search(target)):
            continue
        rows.append((text[:24], target))
    print(f"链接 {len(rows)} 条：")
    for text, target in rows:
        host = urlsplit(target).hostname or ""
        print(f"  {('[' + host + ']') if host.endswith('tju.edu.cn') and host != 'www.tju.edu.cn' else '':<34} {text:<24} {target[:96]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))