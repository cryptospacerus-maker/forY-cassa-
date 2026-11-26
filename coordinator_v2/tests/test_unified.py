"""Exercise the live CLI route without GitHub writes or a model API."""

import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from coordinator import __main__ as cli, context
from coordinator.context import Blocked, DEMO_PATH, fixture, git
from coordinator.runner import WORK
from coordinator.unified import routing
from coordinator_v2 import demo_fixture
from coordinator_v2.engine import PLAN


BOT = {"login": "github-actions[bot]", "type": "Bot"}
ACTORS = ("greatsoutherntrendkill", "tolyapolya2006-png")


class GatewayFixture:
    def __init__(self, gateway):
        self.gateway = gateway

    @staticmethod
    def model_config(key):
        if key != "fixture-key":
            raise AssertionError("A generation session unexpectedly started.")
        return "fixture-model"

    def __call__(self, role, instruction, payload, contract):
        if contract == PLAN:
            plan = demo_fixture.plan("parallel")
            for task in plan["tasks"]:
                if not payload["mcp_categories"]:
                    task["mcp_category"] = ""
            return {"answer": plan}
        if contract == WORK:
            return {"answer": {"summary": "Synthetic V1", "files": [{
                "path": DEMO_PATH,
                "content": json.dumps({"доставка": 2, "оплата": 1, "возврат": 1}, ensure_ascii=False),
            }]}}
        if role == "tester" and "committed_files" in payload:
            return {"answer": {"sha": payload["sha"], "verdict": "pass", "findings": "Synthetic V1 QA"}}
        return demo_fixture.model(role, instruction, payload, contract)


class GitHubFixture:
    def __init__(self, base):
        self.base = base
        self.comments = {13: [], 17: []}
        self.fail_report = False

    def __call__(self, repo, route, *, method="GET", data=None, paginate=False):
        if repo != "owner/repo":
            raise AssertionError(repo)
        if route == "git/ref/heads/main":
            return {"object": {"sha": self.base}}
        if route.startswith("collaborators/"):
            actor = route.split("/")[1]
            return {"permission": "write" if actor in ACTORS else "read"}
        if route.startswith("actions/runs/") and "/artifacts" in route:
            return [{"artifacts": [{"name": "coordinator-state-1", "expired": False}]}]
        if route.startswith("issues/"):
            issue = int(route.split("/")[1])
            if route.count("/") == 1:
                data = fixture() if issue == 13 else demo_fixture.SNAPSHOT
                return dict(data, state="open")
            if method == "POST":
                if self.fail_report and data["body"].startswith("<!-- coordinator:report:"):
                    raise Blocked("Synthetic GitHub failure")
                row = {"body": data["body"], "user": BOT}
                self.comments[issue].append(row)
                return row
            if route.split("/")[2].startswith("comments"):
                return [self.comments[issue]]
        raise AssertionError(f"Unexpected GitHub operation: {method} {route}")

    def reports(self, issue):
        return [row["body"] for row in self.comments[issue] if row["body"].startswith("<!-- coordinator:report:")]


