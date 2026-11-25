"""Command-line entry points for local runs and the GitHub Actions jobs."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .context import (
    api, Blocked, check_author, comments, digest, fixture, git, identity,
    is_actions, issue_data, LOGIN, REPO, SHA,
)
from .engine import allowed_path, demo_check, estimate_usage, execute, make_commit, Run
from .runner import Codex


def request_meta(args):
    if args.issue < 1:
        raise Blocked("Номер Issue должен быть положительным.")
    actor = identity("local" if args.fixture else os.environ.get("GITHUB_ACTOR", args.actor), LOGIN)
    repo = identity(args.repo, REPO)
    request = identity(args.request, re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}"))
    base = identity(git("rev-parse", "HEAD").decode().strip(), SHA)
    key = digest([repo.lower(), args.issue, actor.lower(), request, args.mode])[:24]
    workflow = os.environ.get("GITHUB_RUN_ID", "local")
    identity(workflow, re.compile(r"local|[1-9][0-9]{0,19}"))
    return {
        "issue": args.issue, "actor": actor, "repo": repo,
        "request": request, "mode": args.mode, "base": base,
        "run_id": f"{workflow}-{key}", "key": key, "channel": "github-issue",
        "workflow": workflow,
    }


def reservations(meta):
    return [item for item in comments(meta["repo"], meta["issue"]) if is_actions(item)]


def marker(meta, kind):
    return f"<!-- coordinator:{kind}:{meta['key']}:{meta['workflow']} -->"


def previous_report(items, meta):
    return any(
        (item.get("body") or "").startswith(f"<!-- coordinator:report:{meta['key']}:")
        for item in items
    )


def add_comment(meta, body):
    return api(
        meta["repo"], f"issues/{meta['issue']}/comments", method="POST",
        data={"body": body},
    )


def run_url(meta):
    return f"https://github.com/{meta['repo']}/actions/runs/{meta['workflow']}"


def output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
        for name, value in values.items():
            print(f"{name}={value}", file=handle)


def verify_main(meta):
    head = api(meta["repo"], "git/ref/heads/main")["object"]["sha"]
    if head != meta["base"]:
        raise Blocked("Ветка main обновилась после начала запуска.")


def prepare(meta):
    check_author(meta["repo"], meta["actor"])
    items = reservations(meta)
    if previous_report(items, meta):
        print("У запроса уже есть итоговый отчёт.")
        return output(proceed="false", resume="")
    claim_prefix = f"<!-- coordinator:claim:{meta['key']}:"
    claim = next((item for item in items if (item.get("body") or "").startswith(claim_prefix)), None)
    if claim and not claim["body"].startswith(marker(meta, "claim")):
        print("Запрос закреплён за предыдущим запуском. Для восстановления повторите его в Actions.")
        return output(proceed="false", resume="")
    if not claim:
        verify_main(meta)
        issue_data(meta["repo"], meta["issue"], meta["base"])
        add_comment(meta, f"{marker(meta, 'claim')}\nЗапрос принят. [Запуск GitHub Actions]({run_url(meta)}).")
    artifacts = api(
        meta["repo"], f"actions/runs/{meta['workflow']}/artifacts?per_page=100", paginate=True,
    )
    candidates = [
        int(item["name"].split("-")[-1])
        for page in artifacts for item in page.get("artifacts", [])
        if not item["expired"] and re.fullmatch(r"coordinator-state-[1-9][0-9]{0,6}", item["name"])
    ]
    resume = f"coordinator-state-{max(candidates)}" if candidates else ""
    output(proceed="true", resume=resume)


def fresh(meta, use_fixture=False):
    if use_fixture:
        return fixture()
    if os.environ.get("GITHUB_ACTIONS") == "true":
        verify_main(meta)
    return issue_data(meta["repo"], meta["issue"], meta["base"])


def run_task(args, meta, path):
    current = lambda: fresh(meta, args.fixture)
    prefixes = tuple(os.environ.get("COORDINATOR_PATHS", "").split())
    run = Run(path, meta)
    try:
        snapshot = current()
    except Blocked as error:
        run.finish("blocked", str(error))
        print(report(run.state))
        return
    if run.state["status"] == "running":
        initial = digest(snapshot)
        if run.state.setdefault("snapshot_hash", initial) != initial:
            run.finish("blocked", "Требования изменились с момента сохранения состояния.")
        else:
            run.save()
    if args.mode == "dry-run" and run.state["status"] == "running":
        role = "interface" if re.search(r"экран|интерфейс|frontend|UI\b", snapshot["title"], re.I) else "developer"
        run.state["route"] = [role, "tester"]
        run.finish("dry-run", "Маршрут предварительный. Сессии Codex не запускались.")
    elif run.state["status"] == "running":
        try:
            if args.mode == "task" and not prefixes:
                raise Blocked("Для обычных задач задайте COORDINATOR_PATHS.")
            execute(run, snapshot, current, Codex(), prefixes)
        except Blocked as error:
            run.finish("blocked", str(error))
    print(report(run.state))


def excerpt(text):
    return " ".join(text.split()).translate(str.maketrans("@<>[]`", "＠‹›［］′"))[:2000]


def report(state, delivery=""):
    meta = state["meta"]
    statuses = {
        "passed": "принято", "failed": "требуется исправление",
        "blocked": "заблокировано", "dry-run": "план без запуска", "running": "в работе",
    }
    lines = [f"## Coordinator MVP · {statuses[state['status']]}",
             f"Issue #{meta['issue']} · Run `{meta['run_id']}` · канал GitHub Issue"]
    if state.get("plan"):
        lines.append("Критерии приёмки")
        lines.extend(f"- {excerpt(item)}" for item in state["plan"]["criteria"])
    if state.get("reason"):
        lines.append(excerpt(state["reason"]))
    if "route" in state:
        lines.append("Маршрут " + " → ".join(state["route"]))
    for role, value in state.get("handoffs", {}).items():
        lines.append(f"- {role} · {excerpt(value)}")
    for check in state["checks"]:
        lines.append(f"- QA · `{check['sha']}` · {check['verdict']} · {excerpt(check['findings'])}")
    totals, estimate = estimate_usage(state["sessions"])
    complete = sum(bool(item.get("usage")) for item in state["sessions"].values())
    lines.append(
        f"Сессии с данными о расходе {complete}/{len(state['sessions'])}. "
        f"Учтённые токены {totals['input_tokens']} входных, "
        f"{totals['cached_input_tokens']} кэшированных входных и {totals['output_tokens']} выходных."
    )
    if estimate is not None:
        lines.append(f"Оценка стоимости по заданным тарифам — ${estimate}.")
    if delivery:
        lines.append(delivery)
    return "\n\n".join(lines)


def push_result(meta, state):
    prefixes = tuple(os.environ.get("COORDINATOR_PATHS", "").split())
    changes = state["changes"]
    if not changes or len(changes) > 8 or not all(
        allowed_path(path, prefixes, meta["mode"] == "demo")
        and isinstance(value, str) and "\0" not in value and len(value.encode()) <= 12000
        for path, value in changes.items()
    ):
        raise Blocked("Артефакт содержит изменение за пределами разрешённой области.")
    commit = make_commit(meta["base"], changes, meta["run_id"], state["round"])
    if commit != state["checks"][-1]["sha"] or state["checks"][-1]["verdict"] != "pass":
        raise Blocked("Публикуемый коммит отличается от принятой Андрюхой версии.")
    if meta["mode"] == "demo" and demo_check(commit):
        raise Blocked("Публикуемый коммит не проходит проверку контрольного примера.")
    branch = f"coordinator/result-{meta['issue']}-{meta['key']}"
    ref = f"refs/heads/{branch}"
    remote = git("ls-remote", "--refs", "origin", ref).decode().split()
    if remote and remote[0] != commit:
        raise Blocked("Ветка результата уже указывает на другой коммит.")
    if not remote:
        git("push", f"--force-with-lease={ref}:", "origin", f"{commit}:{ref}")
    url = f"https://github.com/{meta['repo']}/commit/{commit}"
    delivery = f"[Проверенный коммит]({url})"
    if os.environ.get("COORDINATOR_DRAFT_PR") == "true":
        try:
            prs = api(meta["repo"], f"pulls?state=all&head={meta['repo'].split('/')[0]}:{branch}&base=main")
            existing = next((pr for pr in prs if pr["head"]["sha"] == commit), None)
            pr = existing or api(meta["repo"], "pulls", method="POST", data={
                "head": branch, "base": "main", "draft": True,
                "title": f"Coordinator result for #{meta['issue']}",
                "body": f"Issue #{meta['issue']}. Проверенная версия `{commit}`.",
            })
            delivery += f" · [PR с результатом]({pr['html_url']})"
        except Blocked:
            delivery += ". GitHub не позволил открыть черновик PR."
    return delivery


def publish(meta, path):
    check_author(meta["repo"], meta["actor"])
    if previous_report(reservations(meta), meta):
        return
    if not path.is_file():
        raise Blocked("Артефакт состояния отсутствует. Повторите исходный запуск.")
    state = Run(path, meta).state
    if state["status"] == "running":
        raise Blocked("Запуск прерван. Повторите его в Actions для восстановления.")
    delivery = f"[Журнал запуска]({run_url(meta)})"
    if state["status"] == "passed":
        try:
            unchanged = digest(fresh(meta)) == state["snapshot_hash"]
        except Blocked:
            unchanged = False
        if unchanged:
            try:
                delivery += " · " + push_result(meta, state)
            except Blocked as error:
                state.update(status="blocked", reason=f"Публикация результата: {error}")
        else:
            state.update(status="blocked", reason="Требования обновились перед публикацией.")
    add_comment(meta, f"{marker(meta, 'report')}\n@{meta['actor']}\n\n{report(state, delivery)}")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("operation", choices=("run", "prepare", "publish"))
    result.add_argument("--issue", required=True, type=int)
    result.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "cryptospacerus-maker/forY-cassa-"))
    result.add_argument("--actor", default="local")
    result.add_argument("--request", default="mvp")
    result.add_argument("--mode", choices=("dry-run", "demo", "task"), default="dry-run")
    result.add_argument("--fixture", action="store_true")
    result.add_argument("--state", type=Path)
    return result


def main():
    args = parser().parse_args()
    try:
        meta = request_meta(args)
        if args.operation != "run":
            if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REF") != "refs/heads/main"
                    or args.fixture or args.state is None):
                raise Blocked("Подготовка и публикация доступны из GitHub Actions на main.")
            if args.operation == "prepare":
                prepare(meta)
            else:
                publish(meta, args.state)
            return 0
        if args.fixture and (
            args.mode == "task"
            or (args.mode == "demo" and os.environ.get("GITHUB_ACTIONS") == "true")
        ):
            raise Blocked("Локальный пример поддерживает режимы dry-run и demo.")
        if not args.fixture:
            check_author(meta["repo"], meta["actor"])
            if os.environ.get("GITHUB_ACTIONS") != "true":
                login = subprocess.run(
                    ["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=30,
                )
                if login.returncode or login.stdout.strip().lower() != meta["actor"].lower():
                    raise Blocked("Локальный --actor должен совпадать с учётной записью gh.")
        path = args.state or (Path(git("rev-parse", "--absolute-git-dir").decode().strip())
                              / "coordinator" / f"{meta['run_id']}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            run_task(args, meta, path)
        return 0
    except (Blocked, subprocess.SubprocessError, OSError) as error:
        message = str(error) if isinstance(error, Blocked) else "Операция координатора завершилась ошибкой."
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
