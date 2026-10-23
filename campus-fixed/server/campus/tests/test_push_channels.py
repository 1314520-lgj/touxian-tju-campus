"""Private push isolation and DNS-pinned transport. No test performs network I/O."""

import copy
import json
import socket
import ssl
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from server.campus.channels import PushGateway, _is_public_https, parse_endpoints
from server.campus.delivery import DEFAULTS, QUIET_CHANNELS
from server.campus.tests import test_integration as fixtures

USER_A, USER_B = str(uuid.uuid4()), str(uuid.uuid4())


def public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class RecordingConnection:
    def __init__(self, host, ip, port, timeout, calls, status=204, fail=None):
        self.host, self.ip, self.port, self.timeout = host, ip, port, timeout
        self.calls, self.status, self.fail = calls, status, fail

    def connect(self):
        if self.fail == "connect":
            raise TimeoutError("synthetic connect timeout")

    def request(self, method, path, body, headers):
        self.calls.append(
            {"host": self.host, "ip": self.ip, "path": path, "body": json.loads(body), "headers": headers}
        )
        if self.fail == "request":
            raise TimeoutError("synthetic request timeout")

    def getresponse(self):
        if self.fail == "response":
            raise TimeoutError("synthetic response timeout")
        return Mock(status=self.status)

    def close(self):
        pass


def connector(calls, statuses=None, fail=None):
    statuses = iter(statuses or [])
    return lambda host, ip, port, timeout: RecordingConnection(
        host, ip, port, timeout, calls, next(statuses, 204), fail
    )


