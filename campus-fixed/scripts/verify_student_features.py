"""Exercise every student-facing feature against the real data instance.

Not a unit test: this drives the running product over HTTP exactly as the web
client does — register, set a profile, read the feed, open a notice, search, ask
a question, save it, make it a to-do, get a calendar file, change preferences —
and reports what actually works. Anything that fails is a real defect.
"""

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8160"
# Any mailbox works in operator mode. Override it when the default one is rate
# limited: the development login code allows 6 per mailbox per hour.
EMAIL = sys.argv[2] if len(sys.argv) > 2 else "operator@tju.edu.cn"

results = []


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


def header(headers, name, default=None):
    """HTTP header lookup is case-insensitive on the wire.

    ``dict(headers)`` keeps the server's own casing, and the product emits the
    development login code as lowercase ``x-dev-login-code`` — an exact
    ``.get("X-Dev-Login-Code")`` silently returns None and the session request
    then fails with 422. ``HTTPMessage.get`` is case-insensitive; fall back to a
    manual scan for plain dicts.
    """
    try:
        value = headers.get(name)
    except AttributeError:
        value = None
    if value is not None:
        return value
    for key, candidate in (headers.items() if hasattr(headers, "items") else []):
        if key.lower() == name.lower():
            return candidate
    return default


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


# 1. login -------------------------------------------------------------------
# The development login code is limited to 6 per mailbox per hour, so reuse a
# cached cookie when it still validates instead of spending a code every run.
session_cache = Path(tempfile.gettempdir()) / "campus-student-check-session.json"
cookie = None
csrf = None
if session_cache.is_file():
    stored = json.loads(session_cache.read_text(encoding="utf-8"))
    status, _, body = call("GET", "/api/v1/me", cookie=stored.get("cookie"))
    if status == 200:
        payload = json.loads(body)
        cookie, csrf = stored["cookie"], payload["csrf_token"]
        check("1. 学生登录（复用已验证会话）", True, payload["user"]["email"])

if cookie is None:
    status, headers, body = call("POST", "/api/v1/auth/challenges", {"email": EMAIL})
    if status == 429:
        print("登录码限流：请等一小时，或复用已有会话。")
        sys.exit(2)
    if status != 201:
        print("challenge failed:", status, body[:200].decode("utf-8", "replace"))
        sys.exit(1)
    code = header(headers, "X-Dev-Login-Code")
    challenge = json.loads(body)
    status, headers, body = call(
        "POST", "/api/v1/auth/sessions", {"challenge_id": challenge["challenge_id"], "code": code}
    )
    ok = check("1. 学生登录（邮箱验证码）", status == 200, f"HTTP {status}")
    if not ok:
        sys.exit(1)
    session = json.loads(body)
    cookie = (header(headers, "Set-Cookie", "") or "").split(";", 1)[0]
    csrf = session["csrf_token"]
    session_cache.write_text(
        json.dumps({"cookie": cookie}, ensure_ascii=False), encoding="utf-8", newline="\n"
    )
    check("1b. 拿到用户身份", bool(session.get("user", {}).get("id")), session["user"]["email"])

auth = {"X-CSRF-Token": csrf}
write_auth = {"X-CSRF-Token": csrf, "Idempotency-Key": "check-" + csrf[:12]}

# 2. catalog + profile --------------------------------------------------------
status, _, body = call("GET", "/api/v1/catalog", cookie=cookie)
catalog = json.loads(body) if status == 200 else {}
colleges = catalog.get("colleges") or []
check("2. 学院目录可读", status == 200 and bool(colleges), f"{len(colleges)} 个学院")

profile_body = {
    # The frozen contract requires every one of these fields.
    "college_id": colleges[0]["id"] if colleges else None,
    "major": None,
    "education_level": "undergraduate",
    "entry_year": 2024,
    "campus": "beiyangyuan",
    "interests": [],
}
status, _, body = call("PUT", "/api/v1/me/profile", profile_body, write_auth, cookie)
check("3. 保存学生画像（学院/年级/校区）", status in (200, 201), f"HTTP {status} {body[:120].decode('utf-8','replace')}")
status, _, body = call("GET", "/api/v1/me/profile", cookie=cookie)
saved = json.loads(body) if status == 200 else {}
check("3b. 画像读回一致", saved.get("entry_year") == 2024 and saved.get("campus") == "beiyangyuan",
      json.dumps(saved, ensure_ascii=False)[:120])

# 4. feed --------------------------------------------------------------------
status, _, body = call("GET", "/api/v1/me/feed?limit=20", cookie=cookie)
feed = json.loads(body) if status == 200 else {}
items = feed.get("items") or []
check("4. 今日清单可读（登录后个性化）", status == 200 and bool(items), f"{len(items)} 条")
sample = items[0] if items else None
if sample:
    check("4b. 条目带分类与截止信息", "classification" in sample,
          f"classification={sample.get('classification')} deadline={sample.get('deadline_label')}")

# 5. public events + detail ---------------------------------------------------
status, _, body = call("GET", "/api/v1/events?limit=5")
public = json.loads(body) if status == 200 else {}
pub_items = public.get("items") or []
check("5. 公开事项列表（未登录可看）", status == 200 and bool(pub_items), f"{len(pub_items)} 条")

event_id = pub_items[0]["id"] if pub_items else None
status, _, body = call("GET", f"/api/v1/events/{event_id}", cookie=cookie)
detail = json.loads(body) if status == 200 else {}
check("6. 事项详情可读", status == 200 and bool(detail.get("title")), (detail.get("title") or "")[:40])
evidence = detail.get("evidence") or []
check("6b. 详情带原文证据链", bool(evidence) and bool(evidence[0].get("source_url")),
      (evidence[0].get("source_url") if evidence else "none") or "none")

