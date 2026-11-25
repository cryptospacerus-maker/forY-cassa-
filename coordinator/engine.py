"""Plan work, materialize bounded text changes, and review the exact Git commit."""

from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from .context import Blocked, DEMO_PATH, digest, fixture, git, identity, protected_path, SHA
from .runner import PLAN, REVIEW, WORK, Sessions, usage_totals

ROLES = {"developer", "interface"}
PATH = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*")
TEXT_TYPES = {".md", ".txt", ".json", ".csv", ".html", ".css", ".js", ".ts", ".py"}


def allowed_path(path, prefixes, demo):
    if demo:
        return path == DEMO_PATH
    return (
        isinstance(path, str) and len(path) <= 180 and PATH.fullmatch(path)
        and not protected_path(path)
        and not path.startswith("coordinator_v2/")
        and Path(path).suffix in TEXT_TYPES
        and any(path.startswith(prefix + "/") for prefix in prefixes)
    )


def plan_tasks(review, prefixes, demo):
    criteria, tasks = review["criteria"], review["tasks"]
    if review["blocker"]:
        raise Blocked("Аналитик отметил неоднозначные требования или недоступный объём работ.")
    if not (1 <= len(criteria) <= 8 and all(0 < len(item) <= 500 for item in criteria)):
        raise Blocked("План должен содержать до восьми проверяемых критериев.")
    if not 1 <= len(tasks) <= 2:
        raise Blocked("План должен содержать одну или две задачи.")
    allocations = {}
    paths = set()
    for task in tasks:
        role = task["role"]
        assigned = task["paths"]
        if (
            role not in ROLES or role in allocations or not 0 < len(task["instruction"]) <= 2000
            or not 1 <= len(assigned) <= 4 or len(assigned) != len(set(assigned))
            or not all(allowed_path(path, prefixes, demo) for path in assigned)
            or paths.intersection(assigned)
        ):
            raise Blocked("План содержит пересекающиеся пути, недопустимую роль или область записи.")
        allocations[role] = task
        paths.update(assigned)
    remaining = dict(allocations)
    available = set()
    waves = []
    while remaining:
        wave = [role for role, task in remaining.items() if set(task["depends_on"]) <= available]
        if not wave:
            raise Blocked("В плане есть цикл или неизвестная зависимость.")
        waves.append(wave)
        available.update(wave)
        for role in wave:
            del remaining[role]
    return allocations, waves


def base_files(base, paths):
    files = {}
    for path in paths:
        entry = git("ls-tree", base, "--", path)
        if not entry:
            continue
        if not entry.startswith(b"100644 blob "):
            raise Blocked("Разрешено изменять только обычные текстовые файлы.")
        size = int(git("cat-file", "-s", f"{base}:{path}"))
        if size > 12000:
            raise Blocked("Файл превышает 12 Кбайт.")
        try:
            files[path] = git("show", f"{base}:{path}").decode("utf-8")
        except UnicodeDecodeError:
            raise Blocked("Файл не является текстом UTF-8.") from None
    return files


def validate_files(result, paths):
    files = {}
    for item in result["files"]:
        path, content = item["path"], item["content"]
        if path not in paths or path in files or "\0" in content or len(content.encode()) > 12000:
            raise Blocked("Исполнитель вернул недопустимое изменение.")
        files[path] = content
    if not files or len(result["summary"]) > 2000:
        raise Blocked("Исполнитель не вернул допустимый результат.")
    return files


def make_commit(base, changes, run_id, round_no):
    # An isolated index keeps model-proposed files out of the coordinator checkout.
    with TemporaryDirectory(prefix="coordinator-index-") as directory:
        timestamp = git("show", "-s", "--format=%ct", base).decode().strip()
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
        for actor in ("AUTHOR", "COMMITTER"):
            env[f"GIT_{actor}_NAME"] = "Coordinator MVP"
            env[f"GIT_{actor}_EMAIL"] = "github-actions[bot]@users.noreply.github.com"
            env[f"GIT_{actor}_DATE"] = f"@{timestamp} +0000"
        git("read-tree", base, env=env)
        for path, contents in sorted(changes.items()):
            obj = git("hash-object", "-w", "--stdin", data=contents.encode(), env=env).decode().strip()
            git("update-index", "--add", "--cacheinfo", f"100644,{obj},{path}", env=env)
        tree = git("write-tree", env=env).decode().strip()
        if tree == git("rev-parse", f"{base}^{{tree}}").decode().strip():
            raise Blocked("Содержимое результата совпадает с исходной версией.")
        return git(
            "commit-tree", tree, "-p", base, "-m", f"Coordinator {run_id} / QA {round_no}",
            env=env,
        ).decode().strip()


