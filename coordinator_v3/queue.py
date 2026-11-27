"""Durable worker registration, fenced leases and audit events."""

from contextlib import contextmanager
import json
import sqlite3
import time
from uuid import uuid4

from coordinator.context import digest
from .spike import ROUTES

ERRORS = {"auth", "quota", "timeout", "unavailable", "cli_error",
          "invalid_output", "wrong_session", "uncertain", "invalid_job"}
PAUSED = {"auth", "quota", "unavailable", "uncertain"}
MAX_BODY = 128 * 1024


class Conflict(Exception):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


@contextmanager
def transaction(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        with db:
            yield db
    finally:
        db.close()


class Queue:
    def __init__(self, path, *, lease=45, presence=90, clock=time.time):
        self.path, self.lease, self.presence, self.clock = path, lease, presence, clock
        with sqlite3.connect(path, timeout=10) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY, account TEXT UNIQUE NOT NULL,
                    version TEXT NOT NULL, seen REAL NOT NULL, pause TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    result TEXT, published TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id), role TEXT NOT NULL,
                    worker TEXT NOT NULL, round INTEGER NOT NULL, payload TEXT NOT NULL,
                    parent TEXT REFERENCES jobs(id), status TEXT NOT NULL DEFAULT 'queued',
                    deadline REAL, attempt TEXT UNIQUE, result TEXT, error TEXT,
                    UNIQUE(run, role, round)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_job_per_worker
                    ON jobs(worker) WHERE status='leased';
                CREATE TABLE IF NOT EXISTS checks (
                    run TEXT NOT NULL REFERENCES runs(id), sha TEXT NOT NULL, name TEXT NOT NULL,
                    evidence TEXT NOT NULL, PRIMARY KEY(run, sha, name)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, time REAL NOT NULL, kind TEXT NOT NULL,
                    run TEXT, job TEXT, worker TEXT, status TEXT
                );
            """)

    def event(self, db, kind, *, run=None, job=None, worker=None, status=None):
        db.execute("INSERT INTO events(time,kind,run,job,worker,status) VALUES(?,?,?,?,?,?)",
                   (self.clock(), kind, run, job, worker, status))

    def register(self, worker, account, version):
        if worker not in ROUTES.values() or not account or not version:
            raise Conflict("invalid_worker")
        with transaction(self.path) as db:
            try:
                db.execute("""INSERT INTO workers(id,account,version,seen) VALUES(?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET version=excluded.version, seen=excluded.seen
                    WHERE workers.account=excluded.account""", (worker, account, version, self.clock()))
            except sqlite3.IntegrityError:
                raise Conflict("account_already_registered") from None
            self.touch(db, worker, account)

    def touch(self, db, worker, account):
        if db.execute("UPDATE workers SET seen=? WHERE id=? AND account=?",
                      (self.clock(), worker, account)).rowcount != 1:
            raise Conflict("worker_identity_mismatch")

    def unpause(self, worker):
        with transaction(self.path) as db:
            if db.execute("UPDATE workers SET pause=NULL WHERE id=?", (worker,)).rowcount != 1:
                raise Conflict("unknown_worker")
            self.event(db, "worker.resumed", worker=worker)

    def submit(self, spec):
        key = digest({name: spec[name] for name in ("repo", "issue", "actor", "request")})
        packed = encode(spec)
        with transaction(self.path) as db:
            row = db.execute("SELECT spec FROM runs WHERE id=?", (key,)).fetchone()
            if row:
                if row["spec"] != packed:
                    raise Conflict("request_changed")
                return key
            db.execute("INSERT INTO runs(id,spec) VALUES(?,?)", (key, packed))
            self.event(db, "run.submitted", run=key)
        return key

    def step(self, run, role, round_no, payload):
        worker = ROUTES[role]
        key = digest([run, role, round_no])
        packed = encode(payload)
        if len(packed.encode()) > 96 * 1024:
            raise Conflict("job_too_large")
        with transaction(self.path) as db:
            self.expire(db)
            row = db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
            if row:
                if row["payload"] != packed:
                    raise Conflict("step_context_changed")
                return dict(row)
            active = db.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()
            if not active or active["status"] != "pending":
                raise Conflict("run_closed")
            parent = None
            if round_no > 1:
                previous = db.execute("SELECT id FROM jobs WHERE run=? AND role=? AND round=? AND status='ok'",
                                      (run, role, round_no - 1)).fetchone()
                # Deterministic checks can fail before the tester's first turn.
                if previous:
                    parent = previous["id"]
                elif role != "tester":
                    raise Conflict("no_matching_session")
            db.execute("INSERT INTO jobs(id,run,role,worker,round,payload,parent) VALUES(?,?,?,?,?,?,?)",
                       (key, run, role, worker, round_no, packed, parent))
            self.event(db, "job.queued", run=run, job=key, worker=worker)
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone())

    def expire(self, db):
        for row in db.execute("SELECT * FROM jobs WHERE status='leased' AND deadline<=?",
                              (self.clock(),)).fetchall():
            db.execute("UPDATE jobs SET status='needs_review',error='uncertain' WHERE id=?", (row["id"],))
            db.execute("UPDATE workers SET pause=coalesce(pause,'uncertain') WHERE id=?", (row["worker"],))
            self.event(db, "job.expired", run=row["run"], job=row["id"], worker=row["worker"],
                       status="uncertain")

    def claim(self, worker, account):
        with transaction(self.path) as db:
            self.touch(db, worker, account)
            self.expire(db)
            state = db.execute("SELECT pause FROM workers WHERE id=?", (worker,)).fetchone()
            busy = db.execute("SELECT 1 FROM jobs WHERE worker=? AND status='leased'", (worker,)).fetchone()
            if state["pause"] or busy:
                return None
            row = db.execute("""SELECT j.* FROM jobs j JOIN runs r ON j.run=r.id
                WHERE j.worker=? AND j.status='queued' AND r.status='pending' ORDER BY j.rowid LIMIT 1""",
                             (worker,)).fetchone()
            if not row:
                return None
            attempt = uuid4().hex
            db.execute("UPDATE jobs SET status='leased',deadline=?,attempt=? WHERE id=?",
                       (self.clock() + self.lease, attempt, row["id"]))
            self.event(db, "job.leased", run=row["run"], job=row["id"], worker=worker)
            return {**json.loads(row["payload"]), **{k: row[k] for k in ("id", "run", "role", "parent")},
                    "worker": worker, "account": account, "attempt": attempt, "heartbeat": self.lease / 3}

    def owned(self, db, worker, job, attempt):
        row = db.execute("SELECT * FROM jobs WHERE id=? AND worker=? AND attempt=?",
                         (job, worker, attempt)).fetchone()
        if not row:
            raise Conflict("unknown_lease")
        return row

    def heartbeat(self, worker, account, job, attempt):
        with transaction(self.path) as db:
            self.touch(db, worker, account)
            row = self.owned(db, worker, job, attempt)
            if row["status"] != "leased" or row["deadline"] <= self.clock():
                raise Conflict("lease_expired")
            db.execute("UPDATE jobs SET deadline=? WHERE id=?", (self.clock() + self.lease, job))

    def complete(self, worker, account, job, attempt, *, answer=None, error=None):
        if (answer is None) == (error is None) or (error is not None and error not in ERRORS):
            raise Conflict("invalid_result")
        packed = encode(answer) if answer is not None else None
        status = "ok" if error is None else "needs_review"
        with transaction(self.path) as db:
            self.touch(db, worker, account)
            row = self.owned(db, worker, job, attempt)
            if (row["status"], row["result"], row["error"]) == (status, packed, error):
                return
            if row["status"] != "leased" or row["deadline"] <= self.clock():
                raise Conflict("lease_expired")
            db.execute("UPDATE jobs SET status=?,result=?,error=? WHERE id=?", (status, packed, error, job))
            if error in PAUSED:
                db.execute("UPDATE workers SET pause=? WHERE id=?", (error, worker))
            self.event(db, "job.finished", run=row["run"], job=job, worker=worker, status=error or status)

    def status(self):
        with transaction(self.path) as db:
            now = self.clock()
            self.expire(db)
            workers = {}
            for row in db.execute("SELECT id,seen,pause,version FROM workers"):
                busy = db.execute("SELECT 1 FROM jobs WHERE worker=? AND status='leased'", (row["id"],)).fetchone()
                workers[row["id"]] = {"version": row["version"], "status": row["pause"] or
                    ("offline" if now - row["seen"] > self.presence else "busy" if busy else "ready")}
            for worker in set(ROUTES.values()) - workers.keys():
                workers[worker] = {"status": "offline"}
            return {"workers": workers, "runs": [dict(row) for row in db.execute(
                "SELECT id,status,published FROM runs")], "jobs": [dict(row) for row in db.execute(
                "SELECT id,run,role,status,error FROM jobs")]}
