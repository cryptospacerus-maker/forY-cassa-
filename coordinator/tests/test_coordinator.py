import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from coordinator import __main__ as cli
from coordinator.context import Blocked, DEMO_PATH, fixture, git
from coordinator.engine import allowed_path, base_files, execute, make_commit, plan_tasks, Run
from coordinator.runner import Codex, WORK


VALID = {"доставка": 2, "оплата": 1, "возврат": 1}


def task(role, path, depends_on=()):
    return {
        "role": role, "paths": [path], "depends_on": list(depends_on),
        "instruction": "Реализуй данный результат",
    }


def proposal(*tasks):
    return {"blocker": "", "criteria": ["Проверить значения"], "tasks": list(tasks)}


def response(answer):
    return {"session": "fixture-session", "usage": {"input_tokens": 10, "output_tokens": 3},
            "answer": answer}


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.meta = {
            "base": git("rev-parse", "HEAD").decode().strip(), "mode": "demo", "run_id": "test-1",
            "issue": 12, "actor": "alice", "channel": "github-issue",
        }
        self.fixture = fixture()

    def test_independent_review_and_one_repair_on_real_git_commits(self):
        received_sha = []

        def roles(role, unused, context, schema):
            if role == "analyst":
                return response(proposal(task("developer", DEMO_PATH)))
            if role == "developer":
                contents = VALID if context["feedback"] else {"доставка": 3}
                return response({"summary": "Сводка", "files": [
                    {"path": DEMO_PATH, "content": json.dumps(contents, ensure_ascii=False)},
                ]})
            self.assertEqual(role, "tester")
            received_sha.append(context["sha"])
            self.assertEqual(context["committed_files"], base_files(context["sha"], [DEMO_PATH]))
            return response({"sha": context["sha"], "verdict": "pass", "findings": "Сверка тем"})

        model = Mock(side_effect=roles)
        run = Run(self.path, self.meta)
        execute(run, self.fixture, fixture, model, ())
        self.assertEqual(run.state["status"], "passed")
        self.assertEqual([item["verdict"] for item in run.state["checks"]], ["fail", "pass"])
        self.assertEqual(len(set(received_sha)), 2)
        self.assertEqual(json.loads(base_files(received_sha[-1], [DEMO_PATH])[DEMO_PATH]), VALID)
        self.assertEqual(make_commit(self.meta["base"], run.state["changes"], "test-1", 2), received_sha[-1])
        self.assertEqual(model.call_count, 5)
        execute(Run(self.path, self.meta), self.fixture, fixture, model, ())
        self.assertEqual(model.call_count, 5)

    def test_cached_planner_survives_restart(self):
        run = Run(self.path, self.meta)
        run.state["sessions"]["plan"] = response(proposal(task("developer", DEMO_PATH)))
        run.save()
        worker = Mock(return_value={"error": "Сбой сессии"})
        resumed = Run(self.path, self.meta)
        execute(resumed, self.fixture, fixture, worker, ())
        self.assertEqual(worker.call_args.args[0], "developer")
        self.assertEqual(resumed.state["status"], "blocked")

    def test_review_must_match_commit(self):
        run = Run(self.path, self.meta)
        mock = Mock(side_effect=[
            response(proposal(task("developer", DEMO_PATH))),
            response({"summary": "result", "files": [{"path": DEMO_PATH, "content": "{}"}]}),
            response({"sha": "0" * 40, "verdict": "pass", "findings": ""}),
        ])
        execute(run, self.fixture, fixture, mock, ())
        self.assertEqual(run.state["status"], "blocked")
        self.assertIn("SHA", run.state["reason"])

    def test_stale_requirements_prevent_first_worker(self):
        run = Run(self.path, self.meta)
        model = Mock(return_value=response(proposal(task("developer", DEMO_PATH))))
        execute(run, self.fixture, lambda: dict(self.fixture, body="Changed"), model, ())
        self.assertEqual(run.state["status"], "blocked")
        self.assertEqual(model.call_count, 1)

    def test_parallel_roles_and_dependent_handoff(self):
        inputs = []

        def roles(role, unused, context, schema):
            if role == "analyst":
                return response(proposal(
                    task("developer", "example-a/a.txt"),
                    task("interface", "example-a/b.txt", ["developer"]),
                ))
            if role == "tester":
                return response({"sha": context["sha"], "verdict": "pass", "findings": "OK"})
            inputs.append(context)
            path, = context["allowed_paths"]
            return response({"summary": role, "files": [{"path": path, "content": role}]})

        self.meta["mode"] = "task"
        run = Run(self.path, self.meta)
        execute(run, self.fixture, fixture, roles, ("example-a",))
        self.assertEqual(run.state["status"], "passed")
        self.assertEqual(inputs[1]["handoffs"], {"developer": "developer"})
        self.assertEqual(inputs[1]["files"]["example-a/a.txt"], "developer")
        _, waves = plan_tasks(proposal(
            task("developer", "example-a/a.txt"), task("interface", "example-a/b.txt"),
        ), ("example-a",), False)
        self.assertEqual(waves, [["developer", "interface"]])

    def test_reject_cycles_conflicts_and_protected_paths(self):
        path = "example-a/a.txt"
        with self.assertRaises(Blocked):
            plan_tasks(proposal(task("developer", path), task("interface", path)), ("example-a",), False)
        with self.assertRaises(Blocked):
            plan_tasks(proposal(task("developer", path, ["interface"]),
                                task("interface", "example-a/b.txt", ["developer"])), ("example-a",), False)
        for path in (".github/workflows/a.md", "dashboard/file.html", "demo/../a.md",
                     "demo/.env.txt", "demo/file.sh", "demo-other/a.md"):
            with self.subTest(path=path):
                self.assertFalse(allowed_path(path, (".github", "dashboard", "demo"), False))

    def test_reject_state_from_another_initiator(self):
        Run(self.path, self.meta)
        with self.assertRaises(Blocked):
            Run(self.path, dict(self.meta, actor="bob"))


