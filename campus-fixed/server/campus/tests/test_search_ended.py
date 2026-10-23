"""Search hit ``ended`` semantics, mirroring the front-end feed folding.

The predicate is shared with the feed so a search result stops showing an
expired deadline as if it were still open. These unit tests pin the truth
table; the integration test in test_integration.py checks the wire shape.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from server.campus.search import _deadline_passed, _event_ended

NOW = datetime(2030, 1, 1, 4, tzinfo=UTC)  # 2030-01-01 12:00 Asia/Shanghai


class DeadlinePassedTests(unittest.TestCase):
    def test_date_before_today_is_passed(self):
        self.assertTrue(_deadline_passed({"precision": "date", "date": "2029-12-31"}, NOW))

    def test_date_today_is_not_passed(self):
        self.assertFalse(_deadline_passed({"precision": "date", "date": "2030-01-01"}, NOW))

    def test_date_future_is_not_passed(self):
        self.assertFalse(_deadline_passed({"precision": "date", "date": "2030-01-02"}, NOW))

    def test_date_malformed_is_not_passed(self):
        self.assertFalse(_deadline_passed({"precision": "date", "date": "2030/01/01"}, NOW))
        self.assertFalse(_deadline_passed({"precision": "date", "date": ""}, NOW))

    def test_minute_past_is_passed(self):
        self.assertTrue(_deadline_passed({"precision": "minute", "at": "2029-12-31T23:59:00+08:00"}, NOW))

    def test_minute_future_is_not_passed(self):
        self.assertFalse(_deadline_passed({"precision": "minute", "at": "2030-01-01T13:00:00+08:00"}, NOW))

    def test_minute_malformed_is_not_passed(self):
        self.assertFalse(_deadline_passed({"precision": "minute", "at": "not-a-date"}, NOW))
        self.assertFalse(_deadline_passed({"precision": "minute", "at": None}, NOW))

    def test_unknown_or_missing_precision_is_not_passed(self):
        self.assertFalse(_deadline_passed({"precision": "unknown", "raw_text": "开学后"}, NOW))
        self.assertFalse(_deadline_passed({}, NOW))


class EventEndedTests(unittest.TestCase):
    def test_cancelled_is_ended(self):
        self.assertTrue(_event_ended({"status": "cancelled", "deadlines": []}, NOW))

    def test_no_deadlines_is_not_ended(self):
        self.assertFalse(_event_ended({"status": "active", "deadlines": []}, NOW))

    def test_all_closing_deadlines_passed_is_ended(self):
        event = {
            "status": "active",
            "deadlines": [
                {"kind": "school_deadline", "precision": "date", "date": "2029-12-01"},
                {"kind": "college_deadline", "precision": "minute", "at": "2029-12-31T23:59:00+08:00"},
            ],
        }
        self.assertTrue(_event_ended(event, NOW))

    def test_one_open_closing_deadline_keeps_it_open(self):
        event = {
            "status": "active",
            "deadlines": [
                {"kind": "school_deadline", "precision": "date", "date": "2029-12-01"},
                {"kind": "college_deadline", "precision": "date", "date": "2030-06-01"},
            ],
        }
        self.assertFalse(_event_ended(event, NOW))

    def test_event_time_only_past_is_ended(self):
        # No closing kind at all: the feed falls back to every deadline.
        event = {"status": "active", "deadlines": [{"kind": "event_time", "precision": "date", "date": "2029-11-01"}]}
        self.assertTrue(_event_ended(event, NOW))

    def test_event_time_only_future_is_not_ended(self):
        event = {"status": "active", "deadlines": [{"kind": "event_time", "precision": "date", "date": "2030-06-01"}]}
        self.assertFalse(_event_ended(event, NOW))

    def test_closing_passed_ignores_future_event_time(self):
        event = {
            "status": "active",
            "deadlines": [
                {"kind": "school_deadline", "precision": "date", "date": "2029-12-01"},
                {"kind": "event_time", "precision": "date", "date": "2030-06-01"},
            ],
        }
        self.assertTrue(_event_ended(event, NOW))

    def test_unknown_deadline_keeps_it_open(self):
        event = {
            "status": "active",
            "deadlines": [{"kind": "school_deadline", "precision": "unknown", "raw_text": "开学后"}],
        }
        self.assertFalse(_event_ended(event, NOW))
