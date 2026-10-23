"""t19：把 07:30 那次运行里被判 `article_redirected_off_pattern` 的 URL 逐条重放一遍，
记录**重定向后的最终地址**与**页面证据**（标题、正文开头、验证页特征），用来判定
「真拒绝」还是「误杀」。

用的是产品自己的 SafeFetcher（DNS 固定、逐跳 SSRF 复核、地址安全判定一律照旧），
受信网关来自 `config/campus-sources.json`（等价于 `--gateway-from-config`）。

    python verification/redirect/replay_rejections.py --since 2026-10-20T23:30:00 --until 2026-10-20T23:32:00
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

DB = REPO / "data-operator" / "campus.sqlite3"
OUT = REPO / "verification" / "redirect"

CHALLENGE_MARKERS = ("环境异常", "完成验证后即可继续访问", "请在微信客户端打开链接", "wappoc_appmsgcaptcha", "去验证")
OFFSITE_HOSTS = ("weixin110.qq.com", "mp.weixin.qq.com/mp/", "security.weixin.qq.com")


def backlog_items(code: str, since: str, until: str):
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='source_ingestion'")
    rows = []
    for key, data in cursor.fetchall():
        record = json.loads(data)
        items = record.get("items") or {}
        if not isinstance(items, dict):
            continue
        for digest, item in items.items():
            if item.get("error_code") != code:
                continue
            attempted = (item.get("last_attempt_at") or "").replace("+00:00", "")
            if since <= attempted <= until:
                rows.append({"source_id": key.split(":")[0], "digest": digest, **item})
    rows.sort(key=lambda row: (row["source_id"], row["url"] or ""))
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="article_redirected_off_pattern")
    parser.add_argument("--since", default="2026-10-20T23:30:00")
    parser.add_argument("--until", default="2026-10-20T23:32:00")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（排查用）")
    args = parser.parse_args(argv[1:])

    from campus_collect import gateway_origins_from_config  # noqa: E402
    from server.campus.content import _html_interstitial  # noqa: E402
    from server.campus.sources import FetchError, SafeFetcher, decode_body  # noqa: E402
    from bs4 import BeautifulSoup  # noqa: E402

    origins = gateway_origins_from_config()
    fetcher = SafeFetcher(trusted_origins=origins)
    items = backlog_items(args.code, args.since, args.until)
    if args.limit:
        items = items[: args.limit]
    print(f"replaying {len(items)} items with code={args.code} attempted in [{args.since}, {args.until}]")
    print("trusted origins:", len(origins))

    results = []
    for item in items:
        url = item["url"]
        entry = {
            "source_id": item["source_id"],
            "url": url,
            "list_title": item.get("title"),
            "attempts": item.get("attempts"),
            "last_attempt_at": item.get("last_attempt_at"),
            "final_url": None,
            "fetch_error": None,
            "title": None,
            "snippet": None,
            "challenge_markers": [],
            "interstitial": None,
            "verdict": None,
        }
        try:
            result = fetcher.fetch(url)
            entry["final_url"] = result.url
            entry["content_type"] = result.content_type
            text = decode_body(result.body, result.content_type)
            soup = BeautifulSoup(text, "html.parser")
            entry["title"] = (soup.title.get_text(" ", strip=True) if soup.title else None)
            entry["interstitial"] = _html_interstitial(soup)
            visible = soup.get_text(" ", strip=True)
            entry["snippet"] = visible[:220]
            entry["challenge_markers"] = [m for m in CHALLENGE_MARKERS if m in visible or m in (entry["title"] or "")]
            entry["bytes"] = len(result.body)
        except FetchError as error:
            entry["fetch_error"] = error.code
        except Exception as error:  # noqa: BLE001 - replay must not stop on one item
            entry["fetch_error"] = f"unexpected:{type(error).__name__}"

        same_host = False
        if entry["final_url"] and url:
            same_host = re.sub(r"^https?://", "", entry["final_url"]).split("/")[0] == re.sub(r"^https?://", "", url).split("/")[0]
        if entry["challenge_markers"] or (entry["final_url"] and any(h in entry["final_url"] for h in OFFSITE_HOSTS)):
            entry["verdict"] = "真拒绝：重定向到验证/反爬页"
        elif entry["interstitial"]:
            entry["verdict"] = "真拒绝：落地页被判定为访问拦截页"
        elif entry["fetch_error"]:
            entry["verdict"] = f"真拒绝：抓取失败（{entry['fetch_error']}）"
        elif same_host and entry["title"]:
            entry["verdict"] = "误杀：落地页是同一站点的真实文章"
        else:
            entry["verdict"] = "需人工判读：既非验证页也非成功文章"
        results.append(entry)
        print(f"  {entry['verdict']:<22} {url[:60]}")
        print(f"      final={entry['final_url']}  title={(entry['title'] or '')[:50]}  markers={entry['challenge_markers']}")

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "replay-rejections.json"
    out.write_text(json.dumps({"window": [args.since, args.until], "code": args.code, "results": results}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("\nverdict counts:", Counter(row["verdict"] for row in results).most_common())
    print("wrote", out.relative_to(REPO))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))