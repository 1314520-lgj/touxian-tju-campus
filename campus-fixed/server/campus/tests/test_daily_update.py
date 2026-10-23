"""One command for the daily update: collect -> review -> withdraw.

The operator's daily loop used to be three remembered commands in the right
order (``campus_collect.py collect``, ``review_and_publish.py``, then
``campus_collect.py prune``). One person using the product every day cannot be
relied on to remember that, and running them out of order does nothing. These
tests pin the properties that make a single unattended command trustworthy:

* a dry run reports what *would* happen and writes nothing at all;
* ``--apply`` collects, applies the shared three-state review policy and
  withdraws expired / non-notice events in one pass;
* a stage that fails is recorded with its error code and never stops the later
  stages -- an unreachable official site must not keep yesterday's expired
  notices on a student's list;
* a second concurrent invocation refuses to run instead of writing over the
  first one;
* exit codes are 0 (clean), 1 (failures, not blocking) and 2 (usage error).

The stages themselves are not re-implemented here: the collection stage is
``scripts/campus_collect.py``, the review verdict is
``scripts/review_and_publish.classify`` and the withdrawal is
``Events.withdraw_expired`` / ``Events.withdraw_unusable``. Two tests spy on
those entry points so a future refactor cannot quietly fork the policy.
"""

import importlib.util
import io
import sys
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.campus.sources import FetchError, FetchResult  # noqa: E402

SCRIPTS = ROOT / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect = load("campus_collect")
daily_update = load("daily_update")

SOURCE_ID = "11111111-1111-5111-8111-111111111111"
USER_ID = "22222222-2222-5222-8222-222222222222"
LISTING_URL = "https://public.test/list"
ARTICLE_URL = "https://public.test/info/1/1.htm"
TODAY = date(2026, 10, 20)
LISTING = b'<html><body><a href="/info/1/1.htm">2026 notice</a></body></html>'


def fixture_source():
    return {
        "id": SOURCE_ID,
        "name": "Fixture official column",
        "kind": "website",
        "origin_url": LISTING_URL,
        "adapter": "website",
        "source_identity": "fixture",
        "verification": "verified_official",
        "verification_basis": "fixture",
        "enabled": True,
        "health": "never_checked",
        "last_success_at": None,
        "last_checked_at": None,
        "last_error_code": None,
    }


