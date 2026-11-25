"""Exercise the pinned Codex binary with a loopback Responses API fixture."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import shutil
import subprocess
from threading import Thread
import unittest
from unittest.mock import patch

from coordinator.runner import Codex, WORK


class FixtureProvider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((
            self.path, {tool.get("name", tool.get("type")) for tool in data.get("tools", [])},
        ))
        content = {
            "type": "output_text", "text": '{"summary":"provider fixture","files":[]}',
            "annotations": [],
        }
        item = {"id": "msg_fixture", "type": "message", "status": "completed",
                "role": "assistant", "content": [content]}
        response = {
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
            {"type": "response.completed", "response": response},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in events:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())


class CodexTransportTest(unittest.TestCase):
    def test_pinned_cli_exposes_no_host_tools_and_returns_usage(self):
        binary = shutil.which("codex")
        if not binary:
            self.skipTest("Install Codex CLI 0.172.0 to run the transport test.")
        call = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        if call.stdout.strip() != "codex-cli 0.172.0":
            self.skipTest("The transport test uses the pinned Codex CLI 0.172.0.")

        with HTTPServer(("127.0.0.1", 0), FixtureProvider) as server:
            server.requests = []
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}/v1"
            original = subprocess.run

            def loopback(command, **kwargs):
                overrides = [
                    "-c", 'model_provider="fixture"', "-c",
                    f'model_providers.fixture={{name="Fixture",base_url="{base_url}",wire_api="responses"}}',
                ]
                kwargs["timeout"] = 20
                return original([*command[:2], *overrides, *command[2:]], **kwargs)

            try:
                with (
                    patch.dict(os.environ, {"CODEX_API_KEY": "fixture-key", "COORDINATOR_MODEL": "gpt-5.4"}),
                    patch("coordinator.runner.subprocess.run", side_effect=loopback),
                ):
                    result = Codex()("developer", "Верни результат", {}, WORK)
                self.assertEqual(len(server.requests), 1)
                route, tools = server.requests[0]
                self.assertEqual(route, "/v1/responses")
                self.assertLessEqual(tools, {"request_user_input"})
                self.assertEqual(result["answer"]["summary"], "provider fixture")
                self.assertEqual(result["usage"]["input_tokens"], 10)
            finally:
                server.shutdown()
                thread.join()
