"""Real restart persistence check for the single-user local profile.

Runs in two phases against a real server process:

    python scripts/verify_persistence.py write  <base>   # log in, create state
    python scripts/verify_persistence.py check  <base>   # after a restart: is it there?

The point is that a student closes the window, reopens it tomorrow, and their
todo, note and saved notice are still there.
"""

import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = sys.argv[2]
PHASE = sys.argv[1]
EMAIL = "operator@tju.edu.cn"
STATE = ".test-temp-persistence.json"


def call(method, path, body=None, headers=None, cookie=None):
    request = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, data, timeout=25) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def header(headers, name):
    try:
        value = headers.get(name)
    except AttributeError:
        value = None
    if value is not None:
        return value
    for key, candidate in (headers.items() if hasattr(headers, "items") else []):
        if key.lower() == name.lower():
            return candidate
    return None


def login():
    """Reuse the cached cookie like a browser would, so no code is spent.

    The development login code allows six per mailbox per hour with a 30-second
    cooldown, which a student closing and reopening quickly would hit. The
    session cookie survives a restart (the server secret lives in the data
    directory), so reusing it is both realistic and quiet.
    """
    cached = {}
    try:
        cached = json.loads(open(STATE, encoding="utf-8").read())
    except (OSError, ValueError):
        cached = {}
    if cached.get("cookie"):
        status, _, body = call("GET", "/api/v1/me", cookie=cached["cookie"])
        if status == 200:
            print("复用上次的登录（未消耗验证码）")
            return cached["cookie"], json.loads(body)["csrf_token"]

    status, headers, body = call("POST", "/api/v1/auth/challenges", {"email": EMAIL})
    if status == 429:
        sys.exit("验证码限流：请等 30 秒后重试（同一邮箱每小时最多 6 次）")
    if status != 201:
        sys.exit(f"登录失败：HTTP {status} {body[:160].decode('utf-8', 'replace')}")
    code = header(headers, "X-Dev-Login-Code")
    if not code:
        sys.exit("页面拿不到开发登录码——单机自用档没生效")
    status, headers, body = call(
        "POST", "/api/v1/auth/sessions", {"challenge_id": json.loads(body)["challenge_id"], "code": code}
    )
    if status != 200:
        sys.exit(f"建立会话失败：HTTP {status} {body[:160].decode('utf-8', 'replace')}")
    session = json.loads(body)
    cookie = (header(headers, "Set-Cookie") or "").split(";", 1)[0]
    return cookie, session["csrf_token"]


if PHASE == "write":
    cookie, csrf = login()
    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "persist-1"}
    print("登录成功")

    status, _, body = call("GET", "/api/v1/events?limit=1")
    event = (json.loads(body).get("items") or [None])[0]
    if not event:
        sys.exit("库里没有已发布事项，无法验证收藏")
    event_id = event["id"]
    state = call("GET", f"/api/v1/me/events/{event_id}/state", cookie=cookie)
    current = json.loads(state[2]).get("state") or {}
    status, _, body = call(
        "PATCH",
        f"/api/v1/me/events/{event_id}/state",
        {
            "expected_state_revision": current.get("state_revision", 0),
            "acknowledged_event_version": event.get("version", 1),
            "saved": True,
        },
        headers,
        cookie,
    )
    print("收藏事项 ->", status)

    status, _, body = call(
        "POST",
        "/api/v1/me/items",
        {
            "title": "重启验证：明天交实验报告",
            "action_steps": ["写完报告", "交给助教"],
            "materials": ["实验报告"],
            "entry_urls": [],
            "deadline_date": "2026-11-20",
            "deadline_at": None,
            "timezone": "Asia/Shanghai",
            "evidence_excerpt": None,
            "excerpt_sync_consent": False,
        },
        headers,
        cookie,
    )
    created = json.loads(body) if status == 201 else {}
    print("新建个人事项 ->", status, created.get("item_id") or created.get("id"))
    with open(STATE, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {
                "event_id": event_id,
                "item_id": created.get("item_id") or created.get("id"),
                "title": event["title"],
                "cookie": cookie,
            },
            handle,
            ensure_ascii=False,
        )
    print("写入阶段完成")

else:
    cookie, csrf = login()
    saved = json.loads(open(STATE, encoding="utf-8").read())
    ok = True

    status, _, body = call("GET", "/api/v1/me/items", cookie=cookie)
    items = json.loads(body).get("items") or []
    mine = [item for item in items if (item.get("item_id") or item.get("id")) == saved["item_id"]]
    print(f"重启后个人事项 -> {'还在' if mine else '丢失'}")
    ok = ok and bool(mine)

    status, _, body = call("GET", f"/api/v1/me/events/{saved['event_id']}/state", cookie=cookie)
    state = (json.loads(body).get("state") or {}) if status == 200 else {}
    print(f"重启后收藏状态 -> saved={state.get('saved')}")
    ok = ok and state.get("saved") is True

    print("结论：", "重启后数据完好" if ok else "重启后数据丢失")
    sys.exit(0 if ok else 1)