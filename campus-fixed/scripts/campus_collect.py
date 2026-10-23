#!/usr/bin/env python3
"""Collect real Tianjin University notices into an operator data directory.

This is the operator-facing entry point the product was missing: one command
that registers (or reuses) the configured official sources, fetches every source
through the product's own ``SafeFetcher``, parses the new snapshots, extracts
reviewable candidates, and prints an honest summary. It is safe to run daily
and safe to re-run:

* snapshots are content-addressed (``sources.persist``), so re-fetching an
  unchanged page creates no duplicate document, snapshot or candidate;
* it never deletes data and never seeds synthetic demo records;
* it refuses the bundled ``data/`` sample directory, whose files ship with the
  release, and defaults to ``data-operator/`` instead;
* every path it prints is repository-relative, because its output is kept as
  release evidence and must not describe the machine that produced it.

Network reality on an operator laptop: a proxy TUN resolver answers every
``*.tju.edu.cn`` name with a fake-IP in ``198.18.0.0/15``. ``SafeFetcher``
correctly refuses non-global addresses; the supported fix is the explicit
``CAMPUS_TRUSTED_GATEWAY_ORIGINS`` allowlist, never a weaker address check.
``--gateway-from-config`` fills that allowlist from the repository's own
official source catalog for this process only. See ``docs/REAL-DATA-RUNBOOK.md``.

Usage::

    python scripts/campus_collect.py init                 # fresh dir + collect
    python scripts/campus_collect.py collect               # daily run
    python scripts/campus_collect.py collect --source <id> # one source
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BUNDLED_SAMPLE_DIR = ROOT / "data"
DEFAULT_DATA_DIR = ROOT / "data-operator"
TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE_CONFIG = ROOT / "config" / "campus-sources.json"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# WeChat articles are linked from official columns. Trusting the host lets the
# fetcher reach the page and *reject* an anti-scraping challenge honestly
# (``sources._reject_redirected_off_pattern`` / ``content._html_interstitial``)
# instead of reporting a misleading ``unsafe_address``. It never forges a UA or
# cookie and never bypasses those guards.
EXTRA_GATEWAY_ORIGINS = ("https://mp.weixin.qq.com",)


class UsageError(RuntimeError):
    """The command cannot run as asked; nothing was collected."""


def split_origins(value):
    return [item.strip().rstrip("/") for item in (value or "").split(",") if item.strip()]


def gateway_origins_from_config(path=None):
    """The exact origins of every source the repository already declares official."""
    path = Path(path or SOURCE_CONFIG)
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("sources", [])
    except (OSError, ValueError):
        return []
    origins = set()
    for entry in entries:
        url = entry.get("origin_url") if isinstance(entry, dict) else None
        if not isinstance(url, str):
            continue
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            origins.add(f"{parsed.scheme}://{parsed.netloc}")
    origins.update(EXTRA_GATEWAY_ORIGINS)
    return sorted(origins)


def diagnose_gateway(origins, resolver=socket.getaddrinfo):
    """Origins whose DNS answer is not globally routable (fake-IP / private)."""
    flagged = []
    for origin in origins:
        parsed = urlsplit(origin)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            infos = resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
            addresses = {info[4][0].split("%")[0] for info in infos if info[4]}
        except (OSError, ValueError):
            continue
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                flagged.append(origin)
                break
            if not address.is_global:
                flagged.append(origin)
                break
    return sorted(set(flagged))


def gateway_hint(flagged):
    names = "、".join(flagged[:6]) + (" 等" if len(flagged) > 6 else "")
    return "\n".join(
        [
            f"警告：{len(flagged)} 个官方 origin 解析到非全局地址（本机代理 TUN 的 fake-IP）。",
            f"      {names}",
            "      SafeFetcher 会以 unsafe_address 拒绝——这是防 SSRF 的正确行为，不要改它。",
            "      合规做法二选一（都只用 CAMPUS_TRUSTED_GATEWAY_ORIGINS）：",
            "        python scripts/campus_collect.py collect --gateway-from-config",
            "        $env:CAMPUS_TRUSTED_GATEWAY_ORIGINS = \"<见 docs/REAL-DATA-RUNBOOK.md 第 2 节>\"",
        ]
    )


def display_path(path):
    """Repository-relative when possible; otherwise the bare directory name.

    Evidence is shipped with the release, so a machine path must never reach it.
    """
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.name


def build_core(data_dir, timeout=None):
    """Assemble the product's own Core/Pipeline (no FastAPI, no worker thread)."""
    from server.campus.content import Pipeline
    from server.campus.events import Events
    from server.campus.platform import Core, Settings
    from server.campus.sources import SafeFetcher

    # ``Settings.from_env`` is the product's own configuration entry point; point
    # only CAMPUS_DATA_DIR at this run's directory and restore it immediately so a
    # long-lived caller (or a test) keeps its environment unchanged.
    previous = os.environ.get("CAMPUS_DATA_DIR")
    os.environ["CAMPUS_DATA_DIR"] = str(data_dir)
    try:
        settings = Settings.from_env()
    finally:
        if previous is None:
            os.environ.pop("CAMPUS_DATA_DIR", None)
        else:
            os.environ["CAMPUS_DATA_DIR"] = previous
    core = Core(settings)
    core.pipeline = Pipeline(core)
    # The events service owns publication state, so prune needs the same
    # assembly the app factory performs.
    core.events = Events(core)
    if timeout:
        # Rebuild so the interval applies to every hop; trusted origins still
        # come from the environment variable the allowlist mechanism already uses.
        core.pipeline.sources.fetcher = SafeFetcher(timeout=timeout)
    return core