def demo_check(commit):
    expected = {"доставка": 2, "оплата": 1, "возврат": 1}
    try:
        actual = json.loads(base_files(commit, [DEMO_PATH]).get(DEMO_PATH, "null"))
    except ValueError:
        return "Файл результата должен содержать корректный JSON."
    if (
        not isinstance(actual, dict) or any(type(count) is not int for count in actual.values())
        or actual != expected
    ):
        return "Для пяти записей с одним дубликатом ожидаются доставка=2, оплата=1, возврат=1."
    return ""


class Run:
    def __init__(self, path, meta):
        self.path = path
        self.state = json.loads(path.read_text()) if path.exists() else {
            "meta": meta, "sessions": {}, "checks": [], "status": "running",
        }
        if self.state["meta"] != meta:
            raise Blocked("Состояние запуска относится к другому Issue, инициатору или версии.")
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))
        temporary.replace(self.path)

    def finish(self, status, reason=""):
        self.state.update(status=status, reason=reason)
        self.save()


def demo_plan(snapshot):
    example = fixture()
    if any(snapshot[field] != example[field] for field in ("title", "body")):
        raise Blocked("Для demo требуется неизменённое описание контрольного примера #13.")
    return {
        "blocker": "",
        "criteria": [
            "Сгруппировать обращения по темам, учитывая каждый идентификатор один раз.",
            "Вернуть JSON с целыми значениями и без отсутствующих тем.",
        ],
        "tasks": [{
            "role": "developer", "instruction": "Запиши сводку по синтетическим данным.",
            "paths": [DEMO_PATH], "depends_on": [],
        }],
    }


