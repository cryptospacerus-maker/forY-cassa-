"""Optional V2 orchestration. The V1 entry points and workflows stay independent."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Lock

from coordinator.context import Blocked, digest, git
from coordinator.engine import base_files, make_commit, validate_files
from coordinator.runner import FILES, REVIEW, TEXT, schema, usage_totals

from .mcp import POLICY, TOOL, Gateway
from .runtime import ROLES

PLAN = schema({
    "criteria": {"type": "array", "items": TEXT},
    "blocker": TEXT,
    "tasks": {"type": "array", "items": schema({
        "role": {"type": "string", "enum": sorted(ROLES)},
        "instruction": TEXT,
        "paths": {"type": "array", "items": TEXT},
        "depends_on": {"type": "array", "items": TEXT},
        "mcp_category": TEXT,
    })},
})
WORK = schema({"summary": TEXT, "files": FILES, "check_ids": {"type": "array", "items": TEXT}})
FIXTURE_PLAN = {
    "blocker": "",
    "criteria": ["Отфильтровать синтетический каталог", "Показать пустое состояние и сброс"],
    "tasks": [
        {"role": role, "instruction": instruction,
         "paths": ["coordinator_v2/demo/app/" + file], "depends_on": [], "mcp_category": "office"}
        for role, file, instruction in [
            ("developer", "store.py", "Реализуй выборку каталога по категории."),
            ("interface", "client.js", "Добавь список, переключатель и пустое состояние."),
        ]
    ],
}


def route(plan, project, features):
    if plan["blocker"]:
        raise Blocked("Аналитик заблокировал план.")
    criteria, tasks = plan["criteria"], plan["tasks"]
    if not 1 <= len(criteria) <= 8 or not all(0 < len(item) <= 350 for item in criteria):
        raise Blocked("Некорректные критерии плана.")
    if not 1 <= len(tasks) <= 2:
        raise Blocked("План должен содержать одну или две рабочие роли.")
    by_role, occupied = {}, set()
    for task in tasks:
        role, paths, category = task["role"], task["paths"], task["mcp_category"]
        if (
            role not in ROLES or role in by_role or not 0 < len(task["instruction"]) <= 1400
            or not 1 <= len(paths) <= 4 or len(paths) != len(set(paths)) or occupied.intersection(paths)
            or not all(project.allows(path) for path in paths)
        ):
            raise Blocked("В плане недопустимая роль или пересекающиеся файлы.")
        if category and (category not in POLICY[role][TOOL] or "mcp" not in features):
            raise Blocked("План запросил недоступный MCP-контекст.")
        by_role[role] = task
        occupied.update(paths)
    waves, resolved = [], set()
    while set(by_role) - resolved:
        ready = [role for role, task in by_role.items()
                 if role not in resolved and set(task["depends_on"]) <= resolved]
        if not ready:
            raise Blocked("В плане цикл или неизвестная зависимость.")
        waves.append(ready)
        resolved.update(ready)
    return by_role, waves


def compact(snapshot):
    return {
        "title": snapshot["title"][:350],
        "body": snapshot["body"],
        "comments": [part["body"][:600] for part in snapshot.get("comments", [])[-3:]],
        "linked": [
            {"number": item["number"], "title": item["title"][:180], "body": item["body"][:500]}
            for item in snapshot.get("linked", [])[:4]
        ],
        "project": {name: value[:1500] for name, value in snapshot.get("project", {}).items()},
    }


class Engine:
    def __init__(self, base, project, features, sandbox, agent, journal):
        self.base, self.project, self.features = base, project, features
        self.sandbox, self.agent, self.journal = sandbox, agent, journal
        self.mcp = Gateway()
        self.sessions = []
        self.lock = Lock()

    def invoke(self, role, instruction, payload, contract, round_no):
        size = len(json.dumps(payload, ensure_ascii=False).encode())
        if size > 56_000:
            raise Blocked("Превышен лимит контекста роли.")
        with self.lock:
            if len(self.sessions) >= 8:
                raise Blocked("Превышен лимит сессий V2.")
            index = len(self.sessions)
            self.sessions.append({})
        self.journal.emit("agent.started", role=role, round=round_no, context_bytes=size,
                          version=payload.get("sha", self.base))
        reply = self.agent(role, instruction, payload, contract)
        with self.lock:
            self.sessions[index] = {"usage": reply.get("usage", {}), "context_bytes": size}
        self.journal.emit("agent.finished", role=role, round=round_no, status="error" if reply.get("error") else "ok")
        if reply.get("error"):
            raise Blocked("Сессия роли завершилась ошибкой.")
        return reply["answer"]

    def check(self, check, sha, role, round_no):
        result = self.sandbox.run(check, sha)
        self.journal.emit("check.finished", role=role, round=round_no, feature=check.feature,
                          evidence=result.data())
        if result.status not in {"pass", "fail"}:
            raise Blocked("Проверка прервана или песочница недоступна.")
        return result

    def worker(self, role, task, round_no, criteria, changes, handoffs, files, feedback):
        paths = task["paths"]
        capabilities = {check.name: check for check in self.project.for_role(role, self.features)}
        inventory = None
        if task["mcp_category"]:
            inventory, cached = self.mcp.call(role, TOOL, {"category": task["mcp_category"]})
            self.journal.emit("mcp.read", role=role, round=round_no, tool=TOOL, cached=cached)
        answer = self.invoke(role, task["instruction"], {
            "criteria": criteria, "files": {path: changes.get(path, files.get(path)) for path in paths},
            "allowed_paths": paths, "allowed_checks": list(capabilities), "feedback": feedback,
            "dependencies": {
                dep: dict(handoffs[dep], excerpts={
                    path: changes[path][:1500] for path in handoffs[dep]["files"]
                }) for dep in task["depends_on"]
            },
            "inventory": inventory,
        }, WORK, round_no)
        proposal = validate_files(answer, paths)
        requested = answer["check_ids"]
        if len(requested) != len(set(requested)) or any(name not in capabilities for name in requested):
            raise Blocked("Агент запросил проверку вне своих полномочий.")
        failures = []
        if requested:
            snapshot = make_commit(self.base, dict(changes, **proposal), self.journal.run_id,
                                   round_no * 10 + sorted(ROLES).index(role))
            for name in requested:
                result = self.check(capabilities[name], snapshot, role, round_no)
                if result.status == "fail":
                    failures.append({"check": name, "diagnostics": result.diagnostics})
        return proposal, {"summary": answer["summary"][:500], "files": sorted(proposal),
                          "failed_checks": failures}

    def candidate(self, sha):
        patch = git("diff", "--binary", "--no-ext-diff", self.base, sha, "--", self.project.source)
        pack = git("pack-objects", "--stdout", "--revs", data=f"{sha}\n^{self.base}\n".encode())
        (self.journal.path / "candidate.patch").write_bytes(patch)
        (self.journal.path / "candidate.pack").write_bytes(pack)

    def run(self, snapshot, current, fixture_plan=None):
        offline = fixture_plan is not None
        self.backend = "offline-fixture" if offline else "codex"
        self.journal.emit("run.started", base=self.base, features=sorted(self.features), backend=self.backend,
                          sandbox_image=getattr(self.sandbox, "image", None)
                          if isinstance(getattr(self.sandbox, "image", None), str) else None)
        original = digest(snapshot)
        def recheck():
            if digest(current()) != original:
                self.journal.emit("requirements.changed")
                raise Blocked("Требования изменились. Нужен новый запуск.")

        try:
            recheck()
            requirements = compact(snapshot)
            if offline:
                plan = dict(fixture_plan, tasks=[
                    dict(task, mcp_category=task["mcp_category"] if "mcp" in self.features else "")
                    for task in fixture_plan["tasks"]
                ])
            else:
                candidates = []
                for name in git("ls-tree", "-r", "--name-only", "-z", self.base,
                                "--", self.project.source).decode().split("\0"):
                    if self.project.allows(name):
                        candidates.append(name)
                        if len(candidates) == 40:
                            break
                plan = self.invoke("analyst", "Выбери роли, укажи зависимости и пути. "
                    "mcp_category оставь пустым, если MCP не нужен.", {
                        "requirements": requirements, "allowed_directory": self.project.source,
                        "candidate_paths": candidates,
                        "mcp_categories": {role: sorted(POLICY[role][TOOL]) for role in ROLES}
                        if "mcp" in self.features else {},
                    }, PLAN, 0)
            assignments, waves = route(plan, self.project, self.features)
            self.journal.emit("plan.accepted", waves=waves)
            all_paths = [path for task in assignments.values() for path in task["paths"]]
            files = base_files(self.base, all_paths)
            changes, handoffs, feedback = {}, {}, {}
            repairs = set(assignments)
            qa_checks = self.project.for_role("tester", self.features)
            if any(not any(check.feature == feature for check in qa_checks)
                   for feature in {"tests", "browser"}.intersection(self.features)):
                raise Blocked("В доверенном профиле отсутствуют включённые проверки QA.")
            for round_no in (1, 2):
                recheck()
                for wave in waves:
                    roles = [role for role in wave if role in repairs]
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        future = {role: pool.submit(
                            self.worker, role, assignments[role], round_no, plan["criteria"],
                            dict(changes), dict(handoffs), files, feedback.get(role, []),
                        ) for role in roles}
                        for role in roles:
                            proposal, handoff = future[role].result()
                            changes.update(proposal)
                            handoffs[role] = handoff
                            self.journal.emit("task.handoff", role=role, round=round_no,
                                              paths=handoff["files"],
                                              dependencies=assignments[role]["depends_on"])
                    recheck()
                sha = make_commit(self.base, changes, self.journal.run_id, round_no)
                self.journal.emit("qa.started", role="tester", round=round_no, sha=sha)
                findings = {role: list(handoff["failed_checks"]) for role, handoff in handoffs.items()}
                for check in qa_checks:
                    result = self.check(check, sha, "tester", round_no)
                    if result.status == "fail":
                        for role in (set(check.owners) & assignments.keys()) or assignments:
                            findings[role].append({"check": check.name, "diagnostics": result.diagnostics})
                    recheck()
                if any(findings.values()):
                    verdict = "fail"
                else:
                    answer = self.invoke("tester", "Независимо сверь критерии и проверенный SHA.", {
                        "sha": sha, "criteria": plan["criteria"], "files": base_files(sha, all_paths),
                        "requirements": {key: requirements[key] for key in ("title", "body")},
                        "checks_passed": [check.name for check in qa_checks],
                    }, REVIEW, round_no)
                    verdict = answer["verdict"]
                    if answer["sha"] != sha or verdict not in {"pass", "fail", "blocked"}:
                        raise Blocked("QA вернул неверный SHA или вердикт.")
                    if verdict == "fail":
                        findings = {role: [answer["findings"][:450]] for role in assignments}
                self.journal.emit("qa.finished", role="tester", round=round_no, sha=sha, status=verdict)
                recheck()
                if verdict == "pass":
                    self.candidate(sha)
                    return self.finish("passed", sha=sha, qa_checks=[check.name for check in qa_checks])
                if verdict == "blocked":
                    raise Blocked("Андрюха заблокировал приёмку.")
                feedback = findings
                repairs = {role for role, issues in findings.items() if issues}
                for wave in waves:
                    repairs.update(role for role in wave if repairs.intersection(assignments[role]["depends_on"]))
                if round_no == 1:
                    self.journal.emit("repair.requested", roles=sorted(repairs), sha=sha)
            return self.finish("failed", sha=sha, reason="Дефект сохранился после доработки.")
        except Blocked as error:
            return self.finish("blocked", reason=str(error))
        except (KeyError, TypeError, ValueError):
            return self.finish("blocked", reason="Ответ агента или настройка проекта не соответствует контракту.")

    def finish(self, status, **fields):
        return self.journal.finish(status, base=self.base, backend=self.backend,
            features=sorted(self.features), model_sessions=len(self.sessions),
            context_bytes=sum(session.get("context_bytes", 0) for session in self.sessions),
            usage=usage_totals(dict(enumerate(self.sessions))),
            mcp_requests=self.mcp.requests, mcp_cache_hits=self.mcp.hits, **fields)
