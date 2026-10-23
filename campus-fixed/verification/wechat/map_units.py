"""t29 measurement: which units' WeChat posts also appear on the unit's own site?

Two independent measurements, both read-only:

1. **What our own backlog already sees**: count the ``mp.weixin.qq.com`` items the
   registered sources discover, group them by source, and show the error codes the
   fetch produced. This is measured from the product's own store, not guessed.
2. **Does the same notice exist on the unit's official site?**: take real WeChat
   article titles (the ones the operator relayed in the round-14 cycle, kept verbatim
   under ``deploy/operator/staging/``) and look for the same notice on the official
   columns this product can already fetch.

Usage::

    python verification/wechat/map_units.py --data-dir data-operator
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
UA = {"User-Agent": "Mozilla/5.0 (compatible; tju-campus-source-check)"}
WECHAT = re.compile(r"^https?://mp\.weixin\.qq\.com/s(?:[/?#]|$)")

# Columns the product already registers or can reach; the mapping test asks whether a
# WeChat notice showed up on one of them.
OFFICIAL_COLUMNS = {
    "天津大学新工科教育创新中心 · 竞赛通知": "https://eee.tju.edu.cn/tztg/tz.htm",
    "天津大学教务处 · 竞赛科创通知": "https://oaa.tju.edu.cn/tzgg/js_kc.htm",
    "天津大学教务处 · 学生竞赛": "https://oaa.tju.edu.cn/cxsj/xsjs.htm",
    "天津大学教务处 · 通知公告总览": "https://oaa.tju.edu.cn/tzgg.htm",
    "天津大学本科招生网 · 通知公告": "https://zs.tju.edu.cn/ym21/bkzn/tzgg.htm",
}

# Real WeChat articles from the round-14 relay cycle (verbatim bodies kept in the repo).
WECHAT_SAMPLES = (
    ("2026年下半年全国大学英语四、六级考试报名通知", "https://mp.weixin.qq.com/s/CSniYqr1LfSYobIeCZA6nw",
     "deploy/operator/staging/oaa-competitions/四六级报名通知.txt", ("四、六级", "四六级")),
    ("以代码重写生命的语法！“AI+合成生物”创新大赛（2026）报名通道开启",
     "https://mp.weixin.qq.com/s/G1WO_Pd68kpuBbS7MTMgvg",
     "deploy/operator/staging/oaa-competitions/AI合成生物大赛.txt", ("合成生物",)),
    ("天津大学2025年强基计划校考工作安排", "https://mp.weixin.qq.com/s/（中继周期·招生网同题）",
     "deploy/operator/staging/zs-notices/强基计划校考工作安排.txt", ("强基计划",)),
    ("天津大学2025年本科生招生章程", "https://mp.weixin.qq.com/s/（中继周期·招生网同题）",
     "deploy/operator/staging/zs-notices/本科生招生章程.txt", ("招生章程",)),
)


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch(url, timeout=25):
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.url, response.read(600000)


def decode(raw):
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def texts_in(html):
    """Every anchor as (text, href) plus the page's own text, whitespace-normalised."""
    plain = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    anchors = [
        (" ".join(re.sub(r"<[^>]+>", "", label).split()), href)
        for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S | re.I)
    ]
    return plain, anchors