class PushGatewayTests(unittest.TestCase):
    def gateway(self, endpoints="https://a.example/hook", **kwargs):
        self.calls = []
        return PushGateway(
            user_endpoints={}, endpoints=endpoints, resolver=public_dns, connector=connector(self.calls), **kwargs
        )

    def test_endpoint_list_split(self):
        self.assertEqual(parse_endpoints("a,b;c\nd"), ["a", "b", "c", "d"])
        self.assertEqual(parse_endpoints(""), [])

    def test_only_public_https_configuration(self):
        self.assertTrue(_is_public_https("https://push.example.com/hook"))
        for refused in [
            "http://push.example.com/hook",
            "https://127.0.0.1/hook",
            "https://192.168.1.5/hook",
            "https://10.0.0.1/hook",
            "https://localhost./hook",
            "https://user:pw@push.example.com/hook",
            "https://169.254.169.254/latest/meta-data",
            "https://100.100.100.200/hook",
            "https://[fd00:ec2::254]/hook",
            "https://push.example.com:8443/hook",
        ]:
            with self.subTest(url=refused):
                self.assertFalse(_is_public_https(refused))

    def test_unconfigured_never_uses_legacy_global_endpoints(self):
        with patch.dict(
            "os.environ", {"CAMPUS_PUSH_ENDPOINTS": "https://broadcast.example/hook", "CAMPUS_PUSH_USER_ENDPOINTS": ""}
        ):
            gateway = PushGateway(connector=Mock())
        self.assertEqual(gateway.status(USER_A), {"available": False, "reason": "unconfigured"})
        self.assertEqual(gateway.send_delivery_for_user(USER_A, "合成标题", "正文", "key")["status"], "failed")
        gateway.connector.assert_not_called()

    def test_explicit_transport_cannot_be_a_user_fallback(self):
        gateway = self.gateway()
        self.assertEqual(gateway.send_delivery_for_user(USER_A, "标题", "正文", "key")["status"], "failed")
        self.assertEqual(self.calls, [])

    def test_server_json_mapping_is_loaded_and_has_no_global_fallback(self):
        calls = []
        with patch.dict(
            "os.environ",
            {
                "CAMPUS_PUSH_USER_ENDPOINTS": json.dumps({USER_A: ["https://private.example/hook"]}),
                "CAMPUS_PUSH_ENDPOINTS": "https://broadcast.example/hook",
            },
        ):
            gateway = PushGateway(resolver=public_dns, connector=connector(calls))
        self.assertEqual(gateway.status(USER_A), {"available": True, "reason": "ready"})
        self.assertEqual(gateway.send_delivery_for_user(USER_A, "标题", "正文", "key")["status"], "sent")
        self.assertEqual([call["host"] for call in calls], ["private.example"])
        self.assertEqual(gateway.status(USER_B)["reason"], "unconfigured")

    def test_equivalent_duplicate_destinations_of_one_user_are_sent_once(self):
        calls = []
        gateway = PushGateway(
            user_endpoints={
                USER_A: [
                    "https://Push.Example:443/%68ook?a=1&b=2",
                    "https://push.example/hook?b=2&a=1",
                ]
            },
            resolver=public_dns,
            connector=connector(calls),
        )
        self.assertEqual(gateway.send_delivery_for_user(USER_A, "标题", "正文", "key")["status"], "sent")
        self.assertEqual(len(calls), 1)

    def test_invalid_json_or_mapping_fails_closed(self):
        for raw in [
            "{invalid",
            "[]",
            {"not-a-uuid": ["https://a.example/hook"]},
            {USER_A: "https://a.example/hook"},
            {USER_A: ["http://a.example/hook"]},
        ]:
            with self.subTest(raw=raw):
                gateway = PushGateway(user_endpoints=raw, connector=Mock())
                self.assertEqual(gateway.status(USER_A), {"available": False, "reason": "invalid_configuration"})
                self.assertEqual(gateway.send_delivery_for_user(USER_A, "标题", "正文", "key")["status"], "failed")
                gateway.connector.assert_not_called()

    def test_same_normalized_destination_is_rejected_for_both_users(self):
        gateway = PushGateway(
            user_endpoints={
                USER_A: ["https://Push.Example.com:443/%68ook?a=1&b=2"],
                USER_B: ["https://push.example.com/hook?b=2&a=1"],
            },
            connector=Mock(),
        )
        for user in [USER_A, USER_B]:
            self.assertEqual(gateway.status(user), {"available": False, "reason": "shared_destination"})
            self.assertEqual(gateway.send_delivery_for_user(user, "标题", "正文", "key")["status"], "failed")
        gateway.connector.assert_not_called()

    def test_safe_json_and_idempotency_header(self):
        gateway = self.gateway("https://a.example/hook,https://b.example/hook")
        self.assertEqual(gateway.send_delivery("主题", "正文", "key-1")["status"], "sent")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0]["body"], {"title": "主题", "body": "正文"})
        self.assertEqual(self.calls[0]["headers"]["Idempotency-Key"], self.calls[1]["headers"]["Idempotency-Key"])
        self.assertEqual(self.calls[0]["ip"], "93.184.216.34")

    def test_mixed_public_private_dns_rejects_before_sending_any_message(self):
        gateway = self.gateway("https://a.example/hook,https://b.example/hook")

        def mixed(host, port, **kwargs):
            records = public_dns(host, port)
            return (
                records + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]
                if host == "b.example"
                else records
            )

        gateway.resolver = mixed
        self.assertEqual(gateway.send_delivery("标题", "正文", "key")["status"], "failed")
        self.assertEqual(self.calls, [])

    def test_metadata_dns_and_dns_errors_never_connect(self):
        for address in ["169.254.169.254", "100.100.100.200", "fd00:ec2::254", "127.0.0.1"]:
            gateway = self.gateway()
            gateway.resolver = lambda host, port, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            ]
            self.assertEqual(gateway.send_delivery("标题", "正文", "key")["status"], "failed")
            self.assertEqual(self.calls, [])
        gateway.resolver = Mock(side_effect=socket.gaierror("synthetic"))
        self.assertEqual(gateway.send_delivery("标题", "正文", "key")["status"], "failed")

    def test_dns_resolution_is_pinned_and_not_repeated_at_connect(self):
        gateway = self.gateway()
        gateway.resolver = Mock(
            side_effect=[
                public_dns("a.example", 443),
                [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))],
            ]
        )
        self.assertEqual(gateway.send_delivery("标题", "正文", "key")["status"], "sent")
        gateway.resolver.assert_called_once()
        self.assertEqual(self.calls[0]["ip"], "93.184.216.34")

    def test_default_pinned_connection_keeps_tls_hostname_validation(self):
        from server.campus.sources import _PinnedHTTPS

        tcp, context = Mock(), Mock()
        with (
            patch("server.campus.sources.socket.create_connection", return_value=tcp) as create,
            patch("server.campus.sources.ssl.create_default_context", return_value=context),
        ):
            connection = _PinnedHTTPS("a.example", "93.184.216.34", 443, 3)
            connection.connect()
        create.assert_called_once_with(("93.184.216.34", 443), 3)
        context.wrap_socket.assert_called_once_with(tcp, server_hostname="a.example")

    def test_connect_failure_is_failed_but_post_write_ambiguity_is_unknown(self):
        for phase in ["connect", "request", "response"]:
            gateway = self.gateway()
            gateway.connector = connector(self.calls, fail=phase)
            receipt = gateway.send_delivery("标题", "正文", "key")
            self.assertEqual(receipt["status"], "failed" if phase == "connect" else "unknown")

    def test_tls_failure_before_request_is_failed(self):
        gateway = self.gateway()
        connection = Mock(connect=Mock(side_effect=ssl.SSLError("synthetic certificate failure")))
        gateway.connector = Mock(return_value=connection)
        self.assertEqual(gateway.send_delivery("标题", "正文", "key")["status"], "failed")
        connection.request.assert_not_called()

    def test_redirect_is_not_followed(self):
        gateway = self.gateway("https://a.example/hook,https://b.example/hook")
        gateway.connector = connector(self.calls, statuses=[307])
        receipt = gateway.send_delivery("标题", "正文", "key")
        self.assertEqual(receipt, {"status": "failed", "provider_id": None, "detail": "push_redirect_denied"})
        self.assertEqual(len(self.calls), 1)

    def test_partial_acceptance_is_unknown_to_prevent_duplicate_retry(self):
        gateway = self.gateway("https://a.example/hook,https://b.example/hook")
        gateway.connector = connector(self.calls, statuses=[204, 500])
        self.assertEqual(gateway.send_delivery("标题", "正文", "key")["status"], "unknown")
        gateway.connector = connector(self.calls, statuses=[500, 500])
        self.assertEqual(gateway.send_delivery("标题", "正文", "other")["status"], "failed")

    def test_payload_size_is_limited_before_dns_or_transmission(self):
        gateway = self.gateway(max_payload_bytes=100)
        gateway.resolver = Mock(side_effect=AssertionError("must not resolve"))
        self.assertEqual(gateway.send_delivery("标题", "文" * 100, "key")["detail"], "push_payload_too_large")
        self.assertEqual(self.calls, [])


