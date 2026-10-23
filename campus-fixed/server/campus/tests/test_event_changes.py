"""The public change history of an event: what each capture changed.

The pipeline already stores a change summary per re-captured document. These
tests pin the contract that exposes it, and the two moments where it must stay
silent: a first capture (nothing to compare) and a withdrawn event.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.campus.platform import APIError, Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source
from server.campus_app import create_app

BASE_BODY = "全体本科生请于2030年1月3日前提交确认表。"
EXTRA = {
    "initial": "教务处说明：本次确认用于学籍信息核对。",
    "revised": "教务处说明：本次确认用于学籍信息与导师意向核对。",
    "extended": "教务处说明：本次确认用于学籍信息、导师意向与宿舍安排核对，请提前准备。",
}


class EventChangeSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"CAMPUS_MEILI_URL": "", "CAMPUS_DEMO": "false"})
        self.environment.start()
        self.app = create_app(
            Settings(
                data_dir=Path(self.temp.name),
                secret="event-change-secret-" * 3,
                dev_auth=True,
                admin_emails={"admin@example.com"},
            ),
            start_worker=False,
        )
        self.core = self.app.state.core
        self.client = TestClient(self.app)
        # One source, so repeated captures of the same URL share one document.
        self.source = synthetic_source(self.core)

    def tearDown(self):
        self.client.close()
        self.core.store.close()
        self.environment.stop()
        self.temp.cleanup()

    def capture(self, url, paragraph):
        body = (
            f"<html><head><meta charset='utf-8'></head><body><h1>合成变更历史通知</h1><article>"
            f"<p>{BASE_BODY}</p><p>{paragraph}</p></article></body></html>".encode()
        )
        with self.core.store.transaction():
            snapshot, _ = self.core.pipeline.sources.persist(
                self.source,
                FetchResult(url, body, "text/html; charset=utf-8"),
                "合成变更历史通知",
                "2030-01-01T00:00:00Z",
            )
        self.core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
        candidate = self.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
        validate("DocumentSnapshot", snapshot)
        validate("ExtractionCandidate", candidate)
        return snapshot, candidate

    def publish(self, candidate):
        """Approve through the real review path, then reconcile into the event."""
        accepted = self.core.pipeline.review(
            candidate["candidate_id"],
            {
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "本地合成资料核对",
                "replacement_candidate": None,
            },
            {"id": uid()},
        )
        event = self.core.events.reconcile_candidate(candidate["candidate_id"], accepted["revision"])
        validate("EventVersion", event)
        return event

    def changes(self, event_id, status=200):
        response = self.client.get(f"/api/v1/events/{event_id}/changes")
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def test_first_capture_reports_no_change_rather_than_an_empty_edit(self):
        _, candidate = self.capture("https://example.com/integration/first", EXTRA["initial"])
        event = self.publish(candidate)
        body = self.changes(event["id"])
        validate("EventChangeSummary", body)
        self.assertEqual(body["event_id"], event["id"])
        self.assertEqual(body["version"], event["version"])
        self.assertEqual(body["changes"], [])

    def test_recapture_reports_the_change_and_stays_contract_valid(self):
        url = "https://example.com/integration/stable"
        first_snapshot, candidate = self.capture(url, EXTRA["initial"])
        event = self.publish(candidate)
        second_snapshot, candidate = self.capture(url, EXTRA["revised"])
        self.assertEqual(second_snapshot["document_id"], first_snapshot["document_id"])
        event = self.publish(candidate)
        body = self.changes(event["id"])
        validate("EventChangeSummary", body)
        self.assertTrue(body["changes"])
        reported = {row["snapshot_id"] for row in body["changes"]}
        self.assertIn(second_snapshot["snapshot_id"], reported)
        self.assertNotIn(first_snapshot["snapshot_id"], reported)
        for row in body["changes"]:
            self.assertEqual(set(row), {"snapshot_id", "observed_at", "change"})
            note = row["change"]
            self.assertTrue(note["summary"])
            self.assertLessEqual(len(note["summary"]), 400)
            self.assertEqual(set(note["counts"]), {"added", "removed", "rewritten", "unchanged"})
            self.assertTrue(
                set(note["kinds"])
                <= {"deadline_or_date_touched", "content_added", "content_removed", "wording_revised"}
            )
            self.assertIsInstance(note["truncated"], bool)

    def test_three_approved_captures_keep_both_changes(self):
        url = "https://example.com/integration/three-captures"
        snapshots = []
        for paragraph in (EXTRA["initial"], EXTRA["revised"], EXTRA["extended"]):
            snapshot, candidate = self.capture(url, paragraph)
            snapshots.append(snapshot)
            event = self.publish(candidate)
        body = self.changes(event["id"])
        self.assertEqual(
            [row["snapshot_id"] for row in body["changes"]], [snapshot["snapshot_id"] for snapshot in snapshots[1:]]
        )

    def test_unapproved_intermediate_capture_is_never_a_public_predecessor(self):
        url = "https://example.com/integration/unapproved-middle"
        first, candidate = self.capture(url, EXTRA["initial"])
        self.publish(candidate)
        middle, _ = self.capture(url, "未审核草稿中的敏感测试标记")
        last, candidate = self.capture(url, EXTRA["extended"])
        event = self.publish(candidate)
        body = self.changes(event["id"])
        self.assertEqual(len(body["changes"]), 1)
        self.assertEqual(body["changes"][0]["snapshot_id"], last["snapshot_id"])
        self.assertEqual(body["changes"][0]["change"]["previous_snapshot_id"], first["snapshot_id"])
        self.assertNotIn(middle["snapshot_id"], str(body))
        self.assertNotIn("未审核草稿", str(body))

    def test_withdrawn_historical_version_and_repeated_mappings_are_excluded(self):
        url = "https://example.com/integration/withdrawn-middle"
        first, candidate = self.capture(url, EXTRA["initial"])
        self.publish(candidate)
        middle, candidate = self.capture(url, EXTRA["revised"])
        middle_event = self.publish(candidate)
        stored = self.core.store.get("event_versions", f"{middle_event['id']}:{middle_event['version']}")
        stored["publication_state"] = "withdrawn"
        self.core.store.put("event_versions", f"{stored['id']}:{stored['version']}", stored)
        last, candidate = self.capture(url, EXTRA["extended"])
        event = self.publish(candidate)
        # A repeated approved version must not repeat the same capture in history.
        repeated = dict(event, version=event["version"] + 1)
        self.core.store.put("events", event["id"], repeated)
        self.core.store.put("event_versions", f"{event['id']}:{repeated['version']}", repeated)
        mapping = self.core.store.get("event_snapshot_versions", f"{event['id']}:{event['version']}")
        self.core.store.put("event_snapshot_versions", f"{event['id']}:{repeated['version']}", mapping)
        body = self.changes(event["id"])
        self.assertEqual(len(body["changes"]), 1)
        self.assertEqual(body["changes"][0]["snapshot_id"], last["snapshot_id"])
        self.assertEqual(body["changes"][0]["change"]["previous_snapshot_id"], first["snapshot_id"])
        self.assertNotIn(middle["snapshot_id"], str(body))

    def test_deadline_move_is_flagged_as_a_date_change(self):
        url = "https://example.com/integration/deadline"
        _, candidate = self.capture(url, EXTRA["initial"])
        self.publish(candidate)
        moved = (
            f"<html><head><meta charset='utf-8'></head><body><h1>合成变更历史通知</h1><article>"
            f"<p>全体本科生请于2030年1月9日前提交确认表。</p><p>{EXTRA['initial']}</p>"
            f"</article></body></html>".encode()
        )
        with self.core.store.transaction():
            snapshot, _ = self.core.pipeline.sources.persist(
                self.source,
                FetchResult(url, moved, "text/html; charset=utf-8"),
                "合成变更历史通知",
                "2030-01-01T00:00:00Z",
            )
        self.core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
        candidate = self.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
        event = self.publish(candidate)
        body = self.changes(event["id"])
        rows = {row["snapshot_id"]: row for row in body["changes"]}
        self.assertIn(snapshot["snapshot_id"], rows)
        self.assertIn("deadline_or_date_touched", rows[snapshot["snapshot_id"]]["change"]["kinds"])

    def test_changes_are_ordered_by_observation_time(self):
        """Two documents, each recaptured: the newer observation is reported last."""
        urls = ["https://example.com/integration/ordered-a", "https://example.com/integration/ordered-b"]
        event = None
        latest = {}
        for url in urls:
            _, candidate = self.capture(url, EXTRA["initial"])
            event = self.publish(candidate)
            snapshot, candidate = self.capture(url, EXTRA["revised"])
            event = self.publish(candidate)
            latest[url] = snapshot
        # Freeze an unambiguous, deliberately reversed order instead of relying
        # on wall-clock speed: document B observed first, document A second.
        for index, url in enumerate(urls):
            document = self.core.store.get("documents", latest[url]["document_id"])
            observed = dict(document.get("snapshot_observed_at") or {})
            capture_order = sorted(observed, key=lambda sid: self.core.store.get("snapshots", sid)["fetched_at"])
            for offset, snapshot_id in enumerate(capture_order):
                observed[snapshot_id] = f"2030-02-0{2 - index}T0{offset}:00:00Z"
            document["snapshot_observed_at"] = observed
            self.core.store.put("documents", document["id"], document)
            # Ordering belongs to approved captures, not later mutable observations.
            for key, mapping in [
                (
                    f"{version['id']}:{version['version']}",
                    self.core.store.get("event_snapshot_versions", f"{version['id']}:{version['version']}"),
                )
                for version in self.core.store.list("event_versions")
            ]:
                if not mapping:
                    continue
                for sid in mapping["snapshot_ids"]:
                    if sid in observed:
                        mapping.setdefault("observed_at", {})[sid] = observed[sid]
                self.core.store.put("event_snapshot_versions", key, mapping)
        body = self.changes(event["id"])
        validate("EventChangeSummary", body)
        times = [row["observed_at"] for row in body["changes"]]
        self.assertGreater(len(times), 1)
        self.assertEqual(times, sorted(times))
        self.assertEqual(body["changes"][-1]["snapshot_id"], latest[urls[0]]["snapshot_id"])

    def test_withdrawn_event_hides_its_change_history(self):
        url = "https://example.com/integration/withdrawn"
        _, candidate = self.capture(url, EXTRA["initial"])
        event = self.publish(candidate)
        _, candidate = self.capture(url, EXTRA["revised"])
        event = self.publish(candidate)
        self.assertTrue(self.changes(event["id"])["changes"])
        self.core.events.publication(
            event["id"],
            {
                "expected_event_version": event["version"],
                "publication_state": "withdrawn",
                "reason": "合成变更历史撤回",
            },
            uid(),
        )
        self.changes(event["id"], status=404)

    def test_unknown_event_has_no_change_history(self):
        self.changes(uid(), status=404)

    def test_concurrent_new_version_returns_409_instead_of_mixed_history(self):
        _, candidate = self.capture("https://example.com/integration/history-race", EXTRA["initial"])
        event = self.publish(candidate)
        with patch.object(
            self.core.events, "get_public", side_effect=[event, dict(event, version=event["version"] + 1)]
        ):
            body = self.changes(event["id"], status=409)
        self.assertEqual(body["code"], "event_version_changed")

    def test_concurrent_withdrawal_hides_history(self):
        _, candidate = self.capture("https://example.com/integration/history-withdraw-race", EXTRA["initial"])
        event = self.publish(candidate)
        with patch.object(self.core.events, "get_public", side_effect=[event, APIError(404, "not_found", "撤回")]):
            self.changes(event["id"], status=404)

    def test_unmapped_capture_is_omitted_instead_of_shown_as_an_empty_edit(self):
        """A capture the approved version never adopted must not leak into history."""
        url = "https://example.com/integration/unmapped"
        _, candidate = self.capture(url, EXTRA["initial"])
        event = self.publish(candidate)
        # A second capture exists in the same document but is never reconciled.
        self.capture(url, EXTRA["revised"])
        body = self.changes(event["id"])
        validate("EventChangeSummary", body)
        self.assertEqual(body["changes"], [])

    def test_stored_summary_cannot_widen_the_public_kind_enum(self):
        url = "https://example.com/integration/kinds"
        _, candidate = self.capture(url, EXTRA["initial"])
        self.publish(candidate)
        snapshot, candidate = self.capture(url, EXTRA["revised"])
        event = self.publish(candidate)
        document = self.core.store.get("documents", snapshot["document_id"])
        summaries = document["change_summaries"]
        for snapshot_id in summaries:
            summaries[snapshot_id]["kinds"] = list(summaries[snapshot_id]["kinds"]) + ["invented_kind"]
        self.core.store.put("documents", snapshot["document_id"], document)
        body = self.changes(event["id"])
        validate("EventChangeSummary", body)
        for row in body["changes"]:
            self.assertNotIn("invented_kind", row["change"]["kinds"])

    def test_parse_record_carries_the_same_change_note_as_the_event_history(self):
        """The operator-facing parse record and the student-facing history agree."""
        url = "https://example.com/integration/parse-note"
        first, candidate = self.capture(url, EXTRA["initial"])
        self.publish(candidate)
        first_parsed = self.core.store.get("parsed", first["snapshot_id"])
        self.assertIsNone(first_parsed["change"])
        second, candidate = self.capture(url, EXTRA["revised"])
        second_parsed = self.core.store.get("parsed", second["snapshot_id"])
        validate("ParsedDocument", second_parsed)
        note = second_parsed["change"]
        self.assertIsNotNone(note)
        self.assertEqual(note["previous_snapshot_id"], first["snapshot_id"])
        event = self.publish(candidate)
        rows = {row["snapshot_id"]: row["change"] for row in self.changes(event["id"])["changes"]}
        self.assertEqual(rows[second["snapshot_id"]], note)

    def test_first_capture_of_each_document_is_not_reported_as_a_change(self):
        """Two documents, two first captures: nothing to compare against either."""
        for index in range(2):
            _, candidate = self.capture(f"https://example.com/integration/fresh/{index}", EXTRA["initial"])
            event = self.publish(candidate)
            self.assertEqual(self.changes(event["id"])["changes"], [])


if __name__ == "__main__":
    unittest.main()
