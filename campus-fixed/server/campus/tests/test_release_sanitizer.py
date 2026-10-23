"""Release sample invariants use real records and immutable synthetic blobs."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.campus.platform import Settings
from server.campus.schema import uid
from server.campus.sources import FetchError
from server.campus.tests.test_integration import prepare_notice
from server.campus_app import assemble
from tools.sanitize_public_sample import sanitize


class PublicSampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.core = assemble(Settings(data_dir=self.source, secret="synthetic-sample-secret-" * 3))
        self.notice = prepare_notice(self.core)
        candidate = self.notice["candidate"]
        candidate = self.core.pipeline.review(
            candidate["candidate_id"],
            {
                "expected_candidate_revision": candidate["revision"],
                "decision": "accept",
                "reason": "synthetic only",
                "replacement_candidate": None,
            },
            {"id": uid()},
        )
        self.event = self.core.events.reconcile_candidate(candidate["candidate_id"], candidate["revision"])

    def tearDown(self):
        self.core.store.close()
        self.temp.cleanup()

    def test_sample_excludes_auth_private_queues_and_never_uses_remote_index(self):
        for collection in ("users", "sessions", "challenges", "personal_items", "deliveries", "idempotency"):
            self.core.store.put(collection, uid(), {"private": "synthetic-private-must-not-export"})
        with (
            patch.dict(os.environ, {"CAMPUS_MEILI_URL": "https://index.example", "CAMPUS_MEILI_KEY": "synthetic-key"}),
            patch("server.campus.search.Search._http", side_effect=AssertionError("No external access")),
        ):
            report = sanitize(self.source, self.root / "clean")
        self.assertTrue(report["auth_private_jobs_outbox_secrets_removed"])
        self.assertFalse((self.root / "clean/.server-secret").exists())
        connection = sqlite3.connect(self.root / "clean/campus.sqlite3")
        try:
            self.assertFalse(
                any(
                    "synthetic-private-must-not-export" in row[0]
                    for row in connection.execute("SELECT data FROM campus_records")
                )
            )
            self.assertEqual(connection.execute("SELECT count(*) FROM campus_jobs").fetchone()[0], 0)
        finally:
            connection.close()

    def test_wrong_evidence_hash_is_rejected(self):
        proof = self.event["evidence"][0]
        proof["content_hash"] = "0" * 64
        self.core.store.put("evidence", proof["id"], proof)
        with self.assertRaisesRegex(ValueError, "hash or provenance"):
            sanitize(self.source, self.root / "bad")

    def test_private_provenance_url_is_rejected(self):
        snapshot = self.notice["snapshot"]
        snapshot["source_url"] = "https://metadata.google.internal/computeMetadata/v1/"
        self.core.store.put("snapshots", snapshot["snapshot_id"], snapshot)
        with self.assertRaises(FetchError):
            sanitize(self.source, self.root / "bad-url")

    def test_nested_parsed_evidence_hash_is_checked(self):
        parsed = self.core.store.get("parsed", self.notice["snapshot"]["snapshot_id"])
        parsed["evidence"][0]["content_hash"] = "0" * 64
        self.core.store.put("parsed", parsed["snapshot_id"], parsed)
        with self.assertRaisesRegex(ValueError, "hash or provenance"):
            sanitize(self.source, self.root / "bad-nested")

    def test_standalone_evidence_cannot_claim_another_source(self):
        proof = self.event["evidence"][0]
        proof["source_id"] = uid()
        self.core.store.put("evidence", proof["id"], proof)
        with self.assertRaisesRegex(ValueError, "source mismatch"):
            sanitize(self.source, self.root / "bad-source")

    def test_decision_chain_is_retained_but_never_names_the_actor(self):
        # "163 candidates became 73 published notices" is only checkable if the
        # decisions travel with the sample, so they are kept — minus the person.
        self.core.events.publication(
            self.event["id"],
            {
                "expected_event_version": self.event["version"],
                "publication_state": "withdrawn",
                "reason": "synthetic withdrawal",
            },
            "operator@example.invalid",
        )
        report = sanitize(self.source, self.root / "chain")
        self.assertTrue(report["review_decision_chain_retained"])
        self.assertEqual(
            report["redacted_actor_fields"], {"reviews": ["reviewer_id"], "publication_audits": ["actor_id"]}
        )
        connection = sqlite3.connect(self.root / "chain/campus.sqlite3")
        try:
            reviews = [
                json.loads(row[0])
                for row in connection.execute("SELECT data FROM campus_records WHERE collection='reviews'")
            ]
            audits = [
                json.loads(row[0])
                for row in connection.execute("SELECT data FROM campus_records WHERE collection='publication_audits'")
            ]
        finally:
            connection.close()
        self.assertTrue(reviews and audits)
        for record in reviews + audits:
            self.assertNotIn("reviewer_id", record)
            self.assertNotIn("actor_id", record)
            self.assertNotIn("operator@example.invalid", json.dumps(record, ensure_ascii=False))
        self.assertEqual((reviews[0]["decision"], reviews[0]["reason"]), ("accept", "synthetic only"))
        self.assertEqual((audits[0]["publication_state"], audits[0]["reason"]), ("withdrawn", "synthetic withdrawal"))