def _code(error):
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else type(error).__name__


def _codes(values):
    return ", ".join(f"{code}×{count}" if count > 1 else code for code, count in sorted(Counter(values).items()))


def _counts(mapping):
    return ", ".join(f"{code}×{count}" if count > 1 else code for code, count in sorted(mapping.items()))


def enabled_sources(core):
    return [source for source in core.store.list("sources") if source.get("enabled")]


def select_sources(sources, selectors):
    if not selectors:
        return sources
    by_id = {source["id"]: source for source in sources}
    by_name = {source["name"]: source for source in sources}
    chosen = []
    for selector in selectors:
        if selector in by_id:
            matches = [by_id[selector]]
        elif selector in by_name:
            matches = [by_name[selector]]
        else:
            matches = [source for source in sources if selector.lower() in source["name"].lower()]
            if not matches:
                raise UsageError(f"未找到来源：{selector}")
            if len(matches) > 1:
                raise UsageError(f"来源选择器不唯一（{len(matches)} 个匹配）：{selector}；请改用来源 id")
        for source in matches:
            if source not in chosen:
                chosen.append(source)
    return chosen


def _pending_snapshots(core, source_id):
    return {
        snapshot["snapshot_id"]
        for snapshot in core.store.list("snapshots")
        if snapshot["source_id"] == source_id
        and core.store.get("parsed", snapshot["snapshot_id"]) is None
    }


def _candidate(snapshot_id, core):
    """The parseable parent snapshot a work item belongs to (attachments redirect)."""
    snapshot = core.store.get("snapshots", snapshot_id)
    if not snapshot:
        return None
    document = core.store.get("documents", snapshot["document_id"]) or {}
    latest_id = document.get("latest_snapshot_id") or snapshot_id
    latest = core.store.get("snapshots", latest_id) or snapshot
    return latest.get("parent_snapshot_id") or latest_id


def _backlog(core, source):
    """How much of the discovered index is still queued for later runs."""
    from server.campus.ingestion import SourceBacklog

    return SourceBacklog(core.store).summary(source)


