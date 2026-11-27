"""Worker-local session bindings and an outbox for undelivered results."""

import json
import os
from pathlib import Path
import re
import subprocess
from threading import Event, Thread
import time

from .cli import CliError, validate
from .queue import Conflict, encode, transaction
from .spike import ROUTES

HEX = re.compile(r"[a-f0-9]{64}")


class Worker:
    def __init__(self, client, root, worker, account, version, adapter, *, mode="fixture"):
        self.client, self.root = client, Path(root).resolve()
        self.worker, self.account, self.version = worker, account, version
        self.adapter, self.mode = adapter, mode
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / "worker.sqlite"
        with transaction(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS identity (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), worker TEXT, account TEXT)""")
            db.execute("INSERT OR IGNORE INTO identity VALUES (1,?,?)", (worker, account))
            if tuple(db.execute("SELECT worker,account FROM identity").fetchone()) != (worker, account):
                raise Conflict("worker_state_identity_mismatch")
            db.execute("""CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY, run TEXT NOT NULL, role TEXT NOT NULL, attempt TEXT NOT NULL,
                status TEXT NOT NULL, thread TEXT, outcome TEXT,
                delivered INTEGER NOT NULL DEFAULT 0)""")

    def request(self, action, **payload):
        return self.client(action, {"worker": self.worker, "account": self.account, **payload})

    def deliver(self, row):
        try:
            self.request("complete", job=row["id"], attempt=row["attempt"], **json.loads(row["outcome"]))
        except Conflict as error:
            if str(error) not in {"lease_expired", "unknown_lease"}:
                raise
        with transaction(self.path) as db:
            db.execute("UPDATE turns SET delivered=1 WHERE id=?", (row["id"],))

    def once(self):
        self.request("register", version=self.version)
        with transaction(self.path) as db:
            outbox = db.execute("SELECT * FROM turns WHERE outcome IS NOT NULL AND delivered=0").fetchall()
        for row in outbox:
            self.deliver(row)
        job = self.request("claim")
        if job is None:
            return False
        try:
            self.execute(job)
        except CliError as error:
            # A rejected envelope has never entered the local session store.
            self.request("complete", job=job["id"], attempt=job["attempt"], error=str(error))
        return True

    def execute(self, job):
        if (ROUTES.get(job["role"]) != self.worker or job["worker"] != self.worker
                or job["account"] != self.account or job["mode"] != self.mode
                or not all(HEX.fullmatch(job[key]) for key in ("id", "run"))):
            raise CliError("invalid_job")
        job_id, attempt, role, run = (job[key] for key in ("id", "attempt", "role", "run"))
        with transaction(self.path) as db:
            row = db.execute("SELECT * FROM turns WHERE id=?", (job_id,)).fetchone()
            if row:
                if row["attempt"] != attempt or row["outcome"] is None:
                    raise CliError("uncertain")
            else:
                previous = None
                if job["parent"]:
                    parent = db.execute("SELECT thread FROM turns WHERE id=? AND run=? AND role=? AND status='ok'",
                                        (job["parent"], run, role)).fetchone()
                    if not parent:
                        raise CliError("wrong_session")
                    previous = parent["thread"]
                db.execute("INSERT INTO turns(id,run,role,attempt,status) VALUES(?,?,?,?,'running')",
                           (job_id, run, role, attempt))
        if row:
            self.deliver(row)
            return
        cancelled, stop = Event(), Event()
        lease = {"job": job_id, "attempt": attempt}

        def renew():
            while not stop.wait(job["heartbeat"]):
                try:
                    self.request("heartbeat", **lease)
                except (Conflict, OSError):
                    cancelled.set()
                    return

        monitor = Thread(target=renew, daemon=True)
        monitor.start()
        thread = None
        try:
            self.request("heartbeat", **lease)
            directory = self.root / "work" / role / run
            codex_home = self.root / "codex" / role
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
            env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            subprocess.run(["git", "init", "--quiet", "--template="], cwd=directory, env=env,
                           check=True, capture_output=True, timeout=5)
            thread, answer = self.adapter(job, directory, codex_home, previous, cancelled)
            validate(answer, job["schema"])
            outcome = {"answer": answer}
        except CliError as error:
            outcome = {"error": str(error)}
        except Conflict:
            outcome = {"error": "uncertain"}
        except (OSError, subprocess.SubprocessError):
            outcome = {"error": "unavailable"}
        finally:
            stop.set()
            monitor.join()
        if cancelled.is_set():
            outcome = {"error": "uncertain"}
        with transaction(self.path) as db:
            db.execute("UPDATE turns SET status=?,thread=?,outcome=? WHERE id=?",
                       ("ok" if "answer" in outcome else "needs_review", thread, encode(outcome), job_id))
            row = db.execute("SELECT * FROM turns WHERE id=?", (job_id,)).fetchone()
        self.deliver(row)

    def loop(self):
        while True:
            try:
                if self.once():
                    continue
            except OSError:
                pass
            time.sleep(3)
