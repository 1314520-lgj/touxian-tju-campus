#!/usr/bin/env python3
"""One command for the daily update: collect -> review -> withdraw.

Running the product every day used to mean remembering three commands in the
right order (``campus_collect.py collect``, ``review_and_publish.py``,
``campus_collect.py prune``). One person using this daily cannot be relied on to
remember that, and running them out of order does nothing useful. This command
chains the three stages and returns a truthful exit code, so it can be handed to
Windows Task Scheduler (or cron) and left alone.

What it does **not** do is decide anything itself:

* the collection stage is ``scripts/campus_collect.py`` (imported, not copied);
* the review verdict is ``scripts/review_and_publish.classify`` — the same
  three-state policy (reject / accept only what is actionable today / leave the
  rest to a human) the manual command applies;
* the withdrawal stage is ``Events.withdraw_expired`` and
  ``Events.withdraw_unusable``, the product's own publication state.

The default is a dry run: it reports what *would* be collected, accepted,
rejected, left for a human and withdrawn, and writes nothing. ``--apply`` is the
only thing that writes. Two writes are prevented explicitly rather than assumed
away: the signing key ``Core`` would generate on first open (an in-process
placeholder is used instead) and any new database. The one file a dry run does
create is the lock, because a report taken while another run is writing would be
neither reproducible nor worth printing.

Exit codes (the contract with the scheduler):

* ``0`` — every stage ran and nothing needed attention: no source failed, no
  decision or publish failed. Sources that are merely **not switched on** count
  here too (see ``OPTIONAL_CHANNEL_CODES``): an operator relay that is not
  running is not an outage, and a run that has nothing to report must exit 0 or
  the exit code stops meaning anything;
* ``1`` — the run completed but something really failed: a source could not be
  collected (unreachable host, upstream error, an address refused by the safety
  check), a notice body or an extraction raised, a decision or a publish failed.
  Later stages still ran: an unreachable official site must never keep
  yesterday's expired notices on a student's list;
* ``2`` — the command was not usable (bad flag, refused data directory, bad
  date). Nothing was touched.

Deliberate degradations (an article that redirects off pattern, an attachment
that needs a login or whose legacy URL is dead) do not fail the run: they are
the product refusing to bypass something, the notice itself is still collected,
and they are printed with their exact error codes so an operator can still see
them. Only "the product could not do its job" is an alarm.

A second concurrent invocation is refused through a file lock inside the data
directory, so a scheduled run and a manual one cannot write over each other.

Usage::

    python scripts/daily_update.py                       # dry run, default dir
    python scripts/daily_update.py --apply               # the daily update
    python scripts/daily_update.py --apply --gateway-from-config --json-out ...
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
SCRIPTS = Path(__file__).resolve().parent
LOCK_NAME = ".daily-update.lock"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _load_script(name):
    """Import a sibling operator script as a module (``scripts/`` is not a package)."""
    path = SCRIPTS / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The collection stage and the review policy are imported, never re-implemented:
# a forked copy would drift from the manual commands the runbook documents.
collect = _load_script("campus_collect")
review_policy = _load_script("review_and_publish")


class ProcessLock:
    """Exclusive advisory lock on one file inside the data directory.

    ``msvcrt.locking`` (Windows) and ``flock`` (POSIX) are both released by the
    operating system when the process dies, so a crashed run cannot leave the
    scheduler permanently locked out. The lock is taken non-blocking: a second
    invocation reports and exits instead of queueing behind a long collection.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_bytes(b"\0")
        handle = open(self.path, "r+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


def _code(error):
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    message = getattr(error, "message", None)
    if isinstance(message, str) and message:
        return f"{type(error).__name__}: {message}"
    return type(error).__name__


# Codes the product raises when an *optional* channel is simply not switched on.
# Every one of them is raised before anything is fetched -- ``Sources.config``
# refuses an authorized feed whose origin is not https and not explicitly
# trusted, and ``feed_archive.require_feed_authorization`` refuses a feed source
# whose mode or authorization was never verified. That is evidence about
# configuration, not about a site being down: the operator relay is not running
# today. A source whose only evidence is these codes is SKIPPED, never failed --
# otherwise an unattended run reports a failure every day and the exit code
# stops being an alarm.
#
# Deliberately narrow, and never keyed on a source's name: a feed that *is*
# enabled but serves broken content (``feed_fulltext_missing``,
# ``invalid_feed_content``, ``feed_receipt_missing_or_changed``,
# ``feed_original_*``) is a real failure and still fails the run.
OPTIONAL_CHANNEL_CODES = frozenset(
    {
        "feed_https_required",
        "feed_mode_not_enabled",
        "feed_mode_requires_feed_adapter",
        "feed_authorization_not_verified",
    }
)


def row_codes(row, field):
    """The error codes a collection row carries in one of its evidence fields."""
    return [code for code in (row.get(field) or []) if isinstance(code, str) and code]


def optional_channel_only(row):
    """True when every piece of evidence on this row says "channel not enabled".

    The row must carry at least one code (a clean row is not "skipped"), and all
    of them must belong to the optional-channel family. One real error anywhere
    -- a fetch failure, a body parse failure, an extraction failure -- makes this
    False, so a broken source can never be waved through as skipped.
    """
    codes = (
        row_codes(row, "errors")
        + row_codes(row, "parse_errors")
        + row_codes(row, "candidate_errors")
    )
    return bool(codes) and set(codes) <= OPTIONAL_CHANNEL_CODES


def row_failure_codes(row):
    """The codes that make a collection row a real failure (empty = no failure).

    Three kinds of evidence count, and nothing else:

    * the source fetch itself failed for a reason that is not "not switched on"
      (unreachable host, upstream status, an address refused by the safety check,
      a transport error) -- ``collect.source_status`` already sorts the designed
      refusals (``article_redirected_off_pattern``, ``html_access_interstitial``)
      into its own ``blocked`` state, and they are not failures;
    * a **notice body** failed to parse (``parse_errors``). Attachment parse
      failures are counted separately in ``parse_attachment`` and stay
      degradations: a legacy ``.doc`` the product cannot read does not mean the
      notice is broken;
    * extraction failed (``candidate_errors``).
    """
    if optional_channel_only(row):
        return []
    codes = row_codes(row, "parse_errors") + row_codes(row, "candidate_errors")
    if row.get("status") == "failed":
        codes = row_codes(row, "errors") + codes
    return sorted(set(codes))


def classify_rows(rows):
    """Split collection rows into skipped / failed / degraded with their evidence.

    Skipped and failed rows both carry the codes that produced the verdict, so
    the report can print *why* instead of asking an operator to trust a label.
    """
    skipped, failures, degraded = [], [], []
    for row in rows:
        if optional_channel_only(row):
            codes = sorted(set(row_codes(row, "errors") + row_codes(row, "parse_errors") + row_codes(row, "candidate_errors")))
            row["status"] = "skipped"
            row["skip_reason"] = "可选来源未启用（" + "、".join(codes) + "）：通道没开，不是故障"
            skipped.append({"source_id": row.get("source_id"), "name": row.get("name"), "codes": codes, "reason": row["skip_reason"]})
            continue
        failure_codes = row_failure_codes(row)
        if failure_codes:
            row["status"] = "failed"
            row["failure_reason"] = "真实失败（" + "、".join(failure_codes) + "）"
            failures.append({"source_id": row.get("source_id"), "name": row.get("name"), "codes": failure_codes, "reason": row["failure_reason"]})
            continue
        if row.get("errors") or row.get("parse_errors") or row.get("candidate_errors"):
            row["degrade_reason"] = "设计内的降级（" + "、".join(sorted(set(
                row_codes(row, "errors") + row_codes(row, "parse_errors") + row_codes(row, "candidate_errors")
            ))) + "）：不绕过，通知本身已采到"
            degraded.append({"source_id": row.get("source_id"), "name": row.get("name"), "reason": row["degrade_reason"]})
    return {"skipped": skipped, "failures": failures, "degraded": degraded}


def review_actor(core):
    """Who the automatic review decisions are attributed to.

    ``review_and_publish.decide_directly`` needs a logged-in administrator in
    ``core.settings.admin_emails``; an unattended run has no session and may
    have no such account configured. The identity used here is the same one the
    prune sweep already records, so the audit trail stays answerable
    ("who removed / accepted this") without inventing a user.
    """
    return collect.prune_actor_id(core)


def apply_decision(core, candidate, decision, reason):
    """Apply one decision through the product's own review gate.

    ``Pipeline.review`` is the function the admin endpoint calls: it re-checks
    the candidate revision, validates the evidence, freezes a version and emits
    ``candidate.accepted``. Nothing here bypasses that gate.
    """
    return core.pipeline.review(
        candidate["candidate_id"],
        {
            "expected_candidate_revision": candidate["revision"],
            "decision": decision,
            "reason": reason,
            "replacement_candidate": None,
        },
        {"id": review_actor(core)},
    )


def pending_candidates(core):
    return [c for c in core.store.list("candidates") if c.get("review_status") == "pending"]


def accepted_candidates(core):
    return [c for c in core.store.list("candidates") if c.get("review_status") == "accepted"]


def prepare_readonly_secret(data_dir):
    """Keep a dry run from creating the data directory's signing key.

    ``Core`` generates ``.server-secret`` when the configured secret is empty,
    which would be a write during a dry run. The stored key is reused when it
    exists (so anything signed during the run matches the real instance); on a
    directory that has none, an in-process placeholder is used and nothing is
    written. Returns a restore callable.
    """
    previous = os.environ.get("CAMPUS_SECRET")
    if previous:
        return lambda: None
    stored = Path(data_dir) / ".server-secret"
    try:
        secret = stored.read_text(encoding="ascii").strip()
    except OSError:
        secret = ""
    os.environ["CAMPUS_SECRET"] = secret if len(secret) >= 32 else "dry-run-only-secret-" + "x" * 24

    def restore():
        if previous is None:
            os.environ.pop("CAMPUS_SECRET", None)
        else:
            os.environ["CAMPUS_SECRET"] = previous

    return restore


def selectable_sources(core):
    """Sources a ``--source`` selector may name, before anything is written.

    A fresh data directory has not registered the catalog yet, so validating a
    selector against the store alone would reject ``--source <official column>``
    on exactly the first run. The catalog entries are therefore offered as
    candidates too; registration still happens later, and a genuine typo is
    still a usage error with nothing written.
    """
    from server.campus.sources import builtin_sources

    known = {source["id"]: source for source in core.store.list("sources")}
    for entry in builtin_sources():
        known.setdefault(entry["id"], entry)
    return list(known.values())


def planned_registration(core):
    """What ``Sources.seed()`` would add, computed without writing anything.

    A dry run must leave the data directory byte-for-byte as it found it, so the
    catalog is compared against the stored sources instead of being seeded.
    """
    from server.campus.sources import builtin_sources

    stored = {source["id"] for source in core.store.list("sources")}
    planned = [entry for entry in builtin_sources() if entry["id"] not in stored]
    return {"new": len(planned), "existing": len(stored)}


def collect_stage(core, args, fetcher, apply, registration):
    """Stage 1: fetch, parse and extract candidates for every selected source.

    Source registration is done by the caller (``run``) so that a mistyped
    ``--source`` can be reported as a usage error before any work starts; the
    registration counts are passed in rather than recomputed.
    """
    if not apply:
        sources = collect.enabled_sources(core)
        backlog = 0
        for source in sources:
            summary = collect._backlog(core, source) or {}
            backlog += summary.get("pending_count", 0)
        return {
            "applied": False,
            "registration": registration,
            "planned_sources": len(sources) + registration.get("new", 0),
            "planned_backlog": backlog,
            "totals": {},
            "rows": [],
            "exit_code": EXIT_OK,
        }
    summary = collect.collect(core, selectors=args.source, fetcher=fetcher)
    verdict = classify_rows(summary["sources"])
    totals = dict(summary["totals"])
    # ``campus_collect`` counts a source's status; the exit code is decided here,
    # on the evidence above, so "the relay is not running" cannot keep failing a
    # run that has nothing else to report.
    totals["skipped"] = len(verdict["skipped"])
    totals["failed"] = len(verdict["failures"])
    return {
        "applied": True,
        "registration": registration,
        "rows": summary["sources"],
        "totals": totals,
        "skipped": verdict["skipped"],
        "failures": verdict["failures"],
        "degraded": verdict["degraded"],
        "exit_code": EXIT_FAILED if verdict["failures"] else EXIT_OK,
    }


def review_stage(core, today, apply):
    """Stage 2: apply the shared three-state policy to every pending candidate."""
    pending = pending_candidates(core)
    counts = Counter()
    rows = []
    errors = []
    for candidate in pending:
        try:
            decision, reason = review_policy.classify(candidate, today=today)
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            errors.append(_code(error))
            continue
        counts[decision] += 1
        row = {
            "candidate_id": candidate["candidate_id"],
            "title": candidate.get("title"),
            "decision": decision,
            "reason": reason,
        }
        if apply and decision != "review":
            try:
                apply_decision(core, candidate, decision, reason)
            except Exception as error:  # noqa: BLE001
                errors.append(_code(error))
                row["error"] = _code(error)
                counts["error"] += 1
        rows.append(row)
    # Publishing is a separate step from deciding, exactly as in the manual
    # command: an accepted candidate becomes an event version through
    # reconcile_candidate, which is idempotent per (candidate, revision). Only a
    # call that actually created a new event version is counted as published —
    # otherwise every daily run would re-report last week's notices as today's
    # news, which is exactly the kind of dishonest number this command must not
    # print.
    before_versions = {event["id"]: event["version"] for event in core.store.list("events")}
    reconciled = 0
    published = 0
    publish_errors = []
    if apply:
        for candidate in accepted_candidates(core):
            try:
                event = core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"])
            except Exception as error:  # noqa: BLE001
                publish_errors.append(_code(error))
                continue
            reconciled += 1
            if event and before_versions.get(event["id"]) != event["version"]:
                published += 1
    return {
        "applied": apply,
        "pending": len(pending),
        "accept": counts["accept"],
        "reject": counts["reject"],
        "review": counts["review"],
        "error": counts["error"],
        "reconciled": reconciled,
        "published": published,
        "publish_errors": publish_errors,
        "rows": rows,
        "errors": errors,
        "planned_publish": 0 if apply else len(accepted_candidates(core)),
    }


def withdraw_stage(core, today, apply):
    """Stage 3: take expired notices and non-notices out of the student surfaces."""
    actor = review_actor(core)
    expired = core.events.withdraw_expired(
        today,
        actor_id=actor,
        reason=f"截止时间已过（{today.isoformat()}），自动下架",
        apply=apply,
    )
    unusable = core.events.withdraw_unusable(actor_id=actor, apply=apply)
    return {
        "applied": apply,
        "expired": expired["expired"],
        "unusable": unusable["unusable"],
    }


def render(summary, out):
    mode = "执行（写入）" if summary["apply"] else "演练（不改动数据）"
    print(f"校园通知日常更新 · {mode}", file=out)
    print(f"数据目录：{summary['data_dir']}", file=out)
    gateway = summary.get("gateway") or {}
    if gateway.get("source") == "config":
        print(f"受信网关：config/campus-sources.json（{len(gateway.get('origins') or [])} 个 origin，仅本进程）", file=out)
    elif gateway.get("source") == "env":
        print(f"受信网关：CAMPUS_TRUSTED_GATEWAY_ORIGINS（{len(gateway.get('origins') or [])} 个 origin）", file=out)
    else:
        print("受信网关：未设置（本机 fake-IP 环境下会得到 unsafe_address）", file=out)
    if summary.get("missing_directory"):
        print("\n数据目录尚不存在：这是首次运行。", file=out)
    collection = summary["stages"]["collect"]
    print("\n[1/3] 采集", file=out)
    registration = collection.get("registration") or {}
    print(f"  来源注册    新增 {registration.get('new', 0)} · 复用 {registration.get('existing', 0)}", file=out)
    if not collection.get("applied"):
        print(
            f"  将采集      {collection.get('planned_sources', 0)} 个启用来源 · "
            f"积压 {collection.get('planned_backlog', 0)} 篇待抓",
            file=out,
        )
    elif collection.get("rows"):
        totals = collection["totals"]
        print(
            f"  来源        {totals['sources']} 个：成功 {totals['ok']} · 降级 {totals['degraded']} · "
            f"需授权 {totals.get('blocked', 0)} · 跳过 {totals.get('skipped', 0)} · 失败 {totals['failed']}",
            file=out,
        )
        print(
            f"  入库        新增快照 {totals['new_snapshots']} · 新候选 {totals['candidates_new']} · "
            f"解析 ok {totals['parse']['ok']}/partial {totals['parse']['partial']}/failed {totals['parse']['failed']}",
            file=out,
        )
        for row in collection.get("skipped") or []:
            print(f"    跳过来源  {row['name']}：{row['reason']}", file=out)
        for row in collection.get("failures") or []:
            print(f"    失败来源  {row['name']}：{row['reason']}", file=out)
        degraded_rows = collection.get("degraded") or []
        for row in degraded_rows[:10]:
            print(f"    降级来源  {row['name']}：{row['reason']}", file=out)
        if len(degraded_rows) > 10:
            print(f"    …… 另有 {len(degraded_rows) - 10} 个降级来源", file=out)
        if collection.get("skipped") or collection.get("failures"):
            print(
                "  判据        跳过 = 只带「可选通道未启用」错误码；失败 = 源取不到/正文解析失败/抽取失败；"
                "降级 = 设计内拒绝（如附件要授权），不影响退出码",
                file=out,
            )
        if totals["error_codes"]:
            print(
                "  错误码      "
                + ", ".join(
                    f"{code}×{count}" if count > 1 else code for code, count in sorted(totals["error_codes"].items())
                ),
                file=out,
            )
    else:
        print("  结果        没有启用中的来源；先运行 campus_collect.py init 注册官方来源", file=out)

    review = summary["stages"]["review"]
    print("\n[2/3] 复核", file=out)
    print(
        f"  待复核      {review['pending']} 条 → 自动接受 {review['accept']} · "
        f"拒绝 {review['reject']} · 留给人 {review['review']}"
        + (f" · 决策失败 {review['error']}" if review["error"] else ""),
        file=out,
    )
    if review["applied"]:
        print(
            f"  发布        新增发布 {review['published']} 条 · "
            f"核对已接受候选 {review.get('reconciled', 0)} 条（已发布过的不会重复计入）",
            file=out,
        )
    else:
        print(f"  将发布      已接受但未发布的候选 {review['planned_publish']} 条", file=out)
    if review["publish_errors"]:
        print("  发布失败    " + ", ".join(sorted(Counter(review["publish_errors"]))), file=out)
    if review["review"]:
        print("  说明        留给人判断的候选没有写入任何决策；多数是长期参考资料，正则分不出来。", file=out)

    withdrawal = summary["stages"]["withdraw"]
    print("\n[3/3] 下架", file=out)
    verb = "已下架" if withdrawal["applied"] else "将下架"
    print(f"  过期事项    {len(withdrawal['expired'])} 条（{verb}）", file=out)
    for row in withdrawal["expired"][:10]:
        print(f"    {row['deadline']}  {row['title'][:48]}", file=out)
    if len(withdrawal["expired"]) > 10:
        print(f"    …… 另有 {len(withdrawal['expired']) - 10} 条，见 --json-out", file=out)
    print(f"  非通知      {len(withdrawal['unusable'])} 条（{verb}）", file=out)
    for row in withdrawal["unusable"][:10]:
        print(f"    [{row['reason'][:14]:14s}] {row['title'][:44]}", file=out)
    if len(withdrawal["unusable"]) > 10:
        print(f"    …… 另有 {len(withdrawal['unusable']) - 10} 条，见 --json-out", file=out)
    if withdrawal["expired"] or withdrawal["unusable"]:
        print("  保护        仍带未来截止时间的一律不动，避免误伤还能办的事", file=out)

    print("\n今天新增了什么", file=out)
    if summary.get("nothing_to_read"):
        print(
            "  数据目录还没有产品数据，没有可汇报的内容。"
            "加 --apply 会创建目录、注册官方来源并开始第一次采集。",
            file=out,
        )
    elif summary["apply"]:
        totals = collection.get("totals") or {}
        print(
            f"  采集        新增快照 {totals.get('new_snapshots', 0)} · 新候选 {totals.get('candidates_new', 0)}",
            file=out,
        )
        print(
            f"  复核        接受 {review['accept']} · 拒绝 {review['reject']} · 留给人 {review['review']}",
            file=out,
        )
        print(f"  发布        {review['published']} 条", file=out)
        print(
            f"  下架        {len(withdrawal['expired'])} 条过期 · {len(withdrawal['unusable'])} 条非通知",
            file=out,
        )
        if not (
            (totals.get("new_snapshots") or 0)
            or review["accept"]
            or review["reject"]
            or withdrawal["expired"]
            or withdrawal["unusable"]
        ):
            print("  结论        今天没有新内容：来源未更新，也没有需要下架的事项。", file=out)
    else:
        print(
            f"  计划        采集 {collection.get('planned_sources', 0)} 个来源 · "
            f"接受 {review['accept']} · 拒绝 {review['reject']} · 留给人 {review['review']} · "
            f"发布 {review['planned_publish']} · 下架 "
            f"{len(withdrawal['expired']) + len(withdrawal['unusable'])}",
            file=out,
        )
        print("  下一步      确认无误后加 --apply 执行；不加参数不会改动任何数据。", file=out)
    if summary["errors"]:
        print("\n本段错误", file=out)
        for error in summary["errors"]:
            print(f"  [{error['stage']}] {error['code']}", file=out)
    if summary["exit_code"] == EXIT_FAILED:
        detail = "（有失败但未阻断）"
    else:
        skipped_count = len((summary["stages"].get("collect") or {}).get("skipped") or [])
        detail = f"（干净：{skipped_count} 个可选来源未启用，只算跳过）" if skipped_count else "（干净）"
    print(f"\n退出码 {summary['exit_code']}" + detail, file=out)


def has_existing_store(data_dir):
    """True when there is product data to read in ``data_dir``.

    A dry run over a directory that holds no store yet has nothing to report and
    must not create one (the product creates the database and its secret on
    first open). ``CAMPUS_DATABASE_URL`` may point at a server database outside
    this directory, in which case there is always something to read.
    """
    if os.environ.get("CAMPUS_DATABASE_URL"):
        return True
    directory = Path(data_dir)
    return any((directory / name).exists() for name in ("campus.sqlite3", ".server-secret"))


def run(argv=None, out=None, err=None, fetcher=None, core_factory=None):
    """Run the daily update; return the process exit code.

    ``fetcher`` and ``core_factory`` exist for the offline test suite (an
    explicit fetch double and a core whose source catalog is a fixture); the
    command line never passes them.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(
        prog="daily_update.py",
        description="一条命令完成每日更新：采集 → 复核 → 下架（默认演练）。",
    )
    parser.add_argument("--data-dir", help="数据目录（默认环境变量 CAMPUS_DATA_DIR 或 data-operator/）")
    parser.add_argument("--apply", action="store_true", help="真正写入（采集、决策、发布、下架）；缺省只演练")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只演练，不改动数据（缺省行为；显式写出来便于定时任务自我说明）",
    )
    parser.add_argument("--today", help="以该日期判断过期与可办（YYYY-MM-DD，默认今天，Asia/Shanghai）")
    parser.add_argument("--source", action="append", default=[], help="只采集指定来源（id / 全名 / 名称片段，可重复）")
    parser.add_argument("--timeout", type=int, default=20, help="单次 HTTP 请求超时秒数（默认 20）")
    parser.add_argument("--json-out", help="把机器可读摘要写入该路径")
    parser.add_argument("--gateway-from-config", action="store_true", help="用配置里的官方 origin 填受信网关白名单（仅本进程）")
    parser.add_argument("--check-gateway", action="store_true", help="运行前预检 DNS 是否落到非全局地址（会解析官方域名）")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or EXIT_USAGE)

    if args.apply and args.dry_run:
        print("--apply 与 --dry-run 不能同时使用：一个是写入，一个是演练。", file=err)
        return EXIT_USAGE

    if args.today:
        try:
            today = date.fromisoformat(args.today)
        except ValueError:
            print(f"--today 需要 YYYY-MM-DD 格式的日期，收到：{args.today}", file=err)
            return EXIT_USAGE
    else:
        today = datetime.now(TZ_SHANGHAI).date()

    requested = (
        Path(args.data_dir)
        if args.data_dir
        else Path(os.environ.get("CAMPUS_DATA_DIR") or collect.DEFAULT_DATA_DIR)
    )
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    try:
        data_dir = collect._guard_data_dir(requested)
    except collect.UsageError as error:
        print(str(error), file=err)
        return EXIT_USAGE

    origins = collect.split_origins(os.environ.get("CAMPUS_TRUSTED_GATEWAY_ORIGINS"))
    gateway = {"source": "env" if origins else "none", "origins": origins}
    if not origins and args.gateway_from_config:
        origins = collect.gateway_origins_from_config()
        os.environ["CAMPUS_TRUSTED_GATEWAY_ORIGINS"] = ",".join(origins)
        gateway = {"source": "config", "origins": origins}

    hint = None
    if args.check_gateway and not origins:
        flagged = collect.diagnose_gateway(collect.gateway_origins_from_config())
        if flagged:
            hint = collect.gateway_hint(flagged)

    summary = {
        "command": "daily_update",
        "apply": bool(args.apply),
        "today": today.isoformat(),
        "data_dir": collect.display_path(data_dir),
        "gateway": {"source": gateway["source"], "origins": list(gateway["origins"])},
        "missing_directory": not data_dir.exists(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stages": {},
        "errors": [],
    }
    if hint:
        summary["hint"] = hint

    # A dry run over a directory with no product data has nothing to read and
    # must not create one: report the plan from the shipped catalog and stop.
    if not args.apply and not has_existing_store(data_dir):
        from server.campus.sources import builtin_sources

        planned = len(builtin_sources())
        summary["nothing_to_read"] = True
        summary["stages"] = {
            "collect": {
                "applied": False,
                "registration": {"new": planned, "existing": 0},
                "planned_sources": planned,
                "planned_backlog": 0,
            },
            "review": {
                "applied": False,
                "pending": 0,
                "accept": 0,
                "reject": 0,
                "review": 0,
                "error": 0,
                "published": 0,
                "publish_errors": [],
                "planned_publish": 0,
            },
            "withdraw": {"applied": False, "expired": [], "unusable": []},
        }
        summary["exit_code"] = EXIT_OK
        render(summary, out)
        _write_json(args.json_out, summary, out)
        return EXIT_OK

    lock = None
    core = None
    restore_secret = lambda: None  # noqa: E731 - a no-op default keeps the finally block simple
    try:
        if args.apply:
            data_dir.mkdir(parents=True, exist_ok=True)
        else:
            # ``Core`` would otherwise generate ``.server-secret`` on first open,
            # which is a write: a dry run must leave the directory as it found it.
            restore_secret = prepare_readonly_secret(data_dir)
        lock = ProcessLock(data_dir / LOCK_NAME)
        if not lock.acquire():
            print(
                f"另一个每日更新正在运行（锁：{collect.display_path(data_dir / LOCK_NAME)}）。"
                "本次不做任何改动，请等它结束后重试。",
                file=err,
            )
            summary["exit_code"] = EXIT_FAILED
            summary["errors"].append({"stage": "lock", "code": "already_running"})
            return EXIT_FAILED
        factory = core_factory or collect.build_core
        core = factory(data_dir, timeout=args.timeout)

        def stage(name, function):
            try:
                return function()
            except collect.UsageError as error:
                summary["errors"].append({"stage": name, "code": _code(error)})
                print(str(error), file=err)
                return None
            except Exception as error:  # noqa: BLE001 - recorded, later stages still run
                summary["errors"].append({"stage": name, "code": _code(error)})
                print(f"[{name}] 失败：{_code(error)}（继续后续段落）", file=err)
                return None

        # Registration comes before selector validation: on a fresh directory the
        # catalog is not in the store yet, so `--source <official column>` would
        # otherwise be reported as "not found" on exactly the first run.
        if args.apply:
            existing = {source["id"] for source in core.store.list("sources")}
            core.pipeline.sources.seed()
            registered = {source["id"] for source in core.store.list("sources")}
            registration = {"new": len(registered - existing), "existing": len(existing)}
        else:
            registration = planned_registration(core)

        # A mistyped --source is a usage error, and it must be caught before any
        # fetch: otherwise a scheduled run would quietly collect nothing.
        try:
            collect.select_sources(selectable_sources(core), args.source)
        except collect.UsageError as error:
            print(str(error), file=err)
            return EXIT_USAGE

        collected = stage("collect", lambda: collect_stage(core, args, fetcher, args.apply, registration))
        summary["stages"]["collect"] = collected or {
            "applied": False,
            "registration": {},
            "planned_sources": 0,
            "planned_backlog": 0,
            "exit_code": EXIT_FAILED,
        }
        reviewed = stage("review", lambda: review_stage(core, today, args.apply))
        summary["stages"]["review"] = reviewed or {
            "applied": args.apply,
            "pending": 0,
            "accept": 0,
            "reject": 0,
            "review": 0,
            "error": 0,
            "reconciled": 0,
            "published": 0,
            "publish_errors": [],
            "planned_publish": 0,
        }
        withdrawn = stage("withdraw", lambda: withdraw_stage(core, today, args.apply))
        summary["stages"]["withdraw"] = withdrawn or {
            "applied": args.apply,
            "expired": [],
            "unusable": [],
        }
    except collect.UsageError as error:
        print(str(error), file=err)
        return EXIT_USAGE
    finally:
        if core is not None:
            core.store.close()
        if lock is not None:
            lock.release()
        restore_secret()

    collection, review = summary["stages"]["collect"], summary["stages"]["review"]
    failed = bool(summary["errors"])
    failed = failed or (collection.get("exit_code") or EXIT_OK) != EXIT_OK
    failed = failed or bool(review.get("error")) or bool(review.get("publish_errors"))
    summary["exit_code"] = EXIT_FAILED if failed else EXIT_OK
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    render(summary, out)
    _write_json(args.json_out, summary, out)
    return summary["exit_code"]


def _write_json(path, summary, out):
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"\n机器可读摘要：{collect.display_path(target)}", file=out)


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
