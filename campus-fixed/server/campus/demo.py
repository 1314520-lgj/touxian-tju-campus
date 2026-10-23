"""Explicit, labeled synthetic demo. Never runs on ordinary startup."""

import uuid
from datetime import UTC, datetime, timedelta

from .delivery import TZ
from .schema import now_iso, validate
from .sources import FetchResult


def seed_demo(core):
    if core.settings.production or not core.settings.database_url.startswith("sqlite:"):
        raise ValueError("演示数据只允许写入本地 SQLite 环境")
    source_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "touxian-tju-synthetic-demo"))
    source = {
        "id": source_id,
        "name": "【演示】合成校园通知",
        "kind": "website",
        "verification": "unverified",
        "origin_url": "https://example.org/tju-demo",
        "adapter": "website",
        "source_identity": "synthetic-demo",
        "enabled": False,
        "health": "never_checked",
        "last_success_at": None,
        "last_checked_at": None,
        "verification_basis": None,
        "last_error_code": None,
    }
    validate("AdminSource", source)
    core.store.put("sources", source_id, source)
    core.pipeline.sources.seed()
    for registered in core.store.list("sources"):
        if registered["id"] != source_id:
            registered["enabled"] = False
            core.store.put("sources", registered["id"], registered)
    today = datetime.now(UTC).astimezone(TZ).date()
    samples = [
        ("奖学金材料提交", 3, "奖学金申请表、成绩单", "https://example.org/demo/scholarship"),
        ("校园竞赛报名", 5, "报名表、团队说明", "https://example.org/demo/competition"),
        ("选课确认", 7, "选课确认表", "https://example.org/demo/course"),
    ]
    for index, (title, days, materials, url) in enumerate(samples):
        day = (today + timedelta(days=days)).isoformat()
        text = f"全体本科生请于{day}前提交{materials}。办理入口：{url}。这是功能演示，不能用于真实办理。"
        html = f'<html><h1>【演示】{title}</h1><div class="v_news_content"><p>{text}</p></div></html>'.encode()
        with core.store.transaction():
            snapshot, _ = core.pipeline.sources.persist(
                source,
                FetchResult(f"https://example.org/tju-demo/notice-{index}", html, "text/html"),
                "【演示】" + title,
                now_iso(),
            )
        core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
        candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
        if candidate["review_status"] != "accepted":
            candidate = core.pipeline.review(
                candidate["candidate_id"],
                {
                    "expected_candidate_revision": candidate["revision"],
                    "decision": "accept",
                    "reason": "核对合成演示原文",
                    "replacement_candidate": None,
                },
                {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "demo-reviewer"))},
            )
        core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"])
    # A real deployment indexes a published event when the worker sees
    # public_event.changed. This fixture runs no worker, so it performs that one
    # step itself — otherwise the demo would truthfully report a stale search
    # index forever, and would never exercise the indexed search path.
    for event in core.store.list("events"):
        core.search.index_event({"subject": {"kind": "public_event", "id": event["id"], "revision": event["version"]}})
    core.store.put("runtime", "demo", {"synthetic": True, "created_at": now_iso()})
