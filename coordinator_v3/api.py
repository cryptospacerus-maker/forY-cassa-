"""Bounded GitHub REST access for the service, with conditional reads and backoff."""

from collections import OrderedDict
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import hashlib
import json
import random
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from coordinator.context import API_TRANSPORT, Blocked
from .engine import Deferred
from .transport import NoRedirect

HOST = "https://api.github.com"
MAX_RESPONSE = 2 * 1024 * 1024
REFRESH = 20


def retry_seconds(header, now):
    if not header:
        return 0
    if header.isdecimal():
        return int(header)
    try:
        return max(0, parsedate_to_datetime(header).timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return 0


class GitHubAPI:
    def __init__(self, token_file, *, clock=time.time, monotonic=time.monotonic, opener=None):
        self.token_file = token_file
        self.clock, self.monotonic = clock, monotonic
        self.opener = opener or build_opener(NoRedirect())
        self.cache = OrderedDict()
        self.next_request = self.retry_at = 0
        self.failures = self.requests = self.not_modified = 0

    def permit(self):
        if self.monotonic() < self.retry_at:
            raise Deferred

    @contextmanager
    def installed(self):
        reset = API_TRANSPORT.set(self)
        try:
            yield
        finally:
            API_TRANSPORT.reset(reset)

    def request(self, url, method, data):
        self.permit()
        try:
            token = self.token_file.read_text().strip()
        except OSError:
            raise Blocked("GitHub API недоступен для операции чтения credentials.") from None
        if not re.fullmatch(r"[A-Za-z0-9_]+", token):
            raise Blocked("GitHub API недоступен для операции чтения credentials.")
        cache_key = (hashlib.sha256(token.encode()).digest(), url)
        etag, cached, link = self.cache.get(cache_key, (None, None, None))
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Coordinator-V3"}
        if method == "GET" and etag:
            headers["If-None-Match"] = etag
        if data is not None:
            headers["Content-Type"] = "application/json"
        time.sleep(max(0, self.next_request - self.monotonic()))
        self.next_request = self.monotonic() + .2
        self.requests += 1
        try:
            with self.opener.open(Request(url, headers=headers, method=method,
                                          data=None if data is None else json.dumps(data).encode()), timeout=5) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ValueError("response size")
                value, link = json.loads(raw), response.headers.get("Link")
                if method == "GET":
                    self.cache[cache_key] = (response.headers.get("ETag"), value, link)
                    self.cache.move_to_end(cache_key)
                    if len(self.cache) > 32:
                        self.cache.popitem(last=False)
                self.failures = 0
                return value, link
        except HTTPError as error:
            with error:
                status, response_headers = error.code, error.headers
                if status == 304 and cached is not None:
                    self.failures = 0
                    self.not_modified += 1
                    return cached, response_headers.get("Link", link)
                retry = response_headers.get("Retry-After")
                limited = status == 429 or (status == 403 and (
                    retry or response_headers.get("X-RateLimit-Remaining") == "0"))
                if limited or status >= 500:
                    delay = retry_seconds(retry, self.clock()) or (60 if limited else 0)
                    reset = response_headers.get("X-RateLimit-Reset", "")
                    if limited and response_headers.get("X-RateLimit-Remaining") == "0" and reset.isdecimal():
                        delay = max(delay, int(reset) - self.clock())
                    self.backoff(delay)
                if not limited and status in {401, 403, 404} and method == "GET":
                    self.backoff(60 if status == 403 else 15)
        except (OSError, ValueError):
            self.backoff()
        raise Blocked("GitHub API недоступен для операции запроса.") from None

    def backoff(self, seconds=0):
        self.failures += 1
        delay = min(120, 2 ** min(7, self.failures))
        self.retry_at = self.monotonic() + max(seconds, delay) + random.uniform(0, 1)

    def publisher(self, expected):
        profile, _ = self.request(f"{HOST}/user", "GET", None)
        if profile["login"].lower() != expected.lower():
            raise Blocked("Токен записи принадлежит другому GitHub-актору.")

    def __call__(self, path, *, method="GET", data=None, paginate=False):
        url = f"{HOST}/{path}"
        pages = []
        for _ in range(8):
            result, link = self.request(url, method, data)
            if not paginate:
                return result
            pages.append(result)
            next_page = re.search(r'<([^>]+)>; rel="next"', link or "")
            if not next_page:
                return pages
            url = next_page[1]
            next_url = urlsplit(url)
            if next_url.scheme != "https" or next_url.netloc != "api.github.com":
                break
        raise Blocked("GitHub API недоступен для операции полной пагинации.")
