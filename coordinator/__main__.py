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
from .engine import demo_check, estimate_usage, make_commit, Run
from .gateway import Gateway
from .runner import Codex
from .unified import check_route, execute_route, load_project, open_journal, publication_rules, routing


def make_meta(repo, issue, actor, request, mode, base, workflow):
    if not 1 <= issue <= 999_999_999:
        raise Blocked("Номер Issue должен быть положительным.")
    identity(actor, LOGIN)
    identity(repo, REPO)
    identity(request, re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}"))
    identity(base, SHA)
    identity(workflow, re.compile(r"local|[1-9][0-9]{0,19}"))
    key = digest([repo.lower(), issue, actor.lower(), request, mode])[:24]
    return {
        "issue": issue, "actor": actor, "repo": repo,
        "request": request, "mode": mode, "base": base,
        "run_id": f"{workflow}-{key}", "key": key, "channel": "github-issue",
        "workflow": workflow,
    }


def request_meta(args):
    actor = "local" if args.fixture else os.environ.get("GITHUB_ACTOR", args.actor)
    base = git("rev-parse", "HEAD").decode().strip()
    return make_meta(args.repo, args.issue, actor, args.request, args.mode, base,
                     os.environ.get("GITHUB_RUN_ID", "local"))


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
        return output(proceed="false", resume="", attempt="", resume_only="false")
    claim_prefix = f"<!-- coordinator:claim:{meta['key']}:"
    claim = next((item for item in items if (item.get("body") or "").startswith(claim_prefix)), None)
    if claim and not claim["body"].startswith(marker(meta, "claim")):
        print("Запрос закреплён за предыдущим запуском. Для восстановления повторите его в Actions.")
        return output(proceed="false", resume="", attempt="", resume_only="false")
    policy = routing(meta["mode"])
    resume = ""
    if claim:
        artifacts = api(
            meta["repo"], f"actions/runs/{meta['workflow']}/artifacts?per_page=100", paginate=True,
        )
        candidates = [
            int(item["name"].split("-")[-1])
            for page in artifacts for item in page.get("artifacts", [])
            if not item["expired"] and re.fullmatch(r"coordinator-state-[1-9][0-9]{0,6}", item["name"])
        ]
        if candidates:
            resume = f"coordinator-state-{max(candidates)}"
    else:
        verify_main(meta)
        if policy["engine"] == "v2":
            load_project(meta["base"], policy)
        issue_data(meta["repo"], meta["issue"], meta["base"], meta["actor"])
        add_comment(meta, f"{marker(meta, 'claim')}\nЗапрос принят. [Запуск GitHub Actions]({run_url(meta)}).")
    resume_only = bool(claim and meta["mode"] != "dry-run")
    if resume_only and not resume:
        raise Blocked("У платного запроса нет сохранённого состояния. Повтор с тем же ключом остановлен.")
    attempt = identity(os.environ["GITHUB_RUN_ATTEMPT"], re.compile(r"[1-9][0-9]{0,6}"))
    output(proceed="true", resume=resume, attempt=attempt,
           resume_only="true" if resume_only else "false",
           sandbox="true" if set(policy["features"]) - {"mcp"} else "false",
           route_token=digest(policy))


def fresh(meta, use_fixture=False):
    if use_fixture:
        return fixture()
    if os.environ.get("GITHUB_ACTIONS") == "true":
        verify_main(meta)
    return issue_data(meta["repo"], meta["issue"], meta["base"], meta["actor"])


def summarize_journal(journal, state, event, **fields):
    if journal is None:
        return
    last_check = state["checks"][-1] if state["checks"] else {}
    details = {
        "base": state["meta"]["base"], "sha": last_check.get("sha"),
        "routing": state.get("routing"), "model_sessions": len(state["sessions"]),
        "usage": estimate_usage(state["sessions"])[0], "budget": state.get("budget"),
        "metrics": state.get("v2"), **fields,
    }
    journal.finish(state["status"], event=event, **details)


def run_task(args, meta, path, key):
    current = lambda: fresh(meta, args.fixture)
    prefixes = tuple(os.environ.get("COORDINATOR_PATHS", "").split())
    run = Run(path, meta)
    state = run.state
    journal = None
    try:
        journal = open_journal(args.journal, meta, resume=True)
        policy = routing(args.mode)
        check_route(policy)
        if state.setdefault("routing", policy) != policy:
            raise Blocked("Настройки маршрута изменились. Нужен новый ключ запроса.")
        if journal is None and policy["engine"] == "v2":
            journal = open_journal(path.with_suffix(".journal"), meta, resume=True)
        if state["status"] == "running":
            if journal:
                journal.emit("coordinator.started", routing=policy, base=meta["base"])
            snapshot = current()
            initial = digest(snapshot)
            if state.setdefault("snapshot_hash", initial) != initial:
                raise Blocked("Требования изменились с момента сохранения состояния.")
            if args.mode == "dry-run":
                role = "interface" if re.search(r"экран|интерфейс|frontend|UI\b", snapshot["title"], re.I) else "developer"
                state["route"] = [role, "tester"]
                run.finish("dry-run", "Маршрут предварительный. Сессии Codex не запускались.")
            else:
                if args.mode == "task" and policy["engine"] == "v1" and not prefixes:
                    raise Blocked("Для обычных задач V1 задайте COORDINATOR_PATHS.")
                if state.get("paid_started") or os.environ.get("COORDINATOR_RESUME_ONLY") == "true":
                    raise Blocked("Платный запуск был прерван. Проверьте расход перед созданием нового ключа.")
                model = Codex.model_config(key)
                state["paid_started"] = True
                run.save()
                with Gateway(key, model, args.mode) as gateway:
                    try:
                        execute_route(run, snapshot, current, Codex(gateway), prefixes, policy, journal)
                    finally:
                        state["budget"] = gateway.summary()
                        run.save()
            summarize_journal(journal, state, "coordinator.finished")
    except Blocked as error:
        run.finish("blocked", str(error))
        summarize_journal(journal, state, "coordinator.finished")
    print(report(state))


