#!/usr/bin/env python
"""把一篇公众号文章变成「可复核的候选」——一步做完，而且不许伪造。

运营者从微信里复制一篇通知的链接（或自己在浏览器里打开后另存的那份页面），
这个命令做三件事：

1. **先试自动**：用**产品自己的** ``SafeFetcher`` 抓那个 URL（受信网关照旧，
   地址安全判定一行不改）。抓到的**是通知正文**才算成功；抓到微信的验证页
   （请求被重定向离开文章地址）就**如实报失败**，退非零并告诉你为什么。
2. **逐字留痕**：能拿到的字节原样存下来（``sha256`` 命名的归档文件 + 索引行），
   记清是「机器抓的」还是「运营者浏览器里那份」。
3. **进复核队列**：写成中继既有的暂存格式（``<column>/<slug>.txt`` +
   ``.meta.json``），由 ``relay stage/build`` 生成 feed；``--ingest`` 再把 feed
   经本地回环口送进产品（产品用它**自己的** SafeFetcher 复核取回），
   于是队列里出现一条待人工复核的候选，证据链指向 mp.weixin 原文。

**不做的事（写死在文档里，也写在这里）**：不模拟微信登录、不破解/绕过验证码、
不抓朋友圈/群消息/需登录页面；不伪造 UA 或 cookie；取不到就报取不到——
绝不用近似内容或别的来源顶替。这个产品最不能丢的是「每条都能回到原文核对」。

用法::

    # 只试自动（通常会被微信拒绝，这就是结论）
    python scripts/relay_stage.py https://mp.weixin.qq.com/s/XXXXXXXX

    # 运营者自己的浏览器副本（另存的 .html 或纯文本），逐字留痕并暂存
    python scripts/relay_stage.py https://mp.weixin.qq.com/s/XXXXXXXX \
        --copy "%USERPROFILE%\\Downloads\\通知.html" --column oaa-competitions \
        --title "2026年下半年全国大学英语四、六级考试报名通知" --published-at 2026-09-14

    # 送进复核队列（需要本机运营实例在跑）
    python scripts/relay_stage.py https://mp.weixin.qq.com/s/XXXXXXXX \
        --copy <你那份> --column oaa-competitions --ingest --api http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RELAY_PATH = ROOT / "deploy" / "operator" / "relay.py"
DEFAULT_ARCHIVE_DIR = "wechat-staged"
WECHAT_HOST = "mp.weixin.qq.com"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3  # the product fetched something that is not the article (anti-scraping page)
EXIT_INGEST = 4  # staging worked, the ingest leg failed


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay = load_module("operator_relay", RELAY_PATH)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def normalized_title(value: str) -> str:
    return " ".join((value or "").split())


def title_from_html(raw: bytes) -> str:
    """The page's own title: <h1>, then og:title, then <title>."""
    try:
        from server.campus.sources import decode_body

        text = decode_body(raw, "text/html")
    except Exception:  # noqa: BLE001 - script keeps working without the product import
        text = raw.decode("utf-8", "replace")
    for pattern in (r"<h1[^>]*>(.*?)</h1>", r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                    r"<title[^>]*>(.*?)</title>"):
        match = re.search(pattern, text, re.S | re.I)
        if match:
            title = normalized_title(re.sub(r"<[^>]+>", "", match.group(1)))
            if title:
                return title[:300]
    return ""


def body_paragraphs(raw: bytes, is_html: bool) -> list[str]:
    """Paragraph blocks in the order the page shows them, nothing summarised."""
    if not is_html:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", "replace")
        return [block.strip() for block in text.split("\n\n") if block.strip()]

    from server.campus.sources import decode_body

    text = decode_body(raw, "text/html")
    body = re.search(r"<(?:div[^>]+id=[\"']js_content[\"']|article|main)\b.*?</(?:div|article|main)>",
                     text, re.S | re.I)
    fragment = body.group(0) if body else text
    blocks = re.split(r"</(?:p|div|li|h[1-6]|section|br)\s*>", fragment, flags=re.I)
    cleaned = []
    for block in blocks:
        plain = normalized_title(re.sub(r"<[^>]+>", " ", html_module.unescape(block)))
        if len(plain) >= 2:
            cleaned.append(plain)
    return cleaned