class RunnerTest(unittest.TestCase):
    @patch.dict(os.environ, {
        "CODEX_API_KEY": "fixture-secret", "COORDINATOR_MODEL": "fixture-model",
        "GH_TOKEN": "must-not-forward",
    })
    @patch("coordinator.runner.subprocess.run")
    def test_codex_uses_isolated_environment_and_parses_usage(self, call):
        def cli_result(command, **kwargs):
            self.assertNotIn("GH_TOKEN", kwargs["env"])
            self.assertEqual(kwargs["env"]["CODEX_API_KEY"], "fixture-secret")
            self.assertIn("read-only", command)
            self.assertIn("shell_tool", command)
            self.assertIn("unified_exec", command)
            self.assertNotIn("fixture-secret", kwargs["input"])
            out = Path(command[command.index("--output-last-message") + 1])
            out.write_text('{"summary":"done","files":[]}')
            stdout = '\n'.join(map(json.dumps, [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.completed", "usage": {"input_tokens": 27, "output_tokens": 3}},
            ]))
            return subprocess.CompletedProcess(command, 0, stdout=stdout)

        call.side_effect = cli_result
        result = Codex()("developer", "Test", {"data": []}, WORK)
        self.assertEqual(result["session"], "thread-1")
        self.assertEqual(result["usage"]["input_tokens"], 27)


class GitHubTest(unittest.TestCase):
    def setUp(self):
        self.meta = {
            "issue": 12, "actor": "alice", "repo": "owner/repo", "key": "key",
            "workflow": "100", "mode": "demo", "run_id": "100-key",
        }

    @patch.object(cli, "check_author")
    @patch.object(cli, "reservations")
    @patch.object(cli, "output")
    def test_second_dispatch_does_not_repeat_claim(self, output, reservations, auth):
        reservations.return_value = [{"body": "<!-- coordinator:claim:key:99 -->"}]
        cli.prepare(self.meta)
        output.assert_called_once_with(proceed="false", resume="")

    @patch.object(cli, "check_author")
    @patch.object(cli, "reservations")
    @patch.object(cli, "add_comment")
    @patch.object(cli, "api")
    @patch.object(cli, "output")
    def test_original_run_selects_newest_checkpoint(self, output, api, comment, reservations, auth):
        reservations.return_value = [{"body": "<!-- coordinator:claim:key:100 -->"}]
        api.return_value = [{"artifacts": [
            {"name": "coordinator-state-1", "expired": False},
            {"name": "coordinator-state-3", "expired": True},
            {"name": "coordinator-state-2", "expired": False},
        ]}]
        cli.prepare(self.meta)
        comment.assert_not_called()
        output.assert_called_once_with(proceed="true", resume="coordinator-state-2")

    @patch.object(cli, "check_author")
    @patch.object(cli, "reservations")
    @patch.object(cli, "add_comment")
    def test_final_report_mentions_authenticated_actor_once(self, comment, reservations, auth):
        reservations.return_value = []
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            Run(path, self.meta).finish("blocked", "Нужна настройка")
            cli.publish(self.meta, path)
            body = comment.call_args.args[1]
            self.assertIn("\n@alice\n", body)
            self.assertIn("заблокировано", body)
            reservations.return_value = [{"body": body}]
            cli.publish(self.meta, path)
        comment.assert_called_once()

    @patch.dict(os.environ, {"COORDINATOR_ACTORS": "alice,bob"})
    @patch("coordinator.context.api")
    def test_authorization_checks_allowlist_and_github_permission(self, api):
        api.return_value = {"permission": "write"}
        cli.check_author("owner/repo", "alice")
        with self.assertRaises(Blocked):
            cli.check_author("owner/repo", "eve")
        api.return_value = {"permission": "read"}
        with self.assertRaises(Blocked):
            cli.check_author("owner/repo", "bob")


if __name__ == "__main__":
    unittest.main()
