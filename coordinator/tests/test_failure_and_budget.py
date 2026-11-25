from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.client import HTTPConnection
from io import BytesIO
import json
import subprocess
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

from coordinator import notify
from coordinator.__main__ import make_meta, marker
from coordinator.context import Blocked
from coordinator.gateway import Gateway
from coordinator.runner import Codex, WORK


PAYLOAD = {
    "model": "fixture-model", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "test"}]},
    ],
}


def http_response(data, status=200):
    response = BytesIO(data)
    response.status = status
    return Mock(), response


class BudgetTest(unittest.TestCase):
    @patch("coordinator.gateway.provider_request")
    def test_reject_budget_overshoot_without_generating(self, upstream):
        gateway = Gateway("key", "fixture-model", "demo")
        upstream.return_value = http_response(b'{"input_tokens":32001}')
        with self.assertRaisesRegex(Blocked, "Достигнут лимит"):
            gateway.forward(PAYLOAD)
        self.assertEqual(gateway.summary()["requests"], 0)
        self.assertEqual([call.args[1] for call in upstream.call_args_list], ["/responses/input_tokens"])

    @patch("coordinator.gateway.provider_request")
    def test_parallel_sessions_cannot_both_reserve_the_last_tokens(self, upstream):
        gate = Barrier(2)
        gateway = Gateway("key", "fixture-model", "demo")

        def provider(key, path, payload):
            if path.endswith("/input_tokens"):
                gate.wait(timeout=10)
                return http_response(b'{"input_tokens":20000}')
            return http_response(b"")

        upstream.side_effect = provider
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(gateway.forward, PAYLOAD) for _ in range(2)]
        errors = [result.exception() for result in results]
        self.assertEqual(sum(error is None for error in errors), 1)
        self.assertTrue(any(isinstance(error, Blocked) for error in errors))
        self.assertEqual(gateway.summary()["input_reserved"], 20000)
        self.assertEqual(gateway.summary()["requests"], 1)

    @patch("coordinator.gateway.provider_request")
    def test_uncertain_generation_cannot_be_retried_with_same_ticket(self, upstream):
        upstream.side_effect = [http_response(b'{"input_tokens":5}'), OSError("connection lost")]
        with Gateway("key", "fixture-model", "demo") as gateway, gateway.ticket() as ticket:
            def send(token):
                connection = HTTPConnection("127.0.0.1", gateway.server.server_port, timeout=10)
                try:
                    connection.request("POST", "/v1/responses", json.dumps(PAYLOAD),
                                       {"Authorization": f"Bearer {token}"})
                    response = connection.getresponse()
                    response.read()
                    return response.status
                finally:
                    connection.close()
            self.assertEqual(send("wrong-token"), 403)
            self.assertEqual(send(ticket.token), 502)
            self.assertEqual(send(ticket.token), 403)
            self.assertEqual(gateway.summary()["requests"], 1)
        self.assertEqual(upstream.call_count, 2)

    @patch("coordinator.gateway.provider_request")
    def test_reject_non_text_inputs_before_contacting_provider(self, upstream):
        payload = deepcopy(PAYLOAD)
        payload["input"][0]["content"][0] = {"type": "input_image", "image_url": "https://invalid.test"}
        with self.assertRaisesRegex(Blocked, "только текстовые"):
            Gateway("key", "fixture-model", "demo").forward(payload)
        upstream.assert_not_called()

    @patch("coordinator.gateway.provider_request")
    def test_request_quota_caps_attempts_even_with_tokens_left(self, upstream):
        def provider(key, path, payload):
            return http_response(b'{"input_tokens":1}')
        upstream.side_effect = provider
        gateway = Gateway("key", "fixture-model", "demo")
        for _ in range(4):
            connection, _ = gateway.forward(PAYLOAD)
            connection.close()
        with self.assertRaisesRegex(Blocked, "Достигнут лимит"):
            gateway.forward(PAYLOAD)
        self.assertEqual(gateway.summary()["output_reserved"], 3072)

    @patch("coordinator.runner.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 120))
    def test_cli_timeout_has_a_safe_reportable_reason(self, run):
        with Gateway("key", "fixture-model", "demo") as gateway:
            result = Codex(gateway)("developer", "Test", {}, WORK)
        self.assertIn("тайм-аут", result["error"])
        self.assertNotIn("fixture-model", result["error"])


class FailureNotificationTest(unittest.TestCase):
    def setUp(self):
        self.meta = make_meta("owner/repo", 13, "alice", "pilot-key", "demo", "a" * 40, "100")
        self.event = {"action": "completed", "workflow_run": {
            "id": 100, "path": ".github/workflows/coordinator.yml",
            "display_title": "Coordinator issue=13 mode=demo request=pilot-key",
            "event": "workflow_dispatch", "actor": {"login": "alice"}, "head_branch": "main",
            "head_repository": {"full_name": "owner/repo"}, "head_sha": "a" * 40,
            "conclusion": "cancelled", "run_attempt": 1,
        }}
        self.claim = {"body": marker(self.meta, "claim") + "\nЗапрос принят."}

    @patch.object(notify, "api", return_value={"run_attempt": 1, "status": "completed"})
    @patch.object(notify, "add_comment")
    @patch.object(notify, "reservations")
    def test_cancelled_claim_notifies_the_verified_initiator_once(self, reservations, comment, api):
        reservations.return_value = [self.claim]
        notify.notify("owner/repo", self.event)
        body = comment.call_args.args[1]
        self.assertIn(marker(self.meta, "interrupted"), body)
        self.assertIn("\n@alice\n", body)
        self.assertIn("/actions/runs/100", body)
        reservations.return_value.append({"body": body})
        notify.notify("owner/repo", self.event)
        comment.assert_called_once()

    @patch.object(notify, "api", return_value={"run_attempt": 2, "status": "in_progress"})
    @patch.object(notify, "add_comment")
    @patch.object(notify, "reservations")
    def test_no_notice_without_claim_with_report_or_during_newer_attempt(self, reservations, comment, api):
        for items in ([], [self.claim, {"body": marker(self.meta, "report")}], [self.claim]):
            reservations.return_value = items
            notify.notify("owner/repo", self.event)
        comment.assert_not_called()
        api.assert_called_once()

    @patch.object(notify, "reservations")
    def test_untrusted_workflow_metadata_never_acquires_issue_write_path(self, reservations):
        for key, value in (
            ("head_repository", {"full_name": "outsider/repo"}), ("head_branch", "feature"),
            ("path", ".github/workflows/other.yml"), ("event", "push"),
            ("display_title", "Coordinator issue=13 mode=demo request=hello\n@outsider"),
        ):
            event = deepcopy(self.event)
            event["workflow_run"][key] = value
            notify.notify("owner/repo", event)
        reservations.assert_not_called()
