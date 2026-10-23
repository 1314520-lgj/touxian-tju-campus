"""Discovery (read-only): enumerate official hosts linked from the school's own pages.

No guessing: the candidate hosts come from the school's homepage / 院系 pages, and the
probe only records what the site itself answers.

Usage::

    python verification/freshness/discover_hosts.py [--seed https://www.tju.edu.cn/]
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from urllib.parse import urljoin, urlsplit

UA = "Mozilla/5.0 (compatible; tju-campus-source-check)"
INTERESTING = ("学院", "学部", "教务处", "学工", "团委", "招生", "就业", "图书馆", "研究生院", "中心")


def fetch(url, timeout=25):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.url, response.read(400000)


def decode(raw):
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="official host discovery (read-only)")
    parser.add_argument("--seed", action="append", default=[], help="seed page (repeatable)")
    args = parser.parse_args(argv)
    seeds = args.seed or ["https://www.tju.edu.cn/"]

    hosts = Counter()
    labels = {}
    for seed in seeds:
        try:
            status, final, raw = fetch(seed)
        except urllib.error.HTTPError as error:
            print(f"seed {seed} -> HTTP {error.code}", file=out)
            continue
        except Exception as error:  # noqa: BLE001
            print(f"seed {seed} -> {type(error).__name__}", file=out)
            continue
        text = decode(raw)
        print(f"seed {seed} -> HTTP {status} bytes={len(raw)} final={final}", file=out)
        for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', text, re.S | re.I):
            url = urljoin(final, href)
            host = urlsplit(url).netloc
            if not host.endswith(".tju.edu.cn") or host.count(".") > 3:
                continue
            clean = re.sub(r"<[^>]+>", "", label)
            clean = " ".join(clean.split())
            hosts[host] += 1
            if clean and (any(word in clean for word in INTERESTING) or host not in labels):
                labels.setdefault(host, clean[:24])

    print("", file=out)
    print(f"从种子页发现的官方主机：{len(hosts)} 个", file=out)
    for host, count in sorted(hosts.items()):
        print(f"   {host:<28} links={count:<4} 链接文本示例：{labels.get(host, '')}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())