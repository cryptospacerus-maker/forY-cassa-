"""Authenticated worker pull protocol over HTTPS or loopback HTTP."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from threading import BoundedSemaphore
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .queue import Conflict, MAX_BODY, encode
from .spike import ROUTES


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class Client:
    def __init__(self, url, token):
        endpoint = urlsplit(url)
        if (not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}
                or (endpoint.scheme != "https" and
                    (endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "::1"}))):
            raise ValueError("Use HTTPS or loopback HTTP")
        self.url, self.token = url.rstrip("/"), token
        self.opener = build_opener(NoRedirect())

    def __call__(self, action, payload):
        request = Request(f"{self.url}/v1/{action}", data=encode(payload).encode(), headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
        })
        try:
            with self.opener.open(request, timeout=8) as response:
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise OSError("oversized response")
                return json.loads(raw)["result"]
        except HTTPError as error:
            with error:
                if error.code == 409:
                    raise Conflict(json.loads(error.read(1024))["error"]) from None
                if error.code in {400, 401, 404}:
                    raise Conflict("worker_request_rejected") from None
            raise OSError("coordinator unavailable") from None
        except (URLError, ValueError, KeyError) as error:
            raise OSError("coordinator unavailable") from error


class BoundedServer(ThreadingHTTPServer):
    request_queue_size = 16

    def __init__(self, *args):
        self.slots = BoundedSemaphore(16)
        super().__init__(*args)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def server(port, queue, credentials, *, allowed_version=None):
    if (set(credentials) != set(ROUTES.values()) or
            len({value["token"] for value in credentials.values()}) != len(credentials) or
            len({value["account"] for value in credentials.values()}) != len(credentials) or
            not all(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value["token"]) and 0 < len(value["account"]) <= 80
                    for value in credentials.values())):
        raise ValueError("Configure one distinct credential and account alias per worker")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(10)
            super().setup()

        def log_message(self, *args):
            pass

        def reply(self, code, data):
            packed = encode(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(packed)))
            self.end_headers()
            self.wfile.write(packed)

        def do_POST(self):
            action = self.path.removeprefix("/v1/")
            if self.path != f"/v1/{action}" or action not in {"register", "claim", "heartbeat", "complete"}:
                return self.reply(404, {"error": "unknown_endpoint"})
            supplied = self.headers.get("Authorization", "").encode()
            worker = None
            for name, credential in credentials.items():
                if hmac.compare_digest(supplied, f"Bearer {credential['token']}".encode()):
                    worker = name
            if worker is None:
                return self.reply(401, {"error": "worker_identity_mismatch"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_BODY:
                    raise ValueError
                data = json.loads(self.rfile.read(length))
                owner, account = data.pop("worker"), data.pop("account")
                if owner != worker or account != credentials[worker]["account"]:
                    return self.reply(401, {"error": "worker_identity_mismatch"})
                if action == "register":
                    version = data.get("version")
                    if not isinstance(version, str) or len(version) > 120:
                        raise ValueError
                    if allowed_version is not None and version != allowed_version:
                        raise Conflict("unsupported_cli_version")
                result = getattr(queue, action)(worker, account, **data)
            except Conflict as error:
                return self.reply(409, {"error": str(error)})
            except (ValueError, KeyError, TypeError, AttributeError, OSError):
                return self.reply(400, {"error": "invalid_request"})
            return self.reply(200, {"result": result})

    return BoundedServer(("127.0.0.1", port), Handler)
