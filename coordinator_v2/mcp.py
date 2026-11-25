"""Role-gated MCP transport for the fixed synthetic provider."""

import json
import os
from pathlib import Path
import select
import subprocess
import sys
from threading import Lock
import time

from coordinator.context import Blocked

VERSION = "2025-11-25"
TOOL = "inventory_lookup"
POLICY = {
    "developer": {TOOL: {"office", "kitchen"}},
    "interface": {TOOL: {"office"}},
    "tester": {TOOL: {"office", "kitchen", "outdoor"}},
}
MAX_MESSAGE = 8192


class Stdio:
    def __init__(self, process):
        self.process = process
        self.buffer = b""
        self.deadline = time.monotonic() + 4

    def send(self, method, params=None, request_id=None):
        data = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            data["params"] = params
        if request_id is not None:
            data["id"] = request_id
        self.process.stdin.write(json.dumps(data).encode() + b"\n")
        self.process.stdin.flush()
        if request_id is None:
            return None
        while b"\n" not in self.buffer:
            remain = self.deadline - time.monotonic()
            if remain <= 0 or not select.select([self.process.stdout], [], [], remain)[0]:
                raise Blocked("Тестовый MCP-провайдер превысил тайм-аут.")
            data = os.read(self.process.stdout.fileno(), 1024)
            self.buffer += data
            if not data or len(self.buffer) > MAX_MESSAGE:
                raise Blocked("Тестовый MCP-провайдер прервался или превысил лимит ответа.")
        line, self.buffer = self.buffer.split(b"\n", 1)
        answer = json.loads(line)
        if answer.get("jsonrpc") != "2.0" or answer.get("id") != request_id or "result" not in answer:
            raise Blocked("Тестовый MCP-провайдер вернул недопустимый ответ.")
        return answer["result"]


def fixture_call(category):
    provider = Path(__file__).with_name("mcp_fixture.py")
    with subprocess.Popen(
        [sys.executable, "-I", str(provider)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env={"PATH": os.defpath, "LC_ALL": "C.UTF-8"},
    ) as process:
        try:
            channel = Stdio(process)
            initialized = channel.send("initialize", {
                "protocolVersion": VERSION, "capabilities": {},
                "clientInfo": {"name": "coordinator-v2", "version": "1"},
            }, 1)
            if initialized.get("protocolVersion") != VERSION or "tools" not in initialized.get("capabilities", {}):
                raise Blocked("Тестовый MCP-провайдер предложил неподдерживаемый протокол.")
            channel.send("notifications/initialized")
            result = channel.send("tools/call", {"name": TOOL, "arguments": {"category": category}}, 2)
            structured = result.get("structuredContent", {})
            items = structured.get("items")
            if (
                result.get("isError") or structured.get("category") != category
                or not isinstance(items, list) or len(items) > 12
                or not all(isinstance(item, str) and 0 < len(item) <= 48 for item in items)
            ):
                raise Blocked("Результат MCP не соответствует схеме тестового инструмента.")
            return {"category": category, "items": items}
        except (AttributeError, ValueError, OSError):
            raise Blocked("Не удалось выполнить запрос к тестовому MCP.") from None
        finally:
            if process.poll() is None:
                process.kill()


class Gateway:
    def __init__(self):
        self.lock = Lock()
        self.cache = {}
        self.requests = 0
        self.hits = 0

    def call(self, role, tool, arguments):
        categories = POLICY.get(role, {}).get(tool)
        if not categories:
            raise Blocked("У роли нет полномочий на этот MCP-инструмент.")
        if (not isinstance(arguments, dict) or set(arguments) != {"category"}
                or not isinstance(arguments["category"], str) or arguments["category"] not in categories):
            raise Blocked("Недопустимые параметры тестового MCP-инструмента.")
        category = arguments["category"]
        with self.lock:
            if category in self.cache:
                self.hits += 1
                return self.cache[category], True
            if self.requests >= 3:
                raise Blocked("Исчерпан лимит вызовов тестового MCP.")
            self.requests += 1
            self.cache[category] = fixture_call(category)
            return self.cache[category], False
