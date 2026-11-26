import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock

from coordinator_v3.spike import Rejected, Spike, Work


def fixture(thread, answer="4"):
    return subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, [
        {"type": "thread.started", "thread_id": thread},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps({"answer": answer})
        }},
        {"type": "turn.completed"},
    ])))


class SpikeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "steps.sqlite"
        self.invoke = Mock()
        self.spike = Spike(self.db, self.invoke)
        self.work = Work("repo#19", "analyst", "third", "v1", "plan")
        self.thread = str(uuid4())
        self.invoke.return_value = fixture(self.thread)

    def test_result_and_cwd(self):
        self.assertEqual(self.spike.run(self.work, self.root, "2+2"), (self.thread, {"answer": "4"}))
        self.invoke.assert_called_once_with(self.root, "2+2", None, 5)

    def test_resume_persisted_after_restart(self):
        self.spike.run(self.work, self.root, "2+2")
        follow_up = Work("repo#19", "analyst", "third", "v1", "repair")
        Spike(self.db, self.invoke).run(follow_up, self.root, "continue", continue_step="plan")
        self.invoke.assert_called_with(self.root, "continue", self.thread, 5)

    def test_separate_roles_and_versions(self):
        self.spike.run(self.work, self.root, "plan")
        for candidate in [Work("repo#19", "tester", "third", "v1", "qa"),
                          Work("repo#19", "analyst", "third", "v2", "repair")]:
            with self.assertRaisesRegex(Rejected, "no_matching_session"):
                self.spike.run(candidate, self.root, "continue", continue_step="plan")
        self.assertEqual(self.invoke.call_count, 1)

    def test_duplicate_not_sent_after_restart(self):
        self.spike.run(self.work, self.root, "2+2")
        with self.assertRaisesRegex(Rejected, "duplicate_step"):
            Spike(self.db, self.invoke).run(self.work, self.root, "2+2")
        self.assertEqual(self.invoke.call_count, 1)

    def test_wrong_worker(self):
        with self.assertRaisesRegex(Rejected, "wrong_worker"):
            self.spike.run(Work("repo#19", "tester", "tolya", "v1", "qa"), self.root, "qa")
        self.invoke.assert_not_called()

    def test_timeout_no_automatic_retry(self):
        self.invoke.side_effect = subprocess.TimeoutExpired("fixture", 1)
        with self.assertRaisesRegex(Rejected, "timeout"):
            self.spike.run(self.work, self.root, "2+2", timeout=1)
        with self.assertRaisesRegex(Rejected, "duplicate_step"):
            self.spike.run(self.work, self.root, "2+2")

    def test_unavailable(self):
        self.invoke.side_effect = FileNotFoundError()
        with self.assertRaisesRegex(Rejected, "unavailable"):
            self.spike.run(self.work, self.root, "2+2")

    def test_auth_and_quota(self):
        for code, outcome in [(10, "auth"), (11, "quota")]:
            self.invoke.return_value = subprocess.CompletedProcess([], code, "")
            work = Work("repo#19", "analyst", "third", "v1", str(code))
            with self.assertRaisesRegex(Rejected, outcome):
                self.spike.run(work, self.root, "2+2")

    def test_invalid_output(self):
        self.invoke.return_value = subprocess.CompletedProcess([], 0, '{"type":"turn.completed"}')
        with self.assertRaisesRegex(Rejected, "invalid_output"):
            self.spike.run(self.work, self.root, "2+2")

    def test_resume_cannot_change_thread(self):
        self.spike.run(self.work, self.root, "plan")
        self.invoke.return_value = fixture(str(uuid4()))
        follow_up = Work("repo#19", "analyst", "third", "v1", "repair")
        with self.assertRaisesRegex(Rejected, "wrong_session"):
            self.spike.run(follow_up, self.root, "continue", continue_step="plan")