def excerpt(text):
    text = re.sub(r"(?:https?://|www\.)[^\s]+", "[ссылка удалена]", text, flags=re.I)
    return " ".join(text.split()).translate(str.maketrans("@<>[]`", "＠‹›［］′"))[:2000]


def report(state, delivery=""):
    meta = state["meta"]
    statuses = {
        "passed": "принято", "failed": "требуется исправление",
        "blocked": "заблокировано", "dry-run": "план без запуска", "running": "в работе",
    }
    lines = [f"## Coordinator · {statuses[state['status']]}",
             f"Issue #{meta['issue']} · Run `{meta['run_id']}` · канал GitHub Issue"]
    if policy := state.get("routing"):
        features = ", ".join(policy["features"])
        lines.append(f"Маршрут {policy['engine'].upper()}" + (f" · {features}" if features else ""))
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
    if budget := state.get("budget"):
        lines.append(
            f"Резерв API · {budget['requests']}/{budget['max_requests']} запросов, "
            f"{budget['input_reserved']}/{budget['max_input']} входных, "
            f"{budget['output_reserved']}/{budget['max_output']} выходных токенов."
        )
    if estimate is not None:
        lines.append(f"Оценка стоимости по заданным тарифам — ${estimate}.")
    if delivery:
        lines.append(delivery)
    return "\n\n".join(lines)


def push_result(meta, state):
    prefixes = tuple(os.environ.get("COORDINATOR_PATHS", "").split())
    policy = routing(meta["mode"])
    check_route(policy)
    if state.get("routing", policy) != policy or ("routing" not in state and policy["engine"] == "v2"):
        raise Blocked("Настройки публикации отличаются от настроек запуска.")
    allowed, expected = publication_rules(state, policy, prefixes)
    changes = state["changes"]
    if not changes or len(changes) > 8 or not all(
        allowed(path)
        and isinstance(value, str) and "\0" not in value and len(value.encode()) <= 12000
        for path, value in changes.items()
    ):
        raise Blocked("Артефакт содержит изменение за пределами разрешённой области.")
    commit = make_commit(meta["base"], changes, meta["run_id"], state["round"])
    if commit != state["checks"][-1]["sha"] or state["checks"][-1]["verdict"] != "pass":
        raise Blocked("Публикуемый коммит отличается от принятой Андрюхой версии.")
    if expected is not None:
        evidence = state["checks"][-1].get("evidence", [])
        if (
            len(evidence) != len(expected) or {item["check"] for item in evidence} != expected
            or any(item["sha"] != commit or item["status"] != "pass" for item in evidence)
        ):
            raise Blocked("Для проверенного SHA отсутствуют обязательные результаты QA.")
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
    return delivery, commit


def publish(meta, path, journal_path=None):
    check_author(meta["repo"], meta["actor"])
    items = reservations(meta)
    if previous_report(items, meta):
        journal = open_journal(journal_path, meta, resume=True)
        if journal:
            journal.emit("publication.skipped", notification="already-posted")
        return
    if not any((item.get("body") or "").split("\n", 1)[0] == marker(meta, "claim") for item in items):
        raise Blocked("У запуска нет подтверждённой записи о начале работы.")
    if not path.is_file():
        raise Blocked("Артефакт состояния отсутствует. Повторите исходный запуск.")
    state = Run(path, meta).state
    if state["status"] == "running":
        raise Blocked("Запуск прерван. Повторите его в Actions для восстановления.")
    journal = open_journal(journal_path, meta, resume=True)
    delivery = f"[Журнал запуска и артефакты]({run_url(meta)})"
    published_sha = None
    if state["status"] == "passed":
        try:
            unchanged = digest(fresh(meta)) == state["snapshot_hash"]
        except Blocked:
            unchanged = False
        if unchanged:
            try:
                link, published_sha = push_result(meta, state)
                delivery += " · " + link
            except Blocked as error:
                state.update(status="blocked", reason=f"Публикация результата: {error}")
        else:
            state.update(status="blocked", reason="Требования обновились перед публикацией.")
    try:
        add_comment(meta, f"{marker(meta, 'report')}\n@{meta['actor']}\n\n{report(state, delivery)}")
    except Blocked:
        summarize_journal(journal, state, "publication.failed", published_sha=published_sha, notification="failed")
        raise
    summarize_journal(journal, state, "publication.finished", published_sha=published_sha, notification="issue-comment")


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
    result.add_argument("--journal", type=Path)
    return result


def main():
    args = parser().parse_args()
    key = os.environ.pop("CODEX_API_KEY", None)
    try:
        meta = request_meta(args)
        if args.operation != "run":
            if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REF") != "refs/heads/main"
                    or args.fixture or args.state is None):
                raise Blocked("Подготовка и публикация доступны из GitHub Actions на main.")
            if args.operation == "prepare":
                prepare(meta)
            else:
                publish(meta, args.state, args.journal)
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
            run_task(args, meta, path, key)
        return 0
    except (Blocked, subprocess.SubprocessError, OSError) as error:
        message = str(error) if isinstance(error, Blocked) else "Операция координатора завершилась ошибкой."
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