class UnifiedTest(unittest.TestCase):
    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.root = Path(resources.enter_context(TemporaryDirectory()))
        self.base = git("rev-parse", "HEAD").decode().strip()
        self.remote = self.root / "remote"
        subprocess.run(["git", "init", "--bare", "--quiet", str(self.remote)], check=True)
        self.api = GitHubFixture(self.base)
        resources.enter_context(patch.object(context, "api", self.api))
        resources.enter_context(patch.object(cli, "api", self.api))
        resources.enter_context(patch.object(cli, "Codex", GatewayFixture))
        resources.enter_context(patch.object(cli, "git", side_effect=self.redirect_git))
        resources.enter_context(patch("coordinator.gateway.provider_request",
                                     side_effect=AssertionError("No upstream model call allowed")))
        resources.enter_context(patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true", "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ATTEMPT": "1",
            "COORDINATOR_ACTORS": " ".join(ACTORS), "COORDINATOR_V2_FEATURES": "mcp",
            "COORDINATOR_V2_PROJECT": "lab", "COORDINATOR_RESUME_ONLY": "false",
            "COORDINATOR_DRAFT_PR": "false", "COORDINATOR_PATHS": "", "COORDINATOR_ROUTE_TOKEN": "",
        }))

    def redirect_git(self, *args):
        if args[0] in {"ls-remote", "push"}:
            args = tuple(str(self.remote) if arg == "origin" else arg for arg in args)
        return git(*args)

    def operation(self, action, mode, *, actor=ACTORS[0], workflow="100", attempt="1",
                  issue=17, resume_only="false", key=None, folder=None, request="pilot"):
        prefix = self.root / (folder or workflow)
        prefix.mkdir(exist_ok=True)
        args = [
            "coordinator", action, "--issue", str(issue), "--repo", "owner/repo", "--mode", mode,
            "--request", request, "--state", str(prefix / "state.json"),
            "--journal", str(prefix / "journal"),
        ]
        env = {"GITHUB_ACTOR": actor, "GITHUB_RUN_ID": workflow, "GITHUB_RUN_ATTEMPT": attempt,
               "GITHUB_OUTPUT": str(prefix / "outputs"), "COORDINATOR_RESUME_ONLY": resume_only}
        if key:
            env["CODEX_API_KEY"] = key
        with patch.dict(os.environ, env), patch.object(sys, "argv", args), redirect_stdout(io.StringIO()):
            return cli.main()

    def events(self, workflow="100"):
        path = self.root / workflow / "journal/events.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([row["sequence"] for row in rows], list(range(1, len(rows) + 1)))
        self.assertEqual(len({row["run_id"] for row in rows}), 1)
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError:
            return rows
        schema_path = Path(__file__).resolve().parents[2] / "coordinator/events.schema.json"
        schema = json.loads(schema_path.read_text())
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for row in rows:
            validator.validate(row)
        return rows

    def state(self, workflow="100"):
        return json.loads((self.root / workflow / "state.json").read_text())

    def test_same_dispatch_for_both_initiators_and_demo_ignores_v2_settings(self):
        for workflow, actor in zip(("100", "101"), ACTORS, strict=True):
            self.assertEqual(self.operation("prepare", "dry-run", actor=actor, workflow=workflow), 0)
            self.assertEqual(self.operation("run", "dry-run", actor=actor, workflow=workflow), 0)
            self.assertEqual(self.operation("publish", "dry-run", actor=actor, workflow=workflow), 0)
            self.assertEqual(self.state(workflow)["sessions"], {})
            self.assertEqual(self.events(workflow)[-1]["notification"], "issue-comment")
        self.assertEqual(len(self.api.reports(17)), 2)
        for actor in ACTORS:
            self.assertEqual(sum(f"\n@{actor}\n" in body for body in self.api.reports(17)), 1)

        workflow = "102"
        self.assertEqual(self.operation("prepare", "demo", workflow=workflow, issue=13), 0)
        self.assertEqual(self.operation("run", "demo", workflow=workflow, issue=13, key="fixture-key"), 0)
        self.assertEqual(self.operation("publish", "demo", workflow=workflow, issue=13), 0)
        state = self.state(workflow)
        self.assertEqual(state["routing"]["engine"], "v1")
        self.assertEqual(state["status"], "passed")
        self.assertEqual(len(state["sessions"]), 2)
        self.assertEqual(self.events(workflow)[-1]["published_sha"], state["checks"][-1]["sha"])
        self.assertEqual(self.operation("prepare", "demo", workflow="103", issue=13), 0)
        self.assertIn("proceed=false", (self.root / "103/outputs").read_text())
        self.assertEqual(len(self.api.reports(13)), 1)

    def test_both_actors_can_use_same_task_request_with_v2_enabled(self):
        for workflow, actor in zip(("100", "101"), ACTORS, strict=True):
            self.assertEqual(self.operation("prepare", "task", actor=actor, workflow=workflow), 0)
            route_token = next(line.split("=", 1)[1] for line in
                               (self.root / workflow / "outputs").read_text().splitlines()
                               if line.startswith("route_token="))
            with patch.dict(os.environ, {"COORDINATOR_ROUTE_TOKEN": route_token}):
                self.assertEqual(self.operation("run", "task", actor=actor, workflow=workflow, key="fixture-key"), 0)
                self.assertEqual(self.operation("publish", "task", actor=actor, workflow=workflow), 0)
            self.assertEqual(self.state(workflow)["routing"]["features"], ["mcp"])
            self.assertTrue(any(row["event"] == "mcp.read" for row in self.events(workflow)))
        self.assertEqual(len(self.api.reports(17)), 2)

    def test_route_change_between_prepare_and_execution_is_blocked(self):
        self.assertEqual(self.operation("prepare", "task"), 0)
        prepared = (self.root / "100/outputs").read_text().split("route_token=", 1)[1].strip()
        with patch.dict(os.environ, {"COORDINATOR_ROUTE_TOKEN": prepared, "COORDINATOR_V2_FEATURES": ""}):
            self.assertEqual(self.operation("run", "task"), 0)
        self.assertEqual(self.state()["status"], "blocked")
        self.assertEqual(self.state()["sessions"], {})

    def test_unauthorized_dispatch_does_not_reserve_issue(self):
        self.assertEqual(self.operation("prepare", "task", actor="outsider"), 1)
        self.assertEqual(self.api.comments[17], [])

    def test_no_feature_flags_is_v1_and_unknown_options_fail_closed(self):
        with patch.dict(os.environ, {"COORDINATOR_V2_FEATURES": ""}):
            self.assertEqual(routing("task")["engine"], "v1")
        with patch.dict(os.environ, {"COORDINATOR_V2_FEATURES": "browser unsupported"}):
            with self.assertRaises(Blocked):
                routing("task")
            self.assertEqual(routing("demo"), {"engine": "v1", "features": [], "project": None})

    def test_changed_admin_policy_cannot_publish_or_recompute_a_finished_run(self):
        self.assertEqual(self.operation("prepare", "task"), 0)
        self.assertEqual(self.operation("run", "task", key="fixture-key"), 0)
        self.assertEqual(self.state()["status"], "passed")
        with patch.dict(os.environ, {"COORDINATOR_V2_FEATURES": ""}):
            self.assertEqual(self.operation("run", "task", attempt="2", resume_only="true"), 0)
            self.assertEqual(self.operation("publish", "task", attempt="2"), 0)
        self.assertIn("Настройки маршрута изменились", self.api.reports(17)[0])
        remote = subprocess.check_output(["git", f"--git-dir={self.remote}", "for-each-ref"])
        self.assertEqual(remote, b"")

    @unittest.skipUnless(os.environ.get("COORDINATOR_V2_TEST_IMAGE"), "Docker image not requested")
    def test_combined_issue_to_browser_qa_git_and_one_notification(self):
        with patch.dict(os.environ, {"COORDINATOR_V2_FEATURES": "execution tests browser mcp"}):
            self.assertEqual(self.operation("prepare", "task"), 0)
            self.assertIn("sandbox=true", (self.root / "100/outputs").read_text())
            self.assertEqual(self.operation("run", "task", key="fixture-key"), 0)
            state = self.state()
            self.assertEqual(state["status"], "passed", state)
            bad, good = state["checks"]
            self.assertEqual((bad["verdict"], good["verdict"]), ("fail", "pass"))
            self.assertNotEqual(bad["sha"], good["sha"])
            self.assertEqual({item["check"] for item in good["evidence"]}, {"qa-unit", "qa-browser"})
            sha = good["sha"]
            self.assertTrue(all(item["sha"] == sha and item["status"] == "pass" for item in good["evidence"]))
            self.assertEqual(state["budget"]["requests"], 0)
            self.assertEqual(state["v2"]["mcp_requests"], 1)

            tampered = copy.deepcopy(state)
            tampered["checks"][-1]["evidence"].pop()
            with self.assertRaisesRegex(Blocked, "обязательные результаты QA"):
                cli.push_result(state["meta"], tampered)

            self.api.fail_report = True
            self.assertEqual(self.operation("publish", "task"), 1)
            self.assertEqual(self.events()[-1]["event"], "publication.failed")
            self.api.fail_report = False
            self.assertEqual(self.operation("prepare", "task", attempt="2"), 0)
            self.assertIn("resume=coordinator-state-1", (self.root / "100/outputs").read_text())
            self.assertEqual(self.operation("run", "task", attempt="2", resume_only="true"), 0)
            self.assertEqual(self.state()["sessions"], state["sessions"])
            self.assertEqual(self.operation("publish", "task", attempt="2"), 0)
            self.assertEqual(self.operation("publish", "task", attempt="2"), 0)
            self.assertEqual(self.operation("prepare", "task", workflow="103"), 0)
            self.assertIn("proceed=false", (self.root / "103/outputs").read_text())

        refs = subprocess.check_output(["git", f"--git-dir={self.remote}", "for-each-ref",
                                        "--format=%(objectname) %(refname)"]).decode().splitlines()
        self.assertEqual(refs, [f"{sha} refs/heads/coordinator/result-17-{state['meta']['key']}"])
        self.assertEqual(len(self.api.reports(17)), 1)
        self.assertIn(f"\n@{ACTORS[0]}\n", self.api.reports(17)[0])
        events = self.events()
        self.assertTrue(any(row["event"] == "mcp.read" for row in events))
        self.assertEqual(events[-2]["published_sha"], sha)
        self.assertEqual(events[-1]["notification"], "already-posted")
        self.assertNotIn("pencil", json.dumps(events))
        if evidence_dir := os.environ.get("COORDINATOR_V2_EVIDENCE_DIR"):
            shutil.copytree(self.root / "100/journal", evidence_dir)
