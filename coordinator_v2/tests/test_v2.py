import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

from coordinator.context import Blocked, git
from coordinator_v2 import demo_fixture
from coordinator_v2.engine import Engine, route
from coordinator_v2.journal import Journal
from coordinator_v2.mcp import Gateway, TOOL
from coordinator_v2.runtime import Check, DockerSandbox, Evidence, Project, materialize


class MCPTest(unittest.TestCase):
    def test_real_stdio_protocol_and_cache(self):
        gateway = Gateway()
        reply, cached = gateway.call("developer", TOOL, {"category": "office"})
        self.assertEqual(reply, {"category": "office", "items": ["pencil", "paper"]})
        self.assertFalse(cached)
        self.assertEqual(gateway.call("interface", TOOL, {"category": "office"}), (reply, True))
        self.assertEqual((gateway.requests, gateway.hits), (1, 1))

    def test_role_validation_precedes_cached_result_and_transport(self):
        gateway = Gateway()
        gateway.call("developer", TOOL, {"category": "kitchen"})
        for role, name, arguments in [
            ("interface", TOOL, {"category": "kitchen"}),
            ("analyst", TOOL, {"category": "office"}),
            ("developer", "inventory_write", {"category": "office"}),
            ("developer", TOOL, {"category": "office", "url": "https://example.test"}),
            ("developer", TOOL, {"category": ["office"]}),
        ]:
            with self.subTest(role=role, name=name, arguments=arguments), self.assertRaises(Blocked):
                gateway.call(role, name, arguments)
        self.assertEqual(gateway.requests, 1)


