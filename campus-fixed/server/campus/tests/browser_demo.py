"""Explicit loopback synthetic fixture server, with no worker or external collection."""

import argparse
import os
import uuid
from datetime import UTC
from pathlib import Path

from server.campus.demo import seed_demo
from server.campus.ingestion import SourceBacklog
from server.campus.platform import Settings
from server.campus.schema import now_iso
from server.campus.tests.test_integration import synthetic_source
from server.campus_app import create_app


def seed_feed_capture(core):
    """Explicit synthetic authorized receipt, exercised through the real pipeline."""
    from datetime import datetime, timedelta

    from server.campus.sources import FetchResult
    from server.campus.tests.test_pipeline import MappingFetcher

    source = synthetic_source(core, str(uuid.uuid5(uuid.NAMESPACE_URL, "campus-authorized-feed-browser")))
    source.update(
        name="【合成验收】已授权供稿",
        kind="wechat",
        adapter="werss",
        feed_content_mode="authorized_fulltext",
        origin_url="https://feed.example/browser.xml",
        verification_basis="仅合成测试，无真实公众号授权",
    )
    core.store.put("sources", source["id"], source)
    date = (datetime.now(UTC) + timedelta(days=4)).date().isoformat()
    raw = (
        '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>'
        "<title>【演示】授权供稿材料办理</title><link>https://mp.weixin.qq.com/s/synthetic-browser</link>"
        "<content:encoded><![CDATA[<p>全体本科生请于" + date + "前提交申请表。仅合成演示，不能用于真实办理。</p>]]>"
        "</content:encoded></item></channel></rss>"
    ).encode()
    original = core.pipeline.sources.fetcher
    try:
        core.pipeline.sources.fetcher = MappingFetcher(
            {source["origin_url"]: FetchResult(source["origin_url"], raw, "application/xml")}
        )
        sid = core.pipeline.sources.fetch_source({"source_id": source["id"]})["snapshot_ids"][0]
    finally:
        core.pipeline.sources.fetcher = original
    core.pipeline.parse_document({"snapshot_id": sid})
    candidate = core.pipeline.extract_candidates({"snapshot_id": sid})
    candidate = core.pipeline.review(
        candidate["candidate_id"],
        dict(
            expected_candidate_revision=candidate["revision"],
            decision="accept",
            reason="仅核对合成授权供稿测试",
            replacement_candidate=None,
        ),
        {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "demo-reviewer"))},
    )
    event = core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"])
    return event["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8015)
    parser.add_argument("--data-dir", default=".campus-browser-demo")
    parser.add_argument(
        "--feed-event-info", help="Opt in to a synthetic authorized-feed event and write its ID to this test report"
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use an unprivileged local port")
    if (
        os.getenv("CAMPUS_ENV", "development").lower() in {"production", "prod"}
        or os.getenv("CAMPUS_BLOB_STORE", "local") != "local"
        or os.getenv("CAMPUS_PUBLIC_ORIGIN")
    ):
        parser.error("Run this fixture in a clean local development environment")
    app = create_app(
        Settings(
            data_dir=Path(args.data_dir),
            secret="local-synthetic-browser-fixture-" * 3,
            dev_auth=True,
            admin_emails={"demo-admin@tju.edu.cn"},
            allowed_origins=[f"http://127.0.0.1:{args.port}"],
        ),
        start_worker=False,
    )
    core = app.state.core
    seed_demo(core)
    for kind in ("website", "wechat"):
        source = synthetic_source(core, str(uuid.uuid5(uuid.NAMESPACE_URL, "campus-source-browser:" + kind)))
        source.update(
            name="【合成验收】" + ("网站来源" if kind == "website" else "公众号来源"),
            kind=kind,
            verification_basis="仅浏览器流程合成夹具，不代表真实学校/微信采集",
            health="healthy",
            last_success_at=now_iso(),
        )
        core.store.put("sources", source["id"], source)
        if kind == "website":
            # Explicit synthetic UI state only; this server never fetches or runs a worker.
            backlog = SourceBacklog(core.store)
            _, state = backlog.acquire(source["id"])
            backlog.discover(
                state,
                [
                    dict(url=f"https://example.com/synthetic/{i}", title="合成待采条目", published_at=None)
                    for i in range(6)
                ],
                truncated=True,
            )
            for item in list(state["items"].values())[:4]:
                backlog.outcome(item)
            with core.store.transaction():
                backlog.save(source["id"], state)
            backlog.release(source["id"], state["lease_token"])
    if args.feed_event_info:
        import json

        Path(args.feed_event_info).write_text(
            json.dumps({"event_id": seed_feed_capture(core), "synthetic": True}), encoding="utf-8"
        )
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
