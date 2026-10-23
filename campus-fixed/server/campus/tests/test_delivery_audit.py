"""Delivery audit regressions using real SQLite and authenticated HTTP fixtures."""

import copy
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from server.campus.delivery import DEFAULTS, stamp
from server.campus.platform import APIError
from server.campus.tests import test_integration as fixtures
from server.campus.worker import Worker

FIXED_NOW = fixtures.FIXED_NOW


class DeliveryAuditTests(unittest.TestCase):
    setUp = fixtures.CampusIntegrationTests.setUp
    tearDown = fixtures.CampusIntegrationTests.tearDown
    auth = fixtures.CampusIntegrationTests.auth
    private = fixtures.CampusIntegrationTests.private
    token = fixtures.CampusIntegrationTests.token
    approved_notice = fixtures.CampusIntegrationTests.approved_notice

    def preferences(self, user, **changes):
        prefs = {**copy.deepcopy(DEFAULTS), "quiet_start": None, "quiet_end": None, **changes}
        self.core.delivery.update_preferences(user["id"], prefs)
        return prefs

    def plan(self, user, item):
        self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
        return next(
            d
            for d in self.core.store.list("deliveries")
            if d["subject"] and d["subject"]["id"] == item["id"] and d["kind"] == "deadline"
        )

    def send(self, user, row):
        return self.core.delivery.deliver_message({"user_id": user["id"], "delivery_id": row["id"]})

    def test_delayed_minute_deadline_is_cancelled_before_smtp(self):
        client, user, headers = self.auth()
        self.preferences(user, channels=["email"])
        item, draft, _ = self.private(client, headers)
        draft.update(deadline_date=None, deadline_at=stamp(FIXED_NOW + timedelta(minutes=30)))
        changed = client.patch(
            "/api/v1/me/items/" + item["id"], json={"expected_revision": 1, "draft": draft}, headers=headers
        ).json()
        row = self.plan(user, changed)
        self.core.delivery.clock = lambda: FIXED_NOW + timedelta(hours=1)
        self.core.mail = Mock(send_delivery=Mock(return_value={"status": "sent"}))
        self.send(user, row)
        self.core.mail.send_delivery.assert_not_called()
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "cancelled")

    def test_delayed_date_only_reminder_is_cancelled_on_following_day(self):
        client, user, headers = self.auth()
        self.preferences(user, channels=["email"])
        item, _, _ = self.private(client, headers, date="2030-01-02")
        row = self.plan(user, item)
        self.core.delivery.clock = lambda: FIXED_NOW + timedelta(days=2)
        self.core.mail = Mock(send_delivery=Mock(return_value={"status": "sent"}))
        self.send(user, row)
        self.core.mail.send_delivery.assert_not_called()
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "cancelled")

    def test_replanning_keeps_definitive_failure_retryable_without_false_schedule(self):
        client, user, headers = self.auth()
        self.preferences(user, channels=["email"])
        item, _, _ = self.private(client, headers)
        row = self.plan(user, item)
        self.core.mail = Mock(send_delivery=Mock(return_value={"status": "failed"}))
        with self.assertRaisesRegex(Exception, "投递未被接受"):
            self.send(user, row)
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "failed")
        self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "failed")
        self.core.mail.send_delivery.return_value = {"status": "sent"}
        self.send(user, row)
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "sent")

    def test_email_only_late_replan_preserves_failed_attempt_and_schedule(self):
        client, user, headers = self.auth()
        self.preferences(user, channels=["email"])
        item, _, _ = self.private(client, headers)
        row = self.plan(user, item)
        self.core.mail = Mock(send_delivery=Mock(return_value={"status": "failed"}))
        with self.assertRaises(APIError):
            self.send(user, row)
        self.core.delivery.clock = lambda: FIXED_NOW + timedelta(minutes=7)
        self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
        rows = self.core.store.list("deliveries")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], row["id"])
        self.assertEqual(rows[0]["status"], "failed")

    def test_reenable_cancelled_plan_uses_fresh_delivery_and_real_queue(self):
        client, user, headers = self.auth()
        self.preferences(user)
        item, _, _ = self.private(client, headers)
        row = self.plan(user, item)
        self.preferences(user, reminders_enabled=False)
        self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
        self.send(user, row)
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "cancelled")
        self.preferences(user, reminders_enabled=True)
        self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
        replacement = next(d for d in self.core.store.list("deliveries") if d["status"] == "scheduled")
        self.assertNotEqual(replacement["id"], row["id"])
        worker = Worker(
            self.core, handlers={"deliver_message": self.core.delivery.deliver_message}, clock=lambda: FIXED_NOW
        )
        worker.sweep()
        with patch("server.campus.db.now_iso", return_value=stamp(FIXED_NOW)):
            result = worker.run_once(sweep=False, max_jobs=10)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(self.core.store.get("deliveries", replacement["id"])["status"], "sent")

    def test_terminal_failed_job_can_be_manually_retried_after_replan(self):
        client, user, headers = self.auth()
        self.preferences(user, channels=["email"])
        item, _, _ = self.private(client, headers)
        row = self.plan(user, item)
        self.core.mail = Mock(send_delivery=Mock(return_value={"status": "failed"}))
        worker = Worker(
            self.core, handlers={"deliver_message": self.core.delivery.deliver_message}, clock=lambda: FIXED_NOW
        )
        worker.sweep()
        with patch("server.campus.db.now_iso", return_value=stamp(FIXED_NOW)):
            claimed = self.core.store.claim_job("audit-worker", kinds=["deliver_message"])
            with self.assertRaises(APIError):
                self.send(user, row)
            self.core.store.fail_job(claimed["job"]["id"], "delivery_failed", retry_delay=None)
        self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "failed")
        self.core.mail.send_delivery.return_value = {"status": "sent"}
        self.core.store.retry_job(claimed["job"]["id"])
        with patch("server.campus.db.now_iso", return_value=stamp(FIXED_NOW)):
            result = worker.run_once(sweep=False, max_jobs=10)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "sent")

    def test_queued_digest_respects_disabled_preference_at_execution(self):
        _, user, _ = self.auth()
        self.preferences(user, daily_digest_enabled=False)
        self.core.delivery.build_digest({"user_id": user["id"]})
        self.assertEqual(self.core.store.list("digests"), [])
        self.assertEqual(self.core.store.list("deliveries"), [])

    def test_digest_queue_recovers_after_disable_and_reenable_same_day(self):
        _, user, _ = self.auth()
        self.preferences(user)
        worker = Worker(self.core, handlers={"build_digest": self.core.delivery.build_digest}, clock=lambda: FIXED_NOW)
        worker.sweep()
        self.preferences(user, daily_digest_enabled=False)
        with patch("server.campus.db.now_iso", return_value=stamp(FIXED_NOW)):
            self.assertEqual(worker.run_once(sweep=False, max_jobs=10)["succeeded"], 1)
        self.assertEqual(self.core.store.list("digests"), [])
        self.preferences(user, daily_digest_enabled=True)
        worker.sweep()
        with patch("server.campus.db.now_iso", return_value=stamp(FIXED_NOW)):
            self.assertEqual(worker.run_once(sweep=False, max_jobs=10)["succeeded"], 1)
        self.assertEqual(len(self.core.store.list("digests")), 1)

    def test_digest_queue_respects_later_time_and_still_builds_at_new_time(self):
        _, user, _ = self.auth()
        self.preferences(user)
        worker = Worker(self.core, handlers={"build_digest": self.core.delivery.build_digest}, clock=lambda: FIXED_NOW)
        worker.sweep()
        self.preferences(user, digest_time="20:00")
        with patch("server.campus.db.now_iso", return_value=stamp(FIXED_NOW)):
            worker.run_once(sweep=False, max_jobs=10)
        self.assertEqual(self.core.store.list("digests"), [])
        later = FIXED_NOW + timedelta(hours=8)
        worker.clock = lambda: later
        self.core.delivery.clock = lambda: later
        worker.sweep()
        with patch("server.campus.db.now_iso", return_value=stamp(later)):
            self.assertEqual(worker.run_once(sweep=False, max_jobs=10)["succeeded"], 1)
        self.assertEqual(len(self.core.store.list("digests")), 1)

    def test_digest_revalidates_done_items_before_delivery(self):
        client, user, headers = self.auth()
        self.preferences(user)
        item, _, _ = self.private(client, headers, title="已完成后不应提示待处理")
        self.core.delivery.build_digest({"user_id": user["id"]})
        row = next(d for d in self.core.store.list("deliveries") if d["kind"] == "digest")
        response = client.patch(
            "/api/v1/me/items/" + item["id"], json={"expected_revision": 1, "state": "done"}, headers=headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("已完成后不应提示待处理", self.core.delivery._message(user["id"], row)[1])
        self.assertEqual(self.core.delivery.digests(user["id"])[0]["personal_items"], [])

    def test_digest_delivery_is_bound_to_original_day_and_expires(self):
        client, user, headers = self.auth()
        self.preferences(user, channels=["email"])
        self.private(client, headers, title="第一天的事项")
        self.core.delivery.build_digest({"user_id": user["id"]})
        row = next(d for d in self.core.store.list("deliveries") if d["kind"] == "digest")
        self.core.delivery.clock = lambda: FIXED_NOW + timedelta(days=1)
        self.private(client, headers, title="第二天新增事项")
        self.core.delivery.build_digest({"user_id": user["id"]})
        self.assertNotIn("第二天新增事项", self.core.delivery._message(user["id"], row)[1])
        self.core.mail = Mock(send_delivery=Mock(return_value={"status": "sent"}))
        self.send(user, row)
        self.core.mail.send_delivery.assert_not_called()
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "cancelled")

    def versioned_public_notice(self):
        notice = self.approved_notice()
        client, user, headers = self.auth()
        event = notice["event"]
        response = client.patch(
            "/api/v1/me/events/" + event["id"] + "/state",
            json={"expected_state_revision": 0, "acknowledged_event_version": event["version"], "saved": True},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        revised = fixtures.prepare_notice(self.core, date="2030-01-04", source=notice["source"], url=notice["url"])
        second = self.approved_notice(revised, admin=notice["admin"])["event"]
        self.assertEqual(second["id"], event["id"])
        self.assertEqual(second["version"], event["version"] + 1)
        return client, user, headers, second

    def test_minor_update_preserves_prior_critical_reminder_without_resending(self):
        client, user, headers, second = self.versioned_public_notice()
        self.preferences(user)
        payload = {"user_id": user["id"], "subject": None}
        self.core.delivery.plan_reminders(payload)
        row = next(d for d in self.core.store.list("deliveries") if d["kind"] == "material_change")
        self.send(user, row)
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "sent")
        third = copy.deepcopy(second)
        third.update(version=second["version"] + 1, title=second["title"] + " 标题整理", changed_fields=["title"])
        self.core.store.put("events", third["id"], third)
        self.core.store.put("event_versions", str(third["id"]) + ":" + str(third["version"]), third)
        self.core.delivery.plan_reminders(payload)
        notices = [d for d in self.core.store.list("deliveries") if d["kind"] == "material_change"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["status"], "sent")
        self.assertTrue(self.core.events.changed_since(third["id"], 1, third) & {"deadlines"})

    def test_minor_update_before_dispatch_keeps_current_revision_on_same_delivery(self):
        _, user, _, second = self.versioned_public_notice()
        self.preferences(user)
        payload = {"user_id": user["id"], "subject": None}
        self.core.delivery.plan_reminders(payload)
        row = next(d for d in self.core.store.list("deliveries") if d["kind"] == "material_change")
        third = copy.deepcopy(second)
        third.update(version=second["version"] + 1, title=second["title"] + " 标题整理", changed_fields=["title"])
        self.core.store.put("events", third["id"], third)
        self.core.store.put("event_versions", str(third["id"]) + ":" + str(third["version"]), third)
        self.core.delivery.plan_reminders(payload)
        notices = [d for d in self.core.store.list("deliveries") if d["kind"] == "material_change"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["id"], row["id"])
        self.assertEqual(notices[0]["subject"]["revision"], third["version"])
        self.send(user, notices[0])
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "sent")

    def test_acknowledged_change_is_cancelled_before_dispatch(self):
        client, user, headers, second = self.versioned_public_notice()
        self.preferences(user)
        self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
        row = next(d for d in self.core.store.list("deliveries") if d["kind"] == "material_change")
        state = self.core.events.state(user["id"], second["id"])
        response = client.patch(
            "/api/v1/me/events/" + second["id"] + "/state",
            json={
                "expected_state_revision": state["state_revision"],
                "acknowledged_event_version": second["version"],
                "read": True,
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.send(user, row)
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "cancelled")

    def test_date_only_quiet_shift_past_deadline_day_is_not_planned(self):
        client, user, headers = self.auth()
        self.preferences(
            user,
            channels=["email"],
            reminder_lead_minutes=0,
            date_only_reminder_time="23:00",
            quiet_start="22:00",
            quiet_end="08:00",
        )
        item, _, _ = self.private(client, headers)
        self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
        self.assertEqual(self.core.store.list("deliveries"), [])

    def test_equal_quiet_endpoints_do_not_postpone_forever(self):
        _, user, _ = self.auth()
        prefs = self.preferences(user, quiet_start="22:00", quiet_end="22:00")
        self.assertEqual(self.core.delivery._quiet(FIXED_NOW, prefs), FIXED_NOW)


if __name__ == "__main__":
    unittest.main()