def collect(core, selectors=None, fetcher=None):
    """Fetch, parse and extract every selected source; return a summary dict."""
    if fetcher is not None:
        core.pipeline.sources.fetcher = fetcher
    sources = select_sources(sorted(enabled_sources(core), key=lambda s: s["name"]), selectors)
    if not sources:
        raise UsageError("没有启用中的来源；先运行 init 注册官方来源")

    candidates_before = {
        candidate["candidate_id"]: candidate["revision"] for candidate in core.store.list("candidates")
    }
    rows = []
    for index, source in enumerate(sources, start=1):
        started = time.monotonic()
        row = {
            "source_id": source["id"],
            "name": source["name"],
            "status": "ok",
            "new_snapshots": 0,
            "parse": {"ok": 0, "partial": 0, "failed": 0},
            # An attachment snapshot is a child of the notice that links it. It is
            # counted apart because the two fail for different reasons: a login-walled
            # attachment is expected (the notice stays usable), an empty notice is not.
            "parse_attachment": {"ok": 0, "partial": 0, "failed": 0},
            "candidates_new": 0,
            "candidates_updated": 0,
            "candidates_unchanged": 0,
            "skipped_no_body": 0,
            "errors": [],
            "parse_errors": [],
            "candidate_errors": [],
        }
        try:
            result = core.pipeline.sources.fetch_source({"source_id": source["id"]})
        except Exception as error:  # noqa: BLE001 - reported by code, never swallowed
            code = _code(error)
            row["errors"] = [code]
            current = core.store.get("sources", source["id"]) or source
            row["status"] = source_status(code, current.get("health"))
        else:
            new_ids = list(result.get("snapshot_ids") or [])
            row["new_snapshots"] = len(new_ids)
            row["errors"] = list(result.get("errors") or [])
            unparsed = _pending_snapshots(core, source["id"])
            work = new_ids + sorted(unparsed.difference(new_ids))
            for snapshot_id in work:
                snapshot = core.store.get("snapshots", snapshot_id)
                if not snapshot:
                    continue
                bucket = row["parse_attachment"] if snapshot.get("parent_snapshot_id") else row["parse"]
                try:
                    parsed = core.pipeline.parse_document({"snapshot_id": snapshot_id})
                    state = (parsed or {}).get("state") or "failed"
                except Exception as error:  # noqa: BLE001
                    bucket["failed"] += 1
                    if bucket is row["parse"]:
                        row["parse_errors"].append(_code(error))
                    continue
                bucket[state if state in bucket else "failed"] += 1
            targets = []
            for snapshot_id in work:
                target = _candidate(snapshot_id, core)
                if target and target not in targets:
                    targets.append(target)
            for target in targets:
                parsed = core.pipeline.parsed(target)
                if not parsed or not parsed["segments"]:
                    row["skipped_no_body"] += 1
                    continue
                try:
                    candidate = core.pipeline.extract_candidates({"snapshot_id": target})
                except Exception as error:  # noqa: BLE001
                    row["candidate_errors"].append(_code(error))
                    continue
                if candidate is None:
                    row["skipped_no_body"] += 1
                    continue
                candidate_id = candidate["candidate_id"]
                previous = candidates_before.get(candidate_id)
                if previous is None:
                    row["candidates_new"] += 1
                elif candidate["revision"] > previous:
                    row["candidates_updated"] += 1
                else:
                    row["candidates_unchanged"] += 1
                candidates_before[candidate_id] = candidate["revision"]
            if row["errors"] or row["parse_errors"] or row["candidate_errors"]:
                row["status"] = "degraded"

        current = core.store.get("sources", source["id"]) or source
        row["health"] = current.get("health")
        row["last_error_code"] = current.get("last_error_code")
        row["backlog"] = _backlog(core, current)
        row["elapsed_seconds"] = round(time.monotonic() - started, 2)
        rows.append(row)

    parse_totals = {
        key: sum(row["parse"][key] for row in rows) for key in ("ok", "partial", "failed")
    }
    attachment_totals = {
        key: sum(row["parse_attachment"][key] for row in rows) for key in ("ok", "partial", "failed")
    }
    error_codes = Counter()
    for row in rows:
        error_codes.update(row["errors"])
        error_codes.update(row["parse_errors"])
        error_codes.update(row["candidate_errors"])
    candidates = core.store.list("candidates")
    totals = {
        "sources": len(rows),
        "ok": sum(row["status"] == "ok" for row in rows),
        "degraded": sum(row["status"] == "degraded" for row in rows),
        "blocked": sum(row["status"] == "blocked" for row in rows),
        "failed": sum(row["status"] == "failed" for row in rows),
        "new_snapshots": sum(row["new_snapshots"] for row in rows),
        "documents": len(core.store.list("documents")),
        "candidates_total": len(candidates),
        "candidates_new": sum(row["candidates_new"] for row in rows),
        "candidates_pending": sum(candidate["review_status"] == "pending" for candidate in candidates),
        "parse": parse_totals,
        "parse_attachments": attachment_totals,
        "skipped_no_body": sum(row["skipped_no_body"] for row in rows),
        "backlog_pending": sum((row.get("backlog") or {}).get("pending_count", 0) for row in rows),
        "backlog_failed": sum((row.get("backlog") or {}).get("failed_count", 0) for row in rows),
        "error_codes": dict(sorted(error_codes.items())),
    }
    return {"collected": True, "sources": rows, "totals": totals}


