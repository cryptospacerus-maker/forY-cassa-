"""Run the optional Coordinator V2 without changing the MVP entry point."""

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys

from coordinator.context import Blocked, check_author, git, identity, issue_data, LOGIN, REPO, SHA
from coordinator.gateway import Gateway
from coordinator.runner import Codex

from . import demo_fixture
from .engine import Engine
from .journal import Journal
from .runtime import DockerSandbox, FEATURES, Project


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--mode", choices=("fixture", "task"), default="fixture")
    cli.add_argument("--project", default="lab")
    cli.add_argument("--scenario", choices=("parallel", "dependency"), default="parallel")
    cli.add_argument("--base", help="Exact trusted base SHA (defaults to HEAD)")
    cli.add_argument("--enable", action="append", choices=sorted(FEATURES), default=[])
    cli.add_argument("--image", default="coordinator-v2-sandbox:local")
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--issue", type=int)
    cli.add_argument("--repo", default="cryptospacerus-maker/forY-cassa-")
    cli.add_argument("--allow-paid", action="store_true")
    return cli


def main():
    args = parser().parse_args()
    key = os.environ.pop("CODEX_API_KEY", None)
    try:
        journal = Journal(args.output)
    except OSError:
        print("Нужен новый доступный каталог результатов.", file=sys.stderr)
        return 2
    try:
        base = identity(args.base or git("rev-parse", "HEAD").decode().strip(), SHA)
        if args.mode == "fixture" and args.project != "lab":
            raise Blocked("Тестовый режим поддерживает профиль lab.")
        project = Project.load(base, args.project)
        journal.metadata["project"] = args.project
        if args.mode == "task" and args.issue and args.issue > 0:
            journal.metadata["issue"] = args.issue
        features = set(args.enable)
        sandbox = DockerSandbox(args.image, project, base) if features - {"mcp"} else None
        fixture_plan = None
        if args.mode == "fixture":
            def current():
                return demo_fixture.SNAPSHOT
            agent, fixture_plan = demo_fixture.model, demo_fixture.plan(args.scenario)
            provider = nullcontext()
        else:
            if not args.allow_paid or not args.issue or args.issue < 1:
                raise Blocked("Режим task требует Issue и явного разрешения платного запуска.")
            repo = identity(args.repo, REPO)
            account = subprocess.run(
                ["gh", "api", "user", "--jq", ".login"],
                capture_output=True, text=True, check=True, timeout=30,
            )
            actor = identity(account.stdout.strip(), LOGIN)
            check_author(repo, actor)

            def current():
                return issue_data(repo, args.issue, base, actor)
            provider = Gateway(key, Codex.model_config(key), "task")
        with provider as gateway:
            if gateway is not None:
                agent = Codex(gateway)
            engine = Engine(base, project, features, sandbox, agent, journal)
            answer = engine.run(current(), current, fixture_plan)
            if gateway is not None:
                (journal.path / "budget.json").write_text(json.dumps(gateway.summary(), indent=2) + "\n")
    except (Blocked, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, Blocked) else "Не удалось запустить V2 или прочитать настройки."
        answer = journal.finish("blocked", reason=reason)
    print(json.dumps(answer, ensure_ascii=False, indent=2))
    return 0 if answer["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
