"""t19：把 replay-rejections.json 转成一张可读的逐条判定表（Markdown）。

    python verification/redirect/make_verdict_table.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "verification" / "redirect"


def main() -> int:
    payload = json.loads((OUT / "replay-rejections.json").read_text(encoding="utf-8"))
    rows = payload["results"]
    sources = {}
    for row in rows:
        sources.setdefault(row["source_id"], row["source_id"])
    lines = [
        "# article_redirected_off_pattern 逐条判定（2026-10-21 07:30 那次无人值守运行）",
        "",
        f"- 重放窗口（UTC）：{payload['window'][0]} → {payload['window'][1]}（本机 07:30 的那次运行）",
        f"- 条目数：**{len(rows)}**；判定分布：{dict(Counter(row['verdict'] for row in rows))}",
        "- 判定依据：重定向后的**最终地址** + 落地页里的验证页文案（环境异常 / 完成验证后即可继续访问 / 去验证）",
        "",
        "| # | 来源 | 被请求的文章链接 | 重定向后的最终地址 | 落地页证据 | 判定 |",
        "|---|---|---|---|---|---|",
    ]
    for index, row in enumerate(rows, 1):
        final = row["final_url"] or row.get("fetch_error") or "-"
        evidence = "、".join(row["challenge_markers"]) or (f"抓取失败：{row['fetch_error']}" if row.get("fetch_error") else "-")
        lines.append(
            f"| {index} | {row['source_id'][:8]} | `{(row['url'] or '')[:70]}` | `{final[:78]}` | {evidence} | {row['verdict']} |"
        )
    lines += [
        "",
        "> 原始数据（含每条 220 字落地页文本、字节数、拦截页判定）：`replay-rejections.json`。",
        "> 重放用的是产品自己的 SafeFetcher（地址安全判定、逐跳复核、体积/超时限制一律不变），",
        "> 受信网关来自 `config/campus-sources.json`（等价 `--gateway-from-config`）。",
        "",
    ]
    path = OUT / "verdict-table.md"
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print("wrote", path.relative_to(REPO), f"({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())