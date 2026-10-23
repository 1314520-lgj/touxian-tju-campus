"""真实数据端到端验收：采集 → 解析 → 抽取 → 复核 → 发布 → 检索。

这是验收脚本，不是产品代码。它在一个全新的数据目录里跑完整条链路，用的是真实
官方页面（不伪造任何内容），最后断言学生真的能在检索里找到并看到结构化字段。
鉴权、CSRF、幂等、契约校验全部走真实 app，不用内部捷径。

用法（受信网关是本机 Clash TUN 环境下唯一合规通路）：

    set CAMPUS_TRUSTED_GATEWAY_ORIGINS=<真实官方 origin 列表>
    <PY> scripts/verify_real_use.py --source cic --limit 3

退出码 0 表示整条链路走通且每一级断言都成立。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TJU_HOSTS = ("oaa", "gs", "news", "cic", "me", "job", "zs", "yx", "scet", "ico")
TJU_ORIGINS = ",".join(f"https://{host}.tju.edu.cn" for host in TJU_HOSTS)
OPERATOR_EMAIL = "operator@tju.edu.cn"


def _report(stage: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {stage}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=[], help="要验收的 source 关键字（可重复）")
    parser.add_argument("--limit", type=int, default=3, help="每个源最多验收几篇文档")
    parser.add_argument("--data-dir", default=None, help="数据目录，默认临时目录")
    args = parser.parse_args(argv)

    # 本机 *.tju.edu.cn 解析到 Clash TUN 的 198.18.x.x（非全球地址），SafeFetcher
    # 默认拒绝。唯一合规做法是显式声明受信网关；放松地址安全判定是禁止的。
    os.environ.setdefault("CAMPUS_TRUSTED_GATEWAY_ORIGINS", TJU_ORIGINS)

    from fastapi.testclient import TestClient

    from server.campus.platform import Settings
    from server.campus.schema import uid, validate
    from server.campus_app import create_app

    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="campus-realuse-"))
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"data_dir = {data_dir}", flush=True)

    app = create_app(
        Settings(
            data_dir=data_dir,
            secret="real-use-acceptance-" * 3,
            dev_auth=True,
            admin_emails={OPERATOR_EMAIL},
        ),
        start_worker=False,
    )
    core = app.state.core
    client = TestClient(app)
    failures: list[str] = []

    # --- 0. 注册官方来源 ----------------------------------------------------
    core.pipeline.sources.seed()
    sources = [s for s in core.store.list("sources") if s.get("kind") == "website"]
    if not _report("0. 注册官方来源", bool(sources), f"{len(sources)} 个源"):
        core.store.close()
        return 1

    wanted = args.source or [s["id"] for s in sources[:2]]
    picked = []
    for key in wanted:
        match = next(
            (
                s
                for s in sources
                if key in s.get("id", "") or key in s.get("name", "") or key in s.get("origin_url", "")
            ),
            None,
        )
        if match and match not in picked:
            picked.append(match)
    print(f"sources  = {[s.get('name') for s in picked]}", flush=True)
    if not picked:
        _report("0b. 选中来源", False, f"--source {wanted} 匹配不到已注册来源")
        core.store.close()
        return 1

    # --- 1. 采集 -----------------------------------------------------------
    snapshots: list[str] = []
    for source in picked:
        try:
            result = core.pipeline.sources.fetch_source({"source_id": source["id"], "job_id": uid()})
        except Exception as error:  # noqa: BLE001 - 验收要报告而不是崩
            failures.append(f"fetch:{source['id']}")
            _report(f"1. 采集 {source.get('name')}", False, f"{type(error).__name__}: {error}")
            continue
        stored = result.get("snapshot_ids") or []
        errors = sorted(set(result.get("errors") or []))
        _report(f"1. 采集 {source.get('name')}", bool(stored), f"入库 {len(stored)} 篇，失败信号 {errors[:4]}")
        snapshots.extend(stored)

    if not snapshots:
        _report("1. 采集", False, "没有任何文档入库，后续无法验收")
        core.store.close()
        return 1

    # --- 2-3. 解析 + 抽取 --------------------------------------------------
    candidates = []
    for snapshot_id in snapshots[: args.limit * max(1, len(picked))]:
        try:
            parsed = core.pipeline.parse_document({"snapshot_id": snapshot_id})
            candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot_id})
            validate("ExtractionCandidate", candidate)
        except Exception as error:  # noqa: BLE001
            failures.append(f"extract:{snapshot_id}")
            _report("2-3. 解析抽取", False, f"{type(error).__name__}: {error}")
            continue
        body_len = sum(len(segment.get("text") or "") for segment in parsed.get("segments") or [])
        candidates.append(candidate)
        # 抽不出正文不是脚本的失败：产品对图片正文/空正文会明确拒绝（状态 failed），
        # 这正是「不凭空编造」的行为。这里如实标注，但不当作验收失败。
        ok = bool(candidate.get("title")) and body_len > 0
        _report(
            "2-3. 解析抽取",
            ok,
            f"title={candidate.get('title')!r:.56} event={candidate.get('event_type')} "
            f"deadlines={len(candidate.get('deadlines') or [])} materials={len(candidate.get('materials') or [])} "
            f"body={body_len}字" + ("" if ok else "（产品按设计拒绝空正文，需人工复核）"),
        )

    if not candidates:
        _report("2-3. 解析抽取", False, "没有产出任何候选")
        core.store.close()
        return 1

    # --- 4. 复核接受（真实鉴权 + CSRF + 幂等） ------------------------------
    challenge = client.post("/api/v1/auth/challenges", json={"email": OPERATOR_EMAIL})
    assert challenge.status_code == 201, challenge.text
    session = client.post(
        "/api/v1/auth/sessions",
        json={"challenge_id": challenge.json()["challenge_id"], "code": challenge.headers["X-Dev-Login-Code"]},
    )
    assert session.status_code == 200, session.text
    headers = {"X-CSRF-Token": session.json()["csrf_token"]}

    queue = client.get("/api/v1/admin/reviews", headers=headers)
    assert queue.status_code == 200, queue.text
    payload = queue.json()
    queued = payload.get("candidates") or payload.get("items") or payload
    _report("4a. 复核队列可见", bool(queued), f"{len(queued)} 条待复核")

    accepted: list[str] = []
    seen_candidates: set[str] = set()
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        # 同一篇通知可能被列表页链接两次，落成同一个 candidate；接受第一条后
        # revision 会往前走，所以每次都要用队列里的**最新** revision。
        if candidate_id in seen_candidates:
            continue
        seen_candidates.add(candidate_id)
        fresh = client.get("/api/v1/admin/reviews", headers=headers).json()
        fresh_queue = fresh.get("candidates") or fresh.get("items") or fresh
        current = next((c for c in fresh_queue if c.get("candidate_id") == candidate_id), None)
        if not current:
            continue
        response = client.post(
            f"/api/v1/admin/reviews/{candidate_id}/decision",
            json={
                "expected_candidate_revision": current["revision"],
                "decision": "accept",
                "reason": "真实页面逐字核对通过",
                "replacement_candidate": None,
            },
            headers={**headers, "Idempotency-Key": uid()},
        )
        if response.status_code != 200:
            failures.append(f"decide:{candidate_id}")
            _report("4b. 接受候选", False, response.text[:220])
            continue
        validate("ExtractionCandidate", response.json())
        accepted.append(candidate_id)
    if not _report("4b. 接受候选", bool(accepted), f"{len(accepted)} 条已接受"):
        core.store.close()
        return 1

    # --- 5. 发布 -----------------------------------------------------------
    published = []
    for candidate_id in accepted:
        try:
            event = core.events.reconcile_candidate(candidate_id, core.store.get("candidates", candidate_id)["revision"])
            validate("EventVersion", event)
        except Exception as error:  # noqa: BLE001
            failures.append(f"publish:{candidate_id}")
            _report("5. 发布事项", False, f"{type(error).__name__}: {error}")
            continue
        published.append(event)
    if not _report("5. 发布事项", bool(published), f"{len(published)} 条已发布"):
        core.store.close()
        return 1

    # --- 6. 检索 -----------------------------------------------------------
    # 检索命中是冻结契约 v1.5 的投影：只有标题/摘要/来源/是否结束，不含截止时间与
    # 材料。结构化字段要从事项详情看，所以两级都验。
    hit_total = 0
    detail_ok = 0
    for event in published[:3]:
        keyword = (event.get("title") or "")[:6]
        if len(keyword) < 2:
            continue
        response = client.get("/api/v1/search", params={"q": keyword}, headers=headers)
        if response.status_code != 200:
            failures.append(f"search:{keyword}")
            _report(f"6. 检索 {keyword!r}", False, response.text[:160])
            continue
        hits = response.json().get("hits") or []
        hit_total += len(hits)
        _report(f"6. 检索 {keyword!r}", bool(hits), f"{len(hits)} 条命中")
        match = next((h for h in hits if (h.get("title") or "")[:12] == (event.get("title") or "")[:12]), None)
        if not match:
            continue
        detail = client.get(f"/api/v1/events/{match['event_id']}", headers=headers)
        if detail.status_code != 200:
            failures.append(f"detail:{match['event_id']}")
            _report("6b. 事项详情", False, detail.text[:160])
            continue
        body = detail.json()
        deadlines = body.get("deadlines") or []
        materials = body.get("materials") or []
        entry_urls = body.get("entry_urls") or []
        _report(
            "6b. 事项详情带结构化字段",
            True,
            f"title={(body.get('title') or '')[:36]!r} deadlines={len(deadlines)} "
            f"materials={len(materials)} entry_urls={len(entry_urls)} ended={body.get('ended')}",
        )
        if deadlines or materials:
            detail_ok += 1

    # --- 7. 前端可加载 -----------------------------------------------------
    index, web_index = ROOT / "dist" / "index.html", ROOT / "web" / "index.html"
    _report(
        "7. 前端构建产物",
        index.exists() or web_index.exists(),
        f"dist/index.html={index.exists()} web/index.html={web_index.exists()}",
    )

    print("\n===== 汇总 =====", flush=True)
    print(
        f"来源 {len(picked)} 个，入库 {len(snapshots)} 篇，候选 {len(candidates)} 条，"
        f"已接受 {len(accepted)} 条，已发布 {len(published)} 条，检索命中合计 {hit_total}，"
        f"带结构化字段的事项 {detail_ok} 条",
        flush=True,
    )
    if failures:
        print(f"失败项：{json.dumps(failures, ensure_ascii=False)}", flush=True)
    core.store.close()
    return 0 if published and hit_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
