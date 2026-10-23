"""Isolated synthetic browser fixture for reliability acceptance; never external.

The extra mutation endpoints exist only in this test module, require an admin
session and normal CSRF/Origin validation, and are not installed by the product.
"""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Request

from server.campus.demo import seed_demo
from server.campus.platform import Settings, require_user
from server.campus.schema import now_iso, uid
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source
from server.campus_app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8027)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Loopback port required")
    if os.getenv("CAMPUS_ENV", "development").lower() in {"production", "prod"}:
        parser.error("Local test only")
    if Path(args.data_dir).exists():
        parser.error("Fixture directory must be fresh")
    for variable in (
        "CAMPUS_MEILI_URL",
        "CAMPUS_MEILI_KEY",
        "CAMPUS_PUBLIC_ORIGIN",
        "CAMPUS_GCS_BUCKET",
        "CAMPUS_PUSH_USER_ENDPOINTS",
        "CAMPUS_PUSH_ENDPOINTS",
    ):
        os.environ[variable] = ""
    os.environ["CAMPUS_BLOB_STORE"] = "local"
    app = create_app(
        Settings(
            data_dir=Path(args.data_dir),
            dev_auth=True,
            secret="synthetic-reliability-browser-" * 3,
            admin_emails={"demo-admin@tju.edu.cn"},
            allowed_origins=[f"http://127.0.0.1:{args.port}"],
        ),
        start_worker=False,
    )
    core = app.state.core
    seed_demo(core)
    source = synthetic_source(core)
    date = (datetime.now(UTC) + timedelta(days=7)).date().isoformat()
    actor = {"id": uid()}

    def publish(candidate):
        accepted = core.pipeline.review(
            candidate["candidate_id"],
            {
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "仅合成浏览器验收核对",
                "replacement_candidate": None,
            },
            actor,
        )
        return core.events.reconcile_candidate(accepted["candidate_id"], accepted["revision"])

    def capture_history(marker):
        title = "【演示】可靠性变化历史"
        body = f'<html><meta charset="utf-8"><h1>{title}</h1><article><p>全体本科生请于{date}前提交申请。</p><p>材料：申请表与版本{marker}证明。</p></article></html>'.encode()
        snapshot, _ = core.pipeline.sources.persist(
            source, FetchResult("https://example.com/reliability/history", body, "text/html"), title, now_iso()
        )
        core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
        return publish(core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]}))

    for marker in ("一", "二", "三"):
        history = capture_history(marker)
    title = "【演示】可靠性受限附件"
    body = f'<html><meta charset="utf-8"><h1>{title}</h1><article><p>全体本科生请于{date}前提交申请表。</p><p>材料要求见附件。</p></article></html>'.encode()
    parent, _ = core.pipeline.sources.persist(
        source, FetchResult("https://example.com/reliability/attachment", body, "text/html"), title, now_iso()
    )
    restricted = b'<html><form><input name="codeValue"><img src="createimage.jsp"></form></html>'
    child, _ = core.pipeline.sources.persist(
        source,
        FetchResult("https://example.com/system/_content/download.jsp?wbfileid=123", restricted, "text/html"),
        "合成受限附件",
        now_iso(),
        parent["snapshot_id"],
    )
    core.pipeline.parse_document({"snapshot_id": parent["snapshot_id"]})
    attachment = publish(core.pipeline.extract_candidates({"snapshot_id": parent["snapshot_id"]}))

    @app.post("/__fixture/advance")
    async def advance(request: Request):
        require_user(request, roles={"admin"}, write=True)
        event = capture_history("四")
        return {"synthetic": True, "event_id": event["id"], "version": event["version"]}

    @app.post("/__fixture/drain")
    async def drain(request: Request):
        require_user(request, roles={"admin"}, write=True)
        for candidate in core.store.list("candidates"):
            if candidate["review_status"] == "accepted":
                core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"])
        return {"synthetic": True}

    manifest = {
        "synthetic": True,
        "fixture": "reliability-v1",
        "base": f"http://127.0.0.1:{args.port}",
        "attachment_event_id": attachment["id"],
        "attachment_snapshot_id": child["snapshot_id"],
        "change_event_id": history["id"],
        "admin_email": "demo-admin@tju.edu.cn",
        "advance_path": "/__fixture/advance",
        "drain_path": "/__fixture/drain",
    }
    Path(args.manifest).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