class MappingFetcher:
    """Explicit offline fetch double; every miss is a labeled failure."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def fetch(self, url, headers=None):
        self.calls.append(url)
        value = self.mapping.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise FetchError("fixture_url_missing")
        return value


def article(title, sentence):
    return (
        f"<html><head><title>{title}</title></head><body><h1>{title}</h1>"
        f'<div class="v_news_content"><p>{sentence}</p></div></body></html>'
    ).encode()


class DailyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "operator"

    # -- helpers ------------------------------------------------------------
    def factory(self, data_dir, timeout=None):
        """A core with no catalog sources, so only this test's fixtures exist.

        ``Sources.seed()`` reads the shipped catalog through
        ``builtin_sources()`` rather than the instance's ``config_path``, so the
        catalog is stubbed for the duration of the test: otherwise every run
        would try the twenty real official sites with the offline fetch double
        and report twenty failures.
        """
        from server.campus import sources as sources_module

        patcher = mock.patch.object(sources_module, "builtin_sources", lambda: [])
        patcher.start()
        self.addCleanup(patcher.stop)
        return collect.build_core(data_dir, timeout=timeout)

    def open_core(self):
        return self.factory(self.data_dir)

    def prepare(self):
        """A data directory with one fixture source and one operator account."""
        core = self.open_core()
        core.store.put("sources", SOURCE_ID, fixture_source())
        core.store.put("users", USER_ID, {"id": USER_ID, "email": "operator@example.com", "role": "admin"})
        return core

    def notice(self, core, title, sentence, published="2026-10-01T00:00:00+08:00"):
        """Persist, parse and extract one real notice through the pipeline."""
        source = core.store.get("sources", SOURCE_ID)
        url = "https://public.test/info/1/" + str(abs(hash(title)) % 10**6) + ".htm"
        snapshot, _ = core.pipeline.sources.persist(
            source, FetchResult(url, article(title, sentence), "text/html"), title, published
        )
        core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
        return core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})

    def publish(self, core, title, sentence):
        """Accept and reconcile one notice so it is live, whatever its title."""
        candidate = self.notice(core, title, sentence)
        core.pipeline.review(
            candidate["candidate_id"],
            {
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "fixture publish",
                "replacement_candidate": None,
            },
            {"id": USER_ID},
        )
        accepted = core.store.get("candidates", candidate["candidate_id"])
        return core.events.reconcile_candidate(candidate["candidate_id"], accepted["revision"])

    def read_state(self):
        core = self.open_core()
        try:
            return {
                "candidates": sorted(
                    (c["candidate_id"], c["review_status"], c["revision"]) for c in core.store.list("candidates")
                ),
                "events": sorted((e["id"], e["publication_state"], e["version"]) for e in core.store.list("events")),
                "reviews": len(core.store.list("reviews")),
                "publication_audits": len(core.store.list("publication_audits")),
            }
        finally:
            core.store.close()

    def run_cli(self, argv, fetcher=None):
        out, err = io.StringIO(), io.StringIO()
        code = daily_update.run(
            argv,
            out=out,
            err=err,
            fetcher=fetcher if fetcher is not None else MappingFetcher({}),
            core_factory=self.factory,
        )
        return code, out.getvalue(), err.getvalue()

    # -- dry run ------------------------------------------------------------
    def test_a_dry_run_reports_the_plan_and_writes_nothing(self):
        core = self.prepare()
        self.notice(core, "2027年寒假剑桥项目报名通知", "全体本科生请于2026年11月30日前提交申请表。")
        # A news piece is a real candidate that the review policy refuses; error
        # and access pages never get this far (they fail in the body policy).
        self.notice(core, "天津大学天津医院召开建院165周年高质量发展大会", "会议由院长主持。")
        self.notice(core, "关于研究生课程缓考申请的流程", "本流程长期有效。")
        # A WeChat anti-scraping page never becomes a candidate at all, so the
        # review stage has nothing to reject: that gate is upstream (t5).
        self.assertIsNone(
            self.notice(core, "环境异常", "当前环境异常，完成验证后即可继续访问。"),
            "an access page must not reach the review queue",
        )
        core.store.close()
        before = self.read_state()

        code, out, err = self.run_cli(["--data-dir", str(self.data_dir), "--today", TODAY.isoformat()])

        self.assertEqual(code, 0, err)
        self.assertEqual(self.read_state(), before, "a dry run must not write anything")
        self.assertIn("演练", out)
        self.assertIn("自动接受 1", out)
        self.assertIn("拒绝 1", out)
        self.assertIn("留给人 1", out)
        self.assertIn("今天新增了什么", out)

    def test_a_missing_data_directory_is_planned_without_creating_it(self):
        target = Path(self.tmp.name) / "not-yet"
        code, out, err = self.run_cli(["--data-dir", str(target)])
        self.assertEqual(code, 0, err)
        self.assertFalse(target.exists(), "a dry run must not create the data directory")
        self.assertIn("数据目录尚不存在", out)
        self.assertIn("加 --apply", out)

    def test_a_dry_run_over_existing_data_creates_no_signing_key(self):
        """``Core`` generates ``.server-secret`` on first open — a dry-run write.

        The data directory here holds a store but no key, which is what an
        operator sees if the key was rotated away. The dry run must still report
        and must not put one back.
        """
        core = self.prepare()
        self.notice(core, "2027年寒假剑桥项目报名通知", "全体本科生请于2026年11月30日前提交申请表。")
        core.store.close()
        secret = self.data_dir / ".server-secret"
        self.assertTrue(secret.exists(), "the fixture core created a key")
        secret.unlink()

        code, out, err = self.run_cli(["--data-dir", str(self.data_dir), "--today", TODAY.isoformat()])

        self.assertEqual(code, 0, err)
        self.assertFalse(secret.exists(), "a dry run must not generate a signing key")
        self.assertIn("自动接受 1", out)

    # -- apply --------------------------------------------------------------
    def test_apply_collects_reviews_and_withdraws_in_one_pass(self):
        core = self.prepare()
        self.notice(core, "2027年寒假剑桥项目报名通知", "全体本科生请于2026年11月30日前提交申请表。")
        self.notice(core, "天津大学天津医院召开建院165周年高质量发展大会", "会议由院长主持。")
        self.notice(core, "关于研究生课程缓考申请的流程", "本流程长期有效。")
        self.publish(core, "2020年研究生公共课考试安排", "考试时间：2020年1月11日。")
        self.publish(core, "下载中心", "各类表格下载。")
        core.store.close()

        fetcher = MappingFetcher(
            {
                LISTING_URL: FetchResult(LISTING_URL, LISTING, "text/html"),
                ARTICLE_URL: FetchResult(
                    ARTICLE_URL,
                    article("2026年秋季学期选课通知", "全体本科生请于2026年12月1日前完成选课。"),
                    "text/html",
                ),
            }
        )
        code, out, err = self.run_cli(
            ["--data-dir", str(self.data_dir), "--apply", "--today", TODAY.isoformat()], fetcher=fetcher
        )
        self.assertEqual(code, 0, out + err)

        state = self.read_state()
        statuses = Counter(status for _, status, _ in state["candidates"])
        # Two accepted before this run (the live fixtures), two accepted now: the
        # dated notice from the fixture column and the one the collector fetched.
        self.assertEqual(statuses["accepted"], 4, state["candidates"])
        self.assertEqual(statuses["rejected"], 1, state["candidates"])
        self.assertEqual(statuses["pending"], 1, state["candidates"])
        # The two live fixtures are withdrawn (expired / not a notice); the two
        # newly accepted candidates are published by the same pass.
        published = sorted(row[1] for row in state["events"])
        self.assertEqual(published, ["published", "published", "withdrawn", "withdrawn"], state["events"])
        self.assertGreaterEqual(state["publication_audits"], 2, "expired and unusable events are withdrawn")
        self.assertIn("下架", out)
        self.assertIn("今天新增了什么", out)

    def test_a_failing_source_is_reported_and_pruning_still_runs(self):
        core = self.prepare()
        self.publish(core, "2020年研究生公共课考试安排", "考试时间：2020年1月11日。")
        self.publish(core, "下载中心", "各类表格下载。")
        core.store.close()

        # Every fetch fails: the collect stage must report the code, not stop the run.
        code, out, err = self.run_cli(
            ["--data-dir", str(self.data_dir), "--apply", "--today", TODAY.isoformat()],
            fetcher=MappingFetcher({}),
        )
        self.assertEqual(code, 1, out + err)
        self.assertIn("fixture_url_missing", out)
        state = self.read_state()
        self.assertTrue(state["events"], "events must still exist after withdrawal")
        self.assertEqual([row for row in state["events"] if row[1] == "published"], [], state["events"])
        self.assertIn("下架", out)

    # -- lock ---------------------------------------------------------------
    def test_a_second_run_refuses_while_the_lock_is_held(self):
        core = self.prepare()
        self.notice(core, "2027年寒假剑桥项目报名通知", "全体本科生请于2026年11月30日前提交申请表。")
        core.store.close()
        before = self.read_state()

        lock = daily_update.ProcessLock(self.data_dir / daily_update.LOCK_NAME)
        self.assertTrue(lock.acquire())
        try:
            code, out, err = self.run_cli(["--data-dir", str(self.data_dir), "--apply", "--today", TODAY.isoformat()])
        finally:
            lock.release()

        self.assertEqual(code, 1, out + err)
        self.assertIn("正在运行", err)
        self.assertEqual(self.read_state(), before)

    def test_the_lock_is_released_after_a_normal_run(self):
        core = self.prepare()
        core.store.close()
        code, out, err = self.run_cli(["--data-dir", str(self.data_dir), "--today", TODAY.isoformat()])
        self.assertEqual(code, 0, err)
        lock = daily_update.ProcessLock(self.data_dir / daily_update.LOCK_NAME)
        self.assertTrue(lock.acquire(), "the run must release its lock")
        lock.release()

    # -- exit codes ---------------------------------------------------------
    def test_the_bundled_sample_directory_is_a_usage_error(self):
        code, out, err = self.run_cli(["--data-dir", str(ROOT / "data")])
        self.assertEqual(code, 2)
        self.assertIn("随包样本", err)

    def test_an_invalid_date_is_a_usage_error(self):
        core = self.prepare()
        core.store.close()
        code, out, err = self.run_cli(["--data-dir", str(self.data_dir), "--today", "not-a-date"])
        self.assertEqual(code, 2)
        self.assertIn("YYYY-MM-DD", err)

    def test_an_unknown_source_selector_is_a_usage_error(self):
        core = self.prepare()
        core.store.close()
        code, out, err = self.run_cli(
            ["--data-dir", str(self.data_dir), "--apply", "--source", "does-not-exist", "--today", TODAY.isoformat()]
        )
        self.assertEqual(code, 2, out + err)
        self.assertIn("does-not-exist", err)

    def test_a_catalog_source_is_selectable_on_the_first_run(self):
        """A fresh directory has not registered the catalog yet.

        Validating ``--source`` against the store alone made the documented
        first-run command ("collect only this official column") fail with
        "来源未找到", which is a usage error the operator cannot act on.
        """
        core = self.prepare()
        core.store.close()
        with mock.patch.object(
            daily_update.collect, "select_sources", wraps=daily_update.collect.select_sources
        ) as select:
            code, out, err = self.run_cli(
                [
                    "--data-dir",
                    str(self.data_dir),
                    "--apply",
                    "--source",
                    "Fixture official column",
                    "--today",
                    TODAY.isoformat(),
                ],
                fetcher=MappingFetcher({}),
            )
        self.assertNotEqual(code, 2, "a registered fixture source must be selectable: " + err)
        self.assertTrue(select.called)
        chosen = select.call_args[0]
        self.assertIn(SOURCE_ID, [source["id"] for source in chosen[0]])

    def test_a_clean_run_exits_zero_and_a_failing_source_exits_one(self):
        core = self.prepare()
        core.store.close()
        good = MappingFetcher(
            {
                LISTING_URL: FetchResult(LISTING_URL, LISTING, "text/html"),
                ARTICLE_URL: FetchResult(
                    ARTICLE_URL,
                    article("2026年秋季学期选课通知", "全体本科生请于2026年12月1日前完成选课。"),
                    "text/html",
                ),
            }
        )
        code, out, err = self.run_cli(
            ["--data-dir", str(self.data_dir), "--apply", "--today", TODAY.isoformat()], fetcher=good
        )
        self.assertEqual(code, 0, out + err)
        code, out, err = self.run_cli(
            ["--data-dir", str(self.data_dir), "--apply", "--today", TODAY.isoformat()],
            fetcher=MappingFetcher({LISTING_URL: FetchError("dns_failed")}),
        )
        self.assertEqual(code, 1, out + err)
        self.assertIn("dns_failed", out)

    def test_a_second_apply_does_not_re_report_old_notices_as_new(self):
        """Yesterday's publication is not today's news.

        ``reconcile_candidate`` is idempotent, so a second apply run reconciles
        the same accepted candidates again. Counting those as "published" would
        print a growing number every day for content that has not changed; only
        a call that created a new event version may be counted.
        """
        core = self.prepare()
        self.notice(core, "2027年寒假剑桥项目报名通知", "全体本科生请于2026年11月30日前提交申请表。")
        core.store.close()
        argv = ["--data-dir", str(self.data_dir), "--apply", "--today", TODAY.isoformat()]
        # A fetcher that fails every article keeps this test about the publish
        # counter; the run still completes with exit code 1 (source failure).
        code, first, err = self.run_cli(argv)
        self.assertEqual(code, 1, err)
        self.assertIn("新增发布 1 条", first)
        code, second, err = self.run_cli(argv)
        self.assertEqual(code, 1, err)
        self.assertIn("新增发布 0 条", second, second)
        self.assertIn("今天没有新内容", second)

    def test_a_stage_that_raises_is_recorded_and_later_stages_still_run(self):
        """An exception in the review stage must not skip the withdrawal.

        The fallback summary has to stay renderable too: a stage that dies before
        returning its counts must not turn a reported failure into a traceback.
        """
        core = self.prepare()
        self.notice(core, "2027年寒假剑桥项目报名通知", "全体本科生请于2026年11月30日前提交申请表。")
        self.publish(core, "2020年研究生公共课考试安排", "考试时间：2020年1月11日。")
        core.store.close()

        with mock.patch.object(daily_update, "review_stage", side_effect=RuntimeError("boom")):
            code, out, err = self.run_cli(
                ["--data-dir", str(self.data_dir), "--apply", "--today", TODAY.isoformat()]
            )
        self.assertEqual(code, 1, out + err)
        # The exception type is the recorded code: an arbitrary message is never
        # printed, so no page text or credential can leak into a log.
        self.assertIn("RuntimeError", err)
        self.assertIn("[review] 失败", err)
        self.assertIn("继续后续段落", err)
        state = self.read_state()
        self.assertEqual([row for row in state["events"] if row[1] == "published"], [], state["events"])

    # -- reuse, not a second implementation ---------------------------------
    def test_the_review_verdict_comes_from_the_shared_policy(self):
        core = self.prepare()
        self.notice(core, "2027年寒假剑桥项目报名通知", "全体本科生请于2026年11月30日前提交申请表。")
        core.store.close()
        with mock.patch.object(
            daily_update.review_policy, "classify", wraps=daily_update.review_policy.classify
        ) as classify:
            code, out, err = self.run_cli(["--data-dir", str(self.data_dir), "--today", TODAY.isoformat()])
        self.assertEqual(code, 0, err)
        self.assertTrue(classify.called, "the daily command must reuse review_and_publish.classify")

    def test_the_collection_stage_is_the_existing_collector(self):
        core = self.prepare()
        core.store.close()
        fetcher = MappingFetcher(
            {
                LISTING_URL: FetchResult(LISTING_URL, LISTING, "text/html"),
                ARTICLE_URL: FetchResult(
                    ARTICLE_URL,
                    article("2026年秋季学期选课通知", "全体本科生请于2026年12月1日前完成选课。"),
                    "text/html",
                ),
            }
        )
        with mock.patch.object(daily_update.collect, "collect", wraps=daily_update.collect.collect) as stage:
            code, out, err = self.run_cli(
                ["--data-dir", str(self.data_dir), "--apply", "--today", TODAY.isoformat()], fetcher=fetcher
            )
        self.assertEqual(code, 0, out + err)
        self.assertTrue(stage.called, "the daily command must reuse campus_collect.collect")


if __name__ == "__main__":
    unittest.main()