def classify_fetch(result) -> tuple[bool, str, str]:
    """``(usable, reason, code)`` for one SafeFetcher result."""
    from server.campus.sources import WECHAT_ARTICLE, decode_body

    code = ""
    if result is None:
        return False, "没有取到任何字节", "no_result"
    final = getattr(result, "url", "") or ""
    request_url = getattr(result, "requested_url", "") or ""
    if request_url and final and final.split("#")[0] != request_url.split("#")[0]:
        code = "article_redirected_off_pattern"
    text = ""
    try:
        text = decode_body(result.body, result.content_type)
    except Exception:  # noqa: BLE001
        text = ""
    if not code and ("完成验证后继续访问" in text or "wappoc_appmsgcaptcha" in final):
        code = "article_redirected_off_pattern"
    if not code and WECHAT_ARTICLE.match(final or request_url or ""):
        # A WeChat URL that answered with almost no text is an interstitial, not a notice.
        if len(re.sub(r"<[^>]+>", "", text).strip()) < 200:
            code = "html_access_interstitial"
    if code:
        return False, f"取回的不是通知正文（{code}）；页面：{final or request_url}", code
    return True, "取回成功", ""


def trust_wechat_origin() -> list[str]:
    """Name mp.weixin.qq.com in the trusted gateway, exactly like the collectors do.

    The daily collectors add ``https://mp.weixin.qq.com`` to
    ``CAMPUS_TRUSTED_GATEWAY_ORIGINS`` so a machine behind a fake-IP proxy can still
    *attempt* the article and get the product's real answer (the anti-scraping page)
    instead of a local DNS artifact. Nothing else about the address checks changes.
    """
    from scripts.campus_collect import EXTRA_GATEWAY_ORIGINS, gateway_origins_from_config

    configured = os.environ.get("CAMPUS_TRUSTED_GATEWAY_ORIGINS", "")
    origins = [item.strip() for item in configured.split(",") if item.strip()]
    for origin in list(gateway_origins_from_config()) + list(EXTRA_GATEWAY_ORIGINS):
        if origin not in origins:
            origins.append(origin)
    os.environ["CAMPUS_TRUSTED_GATEWAY_ORIGINS"] = ",".join(origins)
    return origins


def archive(archive_dir: Path, raw: bytes, meta: dict) -> tuple[Path, str]:
    """Store the verbatim bytes under their own sha256 and append one index line."""
    digest = sha256_bytes(raw)
    archive_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".html" if meta.get("is_html") else ".txt"
    body_path = archive_dir / f"{digest}{suffix}"
    if not body_path.exists():
        body_path.write_bytes(raw)
    meta_path = archive_dir / f"{digest}.json"
    record = {**meta, "sha256": digest, "bytes": len(raw), "archived_body": body_path.name}
    meta_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    with (archive_dir / "index.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return body_path, digest


def slugify(title: str, fallback: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:60] or fallback).strip()


def write_staging(column: str, slug: str, paragraphs: list[str], meta: dict, force: bool) -> tuple[Path, Path]:
    directory = relay.STAGING_DIR / column
    directory.mkdir(parents=True, exist_ok=True)
    text_path = directory / f"{slug}.txt"
    meta_path = directory / f"{slug}.meta.json"
    if text_path.exists() and not force:
        raise SystemExit(f"暂存已存在：{text_path}（要覆盖加 --force；不改内容是对的默认）")
    text_path.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8", newline="\n")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return text_path, meta_path


def start_feed_server(port: int) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, str(RELAY_PATH), "serve", "--port", str(port)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(2.0)
    return process


def candidate_titles(api: str, email: str, limit: int = 8) -> list[dict]:
    session = relay.AdminSession(api, email)
    session.login()
    rows = []
    cursor = ""
    while len(rows) < limit:
        page = session.get(f"/api/v1/admin/reviews?limit=100" + (f"&cursor={cursor}" if cursor else ""))
        for candidate in page.get("items") or []:
            rows.append({"title": candidate.get("title"), "candidate_id": candidate.get("candidate_id"),
                         "status": candidate.get("review_status")})
            if len(rows) >= limit:
                break
        cursor = page.get("next_cursor") or ""
        if not cursor:
            break
    return rows