def main(argv=None, out=None):
    out = out or sys.stdout
    args = parser.parse_args(argv or sys.argv[1:])
    daily = load("daily_update")
    collect = daily.collect

    restore = daily.prepare_readonly_secret(ROOT / args.data_dir)
    try:
        core = collect.build_core(ROOT / args.data_dir, timeout=20)
        from server.campus.ingestion import scope as backlog_scope

        sources = {s["id"]: s for s in core.store.list("sources")}
        by_identity = {s.get("source_identity"): s for s in core.store.list("sources")}
        by_scope = {backlog_scope(s): s for s in core.store.list("sources")}
        per_source = defaultdict(Counter)
        per_source_titles = defaultdict(list)
        for row in core.store.list("source_ingestion"):
            source = (
                sources.get(row.get("source_id") or "")
                or by_identity.get(row.get("source_id") or "")
                or by_scope.get(row.get("scope") or "")
                or {}
            )
            label = source.get("name") or row.get("source_id") or "（未知来源）"
            for item in (row.get("items") or {}).values():
                url = item.get("url") or ""
                if WECHAT.match(url):
                    status = item.get("status") or "?"
                    code = item.get("error_code") or ""
                    per_source[label][f"{status}{'/' + code if code else ''}"] += 1
                    if len(per_source_titles[label]) < 4:
                        per_source_titles[label].append((item.get("title") or "")[:64])

        print("=== 一、产品自己的 backlog 里已经看到的公众号链接（按来源）===", file=out)
        total = 0
        for label, counter in sorted(per_source.items(), key=lambda kv: -sum(kv[1].values())):
            n = sum(counter.values())
            total += n
            print(f"  {label:<34} 公众号条目 {n:>3}  状态 {dict(counter)}", file=out)
            for title in per_source_titles[label][:2]:
                print(f"        例：{title}", file=out)
        print(f"  合计：{total} 条公众号链接出现在已注册来源的列表里", file=out)

        print("\n=== 二、这些公众号文章的同题通知是否在同单位官网上 ===", file=out)
        pages = {}
        for label, url in OFFICIAL_COLUMNS.items():
            try:
                status, final, raw = fetch(url)
            except urllib.error.HTTPError as error:
                print(f"  {label}: HTTP {error.code}（取不到，无法作为映射证据）", file=out)
                continue
            except Exception as error:  # noqa: BLE001
                print(f"  {label}: {type(error).__name__}", file=out)
                continue
            html = decode(raw)
            plain, anchors = texts_in(html)
            pages[label] = (url, plain, anchors)
            print(f"  {label}: HTTP {status} bytes={len(raw)} 链接 {len(anchors)} 个", file=out)

        print("", file=out)
        mapped, unmapped = [], []
        for title, url, copy_path, needles in WECHAT_SAMPLES:
            native_hits, index_hits = [], []
            for label, (_url, plain, anchors) in pages.items():
                for needle in needles:
                    if needle not in plain:
                        continue
                    pairs = [(a, href) for a, href in anchors if needle in a]
                    native = [(a, href) for a, href in pairs if "mp.weixin.qq.com" not in href]
                    indexed = [(a, href) for a, href in pairs if "mp.weixin.qq.com" in href]
                    if native:
                        native_hits.append((label, needle, native[0]))
                    if indexed:
                        index_hits.append((label, needle, indexed[0]))
                    break
            line = f"  《{title}》 公众号 {url}"
            print(line, file=out)
            print(f"     仓库内的逐字取证副本：{copy_path}"
                  f"（{'存在' if (ROOT / copy_path).is_file() else '缺失'}）", file=out)
            for label, needle, (anchor, href) in native_hits:
                print(f"     ✓✓ 官网**原生正文**同题：{label}（命中「{needle}」）", file=out)
                print(f"          {anchor[:64]}", file=out)
                print(f"          {href[:110]}", file=out)
            for label, needle, (anchor, href) in index_hits:
                print(f"     ✓ 官网**只是收录/转载了公众号链接**（正文仍在公众号）：{label}（命中「{needle}」）", file=out)
                print(f"          {anchor[:64]} → {href[:90]}", file=out)
            if not native_hits and not index_hits:
                print("     ✗ 在本次探测的官网栏目里没有命中", file=out)
            (mapped if native_hits else unmapped).append((title, native_hits, index_hits))
        print("", file=out)
        print(f"  官网有**原生正文**对应的：{len(mapped)}/{len(WECHAT_SAMPLES)}", file=out)
        print(f"  官网只转载/收录公众号链接的："
              f"{sum(1 for _t, n, i in unmapped if i)}/{len(WECHAT_SAMPLES)}", file=out)
        print(f"  本次探测里两种都没有的："
              f"{sum(1 for _t, n, i in unmapped if not i)}/{len(WECHAT_SAMPLES)}", file=out)

        summary = {
            "wechat_items_seen": {label: dict(c) for label, c in per_source.items()},
            "wechat_items_total": total,
            "official_columns_reachable": {label: url for label, (url, _p, _a) in pages.items()},
            "native_mapped": [
                {"title": t, "columns": [h[0] for h in hits], "href": hits[0][2][1] if hits else None}
                for t, hits, _i in mapped
            ],
            "index_only": [t for t, _n, i in unmapped if i],
            "no_hit": [t for t, _n, i in unmapped if not i],
        }
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
        core.store.close()
        return 0
    finally:
        restore()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="t29 WeChat→official-site mapping measurement")
    parser.add_argument("--data-dir", default="data-operator")
    parser.add_argument("--json-out")
    sys.exit(main())