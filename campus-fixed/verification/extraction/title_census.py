"""t31 census: what title would the product put on a card, for every registered source?

Uses the product's own cells: ``SafeFetcher`` to fetch, ``sources.feed_items`` for the
listing, and ``content``'s parser to see the *card title* the extractor would choose.

Read-only w.r.t. any data directory; network reads only.

Usage::

    python verification/extraction/title_census.py [--source NAME] [--per-source 3]
                                                   [--json-out FILE] [--fixtures DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.campus import content as content_module  # noqa: E402
from server.campus import sources as sources_module  # noqa: E402

# A card title is unusable when it carries no information about the notice: a generic
# notice word, a channel/site name, or a fragment of page furniture.
GENERIC_TITLES = {
    "会议通知", "通知", "公告", "公示", "会议", "活动通知", "讲座通知", "通知公告",
    "总体情况", "下载中心", "更多", "首页", "栏目", "新闻", "正文",
}
CHANNEL_LIKE = re.compile(r"(学院|学部|中心|办公室|研究生院|教务处|招生网|新闻网|招聘|人才培养|人才培养中心)$")


def normalize(value: str) -> str:
    return " ".join((value or "").split())


def classify(title: str, listing_title: str, source_name: str) -> str:
    text = normalize(title)
    if not text:
        return "空标题"
    if text in GENERIC_TITLES:
        return "过泛"
    if text == normalize(listing_title):
        return "只有列表标题（页面无独立标题）"
    if CHANNEL_LIKE.search(text) and len(text) <= 12:
        return "站点名/栏目名"
    if len(text) <= 4:
        return "过短"
    return "可辨识"


def card_title(html: bytes, source_url: str) -> tuple[str, str]:
    """The title the parser would choose, plus its state/warning."""
    segments, state, warnings = content_module.html_body_segments(html, source_url)
    title = next((segment["text"] for segment in segments if segment["id"] == "title"), "")
    return normalize(title), f"{state}{'/' + ','.join(warnings) if warnings else ''}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="t31 title census (network reads only)")
    parser.add_argument("--source", action="append", default=[], help="只看名字里含这些片段的来源")
    parser.add_argument("--per-source", type=int, default=3, help="每个来源抽查几条")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--json-out")
    parser.add_argument("--fixtures", help="把这批真实页面片段存下来，供红测复现")
    args = parser.parse_args(argv)

    origins = []
    try:
        from scripts.campus_collect import EXTRA_GATEWAY_ORIGINS, gateway_origins_from_config

        origins = list(gateway_origins_from_config()) + list(EXTRA_GATEWAY_ORIGINS)
    except Exception:  # noqa: BLE001
        pass
    if origins:
        import os

        os.environ["CAMPUS_TRUSTED_GATEWAY_ORIGINS"] = ",".join(origins)

    fetcher = sources_module.SafeFetcher(timeout=args.timeout)
    catalog = sources_module.builtin_sources()
    selected = [
        entry for entry in catalog
        if not args.source or any(fragment in (entry.get("name") or "") for fragment in args.source)
    ]
    print(f"来源 {len(selected)}/{len(catalog)} 个（受信网关 {len(origins)} 个 origin）\n")

    rows = []
    tally = Counter()
    for entry in selected:
        name = entry.get("name") or entry.get("source_identity")
        config = entry.get("config") or {}
        try:
            result = fetcher.fetch(entry["origin_url"])
        except Exception as error:  # noqa: BLE001
            print(f"[{name}] 列表取不到：{getattr(error, 'code', type(error).__name__)}")
            continue
        items = sources_module.feed_items(result, entry.get("adapter") or "website", config, limit=args.per_source * 3)
        print(f"[{name}] 列表 {len(items)} 条")
        for item in items[: args.per_source]:
            listing_title = item.get("title") or ""
            try:
                page = fetcher.fetch(item["url"])
                title, state = card_title(page.body, item["url"])
            except Exception as error:  # noqa: BLE001
                title, state = "", f"fetch:{getattr(error, 'code', type(error).__name__)}"
            verdict = classify(title, listing_title, name)
            tally[verdict] += 1
            rows.append({
                "source": name,
                "url": item["url"],
                "listing_title": listing_title,
                "card_title": title,
                "state": state,
                "verdict": verdict,
            })
            print(f"    [{verdict}] 卡片=《{title}》")
            print(f"              列表=《{listing_title}》")
            print(f"              {item['url']} · {state}")
            if args.fixtures:
                directory = Path(args.fixtures)
                directory.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r"[^0-9A-Za-z]+", "_", item["url"].split("//")[-1])[:80]
                (directory / f"{slug}.html").write_bytes(page.body)
    print("\n分类合计：", dict(tally), file=sys.stdout)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"tally": dict(tally), "rows": rows}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())