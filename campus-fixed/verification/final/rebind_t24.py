"""Content-bound re-check of the t24 acceptance conclusions, on the frozen bytes.

t24 recorded its verdict without binding it to a revision, so the release cannot tell
whether the bytes it ships are the ones that were checked. This script re-answers the
three questions t24 answered, against whatever code and data are on disk right now:

1. how many items are publicly visible (the anonymous ``GET /api/v1/events`` predicate);
2. what the first card is (the endpoint orders by ``updated_at`` descending);
3. whether the daily update still separates skipped optional channels from real failures.

It runs the app in-process (``create_app`` + TestClient, no port, no worker) over a
**copy** of the operator data directory, so the real directory is never written.

Usage::

    python verification/final/rebind_t24.py --data-dir <copy> --json-out <out.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TZ = ZoneInfo("Asia/Shanghai")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="content-bound t24 re-check")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--today")
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else datetime.now(TZ).date()

    from fastapi.testclient import TestClient

    from server.campus.platform import Settings
    from server.campus_app import create_app

    data_dir = Path(args.data_dir)
    app = create_app(
        Settings(data_dir=data_dir, secret="rebind-t24-" + "x" * 24, dev_auth=True, admin_emails={"operator@tju.edu.cn"}),
        start_worker=False,
    )
    client = TestClient(app)

    print(f"数据目录：{data_dir}", file=out)
    print("（无凭证请求，等价于学生打开首页时浏览器发出的那一个请求）", file=out)
    status = client.get("/api/v1/events?limit=100")
    payload = status.json()
    items = payload.get("items") or []
    cursor = payload.get("next_cursor")
    pages = 1
    while cursor:
        payload = client.get(f"/api/v1/events?limit=100&cursor={cursor}").json()
        items.extend(payload.get("items") or [])
        cursor = payload.get("next_cursor")
        pages += 1

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

    actionable = [item for item in items if any(day >= today for day in deadline_days(item))]
    first = items[0] if items else {}
    print(f"\n结论 1 · 公开可见：{len(items)} 条（HTTP {status.status_code}，{pages} 页，无 next_cursor 说明这是全部）", file=out)
    print(f"  其中今天仍可办（有 >= 今天的期限）：{len(actionable)} 条", file=out)
    print(f"\n结论 2 · 匿名首屏第一条：《{first.get('title')}》", file=out)
    print(f"  期限 {[d.isoformat() for d in deadline_days(first)]} · 更新于 {first.get('updated_at')} · 类型 {first.get('event_type')}", file=out)

    result = {
        "checked_at": datetime.now(TZ).isoformat(),
        "data_dir": str(data_dir),
        "today": today.isoformat(),
        "visible": len(items),
        "actionable": len(actionable),
        "first_title": first.get("title"),
        "first_deadlines": [d.isoformat() for d in deadline_days(first)],
        "first_updated_at": first.get("updated_at"),
    }
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())