# 7. search -------------------------------------------------------------------
status, _, body = call("GET", "/api/v1/search?q=%E9%80%89%E6%8B%94", cookie=cookie)
search = json.loads(body) if status == 200 else {}
hits = search.get("hits") or []
check("7. 全文检索", status == 200 and bool(hits), f"{len(hits)} 条命中")

# 8. Q&A ----------------------------------------------------------------------
request_college = colleges[0]["id"] if colleges else None
status, _, body = call(
    "POST",
    "/api/v1/answers",
    {
        "question": "选博连读什么时候截止？",
        "education_level": "undergraduate",
        "college_id": request_college,
        "include_historical": False,
    },
    write_auth,
    cookie,
)
answer = json.loads(body) if status in (200, 201) else {}
check("8. 提问（有依据才回答）", status in (200, 201),
      f"HTTP {status} grounded={answer.get('grounded')} cites={len(answer.get('citations') or [])}")

def state_payload(headers_body):
    """The state endpoint answers {"state": {...}}; read it whichever shape it is."""
    if not isinstance(headers_body, dict):
        return {}
    nested = headers_body.get("state")
    return nested if isinstance(nested, dict) else headers_body


# 9. personal state: save + todo ----------------------------------------------
# The state PATCH is optimistic-concurrency protected: it needs the current
# state revision and the event version the student acknowledged.
status, _, body = call("GET", f"/api/v1/me/events/{event_id}/state", cookie=cookie)
current = state_payload(json.loads(body) if status == 200 else {})
state_body = {
    "expected_state_revision": current.get("state_revision", 0),
    "acknowledged_event_version": detail.get("version", 1),
    "saved": True,
}
status, _, body = call("PATCH", f"/api/v1/me/events/{event_id}/state", state_body, write_auth, cookie)
check("9. 收藏事项", status in (200, 204), f"HTTP {status} {body[:100].decode('utf-8','replace') if status >= 400 else ''}")
status, _, body = call("GET", f"/api/v1/me/events/{event_id}/state", cookie=cookie)
state = state_payload(json.loads(body) if status == 200 else {})
check("9b. 收藏状态已持久化", state.get("saved") is True, json.dumps(state, ensure_ascii=False)[:110])

status, _, body = call(
    "PATCH",
    f"/api/v1/me/events/{event_id}/state",
    {
        "expected_state_revision": state.get("state_revision", 0),
        "acknowledged_event_version": detail.get("version", 1),
        "state": "done",
        "read": True,
    },
    write_auth,
    cookie,
)
check("9c. 标记完成（待办）", status in (200, 204),
      f"HTTP {status} {body[:100].decode('utf-8','replace') if status >= 400 else ''}")

# 10. personal items ---------------------------------------------------------
draft = {
    # PersonalItemDraft is flat: one optional deadline, not a list.
    "title": "自查：交实验报告",
    "action_steps": ["写完实验报告", "交到教务处"],
    "materials": ["实验报告"],
    "entry_urls": [],
    "deadline_date": "2026-11-20",
    "deadline_at": None,
    "timezone": "Asia/Shanghai",
    "evidence_excerpt": None,
    "excerpt_sync_consent": False,
}
status, _, body = call("POST", "/api/v1/me/items", draft, write_auth, cookie)
created = json.loads(body) if status in (200, 201) else {}
item_id = created.get("item_id") or created.get("id")
check("10. 新建个人事项", status in (200, 201) and bool(item_id),
      f"HTTP {status} {body[:110].decode('utf-8','replace') if status >= 400 else ''}")
if item_id:
    status, _, body = call("GET", "/api/v1/me/items", cookie=cookie)
    listed = json.loads(body) if status == 200 else {}
    mine = listed.get("items") or []
    check("10b. 个人事项列表可见", any((i.get("item_id") or i.get("id")) == item_id for i in mine), f"{len(mine)} 条")

# 11. calendar ---------------------------------------------------------------
status, _, body = call("POST", "/api/v1/me/calendar-token", {}, write_auth, cookie)
token_body = json.loads(body) if status in (200, 201) else {}
token = token_body.get("token") or token_body.get("calendar_token")
check("11. 生成日历订阅链接", status in (200, 201) and bool(token), f"HTTP {status}")
if token:
    status, _, body = call("GET", f"/api/v1/calendar/{token}.ics")
    ics = body.decode("utf-8", "replace")
    check("11b. ICS 日历可下载且含事件", status == 200 and "BEGIN:VCALENDAR" in ics and "BEGIN:VEVENT" in ics,
          f"{len(ics)} 字节, {ics.count('BEGIN:VEVENT')} 个事件")

# 12. preferences + push ------------------------------------------------------
status, _, body = call("GET", "/api/v1/me/preferences", cookie=cookie)
prefs = json.loads(body) if status == 200 else {}
check("12. 提醒偏好可读", status == 200, json.dumps(prefs, ensure_ascii=False)[:110])
status, _, body = call("GET", "/api/v1/me/push-status", cookie=cookie)
check("12b. 推送状态可读", status == 200, body[:100].decode("utf-8", "replace"))

# 13. subscriptions -----------------------------------------------------------
status, _, body = call("GET", "/api/v1/me/subscriptions", cookie=cookie)
subs = json.loads(body) if status == 200 else {}
check("13. 订阅设置可读", status == 200, json.dumps(subs, ensure_ascii=False)[:110])

# summary --------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} 项通过 =====")
failed = [name for name, ok, _ in results if not ok]
if failed:
    print("失败项：", "、".join(failed))
sys.exit(0 if passed == len(results) else 1)
