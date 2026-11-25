"""Read a bounded GitHub task and its versioned repository context."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

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


def git(*args, env=None, data=None):
    try:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=ROOT,
            input=data, capture_output=True, check=True, timeout=30, env=env,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        raise Blocked("Операция Git завершилась ошибкой.") from None


def api(repo, route, *, method="GET", data=None, paginate=False):
    args = ["gh", "api", f"repos/{identity(repo, REPO)}/{route}", "-X", method]
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


def clean_comments(items):
    return [
        {"author": item["user"]["login"], "body": (item.get("body") or "")[:2000]}
        for item in items if not is_actions(item)
    ][-12:]


def check_author(repo, actor):
    actor = identity(actor, LOGIN)
    allowed = os.environ.get("COORDINATOR_ACTORS", "").lower().replace(",", " ").split()
    if actor.lower() not in allowed:
        raise Blocked("Инициатор отсутствует в COORDINATOR_ACTORS.")
    permission = api(repo, f"collaborators/{actor}/permission")["permission"]
    if permission not in {"write", "maintain", "admin"}:
        raise Blocked("Инициатору нужны права записи в репозиторий.")


def issue_data(repo, number, base):
    issue = api(repo, f"issues/{number}")
    if "pull_request" in issue or issue["state"] != "open":
        raise Blocked("Для запуска нужен открытый Issue, карточки PR не поддерживаются.")
    body = issue.get("body") or ""
    if len(body) > 12000:
        raise Blocked("Описание Issue превышает 12 000 символов.")
    linked = []
    references = dict.fromkeys(int(match) for match in re.findall(r"(?<![\w/])#([1-9]\d{0,8})\b", body))
    for ref in [ref for ref in references if ref != number][:4]:
        related = api(repo, f"issues/{ref}")
        linked.append({
            "number": ref, "title": related["title"], "state": related["state"],
            "body": (related.get("body") or "")[:3000],
            "comments": clean_comments(comments(repo, ref))[-4:],
        })
    return {
        "number": number, "title": issue["title"], "body": body,
        "comments": clean_comments(comments(repo, number)), "linked": linked,
        "project": {
            name: git("show", f"{identity(base, SHA)}:{name}").decode()[:8000]
            for name in ("AGENTS.md", "PROJECT.md")
        },
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def fixture():
    return json.loads((ROOT / "coordinator/demo/task.json").read_text())


def profiles(role):
    return (ROOT / "coordinator/profiles" / f"{role}.md").read_text()
