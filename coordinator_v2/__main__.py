"""Run synthetic V2 scenarios locally without GitHub or a paid provider."""

import argparse
import json
from pathlib import Path
import sys

from coordinator.context import Blocked, git, identity, SHA

from . import demo_fixture
from .engine import Engine
from .journal import Journal
from .runtime import DockerSandbox, FEATURES, Project


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--mode", choices=("fixture",), default="fixture")
    cli.add_argument("--project", choices=("lab",), default="lab")
    cli.add_argument("--scenario", choices=("parallel", "dependency"), default="parallel")
    cli.add_argument("--base", help="Exact trusted base SHA (defaults to HEAD)")
    cli.add_argument("--enable", action="append", choices=sorted(FEATURES), default=[])
    cli.add_argument("--image", default="coordinator-v2-sandbox:local")
    cli.add_argument("--output", type=Path, required=True)
    return cli


def main():
    args = parser().parse_args()
    try:
        journal = Journal(args.output)
    except OSError:
        print("Нужен новый доступный каталог результатов.", file=sys.stderr)
        return 2
    try:
        base = identity(args.base or git("rev-parse", "HEAD").decode().strip(), SHA)
        project = Project.load(base, args.project)
        journal.metadata["project"] = args.project
        features = set(args.enable)
        sandbox = DockerSandbox(args.image, project, base) if features - {"mcp"} else None
        engine = Engine(base, project, features, sandbox, demo_fixture.model, journal)
        answer = engine.run(demo_fixture.SNAPSHOT, lambda: demo_fixture.SNAPSHOT,
                            demo_fixture.plan(args.scenario))
    except (Blocked, OSError, ValueError, KeyError) as error:
        reason = str(error) if isinstance(error, Blocked) else "Не удалось запустить V2 или прочитать настройки."
        answer = journal.finish("blocked", reason=reason)
    print(json.dumps(answer, ensure_ascii=False, indent=2))
    return 0 if answer["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