def wait_for_candidate(api: str, email: str, title: str, feed_port: int, timeout: float) -> dict:
    """Keep the feed up until the product's *queued* fetch has run.

    ``POST /admin/sources/{id}/fetch`` answers 202 and the worker runs later, so a tool
    that stops the loopback feed right after registering leaves the product fetching a
    dead origin. Poll the relay source health and the review queue instead.
    """
    session = relay.AdminSession(api, email)
    session.login()
    deadline = time.monotonic() + timeout
    seen = {"sources": [], "candidate": None}
    while time.monotonic() < deadline:
        try:
            sources = session.get("/api/v1/admin/sources?limit=100").get("items") or []
        except Exception:  # noqa: BLE001
            sources = []
        seen["sources"] = [
            {
                "name": item.get("name"),
                "health": item.get("health"),
                "last_success": item.get("last_success"),
                "item_count": item.get("item_count"),
            }
            for item in sources
            if str(item.get("source_identity") or "").startswith("relay-")
        ]
        try:
            items = session.get("/api/v1/admin/reviews?limit=100").get("items") or []
        except Exception:  # noqa: BLE001
            items = []
        mine = [row for row in items if normalized_title(row.get("title") or "")[:24] == title[:24]]
        if mine:
            seen["candidate"] = {
                "candidate_id": mine[0].get("candidate_id"),
                "title": mine[0].get("title"),
                "review_status": mine[0].get("review_status"),
                "deadlines": [d.get("date") for d in (mine[0].get("deadlines") or [])],
            }
            break
        if any(row["health"] == "healthy" and row["last_success"] for row in seen["sources"]):
            # The source fetched, but this article may have been deduplicated; one more look.
            break
        time.sleep(3.0)
    return seen


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="relay_stage.py",
        description="把一篇公众号文章变成可复核候选（不模拟登录、不破验证码、取不到就报失败）",
        epilog="可用中继栏目：" + "、".join(sorted(relay.COLUMNS)),
    )
    parser.add_argument("url", help="文章 URL（运营者自己从微信里复制的那个）")
    parser.add_argument("--column", default="oaa-competitions", choices=sorted(relay.COLUMNS),
                        help="中继栏目（决定进哪个来源的复核队列）")
    parser.add_argument("--copy", help="运营者自己浏览器里那份（另存的 .html 或纯文本；- 表示从 stdin 读）")
    parser.add_argument("--title", help="标题（缺省时从副本里读；两边都没有就报错，不编造）")
    parser.add_argument("--published-at", help="原文发布日期 YYYY-MM-DD（不知道就别写）")
    parser.add_argument("--data-dir", default=os.environ.get("CAMPUS_DATA_DIR") or "data-operator",
                        help="归档目录所在的数据目录（默认 CAMPUS_DATA_DIR 或 data-operator）")
    parser.add_argument("--timeout", type=float, default=20.0, help="SafeFetcher 超时（秒）")
    parser.add_argument("--force", action="store_true", help="同名暂存已存在时覆盖")
    parser.add_argument("--slug", help="暂存文件名（缺省由标题生成；同名文章要并存时用它）")
    parser.add_argument("--ingest", action="store_true", help="暂存后把 feed 送进产品并抓成候选")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="--ingest 用的运营实例地址")
    parser.add_argument("--email", default="operator@tju.edu.cn",
                        help="--ingest 登录用的运营者邮箱（必须在该实例的 CAMPUS_ADMIN_EMAILS 里；"
                             "operator 模式默认 operator@touxian.local）")
    parser.add_argument("--feed-port", type=int, default=8791, help="--ingest 用的本地 feed 回环口")
    parser.add_argument("--wait-seconds", type=float, default=90.0,
                        help="--ingest 等产品把 feed 抓成候选的最长秒数（feed 要一直活着）")
    parser.add_argument("--dry-run", action="store_true", help="只报告会做什么，不写任何文件")
    args = parser.parse_args(argv)

    parsed = urlsplit(args.url)
    if parsed.scheme != "https" or not parsed.hostname:
        print(f"URL 必须是 https 且带主机名：{args.url}", file=sys.stderr)
        return EXIT_USAGE
    if relay._unsafe_host(parsed.hostname):
        print(f"URL 指向非公网主机，拒绝：{parsed.hostname}", file=sys.stderr)
        return EXIT_USAGE

    print(f"文章 URL：{args.url}")
    print(f"目标栏目：{args.column}（{relay.COLUMNS[args.column]['name']}）")
    print("")

    # --- 1. 自动那一半：产品自己的 SafeFetcher ---------------------------------
    from server.campus.sources import SafeFetcher

    origins = trust_wechat_origin()
    print(f"受信网关照旧只放行：{len(origins)} 个 origin（含 mp.weixin.qq.com，与日常采集一致）")

    auto_raw = auto_url = None
    auto_reason = "未尝试"
    try:
        fetcher = SafeFetcher(timeout=args.timeout)
        result = fetcher.fetch(args.url)
        usable, auto_reason, code = classify_fetch(result)
        if usable:
            auto_raw, auto_url = result.body, getattr(result, "url", args.url)
            print(f"[1/4] 自动抓取：成功（{len(auto_raw)} 字节）——本次不需要人工副本")
        else:
            print(f"[1/4] 自动抓取：**取不到正文**。{auto_reason}")
            print("      这是产品如实拒绝，不是失败重试的理由：没有正文就不入库。")
    except Exception as error:  # noqa: BLE001
        code = getattr(error, "code", None) or type(error).__name__
        auto_reason = f"{code}"
        print(f"[1/4] 自动抓取：**取不到正文**（{code}）。{error if str(error) else ''}".rstrip())

    # --- 2. 运营者自己的副本 ---------------------------------------------------
    raw = None
    how = ""
    if args.copy:
        if args.copy == "-":
            raw = sys.stdin.buffer.read()
            source_name = "stdin"
        else:
            path = Path(args.copy).expanduser()
            if not path.is_file():
                print(f"--copy 指向的文件不存在：{path}", file=sys.stderr)
                return EXIT_USAGE
            raw = path.read_bytes()
            source_name = str(path)
        if not raw.strip():
            print("--copy 给的内容是空的：拒绝入库（空内容不是通知）", file=sys.stderr)
            return EXIT_REFUSED
        how = f"operator-browser-copy:{source_name}"
        print(f"[2/4] 运营者副本：{source_name}（{len(raw)} 字节，逐字留痕）")
    elif auto_raw is not None:
        raw = auto_raw
        how = "safe-fetcher"
        print("[2/4] 使用自动抓到的字节（无需人工副本）")
    else:
        print("")
        print("结论：只有自动这一条路，而它取不到正文 → **不入库**。")
        print("做法：你在自己的浏览器里打开这篇通知、另存为 .html（或复制正文存成 .txt），")
        print("      然后用 --copy <那个文件> 再跑一次；命令会把那份内容逐字归档并留 sha256。")
        print(f"      拒绝原因：{auto_reason}")
        return EXIT_REFUSED

    is_html = raw.lstrip()[:1] == b"<" or (args.copy or "").lower().endswith((".html", ".htm"))
    title = normalized_title(args.title or title_from_html(raw))
    if not title:
        print("标题既没给（--title）也没能从副本里读出来：不编造，停止。", file=sys.stderr)
        return EXIT_USAGE
    paragraphs = body_paragraphs(raw, is_html)
    if not paragraphs:
        print("副本里没有可用的正文段落：不编造，停止。", file=sys.stderr)
        return EXIT_USAGE

    meta = {
        "url": args.url,
        "title": title,
        "published_at": args.published_at,
        "fetched_at": now_iso(),
        "fetched_by": how,
        "note": f"t29 relay_stage；{how}；产品自动抓取结果：{auto_reason}",
    }
    if args.dry_run:
        print(f"[3/4] --dry-run：会归档 {len(raw)} 字节、暂存 {len(paragraphs)} 段，不动磁盘")
        return EXIT_OK

    archive_dir = Path(args.data_dir).expanduser() / DEFAULT_ARCHIVE_DIR
    body_path, digest = archive(archive_dir, raw, {**meta, "is_html": is_html})
    print(f"[3/4] 逐字归档：{body_path}")
    print(f"      sha256={digest}（这份哈希就是这篇内容的身份，复核时可对）")

    slug = slugify(args.slug or title, digest[:12])
    text_path, meta_path = write_staging(args.column, slug, paragraphs, meta, args.force)
    print(f"      暂存：{text_path.name} + {meta_path.name}（{len(paragraphs)} 段）")
    written = relay.stage(relay.STAGING_DIR)
    built = relay.build(relay.STAGING_DIR, relay.OUT_DIR)
    print(f"      relay stage/build：{written} 条记录；feed {[k for k in built.get('feeds', {})]}")

    if not args.ingest:
        print("[4/4] 没有 --ingest：feed 已生成，还没进队列。要进队列：")
        print(f"      python deploy/operator/relay.py serve --port {args.feed_port}")
        print(f"      python deploy/operator/relay.py register --feed-origin http://127.0.0.1:{args.feed_port}")
        print("      （或直接加 --ingest，本命令会自己起 feed、注册、抓取）")
        return EXIT_OK

    # --- 3. 送进产品（产品用它自己的 SafeFetcher 取回回环 feed） ---------------
    server = start_feed_server(args.feed_port)
    try:
        feed_origin = f"http://127.0.0.1:{args.feed_port}"
        print(f"[4/4] 本地 feed：{feed_origin}（仅回环）")
        try:
            registered = relay.register(args.api, args.email, feed_origin, trigger=True)
        except SystemExit as error:
            print(f"      注册失败：{error}", file=sys.stderr)
            return EXIT_INGEST
        except Exception as error:  # noqa: BLE001
            print(f"      注册失败：{type(error).__name__}: {error}", file=sys.stderr)
            print(f"      最常见的原因是登录邮箱与实例的 CAMPUS_ADMIN_EMAILS 不一致"
                  f"（operator 模式默认 operator@touxian.local），或实例没开开发登录码。", file=sys.stderr)
            return EXIT_INGEST
        print(f"      注册：{registered.get('sources')}")
        seen = wait_for_candidate(args.api, args.email, title, args.feed_port, args.wait_seconds)
        print(f"      中继源状态：{seen['sources']}")
        if seen["candidate"]:
            print(f"      **候选已进复核队列**：{seen['candidate']}")
            return EXIT_OK
        print("      本次等待窗口内队列里没看到这篇文章：可能被去重（同一篇已在队列/已发布），")
        print("      或该实例的 worker 没跑起来。用 --wait-seconds 加长，或在实例的管理页核对。")
        return EXIT_INGEST
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())