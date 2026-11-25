"""Versioned, append-only events for a future panel."""

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4


class Journal:
    def __init__(self, output):
        self.path = Path(output)
        self.path.mkdir(parents=True, exist_ok=False)
        self.events = self.path / "events.jsonl"
        self.run_id = uuid4().hex
        self.sequence = 0
        self.lock = Lock()

    def emit(self, event, **fields):
        with self.lock:
            self.sequence += 1
            data = {
                "schema_version": 2, "sequence": self.sequence, "run_id": self.run_id,
                "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "event": event, **fields,
            }
            with self.events.open("a", encoding="utf-8") as output:
                output.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")

    def finish(self, status, **fields):
        result = {"schema_version": 2, "run_id": self.run_id, "status": status, **fields}
        self.emit("run.finished", **result)
        (self.path / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
