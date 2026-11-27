"""Offline transport spike. A test injects a CLI double; no live worker is wired."""
from contextlib import closing
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


ROUTES = {"developer": "tolya", "interface": "semen",
          "analyst": "third", "tester": "third"}


@dataclass(frozen=True)
class Work:
    task: str
    role: str
    worker: str
    revision: str
    step: str


class Rejected(Exception):
    pass


class Spike:
    def __init__(self, database, invoke):
        self.database = database
        self.invoke = invoke
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS steps (
                    task TEXT, role TEXT, worker TEXT, revision TEXT, step TEXT,
                    status TEXT NOT NULL, thread TEXT,
                    PRIMARY KEY (task, role, worker, revision, step)
                )
            """)

    def run(self, work, directory, prompt, *, continue_step=None, timeout=5):
        if ROUTES.get(work.role) != work.worker or work.role not in ROUTES:
            raise Rejected("wrong_worker")
        scope = (work.task, work.role, work.worker, work.revision)
        key = (*scope, work.step)
        with closing(sqlite3.connect(self.database)) as db, db:
            previous = None
            if continue_step is not None:
                row = db.execute("""
                    SELECT thread FROM steps WHERE task=? AND role=? AND worker=?
                    AND revision=? AND step=? AND status='ok'
                """, (*scope, continue_step)).fetchone()
                if not row:
                    raise Rejected("no_matching_session")
                previous = row[0]
            claim = db.execute(
                "INSERT OR IGNORE INTO steps VALUES (?, ?, ?, ?, ?, 'running', NULL)", key
            )
            if claim.rowcount != 1:
                raise Rejected("duplicate_step")
        try:
            result = self.invoke(Path(directory), prompt, previous, timeout)
            if result.returncode != 0:
                # These exit codes belong to the double. Real CLI mapping is unverified.
                raise Rejected({10: "auth", 11: "quota"}.get(result.returncode, "cli_error"))
            thread, answer = read_events(result.stdout)
            if previous is not None and thread != previous:
                raise Rejected("wrong_session")
        except (Rejected, OSError, subprocess.TimeoutExpired, ValueError, TypeError) as error:
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("""
                    UPDATE steps SET status='needs_review'
                    WHERE task=? AND role=? AND worker=? AND revision=? AND step=?
                """, key)
            if isinstance(error, Rejected):
                raise
            if isinstance(error, subprocess.TimeoutExpired):
                raise Rejected("timeout") from None
            if isinstance(error, OSError):
                raise Rejected("unavailable") from None
            raise Rejected("invalid_output") from None
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""
                UPDATE steps SET status='ok', thread=?
                WHERE task=? AND role=? AND worker=? AND revision=? AND step=?
            """, (thread, *key))
        return thread, answer


def read_events(stdout):
    events = [json.loads(line) for line in stdout.splitlines()]
    if not all(isinstance(event, dict) for event in events):
        raise ValueError("event")
    identifiers = [event.get("thread_id") for event in events if event.get("type") == "thread.started"]
    completed = [event for event in events if event.get("type") == "turn.completed"]
    if len(identifiers) != 1 or len(completed) != 1 or events[-1] != completed[0]:
        raise ValueError("turn")
    if any(event.get("type") in {"error", "turn.failed"} for event in events):
        raise ValueError("failed turn")
    thread = str(UUID(str(identifiers[0])))
    messages = [event.get("item") for event in events if event.get("type") == "item.completed"]
    if any(not isinstance(item, dict) for item in messages):
        raise ValueError("item")
    if any(item.get("type") not in {"agent_message", "reasoning"} for item in messages):
        raise ValueError("side effect")
    replies = [item.get("text") for item in messages if item.get("type") == "agent_message"]
    if not replies:
        raise ValueError("answer")
    value = json.loads(replies[-1])
    if not isinstance(value, dict) or set(value) != {"answer"} or not isinstance(value["answer"], str):
        raise ValueError("schema")
    return thread, value
