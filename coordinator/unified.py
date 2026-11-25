"""Select the trusted coordinator route and bridge V2 into the shared run state."""

import os

from coordinator_v2.engine import Engine
from coordinator_v2.journal import Journal
from coordinator_v2.runtime import DockerSandbox, FEATURES, Project, CHECK_ID

from .context import Blocked, digest, git, identity
from .engine import allowed_path, base_files, execute


def routing(mode):
    features = set(os.environ.get("COORDINATOR_V2_FEATURES", "").replace(",", " ").split()) if mode == "task" else set()
    if not features <= FEATURES:
        raise Blocked("COORDINATOR_V2_FEATURES содержит неизвестную возможность.")
    if not features:
        return {"engine": "v1", "features": [], "project": None}
    project = identity(os.environ.get("COORDINATOR_V2_PROJECT", ""), CHECK_ID)
    return {"engine": "v2", "features": sorted(features), "project": project}


def check_route(policy):
    token = os.environ.get("COORDINATOR_ROUTE_TOKEN")
    if token and token != digest(policy):
        raise Blocked("Разрешённые настройки изменились после подготовки запуска.")


def open_journal(path, meta, resume=False):
    if path is None:
        return None
    return Journal(path, metadata={
        "repository": meta["repo"], "issue": meta["issue"],
        "initiator": meta["actor"], "mode": meta["mode"], "workflow_run_id": meta["workflow"],
    }, run_id=meta["run_id"], resume=resume)


def load_project(base, policy):
    try:
        return Project.load(base, policy["project"])
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise Blocked("Некорректный доверенный профиль V2.") from error


def execute_route(run, snapshot, current, model, prefixes, policy, journal):
    if policy["engine"] == "v1":
        return execute(run, snapshot, current, model, prefixes, journal=journal)

    base = run.state["meta"]["base"]
    project = load_project(base, policy)
    features = set(policy["features"])
    sandbox = DockerSandbox("coordinator-v2-sandbox:local", project, base) if features - {"mcp"} else None
    engine = Engine(base, project, features, sandbox, model, journal, export_candidate=False)
    result = engine.run(snapshot, current)
    run.state["sessions"] = {str(index): session for index, session in enumerate(engine.sessions)}
    run.state["checks"] = engine.qa_rounds
    if engine.criteria:
        run.state["plan"] = {"criteria": engine.criteria}
    run.state["v2"] = {name: result[name] for name in ("context_bytes", "mcp_requests", "mcp_cache_hits")}
    if result["status"] == "passed":
        sha = result["sha"]
        paths = git("diff", "--name-only", "-z", base, sha).decode().split("\0")[:-1]
        if not paths or not all(project.allows(path) for path in paths):
            raise Blocked("Проверенный SHA содержит файлы вне профиля V2.")
        run.state.update(round=result["round"], changes=base_files(sha, paths))
    run.finish(result["status"], result.get("reason", ""))


def publication_rules(state, policy, prefixes):
    if policy["engine"] == "v1":
        return lambda path: allowed_path(path, prefixes, state["meta"]["mode"] == "demo"), None
    project = load_project(state["meta"]["base"], policy)
    expected = {check.name for check in project.for_role("tester", set(policy["features"]))}
    return project.allows, expected