BLOCKED_CODES = frozenset({"article_redirected_off_pattern", "html_access_interstitial"})


def source_status(code, health=None):
    """Map a fetch outcome to what an operator should conclude.

    ``blocked`` is its own state: the site is reachable but refuses to serve the
    article body to an unauthenticated client, so the source needs an authorized
    channel (relay or authorized feed) and will keep producing nothing until it
    has one. That is not an outage and must not fail a daily run forever.
    """
    if code in BLOCKED_CODES or health == "auth_expired":
        return "blocked"
    return "failed"


def reextract(core, selectors=None):
    """Re-run extraction for candidates produced by older rules.

    Upgrading the extractor only helps if the stored candidates are produced
    again: the pipeline already flags an upgraded candidate as
    ``extractor_upgrade_requires_review``, but nothing used to call it for
    snapshots extracted earlier. Without this, a rule fix reaches no existing
    row — a real defect (four notices stuck as unpublishable cancellations).
    Nothing is deleted: the candidate gains a revision and needs a fresh human
    decision.

    ``selectors`` reruns specific candidates (id or title fragment) even when
    their stored version already matches, which is what a rule change inside the
    same version looks like to the operator.
    """
    from server.campus.content import EXTRACTOR_VERSION

    candidates = core.store.list("candidates")
    if selectors:
        wanted = []
        for selector in selectors:
            match = [
                candidate
                for candidate in candidates
                if selector == candidate.get("candidate_id") or selector in (candidate.get("title") or "")
            ]
            wanted.extend(match)
        stale = list({candidate["candidate_id"]: candidate for candidate in wanted}.values())
    else:
        stale = [candidate for candidate in candidates if candidate.get("extractor_version") != EXTRACTOR_VERSION]
    upgraded = 0
    failed = []
    superseded = []
    for candidate in stale:
        snapshot_ids = candidate.get("snapshot_ids") or []
        if not snapshot_ids:
            failed.append({"candidate_id": candidate.get("candidate_id"), "error": "no_snapshot"})
            continue
        # A duplicate row for a document whose current-rules candidate already
        # exists cannot be refreshed: extraction resolves by (snapshot, rules), and
        # that slot is taken. The content is covered by the sibling, so reporting
        # it as a failure made a healthy run look broken.
        title = (candidate.get("title") or "").strip()
        if title and any(
            other is not candidate
            and (other.get("title") or "").strip() == title
            and other.get("extractor_version") == EXTRACTOR_VERSION
            for other in candidates
        ):
            superseded.append({"candidate_id": candidate.get("candidate_id"), "title": candidate.get("title")})
            continue
        try:
            refreshed = core.pipeline.extract_candidates({"snapshot_id": snapshot_ids[0]})
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            failed.append({"candidate_id": candidate.get("candidate_id"), "error": _code(error)})
            continue
        if refreshed and refreshed.get("revision", 0) > candidate.get("revision", 0):
            upgraded += 1
    return {
        "scanned": len(stale),
        "upgraded": upgraded,
        "superseded": superseded,
        "failed": failed,
        "extractor_version": EXTRACTOR_VERSION,
    }


def exit_code(totals):
    if not totals.get("sources"):
        return EXIT_USAGE
    return EXIT_FAILED if totals.get("failed") else EXIT_OK


def prune_actor_id(core):
    """Attribute an automatic withdrawal to a named operator.

    The publication audit records an actor, and "who removed this notice" should
    stay answerable. Prefer the configured operator address; fall back to a
    stable synthetic id so the audit never carries an empty actor.
    """
    configured = (os.environ.get("CAMPUS_ADMIN_EMAILS") or "").split(",")
    for candidate in configured:
        if candidate.strip():
            return f"operator:{candidate.strip()}"
    admins = [user for user in core.store.list("users") if user.get("role") == "admin"]
    if admins:
        return f"operator:{admins[0].get('email') or admins[0]['id']}"
    return "operator:automatic-prune"


