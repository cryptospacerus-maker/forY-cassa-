import json
from pathlib import Path
import sys
import shlex
from tempfile import TemporaryDirectory
from threading import Event, Thread
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
from uuid import uuid4

from coordinator.context import Blocked, git
from coordinator.runner import TEXT, schema
from coordinator_v2 import demo_fixture
from coordinator_v2.runtime import Evidence
from .cli import Adapter, CliError
from .engine import advance, delivery_state
from . import github
from .queue import Conflict, Queue, transaction
from .transport import Client, server
from .worker import Worker

IMAGE = "sha256:" + "1" * 64
SCHEMA = schema({"answer": TEXT})
ROLES = {"tolya": "developer", "semen": "interface", "third": "analyst"}


class Sandbox:
    image = IMAGE

    def __init__(self):
        self.calls = []

    def run(self, check, sha):
        self.calls.append((check.name, sha))
        bad = check.name == "qa-unit" and b'category == "unknown"' in git(
            "show", f"{sha}:coordinator_v2/demo/app/store.py")
        return Evidence(check.name, sha, "fail" if bad else "pass", int(bad), 0, "0" * 64,
                        ("Unknown category",) if bad else ())


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "queue.sqlite"
        self.time = 0
        self.queue = self.open_queue()
        self.spec = {"repo": "team/repo", "issue": 18, "actor": "owner", "request": "test",
                     "mode": "fixture", "base": git("rev-parse", "29fd98b").decode().strip(),
                     "image": IMAGE, "project": "lab", "features": ["tests", "browser"],
                     "snapshot": demo_fixture.SNAPSHOT}
        self.key = self.queue.submit(self.spec)

    def open_queue(self):
        return Queue(self.path, clock=lambda: self.time)

    def test_restart_and_expiry_do_not_requeue_leased_job(self):
        row = self.queue.step(self.key, "analyst", 0, {})
        self.queue.register("third", "acct", "test")
        self.assertEqual(self.queue.status()["workers"]["tolya"]["status"], "offline")
        job = self.queue.claim("third", "acct")
        self.time = 46
        reopened = self.open_queue()
        self.assertEqual(reopened.submit(self.spec), self.key)
        self.assertEqual(reopened.status()["workers"]["third"]["status"], "uncertain")
        self.assertEqual(reopened.step(self.key, "analyst", 0, {})["status"], "needs_review")
        with self.assertRaisesRegex(Conflict, "lease_expired"):
            reopened.complete("third", "acct", row["id"], job["attempt"], answer={"answer": "late"})
        with self.assertRaisesRegex(Conflict, "unresolved_execution"):
            reopened.unpause("third")
        reopened.review(row["id"], "confirmed_stopped")
        reopened.unpause("third")
        self.assertIsNone(reopened.claim("third", "acct"))
        with self.assertRaisesRegex(Conflict, "request_changed"):
            reopened.submit(dict(self.spec, snapshot={"title": "changed"}))

    def test_concurrent_claim_and_quota_pause_only_assigned_worker(self):
        for role in ("analyst", "tester", "developer"):
            self.queue.step(self.key, role, 0, {})
        self.queue.register("third", "acct", "test")
        self.queue.register("tolya", "other", "test")
        with self.assertRaisesRegex(Conflict, "account_already_registered"):
            self.queue.register("semen", "acct", "test")
        with ThreadPoolExecutor(8) as pool:
            jobs = [job for job in pool.map(lambda _: self.queue.claim("third", "acct"), range(12)) if job]
        self.assertEqual(len(jobs), 1)
        job, = jobs
        self.queue.complete("third", "acct", job["id"], job["attempt"], error="quota")
        self.queue.complete("third", "acct", job["id"], job["attempt"], error="quota")
        self.assertIsNone(self.queue.claim("third", "acct"))
        self.assertEqual(self.queue.claim("tolya", "other")["role"], "developer")
        self.queue.unpause("third")
        self.assertEqual(self.queue.claim("third", "acct")["role"], "tester")

    def test_outbox_retry_uses_local_result_without_reinvoking(self):
        self.queue.step(self.key, "analyst", 0, {"schema": SCHEMA, "mode": "fixture"})
        executions = []

        def adapter(job, *args):
            executions.append(job["id"])
            return str(uuid4()), {"answer": "done"}, None

        def client(action, payload):
            worker, account = payload.pop("worker"), payload.pop("account")
            result = getattr(self.queue, action)(worker, account, **payload)
            if action == "complete":
                raise OSError("response lost after commit")
            return result

        path = self.root / "third"
        worker = Worker(client, path, "third", "acct", "test", adapter)
        with self.assertRaises(OSError):
            worker.once()

        def restored(action, payload):
            return getattr(self.queue, action)(**payload)

        restarted = Worker(restored, path, "third", "acct", "test", adapter)
        self.assertFalse(restarted.once())
        self.assertEqual(len(executions), 1)
        self.assertEqual(self.queue.status()["jobs"][0]["status"], "ok")
        with self.assertRaisesRegex(Conflict, "worker_state_identity_mismatch"):
            Worker(restored, path, "third", "changed", "test", adapter)

    def test_http_full_cycle_with_three_workers_repair_and_exact_sha(self):
        creds = {name: {"token": name * 16, "account": name + "-account"} for name in ROLES}
        http = server(0, self.queue, creds)
        listener = Thread(target=http.serve_forever, daemon=True)
        listener.start()
        try:
            url = f"http://127.0.0.1:{http.server_port}"
            other = Client(url, creds["tolya"]["token"])
            with self.assertRaisesRegex(Conflict, "worker_request_rejected"):
                other("register", {"worker": "third", "account": creds["third"]["account"], "version": "test"})
            calls = []
            requirement = "Отдельное условие из Issue, отсутствующее в плане Игорька."
            snapshot = {**self.spec["snapshot"], "body": requirement}
            self.spec = dict(self.spec, snapshot=snapshot, request="complete")
            self.key = self.queue.submit(self.spec)
            with transaction(self.path) as db:
                db.execute("UPDATE runs SET status='cancelled' WHERE id!=?", (self.key,))

            def fake(job, work, home, previous, cancelled):
                role = job["role"]
                payload = json.loads(job["prompt"].rsplit("\nКонтекст: ", 1)[1])
                self.assertEqual(payload["requirements"]["body"], requirement)
                from coordinator.context import digest
                self.assertEqual(payload["requirements_sha256"], digest(snapshot))
                self.assertEqual(payload["base_sha"], self.spec["base"])
                if role in {"developer", "interface"}:
                    self.assertTrue(payload["criteria"] and payload["allowed_paths"] and payload["files"])
                if role == "interface":
                    self.assertIn("developer", payload["dependencies"])
                if role == "analyst":
                    answer = demo_fixture.plan("dependency")
                    for task in answer["tasks"]:
                        task["mcp_category"] = ""
                else:
                    answer = demo_fixture.model(role, "", payload, job["schema"])["answer"]
                thread = previous or str(uuid4())
                calls.append((role, work, home, previous, thread))
                return thread, answer, {"input_tokens": 2} if role == "developer" else None

            workers = [
                Worker(Client(url, cred["token"]), self.root / name, name, cred["account"], "fake CLI", fake)
                for name, cred in creds.items()
            ]
            sandbox = Sandbox()
            for _ in range(12):
                advance(self.open_queue(), self.root, lambda spec: spec["snapshot"], lambda *_: sandbox)
                for worker in workers:
                    worker.once()
                if all(run["status"] != "pending" for run in self.queue.status()["runs"]):
                    break
            with transaction(self.path) as db:
                result = json.loads(db.execute("SELECT result FROM runs WHERE id=?", (self.key,)).fetchone()[0])
            self.assertEqual(result["status"], "passed", result)
            self.assertEqual([check["verdict"] for check in result["checks"]], ["fail", "pass"])
            self.assertEqual(delivery_state(self.spec, self.key, result), result["sha"])
            self.assertIsNone(result["usage"]["total"])
            samples = result["usage"]["samples"]
            self.assertTrue(any(item["usage"] is None for item in samples))
            self.assertTrue(any(item["resume"] and item["usage"] == {"input_tokens": 2} for item in samples))
            rejected = dict(result, checks=[dict(result["checks"][-1], evidence=[])])
            with self.assertRaises(Blocked):
                delivery_state(self.spec, self.key, rejected)
            self.assertEqual(len(set(sandbox.calls)), len(sandbox.calls))

            analyst, developer, interface, repair, dependent, tester = calls
            self.assertEqual([call[0] for call in calls],
                             ["analyst", "developer", "interface", "developer", "interface", "tester"])
            self.assertNotEqual(analyst[1], tester[1])
            self.assertNotEqual(analyst[2], tester[2])
            self.assertNotEqual(analyst[4], tester[4])
            self.assertEqual(developer[4], repair[3])
            self.assertEqual(interface[4], dependent[3])
            self.assertEqual(repair[1], developer[1])
            self.assertIsNone(tester[3])
            journal = (self.root / f"{self.key}.jsonl").read_text()
            self.assertNotIn(analyst[4], journal)
            self.assertIn("qa.finished", journal)
        finally:
            http.shutdown()
            http.server_close()
            listener.join()

    def test_changed_requirements_cancel_pending_steps(self):
        sandbox = Sandbox()
        advance(self.queue, self.root, lambda spec: spec["snapshot"], lambda *_: sandbox)
        advance(self.queue, self.root, lambda spec: {"title": "changed"}, lambda *_: sandbox)
        self.queue.register("third", "acct", "test")
        self.assertIsNone(self.queue.claim("third", "acct"))
        self.assertEqual(self.queue.status()["runs"][0]["status"], "blocked")

    def test_ingest_uses_only_admitted_issue_comment_and_deduplicates(self):
        event = {"user": {"type": "User", "login": "Owner"}, "body": "/coordinator-v3 same",
                 "issue_url": "https://api.github.com/repos/team/repo/issues/18"}

        def fetch(repo, route):
            return event if route.startswith("issues/comments/") else {"object": {"sha": self.spec["base"]}}

        kwargs = {"project": "lab", "features": ["tests"], "image": IMAGE}
        with patch.object(github, "api", side_effect=fetch), patch.object(github, "check_author") as admit, patch.object(
                github, "issue_data", return_value=demo_fixture.SNAPSHOT) as data:
            with self.assertRaises(Blocked):
                github.ingest(self.queue, "team/repo", 99, 123, **kwargs)
            admit.assert_not_called()
            key = github.ingest(self.queue, "team/repo", 18, 123, **kwargs)
            self.assertEqual(github.ingest(self.open_queue(), "team/repo", 18, 123, **kwargs), key)
            self.assertEqual(data.call_count, 1)
            admit.assert_called_with("team/repo", "owner")

    def test_expiry_after_worker_stops_heartbeats_fences_result(self):
        self.queue.step(self.key, "analyst", 0, {})
        self.queue.register("third", "acct", "test")
        job = self.queue.claim("third", "acct")
        lease = (job["id"], job["attempt"])
        self.time = 40
        self.queue.heartbeat("third", "acct", *lease)
        self.time = 55
        self.assertEqual(self.open_queue().status()["jobs"][0]["status"], "leased")
        self.time = 86
        self.assertEqual(self.open_queue().status()["jobs"][0]["status"], "needs_review")
        with self.assertRaisesRegex(Conflict, "lease_expired"):
            self.queue.heartbeat("third", "acct", *lease)

    def test_publication_recovers_lost_reply_without_second_comment(self):
        with transaction(self.path) as db:
            spec = dict(self.spec, mode="github")
            db.execute("UPDATE runs SET status='blocked',spec=?,result=?",
                       (json.dumps(spec), json.dumps({"status": "blocked", "checks": []})))
        posted = []

        def add(meta, body):
            posted.append({"body": body, "html_url": "https://example.test/comment", "user": {"login": "bot"}})
            raise Blocked("connection lost")

        with patch.object(github, "comments", side_effect=lambda *_: posted), patch.object(
                github, "check_author"), patch.object(github, "add_comment", side_effect=add):
            with self.assertRaisesRegex(Blocked, "connection lost"):
                github.publish(self.queue, self.key, "bot")
            self.assertEqual(github.publish(self.open_queue(), self.key, "bot"), posted[0]["html_url"])
            self.assertEqual(github.publish(self.open_queue(), self.key, "bot"), posted[0]["html_url"])
        self.assertEqual(len(posted), 1)


class CliTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.binary = self.root / "codex"
        self.thread = str(uuid4())
        script = self.root / "fixture python.py"
        self.binary.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                               + shlex.quote(str(script)) + ' "$@"\n')
        script.write_text("""
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
assert 'OPENAI_API_KEY' not in os.environ and 'GH_TOKEN' not in os.environ
if args[:2] == ['login', 'status']:
    print('Logged in using ChatGPT')
    sys.exit()
prompt = sys.stdin.read()
if prompt == 'quota':
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'Usage limit reached'}}))
    sys.exit(2)
if prompt == 'auth':
    print('Unauthorized', file=sys.stderr)
    sys.exit(1)
if prompt == 'timeout':
    time.sleep(10)
if prompt == 'overflow':
    print('x' * 900000)
assert args[:2] == ['exec', '--ignore-user-config']
assert args[args.index('--sandbox') + 1] == 'read-only'
assert args[-1] == '-'
thread = """ + repr(self.thread) + """
if 'resume' in args and prompt != 'mismatch':
    assert args[-2] == thread
if prompt == 'mismatch':
    thread = 'c7065185-e44e-41bf-a813-c30b33b2fed7'
answer = {'answer': prompt}
Path(args[args.index('-o')+1]).write_text(json.dumps(answer if prompt != 'invalid' else {}))
print('failed to load models cache: missing field supports_parallel_tool_calls', file=sys.stderr)
print(json.dumps({'type': 'thread.started', 'thread_id': thread}))
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(answer)}}))
print(json.dumps({'type': 'turn.completed', 'usage': (
    {'input_tokens': 8, 'cached_input_tokens': 0, 'internal': 12} if prompt == 'resume' else None)}))
""")
        self.binary.chmod(0o700)

    def call(self, prompt, previous=None, timeout=2):
        return Adapter(self.binary, timeout)({"prompt": prompt, "schema": SCHEMA}, self.root,
                                            self.root / "codex-home", previous, Event())

    def test_poc_format_environment_and_explicit_resume(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test", "GH_TOKEN": "test"}):
            self.assertEqual(self.call("create"), (self.thread, {"answer": "create"}, None))
            self.assertEqual(self.call("resume", self.thread), (self.thread, {"answer": "resume"}, {"input_tokens": 8, "cached_input_tokens": 0}))

    def test_classifies_failures_and_bounds_output(self):
        for prompt, error in (("quota", "quota"), ("auth", "auth"), ("invalid", "invalid_output"),
                              ("overflow", "invalid_output"), ("mismatch", "wrong_session")):
            with self.subTest(prompt=prompt), self.assertRaisesRegex(CliError, error):
                self.call(prompt, self.thread)
        with self.assertRaisesRegex(CliError, "timeout"):
            self.call("timeout", timeout=.35)
        self.binary.unlink()
        with self.assertRaisesRegex(CliError, "unavailable"):
            self.call("create")
