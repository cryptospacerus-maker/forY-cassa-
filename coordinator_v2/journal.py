"""Versioned, append-only events for a future panel."""

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4

from coordinator.context import Blocked


class Journal:
    def __init__(self, output, metadata=None, *, run_id=None, resume=False):
        self.path = Path(output)
        self.path.mkdir(parents=True, exist_ok=resume)
        self.events = self.path / "events.jsonl"
        self.run_id = run_id or uuid4().hex
        self.sequence = 0
        self.lock = Lock()
        self.metadata = dict(metadata or {})
        if resume and self.events.exists():
            self.restore_sequence()

    def restore_sequence(self):
        if self.events.stat().st_size > 512 * 1024:
            raise Blocked("Превышен размер журнала координатора.")
        try:
            for number, line in enumerate(self.events.read_text().splitlines(), 1):
                event = json.loads(line)
                if (event["schema_version"], event["sequence"], event["run_id"]) != (2, number, self.run_id):
                    raise ValueError("invalid journal identity")
                self.sequence = number
        except (KeyError, TypeError, ValueError) as error:
            raise Blocked("Сохранённый журнал относится к другому запуску или повреждён.") from error

    def emit(self, event, **fields):
        with self.lock:
            self.sequence += 1
            data = {
                "schema_version": 2, "sequence": self.sequence, "run_id": self.run_id,
                "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "event": event, **self.metadata, **fields,
            }
            with self.events.open("a", encoding="utf-8") as output:
                output.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")

    def summarize(self, status, **fields):
        result = {"schema_version": 2, "run_id": self.run_id, "status": status, **self.metadata, **fields}
        temporary = self.path / "summary.tmp"
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.path / "summary.json")
        return result

    def finish(self, status, *, event="run.finished", **fields):
        self.emit(event, status=status, **fields)
        return self.summarize(status, **fields)
