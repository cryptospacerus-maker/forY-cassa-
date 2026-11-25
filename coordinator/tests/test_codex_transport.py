"""Run the pinned Codex binary and gateway against an offline Responses API fixture."""

from io import BytesIO
import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

from coordinator.gateway import Gateway
from coordinator.runner import Codex, WORK


def fake_response(data, status=200):
    connection = Mock()
    response = BytesIO(data)
    response.status = status
    return connection, response


def response_events(text):
    content = {"type": "output_text", "text": text, "annotations": []}
    item = {"id": "msg_fixture", "type": "message", "status": "completed",
            "role": "assistant", "content": [content]}
    result = {
        "id": "resp_fixture", "object": "response", "status": "completed", "output": [item],
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}},
    }
    events = [
        {"type": "response.created", "response": {"id": "resp_fixture"}},
        {"type": "response.output_item.added", "output_index": 0, "item": dict(item, content=[])},
        {"type": "response.output_text.delta", "item_id": item["id"],
         "output_index": 0, "content_index": 0, "delta": content["text"]},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": result},
    ]
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


class CodexTransportTest(unittest.TestCase):
    @patch("coordinator.gateway.provider_request")
    def test_pinned_cli_uses_metered_gateway_for_one_text_only_response(self, upstream):
        binary = shutil.which("codex")
        if not binary:
            self.skipTest("Install Codex CLI 0.172.0 to run the transport test.")
        call = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        if call.stdout.strip() != "codex-cli 0.172.0":
            self.skipTest("The transport test uses the pinned Codex CLI 0.172.0.")
        upstream.side_effect = [
            fake_response(b'{"input_tokens":10}'),
            fake_response(response_events('{"summary":"provider fixture","files":[]}')),
        ]
        with Gateway("fixture-key", "gpt-5.4", "demo") as gateway:
            result = Codex(gateway)("developer", "Верни результат", {}, WORK)
            self.assertEqual(gateway.summary()["input_reserved"], 10, result)
        count, generate = [call.args for call in upstream.call_args_list]
        self.assertEqual(count[:2], ("fixture-key", "/responses/input_tokens"))
        self.assertEqual(generate[:2], ("fixture-key", "/responses"))
        self.assertEqual(generate[2]["input"], count[2]["input"])
        self.assertEqual(generate[2]["tools"], [])
        self.assertFalse(generate[2]["store"])
        self.assertEqual(generate[2]["max_output_tokens"], 768)
        self.assertEqual(result["answer"]["summary"], "provider fixture")
        self.assertEqual(result["usage"]["input_tokens"], 10)