class RunTest(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.journal = Journal(Path(work.name) / "results")
        self.base = git("rev-parse", "HEAD").decode().strip()
        self.project = Project.load(self.base, "lab")

    def run_fixture(self, agent, features, sandbox=None, scenario="parallel", fresh=None):
        engine = Engine(self.base, self.project, features, sandbox, agent, self.journal)
        result = engine.run(demo_fixture.SNAPSHOT, fresh or (lambda: demo_fixture.SNAPSHOT),
                            demo_fixture.plan(scenario))
        events = [json.loads(line) for line in self.journal.events.read_text().splitlines()]
        self.assertEqual([item["sequence"] for item in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(item["schema_version"] == 2 for item in events))
        return result, events

    def test_parallel_waits_for_both_and_deduplicates_mcp_reads(self):
        barrier = Barrier(2)

        def agent(role, instruction, payload, contract):
            if role in {"developer", "interface"} and not payload["feedback"]:
                barrier.wait(timeout=8)
            if role == "developer":
                payload = dict(payload, feedback=["fixture fixes immediately"])
            return demo_fixture.model(role, instruction, payload, contract)

        result, events = self.run_fixture(agent, {"mcp"})
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["model_sessions"], 3)
        self.assertEqual((result["mcp_requests"], result["mcp_cache_hits"]), (1, 1))
        self.assertTrue((self.journal.path / "candidate.pack").exists())
        self.assertNotIn("pencil", self.journal.events.read_text())
        self.assertEqual([item["role"] for item in events if item["event"] == "task.handoff"],
                         ["developer", "interface"])

    def test_dependency_contains_previous_result_and_constrained_excerpt(self):
        def agent(role, instruction, payload, contract):
            if role == "interface":
                previous = payload["dependencies"]["developer"]
                self.assertEqual(previous["files"], ["coordinator_v2/demo/app/store.py"])
                self.assertIn("def select", next(iter(previous["excerpts"].values())))
            if role == "developer":
                payload = dict(payload, feedback=["done"])
            return demo_fixture.model(role, instruction, payload, contract)

        result, events = self.run_fixture(agent, set(), scenario="dependency")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(next(item["waves"] for item in events if item["event"] == "plan.accepted"),
                         [["developer"], ["interface"]])

    def test_stale_requirements_stop_qa_and_artifact(self):
        counter = 0

        def fresh():
            nonlocal counter
            counter += 1
            return demo_fixture.SNAPSHOT if counter < 3 else dict(demo_fixture.SNAPSHOT, body="Changed")

        result, events = self.run_fixture(demo_fixture.model, set(), fresh=fresh)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("requirements.changed", {item["event"] for item in events})
        self.assertNotIn("qa.started", {item["event"] for item in events})
        self.assertFalse((self.journal.path / "candidate.pack").exists())

    def test_infrastructure_failure_stops_without_qa_approval(self):
        def fail(check, sha):
            return Evidence(check.name, sha, "timeout", None, 0, hashlib.sha256(b"").hexdigest())
        result, events = self.run_fixture(demo_fixture.model, {"tests"}, Mock(run=fail))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("timeout", [i["evidence"]["status"] for i in events if i["event"] == "check.finished"])
        self.assertNotIn("qa.finished", {item["event"] for item in events})

    def test_untrusted_worker_cannot_request_qa_or_shell(self):
        sandbox = Mock()

        def agent(role, instruction, payload, contract):
            answer = demo_fixture.model(role, instruction, payload, contract)
            answer["answer"]["check_ids"] = ["qa-unit" if role == "developer" else "sh"]
            return answer

        result, _ = self.run_fixture(agent, {"execution", "tests"}, sandbox)
        self.assertEqual(result["status"], "blocked")
        sandbox.run.assert_not_called()

    def test_overlapping_paths_cycle_and_unsafe_output_are_rejected(self):
        plan = demo_fixture.plan("dependency")
        plan["tasks"][0]["depends_on"] = ["interface"]
        for task in plan["tasks"]:
            task["mcp_category"] = ""
        with self.assertRaises(Blocked):
            route(plan, self.project, set())
        plan["tasks"][0]["depends_on"] = []
        plan["tasks"][1]["paths"] = plan["tasks"][0]["paths"]
        with self.assertRaises(Blocked):
            route(plan, self.project, set())
        self.assertFalse(self.project.allows("coordinator_v2/demo/app/../harness/browser.cjs"))
        self.assertFalse(self.project.allows(".github/workflows/coordinator-v2.yml"))

    def test_reject_symlinks_before_materializing_snapshot(self):
        with TemporaryDirectory() as work, patch("coordinator_v2.runtime.git") as fake:
            fake.return_value = b"120000 blob " + b"a" * 40 + b"\tcase/link.txt\0"
            with self.assertRaises(Blocked):
                materialize(self.base, "case", Path(work) / "case")


@unittest.skipUnless(os.environ.get("COORDINATOR_V2_TEST_IMAGE"), "Docker image not requested")
class ActualDockerTest(unittest.TestCase):
    def setUp(self):
        self.base = git("rev-parse", "HEAD").decode().strip()
        self.project = Project.load(self.base, "lab")
        self.sandbox = DockerSandbox(os.environ["COORDINATOR_V2_TEST_IMAGE"], self.project, self.base)

    def test_untrusted_process_timeout_is_bounded_and_container_is_removed(self):
        check = Check("timeout-probe", "execution", ("developer",),
                      ("python3", "-B", "-c", "import time; time.sleep(20)"), 3)
        evidence = self.sandbox.run(check, self.base)
        self.assertEqual(evidence.status, "timeout")
        active = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=coordinator-v2-", "--format", "{{.Names}}"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(active.stdout.strip(), "")

    def test_real_checks_reject_bad_sha_then_accept_repaired_sha(self):
        with TemporaryDirectory() as work:
            journal = Journal(Path(work) / "integration")
            engine = Engine(self.base, self.project, {"execution", "tests", "browser", "mcp"},
                            self.sandbox, demo_fixture.model, journal)
            summary = engine.run(demo_fixture.SNAPSHOT, lambda: demo_fixture.SNAPSHOT,
                                 demo_fixture.plan("parallel"))
            rows = [json.loads(line) for line in journal.events.read_text().splitlines()]
            self.assertEqual(summary["status"], "passed", (summary, rows))
            evidence = [row["evidence"] for row in rows if row["event"] == "check.finished"
                        and row["role"] == "tester"]
            self.assertEqual([(item["check"], item["status"]) for item in evidence],
                             [("qa-unit", "fail"), ("qa-browser", "pass"),
                              ("qa-unit", "pass"), ("qa-browser", "pass")])
            self.assertEqual(evidence[0]["sha"], evidence[1]["sha"])
            self.assertEqual(evidence[2]["sha"], evidence[3]["sha"])
            self.assertNotEqual(evidence[0]["sha"], evidence[2]["sha"])
            self.assertEqual(evidence[2]["sha"], summary["sha"])
            repair = next(row for row in rows if row["event"] == "repair.requested")
            self.assertEqual(repair["roles"], ["developer"])
            self.assertEqual(summary["model_sessions"], 4)
