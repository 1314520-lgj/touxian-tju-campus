"""An event belongs to one notice, not to one title.

Two genuinely different notices can carry the same headline -- a scholarship
list republished for a new round, a "第N批" list, a re-issued 公示. Because the
extractor derives a candidate's identity from the snapshot, each notice gets its
own candidate; but the reconciler matches an existing event by source document,
and that match used to trigger on *any* shared document.

These tests pin the two halves of the rule that replaces it:

* a notice and its own attachments fold to a single root document, so the
  attachment republication still merges (no regression to the common case);
* two unrelated source documents sharing only a title are not silently merged
  into one settled event -- the merged event is held for review instead.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.campus.platform import Settings
from server.campus.schema import uid, validate
from server.campus.sources import FetchResult
from server.campus.tests.test_integration import synthetic_source
from server.campus_app import create_app

SHARED_TITLE = "合成同标题公示"
ROUND_ONE = "公示时间：2030年1月5日—2030年1月20日。"
ROUND_TWO = "公示时间：2030年2月6日—2030年2月28日。"


class EventMergeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"CAMPUS_MEILI_URL": "", "CAMPUS_DEMO": "false"})
        self.environment.start()
        self.app = create_app(
            Settings(
                data_dir=Path(self.temp.name),
                secret="event-merge-secret-" * 3,
                dev_auth=True,
                admin_emails={"admin@example.com"},
            ),
            start_worker=False,
        )
        self.core = self.app.state.core
        self.client = TestClient(self.app)
        self.source = synthetic_source(self.core)

    def tearDown(self):
        self.client.close()
        self.core.store.close()
        self.environment.stop()
        self.temp.cleanup()

    def capture(self, url, paragraph, title=SHARED_TITLE, parent_snapshot_id=None):
        body = (
            f"<html><head><meta charset='utf-8'></head><body><h1>{title}</h1>"
            f"<article><p>{paragraph}</p></article></body></html>"
        ).encode()
        with self.core.store.transaction():
            snapshot, _ = self.core.pipeline.sources.persist(
                self.source,
                FetchResult(url, body, "text/html; charset=utf-8"),
                title,
                "2030-01-01T00:00:00Z",
                parent_id=parent_snapshot_id,
            )
        self.core.pipeline.parse_document({"snapshot_id": snapshot["snapshot_id"]})
        candidate = self.core.pipeline.extract_candidates({"snapshot_id": snapshot["snapshot_id"]})
        return snapshot, candidate

    def publish(self, candidate):
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
        return self.core.events.reconcile_candidate(candidate["candidate_id"], accepted["revision"])

    def test_attachments_fold_to_the_parent_notice(self):
        """A notice plus an attachment is one notice, hence one root document."""
        primary, _ = self.capture("https://example.com/merge/primary", ROUND_ONE)
        attachment, _ = self.capture(
            "https://example.com/merge/attach.xlsx", ROUND_ONE, parent_snapshot_id=primary["snapshot_id"]
        )
        roots = self.core.events._document_roots([primary["snapshot_id"], attachment["snapshot_id"]])
        self.assertEqual(len(roots), 1, "attachment must fold into its parent notice")

    def test_same_url_republication_still_merges_into_one_event(self):
        """The common case -- same URL recaptured -- must keep its event identity."""
        url = "https://example.com/merge/stable"
        first, candidate = self.capture(url, ROUND_ONE)
        event = self.publish(candidate)
        second, candidate = self.capture(url, ROUND_ONE)
        self.assertEqual(second["document_id"], first["document_id"])
        again = self.publish(candidate)
        self.assertEqual(again["id"], event["id"], "one URL is one event")

    def test_distinct_documents_sharing_a_title_are_not_settled_silently(self):
        """Unrelated documents must not become one confidently published event."""
        _, first_candidate = self.capture("https://example.com/merge/round-1", ROUND_ONE)
        first_event = self.publish(first_candidate)
        _, second_candidate = self.capture("https://example.com/merge/round-2", ROUND_TWO)

        second_event = self.publish(second_candidate)

        if second_event["id"] == first_event["id"]:
            # Merged on the strength of the title alone: that is only tolerable
            # because the result is explicitly held for review.
            self.assertEqual(
                second_event["status"], "under_review", "a title-only merge must never be published as settled fact"
            )
            self.assertTrue(self.core.events._pending_merge_review)
        else:
            # Keeping the two notices apart is the preferred outcome.
            self.assertNotEqual(second_event["id"], first_event["id"])

    def test_pending_merge_flag_does_not_leak_between_candidates(self):
        """The review signal is per-candidate, never carried across calls."""
        self.capture("https://example.com/merge/leak-1", ROUND_ONE)
        self.capture("https://example.com/merge/leak-2", ROUND_TWO)
        self.assertFalse(self.core.events._pending_merge_review)

    def test_withdrawn_duplicate_is_no_longer_publicly_reachable(self):
        """The duplicate remediation path: a withdrawn event must 404 publicly."""
        _, candidate = self.capture("https://example.com/merge/withdrawn", ROUND_ONE)
        event = self.publish(candidate)
        self.assertEqual(self.client.get(f"/api/v1/events/{event['id']}").status_code, 200)

        current = self.core.store.get("events", event["id"])
        withdrawn = self.core.events.publication(
            event["id"],
            {
                "expected_event_version": current["version"],
                "publication_state": "withdrawn",
                "reason": "合成测试：同标题更新批次已发布，撤回旧批次",
            },
            "operator@example.com",
        )
        validate("EventVersion", withdrawn)

        self.assertEqual(withdrawn["publication_state"], "withdrawn")
        self.assertEqual(self.client.get(f"/api/v1/events/{event['id']}").status_code, 404)
        audit = [row for row in self.core.store.list("publication_audits") if row["event_id"] == event["id"]]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["publication_state"], "withdrawn")
        self.assertEqual(audit[0]["actor_id"], "operator@example.com")


if __name__ == "__main__":
    unittest.main()
