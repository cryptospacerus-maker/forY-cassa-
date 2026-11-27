"""Read a bounded GitHub task and its versioned repository context."""

from contextvars import ContextVar
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

API_TRANSPORT = ContextVar("coordinator_api_transport", default=None)
ROOT = Path(__file__).resolve().parents[1]
DEMO_PATH = "coordinator/demo/output/topic-counts.json"
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")


class Blocked(Exception):
    """A condition the coordinator cannot resolve within the current run."""


def identity(value, pattern):
    if not pattern.fullmatch(value):
        raise Blocked("Некорректный идентификатор GitHub.")
    return value


# Independently operated components must stay outside every coordinator project.
def protected_path(path):
    root = path.split("/", 1)[0]
    return (
        root in {"agents", "coordinator", "dashboard", "evaluation", "pilot",
                 "AGENTS.md", "PROJECT.md", "CODEOWNERS"}
        or path == "scripts/evaluator" or path.startswith("scripts/evaluator/")
        or path in {"docs/judge-implementation-report.md", "docs/architecture/evaluator-plan.md",
                    "tests/test_evaluator.py"}
    )


def git(*args, env=None, data=None):
    directory = os.environ.get("COORDINATOR_V3_GIT_STORE")
    try:
        if directory:
            env = dict(os.environ if env is None else env)
            for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE"):
                env.pop(key, None)
            command = ["git", "--git-dir", directory]
        else:
            command = ["git"]
        return subprocess.run(
            [*command, "-c", "core.hooksPath=/dev/null", *args], cwd=ROOT,
            input=data, capture_output=True, check=True, timeout=30, env=env,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        raise Blocked("Операция Git завершилась ошибкой.") from None


def api(repo, route, *, method="GET", data=None, paginate=False):
    path = f"repos/{identity(repo, REPO)}/{route}"
    transport = API_TRANSPORT.get()
    if transport:
        return transport(path, method=method, data=data, paginate=paginate)
    args = ["gh", "api", path, "-X", method]
    if paginate:
        args.append("--paginate")
    if data is not None:
        args += ["--input", "-"]
    try:
        result = subprocess.run(
            args, input=None if data is None else json.dumps(data),
            text=True, capture_output=True, timeout=45, check=True,
        )
        if not paginate:
            return json.loads(result.stdout)
        pages = []
        remaining = result.stdout.lstrip()
        decoder = json.JSONDecoder()
        while remaining:
            page, end = decoder.raw_decode(remaining)
            pages.append(page)
            remaining = remaining[end:].lstrip()
        return pages
    except (subprocess.SubprocessError, OSError, ValueError):
        raise Blocked(f"GitHub API недоступен для операции {method} {route.split('?')[0]}.") from None


def comments(repo, issue):
    return [
        item for page in api(repo, f"issues/{issue}/comments?per_page=100", paginate=True)
        for item in page
    ]


def is_actions(comment):
    return comment["user"]["login"] == "github-actions[bot]" and comment["user"]["type"] == "Bot"


def clean_comments(items, actor):
    return [
        {"author": item["user"]["login"], "body": (item.get("body") or "")[:2000]}
        for item in items if item["user"]["login"].lower() == actor.lower() and not is_actions(item)
    ][-12:]


def check_author(repo, actor):
    actor = identity(actor, LOGIN)
    allowed = os.environ.get("COORDINATOR_ACTORS", "").lower().replace(",", " ").split()
    if actor.lower() not in allowed:
        raise Blocked("Инициатор отсутствует в COORDINATOR_ACTORS.")
    permission = api(repo, f"collaborators/{actor}/permission")["permission"]
    if permission not in {"write", "maintain", "admin"}:
        raise Blocked("Инициатору нужны права записи в репозиторий.")


def issue_data(repo, number, base, actor, *, strict=False):
    issue = api(repo, f"issues/{number}")
    if "pull_request" in issue or issue["state"] != "open":
        raise Blocked("Для запуска нужен открытый Issue, карточки PR не поддерживаются.")
    body = issue.get("body") or ""
    if len(body) > 12000:
        raise Blocked("Описание Issue превышает 12 000 символов.")
    linked = []
    references = dict.fromkeys(int(match) for match in re.findall(r"(?<![\w/])#([1-9]\d{0,8})\b", body))
    relevant = [ref for ref in references if ref != number]
    if strict and len(relevant) > 4:
        raise Blocked("Слишком много связанных требований. Уточните карточку.")
    def requirements_comments(items, maximum):
        if not strict:
            return clean_comments(items, actor)[-maximum:]
        selected = [item for item in items if item["user"]["login"].lower() == actor.lower()
                    and not is_actions(item) and not (item.get("body") or "").strip().startswith("/coordinator-v3 ")]
        if len(selected) > maximum or any(len(item.get("body") or "") > 2000 for item in selected):
            raise Blocked("Комментарии с требованиями превышают лимит. Перенесите их в описание.")
        return [{"author": item["user"]["login"], "body": item.get("body") or ""} for item in selected]

    for ref in relevant[:4]:
        related = api(repo, f"issues/{ref}")
        if strict and len(related.get("body") or "") > 3000:
            raise Blocked("Связанное описание превышает лимит. Вынесите действующие условия в запрос.")
        linked.append({
            "number": ref, "title": related["title"], "state": related["state"],
            "body": (related.get("body") or "")[:3000],
            "comments": requirements_comments(comments(repo, ref), 4),
        })
    project = {name: git("show", f"{identity(base, SHA)}:{name}").decode()
               for name in ("AGENTS.md", "PROJECT.md")}
    if strict and any(len(value) > 8000 for value in project.values()):
        raise Blocked("Проектная инструкция превышает лимит контекста.")
    return {
        "number": number, "title": issue["title"], "body": body,
        "comments": requirements_comments(comments(repo, number), 12), "linked": linked,
        "project": {name: value[:8000] for name, value in project.items()},
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def fixture():
    return json.loads((ROOT / "coordinator/demo/task.json").read_text())


def profiles(role):
    agents = ROOT / "agents"
    general, roles = (agents / "protocol.md").read_text().split("## Роль ", maxsplit=1)
    role_sections = dict(section.split("\n", maxsplit=1) for section in roles.split("\n## Роль "))
    return f"{(agents / f'{role}.md').read_text()}\n{general}\n{role_sections[role]}"
