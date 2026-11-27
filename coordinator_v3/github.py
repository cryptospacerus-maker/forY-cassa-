"""Explicit GitHub admission and recoverable publication on the V3 route."""

import json
import re

from coordinator.__main__ import add_comment, excerpt, make_meta, verify_main
from coordinator.context import Blocked, api, check_author, comments, digest, git, issue_data

from .engine import Deferred, delivery_state
from .queue import encode, transaction

COMMAND = re.compile(r"/coordinator-v3 ([A-Za-z0-9][A-Za-z0-9_-]{0,39})")


def ingest(queue, repo, issue, comment, project, features, image):
    if not (1 <= issue <= 999_999_999 and comment > 0):
        raise Blocked("Некорректный номер Issue или комментария.")
    event = api(repo, f"issues/comments/{comment}")
    command = COMMAND.fullmatch((event.get("body") or "").strip())
    user = event["user"]
    if (not command or user["type"] != "User"
            or event["issue_url"] != f"https://api.github.com/repos/{repo}/issues/{issue}"):
        raise Blocked("Для V3 нужен точный комментарий пользователя в указанном Issue.")
    actor = user["login"].lower()
    check_author(repo, actor)
    key = digest({"repo": repo.lower(), "issue": issue, "actor": actor, "request": command[1]})
    with transaction(queue.path) as db:
        if db.execute("SELECT 1 FROM runs WHERE id=?", (key,)).fetchone():
            return key
    base = api(repo, "git/ref/heads/main")["object"]["sha"]
    spec = make_meta(repo.lower(), issue, actor, command[1], "v3", base, "local")
    spec.update(mode="github", project=project, features=features, image=image,
                snapshot=issue_data(repo, issue, base, actor))
    return queue.submit(spec)


def current(spec):
    try:
        check_author(spec["repo"], spec["actor"])
        verify_main(spec)
        return issue_data(spec["repo"], spec["issue"], spec["base"], spec["actor"])
    except Blocked as error:
        if str(error).startswith("GitHub API недоступен"):
            raise Deferred from None
        raise


def publish(queue, run, publisher):
    with transaction(queue.path) as db:
        row = db.execute("SELECT spec,result,published FROM runs WHERE id=? AND status!='pending'", (run,)).fetchone()
    if not row:
        raise Blocked("Нет завершённого запуска.")
    spec, result = json.loads(row["spec"]), json.loads(row["result"])
    if spec["mode"] != "github":
        raise Blocked("Публикация разрешена для задачи GitHub.")
    if row["published"] and row["published"] != "posting":
        return row["published"]
    marker = f"<!-- coordinator-v3:report:{run} -->"
    existing = next((item for item in comments(spec["repo"], spec["issue"])
                     if item["user"]["login"].lower() == publisher.lower()
                     and (item.get("body") or "").startswith(marker)), None)
    if not existing:
        if row["published"] == "posting":
            raise Blocked("Ответ GitHub при публикации неизвестен. Проверьте комментарий по маркеру.")
        delivery = ""
        details = []
        if result["status"] == "passed":
            if digest(current(spec)) != digest(spec["snapshot"]):
                raise Blocked("Требования изменились до публикации.")
            sha = delivery_state(spec, run, result)
            ref = f"refs/heads/coordinator/v3-result-{spec['issue']}-{run}"
            remote = git("ls-remote", "--refs", "origin", ref).decode().split()
            if remote and remote[0] != sha:
                raise Blocked("Ветка результата уже указывает на другой SHA.")
            if not remote:
                git("push", f"--force-with-lease={ref}:", "origin", f"{sha}:{ref}")
            if digest(current(spec)) != digest(spec["snapshot"]):
                raise Blocked("Требования изменились после отправки проверенной ветки.")
            delivery = f"\n\n[Проверенный SHA](https://github.com/{spec['repo']}/commit/{sha})"
        else:
            check_author(spec["repo"], spec["actor"])
        for check in result.get("checks", []):
            details.append(f"- QA `{check['sha']}` · {check['verdict']}")
        if result.get("reason"):
            details.append(excerpt(result["reason"]))
        summary = "\n".join(details)
        with transaction(queue.path) as db:
            db.execute("UPDATE runs SET published='posting' WHERE id=?", (run,))
        existing = add_comment(spec, f"{marker}\n## Coordinator V3 · {result['status']}\n"
                               f"@{spec['actor']} · запуск `{run}`{delivery}\n\n{summary}")
        if existing["user"]["login"].lower() != publisher.lower():
            raise Blocked("Ответ опубликован другим GitHub-актором. Сверьте учётные данные публикации.")
    url = existing["html_url"]
    with transaction(queue.path) as db:
        db.execute("UPDATE runs SET published=? WHERE id=?", (url, run))
        queue.event(db, "run.published", run=run)
    return url
