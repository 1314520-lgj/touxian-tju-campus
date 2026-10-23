"""t30：把第三方微信公众号 feed（微信读书路线）接到本机的联调工具。

三个子命令，都不碰微信凭证——凭证在你自己跑的第三方工具里（WeWe RSS 之类）：

  build  用仓库里**第 14 轮逐字留存**的真实公众号原件，造出 WeWe RSS 形状的 feed
         （每个公众号一个 feed + 一个多账号 feed），并写 manifest（含 sha256）
  split  把一个多账号 feed 按公众号拆成多个 feed（每个公众号一个来源，不混）
  verify 检查 feed 里每条 item 的 URL 是不是真实 mp.weixin.qq.com 原文
  serve  在回环地址上把 feed 目录 serve 出来，供本产品按普通来源采集

    python verification/wechat/wewe_feed_tool.py build --out <目录>
    python verification/wechat/wewe_feed_tool.py split --feed <多账号 feed> --out <目录>
    python verification/wechat/wewe_feed_tool.py verify --feed <feed>
    python verification/wechat/wewe_feed_tool.py serve --dir <目录> --port 8791
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
STAGING = REPO / "deploy" / "operator" / "staging"
WECHAT_HOST = "mp.weixin.qq.com"
ATOM = "{http://www.w3.org/2005/Atom}"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def account_slug(account: str) -> str:
    keep = re.sub(r"[^\w\-]+", "-", account or "unknown", flags=re.UNICODE).strip("-")
    return keep.lower() or "unknown"


def build(out: Path, include_non_wechat: bool = False) -> int:
    """Verbatim round-14 artifacts → WeWe RSS shaped feeds (one per account + one multi)."""
    out.mkdir(parents=True, exist_ok=True)
    items = []
    skipped = []
    for meta_path in sorted(STAGING.glob("*/*.meta.json")):
        article_path = meta_path.with_name(meta_path.name.replace(".meta.json", ".json"))
        if not article_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        article = json.loads(article_path.read_text(encoding="utf-8"))
        host = (urlsplit(meta["url"]).hostname or "").lower()
        if host != WECHAT_HOST and not include_non_wechat:
            # A WeChat feed may only carry real WeChat posts: the product refuses
            # anything else (feed_item_outside_wechat), so keep them out here.
            skipped.append(meta["url"])
            continue
        note = meta.get("note") or ""
        match = re.search(r"账号：([^）)]+)", note)
        account = match.group(1).strip() if match else "未知公众号"
        items.append(
            {
                "url": meta["url"],
                "title": meta["title"],
                "published_at": meta.get("published_at"),
                "content_html": article.get("content_html") or "",
                "mp_name": account,
                "origin": str(article_path.relative_to(REPO)).replace("\\", "/"),
                "content_sha256": sha256_bytes((article.get("content_html") or "").encode("utf-8")),
            }
        )
    if not items:
        print("没有找到逐字留存的原件（deploy/operator/staging/*/*.json）")
        return 1
    manifest = {"items": items, "count": len(items), "skipped_non_wechat": skipped}
    by_account = {}
    for item in items:
        by_account.setdefault(item["mp_name"], []).append(item)

    written = []
    for account, rows in by_account.items():
        payload = {
            "title": account,
            "mp_name": account,
            "items": [{k: v for k, v in row.items() if k not in ("origin", "content_sha256")} for row in rows],
        }
        path = out / f"feed-{account_slug(account)}.json"
        path.write_bytes(json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8") + b"\n")
        written.append(path)
    multi = {
        "title": "多个公众号合并 feed（用来验证拆分成多个来源）",
        "items": [{k: v for k, v in row.items() if k not in ("origin", "content_sha256")} for row in items],
    }
    multi_path = out / "feed-multi-account.json"
    multi_path.write_bytes(json.dumps(multi, ensure_ascii=False, indent=1).encode("utf-8") + b"\n")
    written.append(multi_path)

    manifest["feeds"] = [
        {"path": path.name, "sha256": sha256_bytes(path.read_bytes()), "bytes": path.stat().st_size}
        for path in written
    ]
    (out / "manifest.json").write_bytes(json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8") + b"\n")
    print(f"写出 {len(written)} 个 feed（{len(by_account)} 个公众号）+ manifest.json 到 {out}")
    for row in manifest["feeds"]:
        print(f"  {row['path']:<34} {row['bytes']:>7} 字节  sha256 {row['sha256'][:16]}…")
    for item in items:
        print(f"  条目 {item['mp_name']} · «{item['title']}» · {item['url']}")
    return 0


def _rows_of(feed_path: Path):
    text = feed_path.read_text(encoding="utf-8")
    if feed_path.suffix in {".atom", ".xml"} or text.lstrip().startswith("<"):
        root = ET.fromstring(text)
        rows = []
        for entry in root.iter(ATOM + "entry"):
            link = entry.find(ATOM + "link")
            author = entry.find(ATOM + "author/" + ATOM + "name")
            content = entry.find(ATOM + "content")
            rows.append(
                {
                    "url": link.get("href") if link is not None else None,
                    "title": (entry.findtext(ATOM + "title") or "").strip(),
                    "published_at": (entry.findtext(ATOM + "updated") or entry.findtext(ATOM + "published") or "").strip(),
                    "content_html": content.text if content is not None else None,
                    "mp_name": (author.text or "").strip() if author is not None else None,
                }
            )
        return rows
    payload = json.loads(text)
    items = payload.get("items") if isinstance(payload, dict) else payload
    account = payload.get("mp_name") if isinstance(payload, dict) else None
    return [
        {
            "url": item.get("url") or item.get("link"),
            "title": item.get("title"),
            "published_at": item.get("published_at") or item.get("pubDate"),
            "content_html": item.get("content_html") or item.get("content"),
            "mp_name": item.get("mp_name") or item.get("mpName") or item.get("author") or account,
        }
        for item in (items or [])
        if isinstance(item, dict)
    ]


def verify(feed_path: Path) -> int:
    rows = _rows_of(feed_path)
    bad = [row for row in rows if (urlsplit(row["url"] or "").hostname or "").lower() != WECHAT_HOST]
    no_body = [row for row in rows if not (row.get("content_html") or "").strip()]
    print(f"{feed_path.name}: {len(rows)} 条条目")
    for row in rows:
        host = urlsplit(row["url"] or "").hostname or "（无 URL）"
        body = len(row.get("content_html") or "")
        print(f"  {'✔' if host == WECHAT_HOST else '✘'} {host:<18} 正文 {body:>5} 字节  «{(row.get('title') or '')[:34]}»")
        print(f"      {row['url']}")
    print(f"非 mp.weixin.qq.com 的条目：{len(bad)} · 正文为空的条目：{len(no_body)}")
    return 1 if (bad or no_body) else 0


def split(feed_path: Path, out: Path) -> int:
    rows = _rows_of(feed_path)
    out.mkdir(parents=True, exist_ok=True)
    groups = {}
    for row in rows:
        groups.setdefault(row.get("mp_name") or "未知公众号", []).append(row)
    if len(groups) < 2:
        print(f"这个 feed 只有 {len(groups)} 个公众号，不需要拆分")
    for account, account_rows in groups.items():
        payload = {
            "title": account,
            "mp_name": account,
            "items": [
                {"url": row["url"], "title": row["title"], "published_at": row["published_at"], "content_html": row["content_html"]}
                for row in account_rows
            ],
        }
        path = out / f"feed-{account_slug(account)}.json"
        path.write_bytes(json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8") + b"\n")
        print(f"  {account:<20} → {path.name}（{len(account_rows)} 条）")
    print(f"拆成 {len(groups)} 个 feed：每个公众号一个来源，候选不会混成一条")
    return 0


def serve(directory: Path, port: int) -> int:
    handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(*args, directory=str(directory), **kwargs)  # noqa: E731
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"serving {directory} on http://127.0.0.1:{port}/ （Ctrl+C 结束）")
    httpd.serve_forever()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wewe_feed_tool.py")
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="用逐字留存的原件造 feed")
    build_parser.add_argument("--out", required=True)
    build_parser.add_argument("--include-non-wechat", action="store_true", help="把非公众号的官网原件也放进 feed（用来验证产品会拒绝）")
    split_parser = sub.add_parser("split", help="按公众号拆分多账号 feed")
    split_parser.add_argument("--feed", required=True)
    split_parser.add_argument("--out", required=True)
    verify_parser = sub.add_parser("verify", help="校验条目 URL 是真实公众号原文")
    verify_parser.add_argument("--feed", required=True)
    serve_parser = sub.add_parser("serve", help="回环 serve feed 目录")
    serve_parser.add_argument("--dir", required=True)
    serve_parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args(argv[1:])
    if args.command == "build":
        return build(Path(args.out).resolve(), include_non_wechat=args.include_non_wechat)
    if args.command == "split":
        return split(Path(args.feed).resolve(), Path(args.out).resolve())
    if args.command == "verify":
        return verify(Path(args.feed).resolve())
    return serve(Path(args.dir).resolve(), args.port)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))