def render_summary(summary, out):
    gateway = summary.get("gateway") or {}
    origin_count = len(gateway.get("origins") or [])
    if gateway.get("source") == "env":
        gateway_line = f"CAMPUS_TRUSTED_GATEWAY_ORIGINS 环境变量（{origin_count} 个 origin）"
    elif gateway.get("source") == "config":
        gateway_line = f"config/campus-sources.json 官方来源（{origin_count} 个 origin，仅本进程）"
    else:
        gateway_line = "未设置（本机 fake-IP 环境下会得到 unsafe_address）"
    print(f"校园通知真实采集 · {summary.get('command', 'collect')}", file=out)
    print(f"数据目录：{summary.get('data_dir')}", file=out)
    registration = summary.get("registration") or {}
    if registration:
        print(f"来源注册：新增 {registration.get('new', 0)} · 复用 {registration.get('existing', 0)}", file=out)
    print(f"受信网关：{gateway_line}", file=out)

    rows = summary.get("sources") or []
    if rows:
        print(f"\n逐源结果（{len(rows)} 个）", file=out)
        for index, row in enumerate(rows, start=1):
            detail = (
                f"新增快照 {row['new_snapshots']} · 通知解析 "
                f"{row['parse']['ok']}/{row['parse']['partial']}/{row['parse']['failed']} · "
                f"新候选 {row['candidates_new']}"
            )
            codes = _codes(row["errors"] + row["parse_errors"] + row["candidate_errors"])
            backlog = row.get("backlog") or {}
            if row["status"] == "failed":
                detail = "错误码 " + (codes or "unknown_error")
            elif codes:
                detail += " · 错误码 " + codes
            if row["status"] != "failed" and backlog.get("pending_count"):
                detail += f" · 积压 {backlog['pending_count']} 待抓"
            print(f"  [{index:>2}/{len(rows)}] {row['status']:<8} {row['name']}", file=out)
            print(f"          {detail}", file=out)

    totals = summary.get("totals") or {}
    print("\n汇总", file=out)
    if summary.get("collected"):
        print(
            f"  来源        {totals['sources']} 个：成功 {totals['ok']} · "
            f"降级 {totals['degraded']} · 需授权 {totals.get('blocked', 0)} · 失败 {totals['failed']}",
            file=out,
        )
        if totals.get("blocked"):
            print(
                "  需授权      这些源的上游拒绝对未授权客户端返回正文（反爬挑战）。"
                "接入授权通道（deploy/operator 中继或授权供稿）前不会产出，"
                "不是故障，不影响其它源。",
                file=out,
            )
        print(f"  入库        新增快照 {totals['new_snapshots']} 个 · 文档总数 {totals['documents']}", file=out)
        print(
            f"  解析        通知 ok {totals['parse']['ok']} · partial {totals['parse']['partial']} · "
            f"failed {totals['parse']['failed']} · 无正文跳过 {totals['skipped_no_body']}",
            file=out,
        )
        attachments = totals.get("parse_attachments") or {}
        if any(attachments.values()):
            print(
                f"  附件        可读 {attachments['ok']} · 部分 {attachments['partial']} · "
                f"取不到 {attachments['failed']}（需登录/授权，不影响对应通知）",
                file=out,
            )
        print(
            f"  候选        新增 {totals['candidates_new']} · 合计 {totals['candidates_total']} · "
            f"待复核 {totals['candidates_pending']}",
            file=out,
        )
        if totals["error_codes"]:
            print(f"  错误码      {_counts(totals['error_codes'])}", file=out)
        if totals.get("backlog_pending") or totals.get("backlog_failed"):
            print(
                f"  积压        待抓 {totals.get('backlog_pending', 0)} · "
                f"失败待重试 {totals.get('backlog_failed', 0)}（每次每源最多处理 8 篇，后续每天自然补齐）",
                file=out,
            )
        if totals["candidates_pending"]:
            print("  复核入口    operator 实例 /admin/reviews（需人工逐条决策后才会发布）", file=out)
    else:
        print(f"  来源        {totals.get('sources', 0)} 个已注册；未采集", file=out)
        if summary.get("prune"):
            print(
                f"  事项        已发布 {totals.get('events_published', 0)} 条 · "
                f"其中已过期 {totals.get('events_expired', 0)} 条",
                file=out,
            )
        else:
            print(
                f"  库内现状    文档 {totals.get('documents', 0)} · "
                f"候选 {totals.get('candidates_total', 0)} · 待复核 {totals.get('candidates_pending', 0)}",
                file=out,
            )
    prune = summary.get("prune")
    if prune:
        print("\n过期下架", file=out)
        rows = prune["expired"]
        if not rows:
            print("  结果        没有已过期的事项，无需下架", file=out)
        else:
            print(f"  结果        {len(rows)} 条截止时间已过" + ("（已下架）" if prune["applied"] else "（演练，未改动）"), file=out)
            for row in rows[:20]:
                print(f"    {row['deadline']}  {row['title'][:52]}", file=out)
            if len(rows) > 20:
                print(f"    …… 另有 {len(rows) - 20} 条，见 --json-out", file=out)
        unusable = prune.get("unusable") or []
        print("\n非通知下架", file=out)
        if not unusable:
            print("  结果        公开列表里没有栏目碎片/附件名/新闻稿", file=out)
        else:
            print(
                f"  结果        {len(unusable)} 条标题不是通知"
                + ("（已下架）" if prune["applied"] else "（演练，未改动）"),
                file=out,
            )
            for row in unusable[:20]:
                print(f"    [{row['reason'][:14]:14s}] {row['title'][:48]}", file=out)
            if len(unusable) > 20:
                print(f"    …… 另有 {len(unusable) - 20} 条，见 --json-out", file=out)
            print("  保护        仍带未来截止时间的一律不动，避免误伤还能办的事", file=out)
        if prune["applied"]:
            print("  效果        这些事项已从清单、公开列表、检索与问答中消失；如需恢复用 /admin/events 改回 published。", file=out)
        else:
            print("  下一步      确认无误后加 --apply 执行；不加参数不会改动任何数据。", file=out)
    outcome = summary.get("reextract")
    if outcome:
        print("\n重抽结果", file=out)
        print(f"  规则版本    {outcome['extractor_version']}", file=out)
        print(f"  旧规则候选  {outcome['scanned']} 条 · 已重抽 {outcome['upgraded']} 条", file=out)
        if outcome.get("superseded"):
            print(
                f"  重复行      {len(outcome['superseded'])} 条：同标题已有新规则版本，内容已覆盖，无需重抽",
                file=out,
            )
        if outcome["failed"]:
            print(f"  失败        {len(outcome['failed'])} 条（见 --json-out 明细）", file=out)
        if outcome["upgraded"]:
            print("  注意        重抽后的候选需要重新人工复核，发布前不改变学生看到的内容。", file=out)
    if summary.get("trusted_origins_hint"):
        print("\n" + summary["trusted_origins_hint"], file=out)


