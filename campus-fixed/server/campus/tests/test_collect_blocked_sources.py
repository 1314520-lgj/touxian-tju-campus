"""A source the upstream refuses to serve is "blocked", not "failed".

教务处 columns publish almost only 公众号 links; an unauthenticated fetch is
answered with an anti-scraping challenge, so a daily run can collect nothing
from them. That is a stable, documented state which needs an authorized channel
(relay / authorized feed) — not a transient outage. Reporting it as `failed`
made a healthy deployment look broken every single day.
"""

import io

from server.campus.sources import FetchError
from scripts import campus_collect as collect


def _summary_with_code(code, health):
    """Build the smallest summary that goes through the real status logic."""
    status = collect.source_status(code, health)
    return {
        "collected": True,
        "gateway": {"source": "config", "origins": []},
        "data_dir": "data-operator",
        "sources": [
            {
                "name": "天津大学教务处 · 学生竞赛",
                "status": status,
                "new_snapshots": 0,
                "parse": {"ok": 0, "partial": 0, "failed": 0},
                "parse_attachment": {"ok": 0, "partial": 0, "failed": 0},
                "candidates_new": 0,
                "errors": [code],
                "parse_errors": [],
                "candidate_errors": [],
                "skipped_no_body": 0,
                "backlog": {},
            }
        ],
        "totals": {
            "sources": 1,
            "ok": 0,
            "degraded": 0,
            "blocked": 1 if status == "blocked" else 0,
            "failed": 1 if status == "failed" else 0,
            "new_snapshots": 0,
            "documents": 0,
            "candidates_total": 0,
            "candidates_new": 0,
            "candidates_pending": 0,
            "parse": {"ok": 0, "partial": 0, "failed": 0},
            "parse_attachments": {"ok": 0, "partial": 0, "failed": 0},
            "skipped_no_body": 0,
            "backlog_pending": 0,
            "backlog_failed": 0,
            "error_codes": {code: 1},
        },
    }


def test_anti_scraping_block_is_a_blocked_source():
    assert collect.source_status("article_redirected_off_pattern", "auth_expired") == "blocked"
    assert collect.source_status("html_access_interstitial", "auth_expired") == "blocked"


def test_a_transport_problem_stays_a_failure():
    """unreachable / rejected addresses are real failures, never "blocked"."""
    assert collect.source_status("unsafe_address", "failed") == "failed"
    assert collect.source_status("fetch_timeout", "failed") == "failed"
    assert collect.source_status("dns_failed", "failed") == "failed"


def test_a_blocked_source_does_not_fail_the_daily_run():
    summary = _summary_with_code("article_redirected_off_pattern", "auth_expired")
    assert collect.exit_code(summary["totals"]) == collect.EXIT_OK


def test_a_failed_source_still_fails_the_daily_run():
    summary = _summary_with_code("unsafe_address", "failed")
    assert collect.exit_code(summary["totals"]) == collect.EXIT_FAILED


def test_the_summary_explains_that_authorization_is_needed():
    summary = _summary_with_code("article_redirected_off_pattern", "auth_expired")
    out = io.StringIO()
    collect.render_summary(summary, out)
    text = out.getvalue()
    assert "需授权" in text or "授权通道" in text


def test_blocked_codes_are_the_documented_ones():
    assert collect.BLOCKED_CODES == frozenset({"article_redirected_off_pattern", "html_access_interstitial"})


def test_fetch_error_codes_used_here_exist_in_the_guard(monkeypatch):
    """Guard against a typo drifting away from what sources.py can actually raise."""
    import server.campus.sources as sources

    assert isinstance(FetchError("article_redirected_off_pattern"), FetchError)
    assert "article_redirected_off_pattern" in set(sources.BLOCKED_BY_UPSTREAM)
