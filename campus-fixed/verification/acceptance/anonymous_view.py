"""Anonymous view: what a student actually sees, straight from the running product.

No session, no cookie, no token -- exactly what an unauthenticated visitor gets:
``GET /api/v1/events`` paged to the end. Prints the total, the first card, and how
many of the visible items still carry a deadline a student can act on today.

Usage::

    python verification/acceptance/anonymous_view.py --base http://127.0.0.1:8431
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def get(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def deadline_days(item):
    found = []
    for entry in item.get("deadlines") or []:
        value = entry.get("date") or (entry.get("at") or "")[:10]
        if not value:
            continue
        try:
            found.append(date.fromisoformat(value))
        except ValueError:
            continue
    return found


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="anonymous student view (read-only)")
    parser.add_argument("--base", default="http://127.0.0.1:8431")
    parser.add_argument("--today", help="YYYY-MM-DD (default Asia/Shanghai today)")
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else datetime.now(TZ).date()

    status, payload = get(f"{args.base}/api/v1/events?limit=100")
    items = payload.get("items") or []
    cursor = payload.get("next_cursor")
    pages = 1
    while cursor:
        status, payload = get(f"{args.base}/api/v1/events?limit=100&cursor={cursor}")
        items.extend(payload.get("items") or [])
        cursor = payload.get("next_cursor")
        pages += 1

    actionable = [item for item in items if any(day >= today for day in deadline_days(item))]
    first = items[0] if items else {}
    urls = sorted({entry.get("source_url") for entry in first.get("evidence") or [] if entry.get("source_url")})

    print(f"匿名视图（{args.base}/api/v1/events，不带任何凭证，共 {pages} 页）", file=out)
    print(f"可见事项：{len(items)} 条（HTTP {status}）", file=out)
    print(f"其中今天仍可办（有未来期限）：{len(actionable)} 条", file=out)
    print("", file=out)
    print("第一条（学生打开首页看到的第一张卡）：", file=out)
    print(f"  标题：{first.get('title')}", file=out)
    print(f"  期限：{[d.isoformat() for d in deadline_days(first)]} · 需要行动 {first.get('action_required')}", file=out)
    print(f"  原文：{urls[:1]}", file=out)
    print(f"  类型：{first.get('event_type')} · 更新于 {first.get('updated_at')}", file=out)
    print("", file=out)
    print("前 10 条标题（学生在首页/列表里依次看到的）：", file=out)
    for index, item in enumerate(items[:10], 1):
        days = [d.isoformat() for d in deadline_days(item)]
        print(f"  {index:>2}. {item.get('title')} · 期限 {days or '无'}", file=out)

    if args.json_out:
        from pathlib import Path

        Path(args.json_out).write_text(
            json.dumps(
                {
                    "base": args.base,
                    "today": today.isoformat(),
                    "visible": len(items),
                    "actionable": len(actionable),
                    "first": {
                        "title": first.get("title"),
                        "deadlines": [d.isoformat() for d in deadline_days(first)],
                        "event_type": first.get("event_type"),
                        "updated_at": first.get("updated_at"),
                        "urls": urls[:2],
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())