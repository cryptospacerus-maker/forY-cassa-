"""Bound and authorize each Responses API request before it can incur generation usage."""

from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from secrets import token_urlsafe
from threading import Lock, Thread

from .context import Blocked

# Each tuple caps generation requests, total counted input, and output per response.
LIMITS = {"demo": (4, 32_000, 768), "task": (8, 100_000, 2400)}
COUNT_FIELDS = {"model", "input", "instructions", "text"}
MAX_BODY = 768 * 1024


@dataclass
class Ticket:
    token: str
    used: bool = False
    error: str = ""


def provider_request(key, path, payload):
    connection = HTTPSConnection("api.openai.com", timeout=45)
    try:
        connection.request("POST", "/v1" + path, json.dumps(payload).encode(), {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
        })
        return connection, connection.getresponse()
    except (OSError, HTTPException):
        connection.close()
        raise


class Gateway:
    def __init__(self, key, model, mode):
        self._key = key
        self.model = model
        self.max_requests, self.max_input, self.output_per_request = LIMITS[mode]
        self.lock = Lock()
        self.tickets = {}
        self.requests = self.input_reserved = 0

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        self.server.gateway = self
        self.server_thread = Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    @contextmanager
    def ticket(self):
        ticket = Ticket(token_urlsafe(32))
        with self.lock:
            self.tickets[ticket.token] = ticket
        try:
            yield ticket
        finally:
            with self.lock:
                del self.tickets[ticket.token]

    def authorize(self, token):
        with self.lock:
            ticket = self.tickets.get(token)
            if ticket is None or ticket.used:
                return None
            # Even a failed or interrupted request cannot be retried with this ticket.
            ticket.used = True
            return ticket

    def summary(self):
        with self.lock:
            return {
                "requests": self.requests, "max_requests": self.max_requests,
                "input_reserved": self.input_reserved, "max_input": self.max_input,
                "output_reserved": self.requests * self.output_per_request,
                "max_output": self.max_requests * self.output_per_request,
            }

    def forward(self, payload):
        with self.lock:
            if self.requests >= self.max_requests:
                raise Blocked("Достигнут лимит запросов или входных токенов.")
        if payload.get("model") != self.model or not isinstance(payload.get("input"), list):
            raise Blocked("Запрос Codex не соответствует настроенной модели или формату.")
        messages = []
        for item in payload["input"]:
            content = item.get("content")
            if (item.get("type") != "message" or item.get("role") not in {"developer", "system", "user"}
                    or not isinstance(content, list) or not content
                    or any(part.get("type") != "input_text" or not isinstance(part.get("text"), str)
                           for part in content)):
                raise Blocked("Разрешены только текстовые входные сообщения.")
            messages.append({"type": "message", "role": item["role"], "content": [
                {"type": "input_text", "text": part["text"]} for part in content
            ]})
        count_payload = {key: payload[key] for key in COUNT_FIELDS if key in payload}
        count_payload.update(input=messages, tools=[], truncation="disabled", reasoning={"effort": "low"})
        connection, response = provider_request(self._key, "/responses/input_tokens", count_payload)
        try:
            if response.status != 200:
                raise Blocked("OpenAI API не подтвердил число входных токенов.")
            count = json.loads(response.read(16_384))["input_tokens"]
            if type(count) is not int or count <= 0:
                raise Blocked("OpenAI API вернул некорректное число токенов.")
        finally:
            connection.close()

        with self.lock:
            if self.requests >= self.max_requests or self.input_reserved + count > self.max_input:
                raise Blocked("Достигнут лимит запросов или входных токенов.")
            self.requests += 1
            self.input_reserved += count

        # The count and generation requests use identical text, instructions and schema.
        # Host tools, continuations, provider options and storage are omitted.
        generation = dict(count_payload, max_output_tokens=self.output_per_request,
                          store=False, stream=True)
        return provider_request(self._key, "/responses", generation)


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        ticket = self.server.gateway.authorize(self.headers.get("Authorization", "").removeprefix("Bearer "))
        if ticket is None:
            self.send_error(403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                raise Blocked("Запрос Codex превышает лимит размера или пуст.")
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(size))
            connection, response = self.server.gateway.forward(payload)
        except Blocked as error:
            ticket.error = str(error)
            self.send_error(422)
            return
        except (AttributeError, KeyError, OSError, ValueError, TypeError, HTTPException):
            ticket.error = "Не удалось подготовить запрос к OpenAI API."
            self.send_error(502)
            return
        try:
            if response.status != 200:
                ticket.error = f"OpenAI API отклонил генерацию (HTTP {response.status})."
                self.send_error(422)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            while line := response.readline(256 * 1024):
                self.wfile.write(line)
        except (OSError, HTTPException):
            ticket.error = "Передача ответа OpenAI API прервана."
        finally:
            connection.close()