class PushDeliveryRoutingTests(unittest.TestCase):
    def setUp(self):
        self.push_environment = patch.dict("os.environ", {"CAMPUS_PUSH_USER_ENDPOINTS": ""})
        self.push_environment.start()
        fixtures.CampusIntegrationTests.setUp(self)
        self.calls = []

    def tearDown(self):
        fixtures.CampusIntegrationTests.tearDown(self)
        self.push_environment.stop()

    auth = fixtures.CampusIntegrationTests.auth
    private = fixtures.CampusIntegrationTests.private

    def bind(self, mapping, statuses=None):
        self.core.push = PushGateway(
            user_endpoints=mapping, resolver=public_dns, connector=connector(self.calls, statuses)
        )

    def preferences(self, user, **changes):
        prefs = {**copy.deepcopy(DEFAULTS), "quiet_start": None, "quiet_end": None, **changes}
        self.core.delivery.update_preferences(user["id"], prefs)
        return prefs

    def plan(self, user, item):
        self.core.delivery.plan_reminders({"user_id": user["id"], "subject": None})
        return next(
            row
            for row in self.core.store.list("deliveries")
            if row["user_id"] == user["id"]
            and row["subject"]
            and row["subject"]["id"] == item["id"]
            and row["kind"] == "deadline"
        )

    def send(self, user, row):
        self.core.delivery.deliver_message({"user_id": user["id"], "delivery_id": row["id"]})

    def test_two_students_private_reminders_and_digests_are_isolated(self):
        client_a, user_a, headers_a = self.auth("synthetic-a@example.com")
        client_b, user_b, headers_b = self.auth("synthetic-b@example.com")
        self.bind({user_a["id"]: ["https://student-a.example/hook"], user_b["id"]: ["https://student-b.example/hook"]})
        for client, user, headers, label in [(client_a, user_a, headers_a, "A"), (client_b, user_b, headers_b, "B")]:
            self.preferences(user, channels=["push"])
            item, _, _ = self.private(client, headers, title="合成用户" + label + "私密事项")
            self.send(user, self.plan(user, item))
            self.core.delivery.build_digest({"user_id": user["id"]})
            row = next(
                row
                for row in self.core.store.list("deliveries")
                if row["user_id"] == user["id"] and row["kind"] == "digest"
            )
            self.send(user, row)
        self.assertEqual(len(self.calls), 4)
        for call in self.calls:
            wanted, refused = ("用户A", "用户B") if call["host"] == "student-a.example" else ("用户B", "用户A")
            self.assertIn(wanted, call["body"]["title"] + call["body"]["body"])
            self.assertNotIn(refused, call["body"]["title"] + call["body"]["body"])

    def test_status_is_authenticated_owner_scoped_and_has_no_secret(self):
        self.assertEqual(self.client.get("/api/v1/me/push-status").status_code, 401)
        client_a, user_a, headers_a = self.auth("synthetic-a@example.com")
        client_b, user_b, _ = self.auth("synthetic-b@example.com")
        self.bind({user_a["id"]: ["https://student-a.example/hook?secret=synthetic-token"]})
        ready = client_a.get("/api/v1/me/push-status").json()
        absent = client_b.get("/api/v1/me/push-status", params={"user_id": user_a["id"]}).json()
        self.assertEqual(ready, {"available": True, "reason": "ready"})
        self.assertEqual(absent, {"available": False, "reason": "unconfigured"})
        self.assertNotIn("synthetic-token", json.dumps(ready))

    def test_push_preferences_reject_unbound_configuration_and_require_csrf(self):
        client_a, user_a, headers_a = self.auth("synthetic-a@example.com")
        client_b, user_b, headers_b = self.auth("synthetic-b@example.com")
        prefs = {**copy.deepcopy(DEFAULTS), "channels": ["push"]}
        response = client_b.put("/api/v1/me/preferences", json=prefs, headers=headers_b)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "PUSH_NOT_READY")
        self.bind({user_a["id"]: ["https://student-a.example/hook"]})
        self.assertEqual(client_a.put("/api/v1/me/preferences", json=prefs).status_code, 403)
        self.assertEqual(client_b.put("/api/v1/me/preferences", json=prefs, headers=headers_a).status_code, 403)
        self.assertEqual(client_a.put("/api/v1/me/preferences", json=prefs, headers=headers_a).status_code, 200)
        self.assertEqual(self.core.delivery.preferences(user_b["id"])["channels"], ["in_app"])

    def test_shared_destination_status_blocks_opt_in_for_both_users(self):
        client_a, user_a, headers_a = self.auth("synthetic-a@example.com")
        client_b, user_b, headers_b = self.auth("synthetic-b@example.com")
        self.bind({user_a["id"]: ["https://push.example:443/hook"], user_b["id"]: ["https://PUSH.example/hook"]})
        for client, headers in [(client_a, headers_a), (client_b, headers_b)]:
            self.assertEqual(
                client.get("/api/v1/me/push-status").json(), {"available": False, "reason": "shared_destination"}
            )
            response = client.put(
                "/api/v1/me/preferences", json={**copy.deepcopy(DEFAULTS), "channels": ["push"]}, headers=headers
            )
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.calls, [])

    def test_legacy_queued_unbound_push_fails_instead_of_broadcasting(self):
        client, user, headers = self.auth()
        self.core.store.put(
            "preferences",
            user["id"],
            {**copy.deepcopy(DEFAULTS), "channels": ["push"], "quiet_start": None, "quiet_end": None},
        )
        item, _, _ = self.private(client, headers)
        row = self.plan(user, item)
        with self.assertRaisesRegex(Exception, "投递未被接受"):
            self.send(user, row)
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "failed")
        self.assertEqual(self.calls, [])

    def test_repeat_claim_or_wrong_user_cannot_duplicate_or_steal_delivery(self):
        client, user, headers = self.auth()
        self.bind({user["id"]: ["https://student.example/hook"]})
        self.preferences(user, channels=["push"])
        item, _, _ = self.private(client, headers)
        row = self.plan(user, item)
        self.core.delivery.deliver_message({"user_id": str(uuid.uuid4()), "delivery_id": row["id"]})
        self.assertEqual(self.calls, [])
        self.send(user, row)
        self.send(user, row)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "sent")

    def test_unknown_delivery_is_persisted_and_never_blindly_resent(self):
        client, user, headers = self.auth()
        self.bind({user["id"]: ["https://a.example/hook", "https://b.example/hook"]}, [204, 500])
        self.preferences(user, channels=["push"])
        item, _, _ = self.private(client, headers)
        row = self.plan(user, item)
        self.send(user, row)
        self.send(user, row)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "unknown")

    def test_concurrent_claim_during_send_does_not_transmit_twice(self):
        client, user, headers = self.auth()
        self.bind({user["id"]: ["https://student.example/hook"]})
        self.preferences(user, channels=["push"])
        item, _, _ = self.private(client, headers)
        row = self.plan(user, item)
        entered, release = threading.Event(), threading.Event()
        original = self.core.push.send_delivery_for_user

        def held_send(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("synthetic test sender was not released")
            return original(*args)

        self.core.push.send_delivery_for_user = held_send
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.send, user, row)
            try:
                self.assertTrue(entered.wait(5))
                self.send(user, row)
                self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "sending")
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "sent")

    def test_push_reminder_respects_quiet_hours(self):
        self.assertIn("push", QUIET_CHANNELS)
        client, user, headers = self.auth()
        self.bind({user["id"]: ["https://student.example/hook"]})
        self.preferences(user, channels=["push"], quiet_start="12:00", quiet_end="13:00")
        item, _, _ = self.private(client, headers)
        row = self.plan(user, item)
        self.send(user, row)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.core.store.get("deliveries", row["id"])["status"], "scheduled")