def _write_json(path, summary):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _guard_data_dir(path):
    resolved = Path(path).resolve()
    sample = BUNDLED_SAMPLE_DIR.resolve()
    if resolved == sample or sample in resolved.parents:
        raise UsageError(
            "拒绝写入 data/：data/ 是随包样本目录（随发布包出厂的公开样例），不得混入运营数据。"
            "请改用默认的 data-operator/ 或用 --data-dir 指定运营目录。"
        )
    if resolved == ROOT.resolve():
        raise UsageError("拒绝把仓库根目录当数据目录；请使用 data-operator/ 或 --data-dir。")
    return resolved


def run(argv=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(
        prog="campus_collect.py",
        description="注册官方来源、真实采集、解析并抽取待复核候选。",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", help="数据目录（默认环境变量 CAMPUS_DATA_DIR 或 data-operator/）")
    common.add_argument("--source", action="append", default=[], help="只采集指定来源（id / 全名 / 名称片段，可重复）")
    common.add_argument("--timeout", type=int, default=20, help="单次 HTTP 请求超时秒数（默认 20）")
    common.add_argument("--json-out", help="把机器可读摘要写入该路径")
    common.add_argument("--gateway-from-config", action="store_true", help="用配置里的官方 origin 填受信网关白名单（仅本进程）")
    common.add_argument("--skip-dns-check", action="store_true", help="跳过 fake-IP 预检（离线/测试用）")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", parents=[common], help="初始化数据目录并跑第一次采集")
    init.add_argument("--no-collect", action="store_true", help="只注册来源，不联网采集")
    commands.add_parser("collect", parents=[common], help="日常采集（注册/复用来源后逐源采集）")
    commands.add_parser("reextract", parents=[common], help="抽取规则升级后，对旧候选重跑抽取（不联网）")
    prune = commands.add_parser("prune", parents=[common], help="把截止时间已过的事项下架（默认只演练）")
    prune.add_argument("--apply", action="store_true", help="真正执行下架（不加则只列出将下架的事项）")
    prune.add_argument("--today", help="以该日期判断是否过期（YYYY-MM-DD，默认取当前日期）")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    requested = Path(args.data_dir) if args.data_dir else Path(os.environ.get("CAMPUS_DATA_DIR") or DEFAULT_DATA_DIR)
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    try:
        data_dir = _guard_data_dir(requested)
    except UsageError as error:
        print(str(error), file=err)
        return EXIT_USAGE
    created = not data_dir.exists()

    origins = split_origins(os.environ.get("CAMPUS_TRUSTED_GATEWAY_ORIGINS"))
    gateway = {"source": "env" if origins else "none", "origins": origins}
    if not origins and args.gateway_from_config:
        origins = gateway_origins_from_config()
        os.environ["CAMPUS_TRUSTED_GATEWAY_ORIGINS"] = ",".join(origins)
        gateway = {"source": "config", "origins": origins}

    do_collect = args.command == "collect" or not getattr(args, "no_collect", False)
    do_reextract = args.command == "reextract"
    do_prune = args.command == "prune"
    if do_reextract or do_prune:
        do_collect = False
    hint = None
    if do_collect and not origins and not args.skip_dns_check:
        flagged = diagnose_gateway(gateway_origins_from_config())
        if flagged:
            hint = gateway_hint(flagged)

    started_at = datetime.now(timezone.utc).isoformat()
    core = None
    try:
        core = build_core(data_dir, timeout=args.timeout)
        existing = {source["id"] for source in core.store.list("sources")}
        core.pipeline.sources.seed()
        registered = {source["id"] for source in core.store.list("sources")}
        registration = {"new": len(registered - existing), "existing": len(existing)}
        if do_collect:
            summary = collect(core, selectors=args.source)
        elif do_reextract:
            outcome = reextract(core, selectors=args.source)
            candidates = core.store.list("candidates")
            summary = {
                "collected": False,
                "reextract": outcome,
                "sources": [],
                "totals": {
                    "sources": len(enabled_sources(core)),
                    "documents": len(core.store.list("documents")),
                    "candidates_total": len(candidates),
                    "candidates_pending": sum(c["review_status"] == "pending" for c in candidates),
                },
            }
        elif do_prune:
            today = date.fromisoformat(args.today) if args.today else datetime.now(TZ_SHANGHAI).date()
            actor = prune_actor_id(core)
            outcome = core.events.withdraw_expired(
                today,
                actor_id=actor,
                reason=f"截止时间已过（{today.isoformat()}），自动下架",
                apply=bool(args.apply),
            )
            # Second sweep: content an earlier, more permissive review gate let
            # through (column fragments, attachment names, news pieces, bare
            # links). It never touches a notice that still has a deadline ahead.
            unusable = core.events.withdraw_unusable(actor_id=actor, apply=bool(args.apply))
            outcome["unusable"] = unusable["unusable"]
            published = [e for e in core.store.list("events") if e["publication_state"] == "published"]
            summary = {
                "collected": False,
                "prune": outcome,
                "sources": [],
                "totals": {
                    "sources": len(enabled_sources(core)),
                    "documents": len(core.store.list("documents")),
                    "events_published": len(published),
                    "events_expired": len(outcome["expired"]),
                    "events_unusable": len(outcome["unusable"]),
                },
            }
        else:
            candidates = core.store.list("candidates")
            summary = {
                "collected": False,
                "sources": [],
                "totals": {
                    "sources": len(enabled_sources(core)),
                    "documents": len(core.store.list("documents")),
                    "candidates_total": len(candidates),
                    "candidates_pending": sum(c["review_status"] == "pending" for c in candidates),
                },
            }
    except UsageError as error:
        print(str(error), file=err)
        return EXIT_USAGE
    finally:
        if core is not None:
            core.store.close()

    summary.update(
        command=args.command,
        data_dir=display_path(data_dir),
        gateway={"source": gateway["source"], "origins": list(gateway["origins"])},
        registration=registration,
        created_directory=created,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    if hint:
        summary["trusted_origins_hint"] = hint
    summary["exit_code"] = exit_code(summary.get("totals") or {}) if do_collect else EXIT_OK
    render_summary(summary, out)
    if args.json_out:
        _write_json(args.json_out, summary)
        print(f"\n机器可读摘要：{display_path(args.json_out)}", file=out)
    return summary["exit_code"]


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