def execute(run, snapshot, current, model, prefixes, *, journal=None):
    state = run.state
    meta = state["meta"]
    base = identity(meta["base"], SHA)
    is_demo = meta["mode"] == "demo"
    if state["status"] != "running":
        return
    initial = digest(snapshot)

    def recheck():
        if digest(current()) != initial:
            if journal:
                journal.emit("requirements.changed")
            raise Blocked("Требования изменились. Создайте новый запуск после сверки.")

    shared = {"requirements": fixture() if is_demo else snapshot, "base": base}
    inventory = []
    if not is_demo:
        for raw in git("ls-tree", "-r", "-z", "--name-only", base, "--", *prefixes).split(b"\0"):
            path = raw.decode("utf-8", errors="replace")
            if allowed_path(path, prefixes, False):
                inventory.append(path)
                if len(inventory) == 50:
                    break
    sessions = Sessions(run, model, journal=journal)
    try:
        if is_demo:
            plan = demo_plan(snapshot)
        else:
            plan, = sessions.batch([(
                "plan", "analyst",
                "Составь критерии и план для developer/interface в разрешённой области. "
                "depends_on — список ролей-предшественников. При блокировке заполни blocker.",
                dict(shared, allowed_paths=list(prefixes), existing_paths=inventory), PLAN,
            )])
        allocations, waves = plan_tasks(plan, prefixes, is_demo)
        if journal:
            journal.emit("plan.accepted", waves=waves)
        all_paths = [path for task in plan["tasks"] for path in task["paths"]]
        initial_files = base_files(base, all_paths)
        state["plan"] = plan
        changes, handoffs = {}, {}
        feedback = ""
        repair_roles = set(allocations)
        for round_no in (1, 2):
            recheck()
            for wave in waves:
                jobs = []
                for role in wave:
                    if role not in repair_roles:
                        continue
                    task = allocations[role]
                    paths = task["paths"]
                    relevant_paths = paths + [
                        path for dep in task["depends_on"] for path in allocations[dep]["paths"]
                    ]
                    payload = dict(
                        shared, criteria=plan["criteria"], feedback=feedback,
                        allowed_paths=paths, files={
                            path: changes.get(path, initial_files.get(path))
                            for path in relevant_paths
                        }, handoffs={dep: handoffs[dep] for dep in task["depends_on"]},
                    )
                    key = f"work-{round_no}-{role}"
                    jobs.append((key, role, task["instruction"], payload, WORK))
                answers = sessions.batch(jobs)
                for (_, role, *unused), result in zip(jobs, answers, strict=True):
                    proposal = validate_files(result, allocations[role]["paths"])
                    changes.update(proposal)
                    handoffs[role] = result["summary"]
                    if journal:
                        journal.emit("task.handoff", role=role, round=round_no, paths=sorted(proposal),
                                     dependencies=allocations[role]["depends_on"])
            recheck()
            commit = make_commit(base, changes, meta["run_id"], round_no)
            if journal:
                journal.emit("qa.started", role="tester", round=round_no, sha=commit)
            deterministic_error = demo_check(commit) if is_demo else ""
            verdict, = sessions.batch([(
                f"qa-{round_no}", "tester",
                "Проверь переданный SHA независимо по исходным требованиям и содержимому "
                "файлов Git. Вердикт pass только при подтверждении критериев. Для проверки, "
                "которую нельзя выполнить по переданным материалам, верни blocked. "
                "Опиши ожидание, фактическое поведение и проверку.",
                dict(shared, sha=commit, criteria=plan["criteria"],
                     committed_files=base_files(commit, all_paths),
                     deterministic_error=deterministic_error), REVIEW,
            )])
            if verdict["sha"] != commit or verdict["verdict"] not in {"pass", "fail", "blocked"}:
                raise Blocked("Андрюха не подтвердил проверяемый SHA или вернул некорректный вердикт.")
            result = "fail" if deterministic_error else verdict["verdict"]
            if journal:
                journal.emit("qa.finished", role="tester", round=round_no, sha=commit, status=result)
            feedback = (deterministic_error + "\n" + verdict["findings"][:2000]).strip()
            check = {"sha": commit, "verdict": result, "findings": feedback}
            state["checks"] = state["checks"][:round_no - 1] + [check]
            state.update(changes=changes, handoffs=handoffs, round=round_no)
            run.save()
            recheck()
            if result == "pass":
                return run.finish("passed")
            if result == "blocked":
                return run.finish("blocked", "Андрюха заблокировал приёмку.")
            # If paths identify specific owners, also rerun any dependent work.
            repair_roles = {role for role, task in allocations.items()
                            if any(path in feedback for path in task["paths"])} or set(allocations)
            for wave in waves:
                repair_roles.update(role for role in wave
                                    if repair_roles.intersection(allocations[role]["depends_on"]))
            if journal and round_no == 1:
                journal.emit("repair.requested", roles=sorted(repair_roles), sha=commit)
        run.finish("failed", "После одного исправления проверка всё ещё выявляет дефект.")
    except Blocked as error:
        run.finish("blocked", str(error))
    except (KeyError, TypeError, ValueError):
        run.finish("blocked", "Ответ агента не соответствует ожидаемому формату.")


def estimate_usage(sessions):
    totals = usage_totals(sessions)
    rates = os.environ.get("COORDINATOR_USD_PER_MTOK", "").split(",")
    if len(rates) != 3 or not sessions:
        return totals, None
    try:
        prices = [Decimal(rate) for rate in rates]
        if any(not price.is_finite() or price < 0 for price in prices):
            return totals, None
        cached = totals["cached_input_tokens"]
        quantities = (max(0, totals["input_tokens"] - cached), cached, totals["output_tokens"])
        cost = sum(quantity * price for quantity, price in zip(quantities, prices, strict=True))
        return totals, f"{cost / 1_000_000:.6f}"
    except InvalidOperation:
        return totals, None
