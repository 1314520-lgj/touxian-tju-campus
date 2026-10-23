"""只抓取：把官网栏目和本机公众号 feed 的通知抓出来，不建库、不复核、不发布。

这是「单纯只用 scraping」的入口 —— 没有服务器、没有审核队列、没有数据库：
抓完就打印，或者写成 JSONL 交给别的工具用。

用法（在仓库根目录执行）：

    python scripts/scrape_only.py --list                     # 列出来源
    python scripts/scrape_only.py --official                 # 34 个官网栏目：标题 + 链接 + 日期
    python scripts/scrape_only.py --wechat                   # 本机公众号 feed（默认 127.0.0.1:4011）
    python scripts/scrape_only.py --official --wechat --out scraped-2026-10-23.jsonl
    python scripts/scrape_only.py --official --body           # 顺手取正文纯文本（较慢）
    python scripts/scrape_only.py --official --limit 3        # 每个来源只取 3 条

公众号 feed 优先读本机 feed 服务（安装目录里的 weread-feed 会起在 4011），
也可以 --feed 指定别的地址或本地 JSON 文件路径。

行为约束与产品一致：不伪造 UA、不绕过验证码/反爬；被反爬页拒绝的来源会跳过并说明。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

DEFAULT_FEED = "http://127.0.0.1:4011/feeds/all.json"
WECHAT_HOST = "mp.weixin.qq.com"


def load_config():
    path = Path(os.environ.get("CAMPUS_SOURCE_CONFIG", REPO / "config" / "campus-sources.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("sources", [])


def official_sources():
    """和产品一致：只取 config 里已登记进 BUILTIN_ORIGINS 的官方栏目。"""
    from server.campus.sources import builtin_sources

    return builtin_sources()


def trusted_origins(sources):
    origins = set()
    for source in sources:
        parsed = urlsplit(source.get("origin_url") or "")
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            origins.add(f"{parsed.scheme}://{parsed.netloc}")
    origins.add("https://" + WECHAT_HOST)
    extra = [item.strip() for item in (os.environ.get("CAMPUS_TRUSTED_GATEWAY_ORIGINS") or "").split(",") if item.strip()]
    origins.update(extra)
    return tuple(sorted(origins))


def body_text(body: bytes, url: str):
    """正文纯文本：用产品自己的解析器，取 title 之外的段落。"""
    from server.campus.content import html_body_segments

    try:
        segments, _state, _warnings = html_body_segments(body, url)
    except Exception:  # noqa: BLE001 - 单个页面解析失败不影响其它条目
        return None
    parts = [segment["text"] for segment in segments if segment["id"] != "title"]
    text = "\n".join(part for part in parts if part.strip())
    return text or None


def scrape_official(fetcher, limit: int, with_body: bool, pause: float, name_filter: str = ""):
    from server.campus.content import html_body_segments
    from server.campus.sources import FetchError, feed_items

    rows = []
    for source in official_sources():
        if name_filter and name_filter not in source["name"]:
            continue
        config = source.get("config") or {}
        try:
            listing = fetcher.fetch(source["origin_url"], config.get("headers"))
            items = feed_items(listing, "website", config, limit)
        except FetchError as error:
            print(f"  跳过 {source['name']}：{error.code}", file=sys.stderr)
            continue
        except Exception as error:  # noqa: BLE001
            print(f"  跳过 {source['name']}：{error}", file=sys.stderr)
            continue
        for item in items[:limit]:
            from server.campus.content import _clean_listing_title

            row = {
                "kind": "official",
                "source": source["name"],
                "title": _clean_listing_title(item.get("title") or ""),
                "url": item.get("url"),
                "published_at": item.get("published_at"),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            if with_body and item.get("url"):
                try:
                    page = fetcher.fetch(item["url"], config.get("headers"))
                    row["body"] = body_text(page.body, item["url"])
                except FetchError as error:
                    row["body_error"] = error.code
                except Exception as error:  # noqa: BLE001
                    row["body_error"] = str(error)
                time.sleep(pause)
            rows.append(row)
        print(f"  {source['name']}：{len(items[:limit])} 条", file=sys.stderr)
        time.sleep(pause)
    return rows


def load_feed(feed: str):
    """读公众号 feed：本机服务地址或本地文件。"""
    if feed.startswith("http://") or feed.startswith("https://"):
        from server.campus.sources import SafeFetcher

        parsed = urlsplit(feed)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        # 本机 feed 是非标准端口：SafeFetcher 的端口检查读环境变量，需要显式信任这个 origin
        current = [item.strip() for item in (os.environ.get("CAMPUS_TRUSTED_GATEWAY_ORIGINS") or "").split(",") if item.strip()]
        for item in (origin, "https://" + WECHAT_HOST):
            if item not in current:
                current.append(item)
        os.environ["CAMPUS_TRUSTED_GATEWAY_ORIGINS"] = ",".join(current)
        fetcher = SafeFetcher(trusted_origins=tuple(sorted(current)))
        return fetcher.fetch(feed)
    from server.campus.sources import FetchResult

    path = Path(feed)
    body = path.read_bytes()
    return FetchResult(feed, body, "application/feed+json")


def scrape_wechat(feed: str, limit: int, with_body: bool):
    from server.campus.sources import FetchError, wechat_feed_items

    os.environ["CAMPUS_WECHAT_FEED_ENABLED"] = "true"
    try:
        result = load_feed(feed)
        items = wechat_feed_items(result, "wechat_feed", {}, limit)
    except FetchError as error:
        print(f"  公众号 feed 读取失败：{error.code}", file=sys.stderr)
        return []
    except Exception as error:  # noqa: BLE001
        print(f"  公众号 feed 读取失败：{error}", file=sys.stderr)
        return []
    rows = []
    for item in items[:limit]:
        row = {
            "kind": "wechat",
            "source": item.get("account"),
            "title": item.get("title"),
            "url": item.get("url"),
            "published_at": item.get("published_at"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        if with_body and item.get("content_html"):
            row["body"] = body_text(item["content_html"].encode("utf-8"), item["url"])
        rows.append(row)
    print(f"  公众号：{len(rows)} 条", file=sys.stderr)
    return rows


def print_table(rows):
    if not rows:
        print("（没有抓到内容）")
        return
    for row in rows:
        date = (row.get("published_at") or "")[:10] or "----------"
        title = (row.get("title") or "").strip() or "（无标题）"
        print(f"{date}  {title[:60]}")
        print(f"          {row.get('url')}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="scrape_only.py",
        description="只抓取：官网栏目 + 本机公众号 feed，不建库不复核不发布。",
    )
    parser.add_argument("--official", action="store_true", help="抓官网栏目")
    parser.add_argument("--wechat", action="store_true", help="抓本机公众号 feed")
    parser.add_argument("--feed", default=DEFAULT_FEED, help=f"公众号 feed 地址或 json 文件（默认 {DEFAULT_FEED}）")
    parser.add_argument("--limit", type=int, default=10, help="每个来源最多取几条（默认 10）")
    parser.add_argument("--source", default="", help="只抓名字里含这个关键词的栏目（例如 教务处）")
    parser.add_argument("--body", action="store_true", help="同时抓正文纯文本（慢）")
    parser.add_argument("--out", help="把结果写成 JSONL 到这个文件")
    parser.add_argument("--pause", type=float, default=0.5, help="每次请求之间的间隔秒数（默认 0.5）")
    parser.add_argument("--list", action="store_true", help="列出来源后退出")
    parser.add_argument("--json", action="store_true", help="把结果以 JSONL 打到标准输出")
    args = parser.parse_args(argv)

    if args.list:
        for source in official_sources():
            print(f"{source['id']}  {source['name']}  {source['origin_url']}")
        print(f"\n公众号：{args.feed}（读你微信读书里订阅的公众号）")
        return 0

    if not args.official and not args.wechat:
        parser.error("至少指定 --official 或 --wechat（--list 可查看来源）")

    os.environ.setdefault("CAMPUS_ENV", "development")
    sources = load_config()
    origins = trusted_origins(sources)

    from server.campus.sources import SafeFetcher

    fetcher = SafeFetcher(trusted_origins=origins)
    rows = []
    if args.official:
        print("官网栏目：", file=sys.stderr)
        rows.extend(scrape_official(fetcher, max(1, args.limit), args.body, max(0.0, args.pause), args.source))
    if args.wechat:
        print("公众号：", file=sys.stderr)
        rows.extend(scrape_wechat(args.feed, max(1, args.limit), args.body))

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"\n已写入 {path}（{len(rows)} 条）")
    if args.json:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
    elif not args.out:
        print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
