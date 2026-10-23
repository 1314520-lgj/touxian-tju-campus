"""The prune command is the operator's daily cleanup of expired notices.

The product is for daily use, so the command that keeps it clean has to be safe
by default: no flags means "show me what would go", and only --apply changes
anything. It must also leave undated and still-valid notices alone.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "campus_collect.py"


def run_prune(data_dir, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "prune", "--data-dir", str(data_dir), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
    )


@pytest.fixture
def seeded(tmp_path):
    """A small real store built through the product's own API."""
    from server.campus.platform import Settings
    from server.campus.schema import uid
    from server.campus.sources import FetchResult
    from server.campus.tests.test_integration import synthetic_source
    from server.campus_app import create_app

    app = create_app(
        Settings(data_dir=tmp_path, secret="prune-cli-" * 4, dev_auth=False, admin_emails=set()),
        start_worker=False,
    )
    core = app.state.core

    def publish(title, paragraphs):
        source = synthetic_source(core)
        body = (
            "<html><h1>"
            + title
            + "</h1><article>"
            + "".join("<p>" + text + "</p>" for text in ["全体本科生请注意。", *paragraphs])
            + "</article></html>"
        ).encode()
        snapshot, _ = core.pipeline.sources.persist(
            source, FetchResult("https://example.com/" + uid(), body, "text/html"), title, "2026-09-01T00:00:00+08:00"
        )
        core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
        candidate = core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
        candidate = core.pipeline.review(
            candidate["candidate_id"],
            {
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "核对本地合成原文",
                "replacement_candidate": None,
            },
            {"id": uid()},
        )
        return core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"])

    expired = publish("过期事项", ["报名截止时间：2020年1月5日。"])
    kept = publish("长期有效事项", ["研究生学位认证请前往一站式服务大厅办理。"])
    core.store.close()
    return tmp_path, expired["id"], kept["id"]


def states(data_dir):
    connection = sqlite3.connect(Path(data_dir) / "campus.sqlite3")
    out = {}
    for (key, raw) in connection.execute("select key, data from campus_records where collection='events'"):
        out[key] = json.loads(raw)["publication_state"]
    connection.close()
    return out


def test_prune_without_flags_changes_nothing(seeded):
    data_dir, expired, kept = seeded
    result = run_prune(data_dir, "--today", "2026-10-20", "--skip-dns-check")
    assert result.returncode == 0, result.stderr
    assert "演练" in result.stdout
    assert states(data_dir)[expired] == "published"


def test_prune_apply_withdraws_only_the_expired_one(seeded):
    data_dir, expired, kept = seeded
    result = run_prune(data_dir, "--today", "2026-10-20", "--apply", "--skip-dns-check")
    assert result.returncode == 0, result.stderr
    current = states(data_dir)
    assert current[expired] == "withdrawn"
    assert current[kept] == "published"


def test_prune_names_what_it_will_remove(seeded):
    data_dir, expired, kept = seeded
    result = run_prune(data_dir, "--today", "2026-10-20", "--skip-dns-check")
    assert "过期事项" in result.stdout
    assert "长期有效事项" not in result.stdout


def test_prune_writes_a_machine_readable_summary(seeded, tmp_path):
    data_dir, expired, kept = seeded
    out = tmp_path / "prune.json"
    result = run_prune(data_dir, "--today", "2026-10-20", "--apply", "--skip-dns-check", "--json-out", str(out))
    assert result.returncode == 0, result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["prune"]["applied"] is True
    assert [row["id"] for row in payload["prune"]["expired"]] == [expired]
    assert payload["totals"]["events_expired"] == 1