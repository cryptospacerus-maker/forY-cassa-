"""Durable worker registration, fenced leases and audit events."""

from contextlib import closing, contextmanager
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
ADMISSION_TTL = 30
MAX_ACTIVE_RUNS = 8


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
        with closing(sqlite3.connect(path, timeout=10)) as db:
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
                CREATE TABLE IF NOT EXISTS admission (
                    run TEXT PRIMARY KEY REFERENCES runs(id), checked REAL NOT NULL, until REAL NOT NULL,
                    snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    job TEXT PRIMARY KEY REFERENCES jobs(id), resolution TEXT NOT NULL, time REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage (
                    job TEXT PRIMARY KEY REFERENCES jobs(id), sample TEXT NOT NULL
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
            unresolved = db.execute("""SELECT 1 FROM jobs j LEFT JOIN reviews r ON r.job=j.id
                WHERE j.worker=? AND j.status='needs_review' AND j.error='uncertain'
                AND (r.resolution IS NULL OR r.resolution='outcome_unknown')""", (worker,)).fetchone()
            if unresolved:
                raise Conflict("unresolved_execution")
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
            if db.execute("SELECT count(*) FROM runs WHERE status='pending'").fetchone()[0] >= MAX_ACTIVE_RUNS:
                raise Conflict("active_run_limit")
            db.execute("INSERT INTO runs(id,spec) VALUES(?,?)", (key, packed))
            self.event(db, "run.submitted", run=key)
        return key

    def revoke(self, run=None):
        with transaction(self.path) as db:
            if run is None:
                db.execute("DELETE FROM admission")
            else:
                db.execute("DELETE FROM admission WHERE run=?", (run,))

    def admit(self, run, snapshot_hash, checked):
        with transaction(self.path) as db:
            row = db.execute("SELECT spec,status FROM runs WHERE id=?", (run,)).fetchone()
            if not row or row["status"] != "pending":
                raise Conflict("run_closed")
            if snapshot_hash != digest(json.loads(row["spec"])["snapshot"]):
                raise Conflict("requirements_changed")
            if not checked <= self.clock() < checked + ADMISSION_TTL:
                raise Conflict("verification_expired")
            db.execute("INSERT OR REPLACE INTO admission VALUES(?,?,?,?)",
                       (run, checked, checked + ADMISSION_TTL, snapshot_hash))

    def finish(self, run, result):
        with transaction(self.path) as db:
            if db.execute("UPDATE runs SET status=?,result=? WHERE id=? AND status='pending'",
                          (result["status"], encode(result), run)).rowcount != 1:
                raise Conflict("run_closed")
            db.execute("DELETE FROM admission WHERE run=?", (run,))
            db.execute("""UPDATE workers SET pause=coalesce(pause,'uncertain')
                WHERE id IN (SELECT worker FROM jobs WHERE run=? AND status='leased')""", (run,))
            db.execute("UPDATE jobs SET status='cancelled' WHERE run=? AND status='queued'", (run,))
            db.execute("""UPDATE jobs SET status='needs_review',error='uncertain'
                WHERE run=? AND status='leased'""", (run,))
            self.event(db, "run.finished", run=run, status=result["status"])

    def review(self, job, resolution):
        with transaction(self.path) as db:
            self.expire(db)
            row = db.execute("SELECT run,status,worker FROM jobs WHERE id=?", (job,)).fetchone()
            if not row or row["status"] != "needs_review":
                raise Conflict("job_not_in_review")
            if not db.execute("""INSERT INTO reviews VALUES(?,?,?)
                    ON CONFLICT(job) DO UPDATE SET resolution=excluded.resolution,time=excluded.time
                    WHERE reviews.resolution='outcome_unknown' AND excluded.resolution!='outcome_unknown'""",
                              (job, resolution, self.clock())).rowcount:
                raise Conflict("review_already_recorded")
            self.event(db, "job.reviewed", run=row["run"], job=job, worker=row["worker"], status=resolution)

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
                sample = db.execute("SELECT sample FROM usage WHERE job=?", (key,)).fetchone()
                return {**dict(row), "usage": json.loads(sample["sample"]) if sample else None}
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
            now = self.clock()
            row = db.execute("""SELECT j.* FROM jobs j JOIN runs r ON j.run=r.id
                LEFT JOIN admission a ON a.run=r.id
                WHERE j.worker=? AND j.status='queued' AND r.status='pending'
                AND (json_extract(r.spec,'$.mode')='fixture' OR (a.checked<=? AND a.until>?))
                ORDER BY j.rowid LIMIT 1""", (worker, now, now)).fetchone()
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

    def complete(self, worker, account, job, attempt, *, answer=None, error=None, usage=None):
        from .cli import usage_sample

        sample = usage_sample(usage)
        if sample != usage or (error is not None and sample is not None):
            raise Conflict("invalid_usage")
        if (answer is None) == (error is None) or (error is not None and error not in ERRORS):
            raise Conflict("invalid_result")
        packed = encode(answer) if answer is not None else None
        status = "ok" if error is None else "needs_review"
        with transaction(self.path) as db:
            self.touch(db, worker, account)
            row = self.owned(db, worker, job, attempt)
            if (row["status"], row["result"], row["error"]) == (status, packed, error):
                saved = db.execute("SELECT sample FROM usage WHERE job=?", (job,)).fetchone()
                if (json.loads(saved["sample"]) if saved else None) != sample:
                    raise Conflict("result_changed")
                return
            if row["status"] != "leased" or row["deadline"] <= self.clock():
                raise Conflict("lease_expired")
            if sample is not None:
                db.execute("INSERT INTO usage VALUES(?,?)", (job, encode(sample)))
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
                """SELECT r.id,r.status,r.published,a.until AS admission_until,
                    CASE WHEN r.status!='pending' THEN 'closed'
                    WHEN json_extract(r.spec,'$.mode')='fixture' THEN 'fixture'
                    WHEN a.checked<=? AND a.until>? THEN 'ready' ELSE 'awaiting_github' END AS dispatch
                    FROM runs r LEFT JOIN admission a ON a.run=r.id""", (now, now))],
                "jobs": [dict(row) for row in db.execute("""SELECT j.id,j.run,j.role,j.status,j.error,
                    r.resolution FROM jobs j LEFT JOIN reviews r ON r.job=j.id""")]}
