from email.message import Message
from io import BytesIO
import json
import os
import shutil
import sqlite3
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from contextlib import closing

from coordinator.context import Blocked, digest, git
from coordinator_v2.demo_fixture import SNAPSHOT

from .api import GitHubAPI
from .engine import Deferred, advance
from . import github
from .queue import Conflict, Queue
from .test_v3 import IMAGE, Sandbox
from .transport import Client, server


class HardeningTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "queue.sqlite"
        self.time = 100
        self.queue = Queue(self.path, clock=lambda: self.time)
        self.base = git("rev-parse", "HEAD").decode().strip()
        self.spec = {"repo": "team/repo", "issue": 18, "actor": "owner", "request": "hardening",
                     "mode": "github", "base": self.base, "image": IMAGE, "project": "lab",
                     "features": ["tests", "browser"], "snapshot": SNAPSHOT}
        self.run = self.queue.submit(self.spec)
        self.queue.register("third", "acct", "test")

    def advance(self, current):
        advance(self.queue, self.root, current, lambda *_: Sandbox())

    def test_deferred_revokes_previously_queued_work_and_requires_fresh_attestation(self):
        self.advance(lambda spec: spec["snapshot"])
        self.assertEqual(self.queue.status()["runs"][0]["dispatch"], "ready")

        def unavailable(_):
            raise Deferred

        self.advance(unavailable)
        self.assertIsNone(self.queue.claim("third", "acct"))
        self.assertEqual(self.queue.status()["runs"][0]["dispatch"], "awaiting_github")
        self.advance(lambda spec: spec["snapshot"])
        self.time += 30
        self.assertIsNone(self.queue.claim("third", "acct"))
        self.advance(lambda spec: spec["snapshot"])
        self.assertEqual(self.queue.claim("third", "acct")["role"], "analyst")

    def test_expired_snapshot_cache_does_not_extend_admission(self):
        self.advance(lambda spec: spec["snapshot"])
        self.time += 31
        self.advance(lambda spec: (self.time - 31, spec["snapshot"]))
        self.assertIsNone(self.queue.claim("third", "acct"))
        self.advance(lambda spec: {"body": "new", "title": spec["snapshot"]["title"]})
        self.assertIsNone(self.queue.claim("third", "acct"))
        self.assertEqual(self.queue.status()["runs"][0]["status"], "blocked")

    def test_queue_denies_missing_admission_and_cancellation_fences_lease(self):
        self.queue.step(self.run, "analyst", 0, {})
        self.assertIsNone(self.queue.claim("third", "acct"))
        with self.assertRaisesRegex(Conflict, "requirements_changed"):
            self.queue.admit(self.run, "x" * 64, self.time)
        self.queue.admit(self.run, digest(SNAPSHOT), self.time)
        job = self.queue.claim("third", "acct")
        self.queue.finish(self.run, {"status": "cancelled"})
        with self.assertRaisesRegex(Conflict, "lease_expired"):
            self.queue.heartbeat("third", "acct", job["id"], job["attempt"])
        self.queue.review(job["id"], "confirmed_stopped")
        self.assertIsNone(self.queue.claim("third", "acct"))
        review = self.queue.status()["jobs"][0]
        self.assertEqual((review["status"], review["resolution"]), ("needs_review", "confirmed_stopped"))

    def test_git_store_preflight_writes_to_bare_store_only(self):
        state = self.root / "state"
        state.mkdir(mode=0o700)
        store = state / "git"
        subprocess.run(["git", "clone", "--bare", "--no-local", ".", str(store)], check=True, capture_output=True)
        store.chmod(0o700)
        subprocess.run(["git", f"--git-dir={store}", "remote", "set-url", "origin",
                        "https://github.com/team/repo.git"], check=True)
        before = git("for-each-ref", "refs/coordinator-v3/preflight")
        result = subprocess.run([sys.executable, "-m", "coordinator_v3", "--state", str(state),
                                 "preflight", "--base", self.base, "--repo", "team/repo"],
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout),
                         {"git": "ok", "sqlite": "ok", "temp": "ok", "container_probe": False})
        self.assertEqual(git("for-each-ref", "refs/coordinator-v3/preflight"), before)
        self.assertTrue((state / "queue.sqlite").exists())
        with patch.dict(os.environ, {"COORDINATOR_V3_GIT_STORE": str(store)}):
            self.assertEqual(git("rev-parse", "--is-bare-repository").strip(), b"true")
            self.assertEqual(git("for-each-ref", "refs/coordinator-v3/preflight"), b"")
        restored = self.root / "restore"
        restored.mkdir(mode=0o700)
        state_queue = Queue(state / "queue.sqlite")
        key = state_queue.submit(self.spec)
        candidate_ref = f"refs/coordinator-v3/{key}"
        with patch.dict(os.environ, {"COORDINATOR_V3_GIT_STORE": str(store)}):
            git("update-ref", candidate_ref, self.base)
        with closing(sqlite3.connect(state / "queue.sqlite")) as source, closing(
                sqlite3.connect(restored / "queue.sqlite")) as target:
            source.backup(target)
            self.assertEqual(target.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        shutil.copytree(store, restored / "git")
        with patch.dict(os.environ, {"COORDINATOR_V3_GIT_STORE": str(restored / "git")}):
            self.assertEqual(git("rev-parse", candidate_ref).decode().strip(), self.base)
        self.assertEqual(Queue(restored / "queue.sqlite").status()["runs"][0]["dispatch"], "awaiting_github")

    def test_authentication_precedes_read_and_rotation_revokes_token(self):
        credentials = {name: {"token": name * 16, "account": name}
                       for name in ("third", "tolya", "semen")}

        def probe(old=None):
            http = server(0, self.queue, credentials, allowed_version="supported")
            thread = Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                # No bytes of the declared body are sent.
                import socket
                with socket.create_connection(("127.0.0.1", http.server_port), timeout=1) as conn:
                    conn.sendall(b"POST /v1/claim HTTP/1.1\r\nHost: localhost\r\nContent-Length: 999\r\n\r\n")
                    self.assertIn(b"401", conn.recv(1024))
                url = f"http://127.0.0.1:{http.server_port}"
                if old:
                    with self.assertRaisesRegex(Conflict, "worker_request_rejected"):
                        Client(url, old)("register", {"worker": "third", "account": "third", "version": "supported"})
                caller = Client(url, credentials["tolya"]["token"])
                with self.assertRaisesRegex(Conflict, "unsupported_cli_version"):
                    caller("register", {"worker": "tolya", "account": "tolya", "version": "wrong"})
                self.assertIsNone(caller("register", {"worker": "tolya", "account": "tolya", "version": "supported"}))
            finally:
                http.shutdown()
                http.server_close()
                thread.join()

        probe()
        old = credentials["third"]["token"]
        credentials["third"]["token"] = "rotated" * 8
        probe(old)


class GitHubBackoffTest(unittest.TestCase):
    def test_etag_and_retry_after_and_no_attempts_during_backoff(self):
        class Response(BytesIO):
            headers = Message()
        now = [100.0]
        response = Response(b'{"state":"open"}')
        response.headers["ETag"] = '"test-etag"'
        headers = Message()
        headers["Retry-After"] = "44"
        calls = []

        def open_url(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                return response
            if len(calls) == 2:
                raise HTTPError(request.full_url, 304, "Not modified", Message(), BytesIO())
            raise HTTPError(request.full_url, 429, "Rate limit", headers, BytesIO())

        with TemporaryDirectory() as temp:
            token = Path(temp) / "token"
            token.write_text("read_token")
            transport = GitHubAPI(token, clock=lambda: now[0],
                                  monotonic=lambda: now[0], opener=type("Opener", (), {"open": staticmethod(open_url)})())
            with patch("coordinator_v3.api.time.sleep"):
                for _ in range(2):
                    self.assertEqual(transport("repos/team/repo/issues/18"), {"state": "open"})
                self.assertEqual(calls[-1].get_header("If-none-match"), '"test-etag"')
                self.assertEqual(transport.not_modified, 1)
                with self.assertRaises(Blocked):
                    transport("repos/team/repo/issues/18")
                now[0] += 43
                with self.assertRaises(Deferred):
                    transport("repos/team/repo/issues/19")
                self.assertEqual(len(calls), 3)

    def test_snapshot_cache_keeps_original_timestamp_and_invalidates_on_outage(self):
        with TemporaryDirectory() as temp:
            now = [100]
            transport = GitHubAPI(Path(temp) / "token", clock=lambda: now[0], monotonic=lambda: now[0])
            cache = github.SnapshotCache(transport)
            with patch.object(github, "current", return_value=SNAPSHOT) as current:
                self.assertEqual(cache({"issue": 18}), (100, SNAPSHOT))
                now[0] += 5
                self.assertEqual(cache({"issue": 18}), (100, SNAPSHOT))
                self.assertEqual(current.call_count, 1)
                transport.retry_at = now[0] + 2
                with self.assertRaises(Deferred):
                    cache({"issue": 18})
                now[0] += 3
                self.assertEqual(cache({"issue": 18}), (108, SNAPSHOT))
                self.assertEqual(current.call_count, 2)
